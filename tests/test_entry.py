# SPDX-FileCopyrightText: 2026 ProtonCats and contributors
# SPDX-License-Identifier: MIT
import contextlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from core import entry as en
from test_peversion import pe


class Entry(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=en.REPO)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.installer = self.root / "installer.exe"
        self.installer.write_bytes(b"local")
        self.profile = self.root / "profile.json"
        self.profile.write_text(json.dumps({"install_dir": "drive_c/Program Files/Example", "program": "bin/app.exe"}))
        self.pins = self.root / "pins.json"
        self.pins.write_text(json.dumps({"ge_tag": "Release-1", "ge_asset": "Release-1-x86_64"}))
        self.patch_file = self.root / "fix.patch"
        self.patch_file.write_bytes(b"patch")
        self.enterContext(patch.object(en, "PROFILE", self.profile))
        self.enterContext(patch.object(en, "PINS", self.pins))
        self.enterContext(patch.object(en.runner, "our_patches", return_value=[(self.patch_file, ["sample"])]))
        self.enterContext(patch.object(en.shutil, "which", return_value="available"))
        self.enterContext(patch.object(en.runner, "llvm_ar", return_value="available"))
        self.enterContext(patch.object(en.runner, "main", side_effect=self.stage))
        self.enterContext(patch.object(en.profiles, "main", side_effect=self.stage))
        self.stdout = self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.stderr = self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.forbid_work = False

    def put(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    @property
    def base(self):
        return self.data / "runners/Release-1-x86_64"

    def stage(self, args):
        if self.forbid_work:
            raise AssertionError("unexpected work")
        def arg(name):
            return Path(args[args.index(name) + 1])
        if args[0] == "fetch":
            self.put(self.base / "version", b"200 Release-1\n")
            self.put(self.base / en.LIB / "sample.dll", b"original")
        elif args[0] == "build":
            work = arg("--work")
            self.put(work / ".patched", en.runner.patch_identity(self.pins.read_bytes(), en.runner.mode_patches(en.runner.our_patches(), "pe")).encode())
            dll = bytearray(512)
            struct.pack_into("<I", dll, 0x3c, 64)
            struct.pack_into("<H", dll, 70, 1)
            struct.pack_into("<H", dll, 84, 128)
            struct.pack_into("<II", dll, 208, 4096, 20)
            struct.pack_into("<8sIIII", dll, 216, b"section", 100, 4096, 100, 300)
            self.put(work / "out/sample.dll", dll)
        elif args[0] == "assemble":
            out = arg("--out")
            self.put(out / "version", (self.base / "version").read_bytes())
            self.put(out / en.LIB / "sample.dll", (arg("--work") / "out/sample.dll").read_bytes())
            manifest = {"base": "Release-1", "patches": ["fix.patch"], "dlls": {"sample.dll": {
                "sha256": en.runner.sha256(out / en.LIB / "sample.dll"),
                "replaces": en.runner.sha256(self.base / en.LIB / "sample.dll")}}}
            self.put(out / "halation-runner.json", json.dumps(manifest).encode())
        elif args[0] == "apply":
            pfx = arg("--prefix")
            self.put(pfx / "system.reg", b'"ProgramFilesDir"="C:\\\\Program Files"')
            install = pfx / "drive_c/Program Files/Example"
            self.put(install / "bin/app.exe", b"program")
            self.put(pfx / en.profiles.MARKER, json.dumps({"profile_sha256": en.runner.sha256(self.profile),
                     "install_dir": "drive_c/Program Files/Example", "install_tree": en.profiles.snapshot(install)}).encode())
        return 0

    def setup(self):
        return en.setup_main([str(self.installer), "--data", str(self.data)])

    def tree(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

    def test_complete_steps_are_verified_and_skipped(self):
        self.assertEqual(self.setup(), 0, self.stderr.getvalue())
        before = self.tree()
        self.forbid_work = True
        self.assertEqual(self.setup(), 0)
        self.assertEqual(self.tree(), before)
        for name in ("fetch", "build", "assemble", "prefix"):
            self.assertRegex(self.stdout.getvalue(), rf"{name}: verified, skipped \([0-9.]+s\)")

    def test_missing_runner_is_rebuilt_without_requesting_prefix_deletion(self):
        import shutil
        self.assertEqual(self.setup(), 0)
        before = (self.data / "pfx/.setup.json").read_bytes()
        shutil.rmtree(self.data / "runners/patched")
        self.assertEqual(self.setup(), 0, self.stderr.getvalue())
        self.assertNotIn(f"remove {self.data / 'pfx'}", self.stderr.getvalue())
        self.assertEqual((self.data / "pfx/.setup.json").read_bytes(), before)

    def test_resume_checks_each_step_once_and_progress_is_live(self):
        self.assertEqual(self.setup(), 0)
        self.forbid_work = True
        with patch.object(en.profiles, "unchanged", wraps=en.profiles.unchanged) as unchanged:
            self.assertEqual(self.setup(), 0)
        self.assertEqual(unchanged.call_count, 1)
        import shutil
        shutil.rmtree(self.data / "runners/patched")
        self.forbid_work = False
        def stage(args):
            print("installer attempt is live", flush=True)
            self.assertIn("installer attempt is live", self.stdout.getvalue())
            return self.stage(args)
        with patch.object(en.runner, "main", side_effect=stage):
            self.assertEqual(self.setup(), 0, self.stderr.getvalue())

    def test_installer_progress_is_visible_before_action_returns_and_failure_is_summarized(self):
        def action(args):
            print("installer attempt 1: exit 7", flush=True)
            self.assertIn("installer attempt 1: exit 7", self.stdout.getvalue())
            raise RuntimeError("injected installer failure")
        with patch.object(en.profiles, "main", side_effect=action):
            with self.assertRaisesRegex(RuntimeError, "injected installer failure"):
                self.setup()
        self.assertIn("prefix: failed", self.stderr.getvalue())

    def test_partial_or_changed_steps_are_refused_without_writes(self):
        self.assertEqual(self.setup(), 0)
        cases = [(self.base / "version", b"broken", self.base),
                 (self.data / "runner-work/.patched", b"old", self.data / "runner-work"),
                 (self.data / "runner-work/out/sample.dll", b"bad", self.data / "runner-work"),
                 (self.data / "runners/patched" / en.LIB / "sample.dll", b"changed", self.data / "runners/patched"),
                 ]
        self.forbid_work = True
        for path, bad, folder in cases:
            with self.subTest(path=path.relative_to(self.data)):
                original = path.read_bytes()
                path.write_bytes(bad)
                before = self.tree()
                self.assertEqual(self.setup(), 2)
                self.assertIn(f"remove {folder}", self.stderr.getvalue())
                self.assertEqual(self.tree(), before)
                path.write_bytes(original)
        self.data.rename(self.root / "complete")
        partial = self.data / "runner-work"
        partial.mkdir(parents=True)
        self.forbid_work = False
        self.assertEqual(self.setup(), 2)
        self.assertTrue(self.base.exists())

    def test_a_finished_prefix_is_never_named_for_removal(self):
        self.assertEqual(self.setup(), 0)
        self.forbid_work = True
        marker = self.data / "pfx" / en.profiles.MARKER
        for path, bad in ((marker, b"{}"), (marker, b""), (self.profile, json.dumps({"install_dir": "drive_c/Elsewhere"}).encode()),
                          (self.data / "pfx/system.reg", b""), (self.data / "pfx/.runner.json", b'{"stamp": 900, "runner": "newer"}'),
                          (self.data / "pfx/drive_c/Program Files/Example/bin/app.exe", b"changed")):
            with self.subTest(path=path.name, bad=bad[:12]):
                original = path.read_bytes() if path.exists() else None
                path.write_bytes(bad)
                before = self.tree()
                self.stderr.truncate(0)
                self.stderr.seek(0)
                self.assertEqual(self.setup(), 2)
                self.assertNotIn("remove", self.stderr.getvalue())
                self.assertIn(f"{self.data / 'pfx'} is not as setup left it", self.stderr.getvalue())
                self.assertIn("Keep it", self.stderr.getvalue())
                self.assertEqual(self.tree(), before)
                path.unlink() if original is None else path.write_bytes(original)
        self.assertEqual(self.setup(), 0, self.stderr.getvalue())

    def test_an_unfinished_prefix_is_still_named_for_removal(self):
        self.assertEqual(self.setup(), 0)
        (self.data / "pfx" / en.profiles.MARKER).unlink()
        self.assertEqual(self.setup(), 2)
        self.assertIn(f"remove {self.data / 'pfx'}", self.stderr.getvalue())

    def test_a_changed_profile_goes_to_the_prefix_step_not_to_removal(self):
        self.assertEqual(self.setup(), 0)
        self.profile.write_text(json.dumps({"install_dir": "drive_c/Program Files/Example", "program": "bin/app.exe",
                                            "windows_version": "win11"}))
        calls = []
        def stage(args):
            calls.append(args[0])
            return self.stage(args)
        with patch.object(en.profiles, "main", side_effect=stage):
            self.assertEqual(self.setup(), 0, self.stderr.getvalue())
        self.assertEqual(calls, ["apply"])
        self.assertNotIn("remove", self.stderr.getvalue())
        self.forbid_work = True
        self.assertEqual(self.setup(), 0, self.stderr.getvalue())

    def test_a_changed_profile_over_a_changed_folder_is_still_refused(self):
        self.assertEqual(self.setup(), 0)
        self.profile.write_text(json.dumps({"install_dir": "drive_c/Program Files/Example", "program": "bin/app.exe",
                                            "windows_version": "win11"}))
        (self.data / "pfx/drive_c/Program Files/Example/bin/app.exe").write_bytes(b"changed")
        self.forbid_work = True
        self.assertEqual(self.setup(), 2)
        self.assertNotIn("remove", self.stderr.getvalue())

    def test_an_app_update_does_not_ask_to_remove_the_prefix(self):
        self.assertEqual(self.setup(), 0)
        program = self.data / "pfx/drive_c/Program Files/Example/bin/app.exe"
        marker = self.data / "pfx" / en.profiles.MARKER
        program.write_bytes(pe((1, 0, 0, 0)))
        marker.write_text(json.dumps({**json.loads(marker.read_text()), "program": "bin/app.exe", "program_version": "1.0.0.0",
                                      "install_tree": en.profiles.snapshot(program.parents[1])}))
        self.forbid_work = True
        program.write_bytes(pe((2, 0, 0, 0)))
        self.assertEqual(self.setup(), 0, self.stderr.getvalue())
        self.assertIn("the app updated itself: 1.0.0.0 to 2.0.0.0 (1 files changed)", self.stdout.getvalue())
        self.assertNotIn("remove", self.stderr.getvalue())
        program.write_bytes(pe((2, 0, 0, 0), extra=b"patched"))
        self.assertEqual(self.setup(), 2)
        self.assertNotIn("remove", self.stderr.getvalue())

    def test_preflight_refuses_missing_tools_before_work(self):
        self.forbid_work = True
        with patch.object(en.shutil, "which", side_effect=lambda n: None if n in ("autoconf", "autoheader") else "ok"), \
             patch.object(en.runner, "llvm_ar", return_value=None):
            self.assertEqual(self.setup(), 2)
        for name in ("autoconf", "autoheader", "llvm-ar"):
            self.assertIn(name, self.stderr.getvalue())
        self.assertFalse(self.data.exists())

    def test_setup_needs_no_ip_tool(self):
        with patch.object(en.shutil, "which", side_effect=lambda n: None if n == "ip" else "ok"):
            self.assertEqual(self.setup(), 0)

    def test_setup_runs_unix_after_pe_and_verifies_final_pair_on_resume(self):
        unix_patch = self.root / "0006.patch"
        unix_patch.write_bytes(b"Unix patch")
        patches = [(self.patch_file, ["sample"]), (unix_patch, ["winex11.drv"])]
        pins = json.loads(self.pins.read_text())
        pins["unix"] = {"fixture": True}
        self.pins.write_text(json.dumps(pins))
        record = {"identity": "fixture", "driver_version": 111,
                  "artifacts": {name: en.runner.sha256(unix_patch) for name in en.runner.UNIX_FILES}}
        calls = []
        def action(args):
            calls.append((args[0], "unix" if "--mode" in args else "pe"))
            def arg(name):
                return Path(args[args.index(name) + 1])
            if "--mode" not in args:
                result = self.stage(args)
                if args[0] in ("fetch", "assemble"):
                    base = self.base if args[0] == "fetch" else arg("--out")
                    for relative in en.runner.UNIX_FILES.values():
                        self.put(base / relative, b"original")
                    self.put(base / "proton", ("    " + en.runner.OPENCL_OVERRIDE + "\n").encode())
                return result
            if args[0] == "build":
                work = arg("--work")
                self.put(work / "unix-build.json", json.dumps(record).encode())
                for name in en.runner.UNIX_FILES:
                    self.put(work / "out" / name, b"Unix patch")
            else:
                import shutil
                base, out = arg("--base"), arg("--out")
                shutil.copytree(base, out)
                manifest = json.loads((out / "halation-runner.json").read_text())
                manifest["patches"] = sorted(p.name for p, _ in patches)
                files = {}
                for name, relative in en.runner.UNIX_FILES.items():
                    self.put(out / relative, b"Unix patch")
                    files[relative] = {"sha256": record["artifacts"][name], "replaces": en.runner.sha256(base / relative)}
                self.put(out / "proton", en.runner.allow_opencl((base / "proton").read_text()).encode())
                proton = {"sha256": en.runner.sha256(out / "proton"), "replaces": en.runner.sha256(base / "proton")}
                manifest["unix"] = {**record, "files": files, "proton": proton}
                self.put(out / "halation-runner.json", json.dumps(manifest).encode())
            return 0
        def verify(pins, work, base):
            self.assertEqual(json.loads((work / "unix-build.json").read_text()), record)
            return {}, [(unix_patch, ["winex11.drv"])], record
        with patch.object(en.runner, "our_patches", return_value=patches), \
                patch.object(en.runner, "unix_pins"), \
                patch.object(en.runner, "verified_unix", side_effect=verify), \
                patch.object(en.runner, "main", side_effect=action):
            self.assertEqual(self.setup(), 0, self.stderr.getvalue())
            self.assertEqual(calls, [("fetch", "pe"), ("build", "pe"), ("assemble", "pe"),
                                     ("build", "unix"), ("assemble", "unix")])
            calls.clear()
            self.assertEqual(self.setup(), 0, self.stderr.getvalue())
            self.assertEqual(calls, [])
            (self.data / "runners/patched" / en.runner.UNIX_FILES["winex11.so"]).write_bytes(b"altered")
            self.assertEqual(self.setup(), 2)
            self.assertEqual(calls, [])
            (self.data / "runners/patched" / en.runner.UNIX_FILES["winex11.so"]).write_bytes(b"Unix patch")
            self.assertEqual(self.setup(), 0, self.stderr.getvalue())
            (self.data / "runners/patched/proton").write_text("    " + en.runner.OPENCL_OVERRIDE + "\n")
            self.assertEqual(self.setup(), 2)
            self.assertEqual(calls, [])

    def test_run_forwards_profile_program_and_extra_arguments(self):
        self.assertEqual(self.setup(), 0)
        def launch(args):
            boundary = args.index("--")
            options = dict(zip(args[1:boundary - 1:2], args[2:boundary - 1:2]))
            self.assertNotIn("--require-tunnel", args)
            self.assertEqual(options["--profile"], str(self.profile))
            self.assertEqual(options["--prefix"], str(self.data / "pfx"))
            self.assertEqual(options["--runner"], str(self.data / "runners/patched"))
            program = Path(args[boundary + 1])
            self.assertEqual(program.read_bytes(), b"program")
            self.assertEqual(program, self.data / "pfx/drive_c/Program Files/Example/bin/app.exe")
            self.assertEqual(args[boundary + 2:], ["--open", "file with spaces"])
            return 7
        with patch.object(en.prefix, "main", side_effect=launch):
            self.assertEqual(en.run_main(["--data", str(self.data), "--", "--open", "file with spaces"]), 7)
        program = self.data / "pfx/drive_c/Program Files/Example/bin/app.exe"
        program.unlink()
        program.symlink_to(self.installer)
        self.assertEqual(en.run_main(["--data", str(self.data)]), 2)

    def test_opencl_vendor_list_is_rebuilt_without_rusticl(self):
        host = self.root / "vendors"
        host.mkdir()
        (host / "nvidia.icd").write_text("libnvidia-opencl.so.1\n")
        (host / "rusticl.icd").write_text("libRusticlOpenCL.so.1\n")
        (host / "notes.txt").write_text("not an ICD")
        with patch.object(en, "OPENCL_HOST", host):
            stale = self.data / "opencl-vendors/old.icd"
            stale.parent.mkdir(parents=True)
            stale.write_text("gone")
            target = en.opencl_vendors(self.data)
            self.assertEqual(sorted(p.name for p in target.iterdir()), ["nvidia.icd"])
            self.assertEqual((target / "nvidia.icd").read_text(), "libnvidia-opencl.so.1\n")
            (host / "nvidia.icd").unlink()
            self.assertIsNone(en.opencl_vendors(self.data))
            self.assertEqual(list(target.iterdir()), [])
        with patch.object(en, "OPENCL_HOST", self.root / "absent"):
            self.assertIsNone(en.opencl_vendors(self.data))
        target.rmdir()
        target.symlink_to(host)
        with patch.object(en, "OPENCL_HOST", host), self.assertRaisesRegex(ValueError, "symlink"):
            en.opencl_vendors(self.data)

    def test_display_option_reaches_the_launch_environment(self):
        self.assertEqual(self.setup(), 0)
        received = self.root / "launch-environment.json"
        def execute(cmd, **kwargs):
            if cmd[0] == "umu-run":
                received.write_text(json.dumps(kwargs["env"]))
                return subprocess.CompletedProcess(cmd, 0)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = en.profiles.treehash.main(cmd[2:])
            return subprocess.CompletedProcess(cmd, code, stdout=output.getvalue())
        with patch.object(en.prefix.subprocess, "run", side_effect=execute), \
             patch.dict(os.environ, {"PROTON_ENABLE_WAYLAND": "stale", "PROTON_USE_WINED3D": "stale",
                                     "WAYLAND_DISPLAY": "wayland-fixture", "DISPLAY": ":42"}):
            for options in ([], ["--display", "x11"], ["--display", "wayland"],
                            ["--renderer", "default"], ["--renderer", "wined3d"],
                            ["--display", "wayland", "--renderer", "wined3d"]):
                with self.subTest(options=options):
                    self.assertEqual(en.run_main(["--data", str(self.data), *options]), 0)
                    env = json.loads(received.read_text())
                    if "wayland" in options:
                        self.assertEqual(env["PROTON_ENABLE_WAYLAND"], "1")
                    else:
                        self.assertNotIn("PROTON_ENABLE_WAYLAND", env)
                    if "wined3d" in options:
                        self.assertEqual(env["PROTON_USE_WINED3D"], "1")
                    else:
                        self.assertNotIn("PROTON_USE_WINED3D", env)
                    self.assertEqual(env["WAYLAND_DISPLAY"], "wayland-fixture")
                    self.assertEqual(env["DISPLAY"], ":42")
        with self.assertRaises(SystemExit) as error:
            en.run_main(["--display", "invalid"])
        self.assertEqual(error.exception.code, 2)
        self.assertIn("invalid choice", self.stderr.getvalue())
        with self.assertRaises(SystemExit) as error:
            en.run_main(["--renderer", "invalid"])
        self.assertEqual(error.exception.code, 2)
        self.assertIn("--renderer: invalid choice", self.stderr.getvalue())

    def test_default_data_directory_uses_the_shared_slug(self):
        with patch.dict(os.environ, {"XDG_DATA_HOME": str(self.root / "xdg")}):
            self.assertEqual(en.data_directory(None), self.root / "xdg" / en.PROJECT_SLUG)
        with patch.dict(os.environ, {"XDG_DATA_HOME": ""}), patch.object(Path, "home", return_value=self.root):
            self.assertEqual(en.data_directory(None), self.root / ".local/share" / en.PROJECT_SLUG)

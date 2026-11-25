# SPDX-FileCopyrightText: 2026 ProtonCats and contributors
# SPDX-License-Identifier: MIT
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from core import entry as en
from tests.test_peversion import pe

REAL_APPLY = en.profiles.main


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
        self.enterContext(patch.object(en.runner, "missing_packages", return_value=[]))
        self.enterContext(patch.object(en.runner, "main", side_effect=self.stage))
        self.enterContext(patch.object(en.profiles, "main", side_effect=self.stage))
        self.stdout = self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.stderr = self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.forbid_work = False
        self.program = b"program"

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
            dll[420:452] = hashlib.sha256(self.patch_file.read_bytes()).digest()
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
            self.put(install / "bin/app.exe", self.program)
            profile = en.profiles.load_profile(self.profile.read_bytes())
            record = en.profiles.applied_record({"install_dir": profile["install_dir"], "install_tree": en.profiles.snapshot(install)},
                                                profile, en.runner.sha256(self.profile))
            self.put(pfx / en.profiles.MARKER, json.dumps(en.profiles.with_version(record, install, profile)).encode())
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
        self.assertEqual(self.setup(), 0)
        before = (self.data / "pfx/.setup.json").read_bytes()
        shutil.rmtree(self.data / "runners/patched")
        self.assertEqual(self.setup(), 0, self.stderr.getvalue())
        self.assertNotIn(f"remove {self.data / 'pfx'}", self.stderr.getvalue())
        self.assertEqual((self.data / "pfx/.setup.json").read_bytes(), before)

    def test_resume_checks_each_step_once_and_progress_is_live(self):
        self.assertEqual(self.setup(), 0)
        self.forbid_work = True
        with patch.object(en.profiles, "snapshot", wraps=en.profiles.snapshot) as snapshot:
            self.assertEqual(self.setup(), 0)
        self.assertEqual(snapshot.call_count, 1)
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

    def refusal(self):
        """What ./setup says when it stops over a finished prefix; it has written nothing and never advises removal."""
        self.forbid_work, before = True, self.tree()
        self.stderr.truncate(0)
        self.stderr.seek(0)
        self.assertEqual(self.setup(), 2)
        self.assertEqual(self.tree(), before)
        message = self.stderr.getvalue()
        self.assertNotIn("remove", message)
        self.assertEqual(message.count("was left as it is"), 1, message)
        self.assertIn(f"{self.data / 'pfx'} was left as it is", message)
        self.assertIn("Keep it: it holds the installed app, its settings and its activation", message)
        return message

    def finished(self, version=(1, 0, 0, 0)):
        """A finished prefix whose program carries a version."""
        self.program = pe(version)
        self.assertEqual(self.setup(), 0, self.stderr.getvalue())
        return self.data / "pfx", self.data / "pfx/drive_c/Program Files/Example/bin/app.exe"

    def test_a_finished_prefix_is_kept_and_each_state_names_its_reason(self):
        pfx, program = self.finished()
        marker, runner = pfx / en.profiles.MARKER, pfx / ".runner.json"
        def profile(**change):
            return json.dumps({"install_dir": "drive_c/Program Files/Example", "program": "bin/app.exe", **change}).encode()
        cases = ((marker, b"{}", "its record .setup.json cannot be read"), (marker, b"", "its record .setup.json cannot be read"),
                 (self.profile, profile(install_dir="drive_c/Elsewhere"), "an installed app is never moved"),
                 (self.profile, profile(program="bin/renamed.exe"), "the program bin/renamed.exe is not in its install folder"),
                 (pfx / "system.reg", b"", "its registry (system.reg) is missing"),
                 (runner, b'{"stamp": 900, "runner": "Release-9"}', "last used by Release-9, newer than Release-1"),
                 (runner, b"{", "its runner record .runner.json cannot be read"),
                 (program, None, "its program bin/app.exe is missing"),
                 (program, b"half", "the version of its program cannot be read"),
                 (program, pe((1, 0, 0, 0), extra=b"patched"), "version of its program stayed 1.0.0.0"),
                 (program, pe((0, 9, 0, 0)), "its program went back from 1.0.0.0 to 0.9.0.0"),
                 (program.parent / "extra.dll", b"new", "version of its program stayed 1.0.0.0"))
        for path, data, reason in cases:
            with self.subTest(reason=reason, path=path.name):
                original = path.read_bytes() if path.exists() else None
                path.unlink() if data is None else path.write_bytes(data)
                self.assertIn(reason, self.refusal())
                path.unlink() if original is None else path.write_bytes(original)
        self.assertEqual(self.setup(), 0, self.stderr.getvalue())

    def test_other_states_of_a_finished_prefix_name_their_reason_too(self):
        pfx, program = self.finished()
        with self.subTest("a file vanishes"), patch.object(en.profiles.treehash, "snapshot", side_effect=FileNotFoundError(2, "x", "app.tmp")):
            self.assertIn("a file vanished while its install folder was read (app.tmp)", self.refusal())
        with self.subTest("an unreadable folder"), patch.object(en.profiles, "differing", side_effect=OSError("unexpected")):
            self.assertIn("it could not be checked: unexpected", self.refusal())
        with self.subTest("an app that runs"):
            child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE,
                                     env={**os.environ, "WINEPREFIX": str(pfx)})
            program.write_bytes(pe((2, 0, 0, 0)))
            self.assertIn("close the app and run setup again", self.refusal())
            program.write_bytes(pe((1, 0, 0, 0)))
            self.assertEqual(self.setup(), 0, self.stderr.getvalue())
            child.stdin.close()
            child.wait(10)
        with self.subTest("a linked record"):
            marker = pfx / en.profiles.MARKER
            marker.rename(self.root / "record.json")
            marker.symlink_to(self.root / "record.json")
            self.assertIn("its record .setup.json cannot be read", self.refusal())
            marker.unlink()
            (self.root / "record.json").rename(marker)
        with self.subTest("a linked prefix"):
            pfx.rename(self.data / "elsewhere")
            pfx.symlink_to(self.data / "elsewhere")
            self.assertIn("it is not as setup left it", self.refusal())

    def test_the_check_records_nothing_itself(self):
        pfx, program = self.finished()
        program.write_bytes(pe((2, 0, 0, 0)))
        before, calls = self.tree(), []
        with patch.object(en.profiles, "main", side_effect=lambda args: calls.append(args[0]) or 0):
            self.assertEqual(self.setup(), 2)
        self.assertEqual((calls, self.tree()), (["apply"], before))
        self.assertIn(f"{pfx} changed again while setup ran; run setup again", self.stderr.getvalue())
        with patch.object(en.profiles, "main", return_value=3):
            self.assertEqual(self.setup(), 2)
        self.assertIn("prefix failed with exit 3\n", self.stderr.getvalue())
        self.assertNotIn("remove", self.stderr.getvalue())

    def wine(self, command, **kwargs):
        """A Wine that only notes what it was asked to run."""
        self.ran.append([Path(command[0]).name, *command[1:]])
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    def real_apply(self):
        """Let ./setup reach the real apply command, over a fake Wine."""
        self.put(self.data / "runners/patched/files/bin/wine", b"wine")
        self.ran = []
        self.enterContext(patch.object(en.profiles, "main", side_effect=REAL_APPLY))
        self.enterContext(patch.object(en.prefix.subprocess, "run", side_effect=self.wine))

    def test_setup_takes_an_app_update_and_a_changed_profile_to_the_real_apply(self):
        pfx, program = self.finished()
        self.real_apply()
        data = pe((1, 1, 0, 0))
        program.write_bytes(data)
        self.profile.write_text(json.dumps({**json.loads(self.profile.read_text()), "registry": [
            {"key": "HKCU\\Software\\Wine\\Example", "name": "Value", "type": "REG_SZ", "data": "1"}]}))
        self.assertEqual(self.setup(), 0, self.stderr.getvalue())
        self.assertIn("the app updated itself: 1.0.0.0 to 1.1.0.0 (1 files changed)", self.stdout.getvalue())
        self.assertIn("profile updated in place", self.stdout.getvalue())
        self.assertRegex(self.stdout.getvalue(), r"prefix: complete")
        self.assertEqual(self.ran, [["wine", "winecfg", "-v", "win10"],
                                    ["wine", "reg", "add", "HKCU\\Software\\Wine\\Example", "/v", "Value", "/t", "REG_SZ", "/d", "1", "/f"],
                                    ["wineserver", "-w"]])
        self.assertEqual(program.read_bytes(), data)
        record = json.loads((pfx / en.profiles.MARKER).read_text())
        self.assertEqual((record["program_version"], record["profile_sha256"]), ("1.1.0.0", en.runner.sha256(self.profile)))
        self.ran.clear()
        self.assertEqual(self.setup(), 0, self.stderr.getvalue())
        self.assertEqual(self.ran, [])
        self.assertIn("prefix: verified, skipped", self.stdout.getvalue())

    def test_setup_fills_in_a_marker_of_the_published_version(self):
        pfx, program = self.finished()
        self.real_apply()
        marker = pfx / en.profiles.MARKER
        record = json.loads(marker.read_text())
        marker.write_text(json.dumps({k: record[k] for k in ("profile_sha256", "install_dir", "install_tree")}))
        self.assertEqual(self.setup(), 0, self.stderr.getvalue())
        self.assertEqual(json.loads(marker.read_text()), record)
        self.assertEqual(self.ran, [])

    def test_a_full_disk_reports_itself(self):
        pfx, program = self.finished()
        self.real_apply()
        program.write_bytes(pe((2, 0, 0, 0)))
        before = self.tree()
        with patch.object(en.profiles.os, "replace", side_effect=OSError(28, "No space left on device")), \
                self.assertRaises(SystemExit) as stop:
            self.setup()
        self.assertIn("No space left on device", str(stop.exception.code))
        self.assertNotIn("remove", str(stop.exception.code) + self.stderr.getvalue())
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.setup(), 0, self.stderr.getvalue())
        self.assertIn("the app updated itself: 1.0.0.0 to 2.0.0.0", self.stdout.getvalue())

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

    def runner_bytes(self):
        runner = self.data / "runners/patched"
        found = {str(p.relative_to(runner)): p.read_bytes() for p in runner.rglob("*") if p.is_file()}
        self.assertIn("version", found)
        return found

    def new_patch(self, text=b"another patch"):
        self.patch_file.write_bytes(text)

    def prefix_tree(self):
        return en.profiles.treehash.snapshot(self.data / "pfx")

    def noting(self, calls):
        """The fake runner commands, noted in calls."""
        return patch.object(en.runner, "main", side_effect=lambda args: calls.append(args) or self.stage(args))

    def test_a_changed_patch_ends_in_the_new_runner_with_one_setup(self):
        self.assertEqual(self.setup(), 0, self.stderr.getvalue())
        before, old = self.prefix_tree(), self.runner_bytes()
        self.new_patch()
        calls = []
        with self.noting(calls):
            self.assertEqual(self.setup(), 0, self.stderr.getvalue())
        self.assertEqual([c[0] for c in calls], ["build", "assemble"])
        for call in calls:
            for option in ("--work", "--out"):
                if option in call:
                    self.assertTrue(Path(call[call.index(option) + 1]).is_relative_to(self.data / "rebuild"), call)
        self.assertNotEqual(self.runner_bytes(), old)
        self.assertNotIn("remove", self.stderr.getvalue())
        self.assertFalse((self.data / "rebuild").exists())
        self.assertEqual(en.profiles.treehash.compare(self.data / "pfx", before), [])
        self.assertRegex(self.stdout.getvalue(), r"switch: complete")
        self.forbid_work = True
        self.assertEqual(self.setup(), 0, self.stderr.getvalue())

    def stage_release(self, args, tag, stamp):
        if args[0] == "fetch":
            base = self.data / f"runners/{tag}-x86_64"
            self.put(base / "version", f"{stamp} {tag}\n".encode())
            self.put(base / en.LIB / "sample.dll", b"original of " + tag.encode())
            return 0
        code = self.stage(args)
        if args[0] == "assemble":
            out, base = (Path(args[args.index(o) + 1]) for o in ("--out", "--base"))
            manifest = json.loads((out / "halation-runner.json").read_text())
            manifest["base"] = tag
            manifest["dlls"]["sample.dll"]["replaces"] = en.runner.sha256(base / en.LIB / "sample.dll")
            (out / "halation-runner.json").write_text(json.dumps(manifest))
            (out / "version").write_bytes((base / "version").read_bytes())
        return code

    def test_a_changed_pin_is_rebuilt_from_the_new_download(self):
        self.assertEqual(self.setup(), 0)
        old_base = self.base
        self.put(self.data / "runners/other/version", b"1 Other\n")
        (self.data / "runners/empty").mkdir()
        self.put(self.data / "runners/odd/version", b"\xff")
        self.pins.write_text(json.dumps({"ge_tag": "Release-2", "ge_asset": "Release-2-x86_64"}))
        with patch.object(en.runner, "main", side_effect=lambda args: self.stage_release(args, "Release-2", 300)):
            self.assertEqual(self.setup(), 0, self.stderr.getvalue())
        self.assertEqual(json.loads((self.data / "runners/patched/halation-runner.json").read_text())["base"], "Release-2")
        self.assertTrue(old_base.exists())
        notice = f"{old_base} (Release-1) is no longer used and can be deleted"
        self.assertEqual(self.stdout.getvalue().count(notice), 1)
        self.assertEqual(self.stdout.getvalue().count("can be deleted"), 1)
        self.assertEqual(self.setup(), 0, self.stderr.getvalue())
        self.assertEqual(self.stdout.getvalue().count("can be deleted"), 1)

    def test_a_failure_or_kill_at_any_step_keeps_the_old_runner_and_the_next_setup_ends_well(self):
        class Kill(BaseException):
            pass
        for step in ("build", "assemble"):
            for moment in ("before", "after", "exit 4"):
                with self.subTest(step=step, moment=moment):
                    self.new_patch(b"first")
                    self.assertEqual(self.setup(), 0, self.stderr.getvalue())
                    old, tree = self.runner_bytes(), self.prefix_tree()
                    self.new_patch(b"second")
                    def action(args):
                        if args[0] == step and moment == "before":
                            raise Kill
                        code = self.stage(args)
                        if args[0] == step and moment == "after":
                            raise Kill
                        return 4 if args[0] == step else code
                    with patch.object(en.runner, "main", side_effect=action):
                        if moment == "exit 4":
                            self.stderr.truncate(0)
                            self.stderr.seek(0)
                            self.assertEqual(self.setup(), 2)
                            self.assertIn(f"{step} failed with exit 4; the runner in use is unchanged", self.stderr.getvalue())
                            self.assertNotIn("remove", self.stderr.getvalue())
                        else:
                            with self.assertRaises(Kill):
                                self.setup()
                    self.assertEqual(self.runner_bytes(), old)
                    self.assertEqual(self.setup(), 0, self.stderr.getvalue())
                    self.assertFalse((self.data / "rebuild").exists())
                    self.assertEqual(en.profiles.treehash.compare(self.data / "pfx", tree), [])

    def test_a_kill_between_any_two_moves_is_finished_by_setup_or_run(self):
        class Kill(BaseException):
            pass
        for finisher in ("setup", "run"):
            for allowed in range(5):
                with self.subTest(finisher=finisher, allowed=allowed):
                    self.new_patch(b"first")
                    self.assertEqual(self.setup(), 0, self.stderr.getvalue())
                    self.new_patch(b"second")
                    left, real = [allowed], en.rebuild.move
                    def move(source, target):
                        if not left[0]:
                            raise Kill
                        left[0] -= 1
                        real(source, target)
                    with patch.object(en.rebuild, "move", side_effect=move):
                        try:
                            self.setup()
                        except Kill:
                            pass
                    if finisher == "run":
                        with patch.object(en.prefix, "main", return_value=0) as launch:
                            self.assertEqual(en.run_main(["--data", str(self.data)]), 0, self.stderr.getvalue())
                        self.assertTrue((Path(launch.call_args[0][0][2]) / "version").is_file())
                    self.assertEqual(self.setup(), 0, self.stderr.getvalue())
                    self.assertFalse((self.data / "rebuild").exists())
                    manifest = json.loads((self.data / "runners/patched/halation-runner.json").read_text())
                    self.assertEqual(manifest["dlls"]["sample.dll"]["sha256"], en.runner.sha256(self.data / "runner-work/out/sample.dll"))

    def test_no_switch_while_a_program_runs_in_the_prefix(self):
        self.assertEqual(self.setup(), 0)
        old = self.runner_bytes()
        self.new_patch()
        with patch.object(en.prefix, "in_use", return_value="app.exe, process 7"):
            self.assertEqual(self.setup(), 2)
        self.assertIn("the new runner is ready, but a program still runs in the prefix (app.exe, process 7); close the app and run setup again",
                      self.stderr.getvalue())
        self.assertEqual(self.runner_bytes(), old)
        calls = []
        with self.noting(calls):
            self.assertEqual(self.setup(), 0, self.stderr.getvalue())
        self.assertEqual(calls, [])
        self.assertFalse((self.data / "rebuild").exists())

    def test_a_staged_set_that_was_changed_is_built_again(self):
        self.assertEqual(self.setup(), 0)
        self.new_patch()
        with patch.object(en.prefix, "in_use", return_value="app.exe, process 7"):
            self.assertEqual(self.setup(), 2)
        (self.data / "rebuild/runners/patched" / en.LIB / "sample.dll").write_bytes(b"changed")
        calls = []
        with self.noting(calls):
            self.assertEqual(self.setup(), 0, self.stderr.getvalue())
        self.assertEqual([c[0] for c in calls], ["assemble"])

    def test_a_staged_set_for_other_inputs_is_deleted_whole(self):
        self.assertEqual(self.setup(), 0)
        self.new_patch(b"second")
        with patch.object(en.prefix, "in_use", return_value="app.exe, process 7"):
            self.assertEqual(self.setup(), 2)
        self.new_patch(b"third")
        self.put(self.data / "rebuild/stray", b"stray")
        calls = []
        def action(args):
            calls.append(args[0])
            self.assertFalse((self.data / "rebuild/stray").exists())
            return self.stage(args)
        with patch.object(en.runner, "main", side_effect=action):
            self.assertEqual(self.setup(), 0, self.stderr.getvalue())
        self.assertEqual(calls, ["build", "assemble"])

    def test_an_unfinished_set_is_resumed_at_the_step_that_failed(self):
        self.assertEqual(self.setup(), 0)
        for leftover in (None, "rebuild/runners/patched/half"):
            with self.subTest(leftover=leftover):
                self.new_patch(str(leftover).encode())
                def action(args):
                    if args[0] == "assemble":
                        if leftover:
                            self.put(self.data / leftover, b"half")
                        return 4
                    return self.stage(args)
                with patch.object(en.runner, "main", side_effect=action):
                    self.assertEqual(self.setup(), 2)
                calls = []
                self.stdout.truncate(0)
                with self.noting(calls):
                    self.assertEqual(self.setup(), 0, self.stderr.getvalue())
                self.assertEqual([c[0] for c in calls], ["assemble"])
                self.assertIn("build: verified, skipped", self.stdout.getvalue())
                self.assertFalse((self.data / "rebuild").exists())
                self.assertFalse((self.data / "runners/patched/half").exists())
        self.assertNotIn("remove", self.stderr.getvalue())

    def test_a_staged_step_that_fails_its_own_check_names_no_folder(self):
        self.assertEqual(self.setup(), 0)
        old = self.runner_bytes()
        self.new_patch()
        def action(args):
            code = self.stage(args)
            if args[0] == "assemble":
                (Path(args[args.index("--out") + 1]) / en.LIB / "sample.dll").write_bytes(b"wrong")
            return code
        with patch.object(en.runner, "main", side_effect=action):
            self.assertEqual(self.setup(), 2)
        self.assertIn("assemble did not pass its check; the runner in use is unchanged, run setup again", self.stderr.getvalue())
        self.assertNotIn("remove", self.stderr.getvalue())
        self.assertEqual(self.runner_bytes(), old)

    def test_a_first_build_that_fails_says_to_run_setup_again(self):
        with patch.object(en.runner, "main", side_effect=lambda args: 4 if args[0] == "build" else self.stage(args)):
            self.assertEqual(self.setup(), 2)
        self.assertIn("build failed with exit 4; run setup again", self.stderr.getvalue())
        self.assertEqual(self.setup(), 0, self.stderr.getvalue())

    def test_a_step_that_stops_with_a_message_keeps_the_old_runner_and_says_so(self):
        self.assertEqual(self.setup(), 0, self.stderr.getvalue())
        old, tree = self.runner_bytes(), self.prefix_tree()
        self.new_patch()
        for step in ("build", "assemble"):
            with self.subTest(step=step):
                def action(args):
                    if args[0] == step:
                        sys.exit("runner.py: failed (1): a command")
                    return self.stage(args)
                with patch.object(en.runner, "main", side_effect=action):
                    self.assertEqual(self.setup(), 2)
                self.assertRegex(self.stderr.getvalue(), rf"a command\n{step}: failed \(\S+\)\nsetup: {step} failed with exit 1; "
                                                         "the runner in use is unchanged, run setup again")
                self.assertEqual(self.runner_bytes(), old)
                self.assertEqual(en.profiles.treehash.compare(self.data / "pfx", tree), [])
        for step in (None, 0):
            with self.subTest(step=step), patch.object(en.runner, "main", side_effect=SystemExit(step)), self.assertRaises(SystemExit):
                self.setup()
        self.assertEqual(self.setup(), 0, self.stderr.getvalue())

    def test_a_set_made_in_place_by_an_earlier_setup_is_recognised(self):
        self.stage(["fetch"])
        self.stage(["build", "--work", str(self.data / "runner-work")])
        self.stage(["assemble", "--work", str(self.data / "runner-work"), "--base", str(self.base), "--out", str(self.data / "runners/patched")])
        self.forbid_work = True
        with patch.object(en.profiles, "main", side_effect=self.apply_only):
            self.assertEqual(self.setup(), 0, self.stderr.getvalue())
        for step in ("build", "assemble"):
            self.assertIn(f"{step}: verified, skipped", self.stdout.getvalue())
        self.forbid_work = False
        self.new_patch()
        self.assertEqual(self.setup(), 0, self.stderr.getvalue())
        self.assertIn("switch: complete", self.stdout.getvalue())

    def apply_only(self, args):
        self.forbid_work = False
        try:
            return self.stage(args)
        finally:
            self.forbid_work = True

    def test_no_older_runner_than_the_prefix_knows(self):
        self.assertEqual(self.setup(), 0)
        old = self.runner_bytes()
        (self.data / "pfx/.runner.json").write_text('{"stamp": 900, "runner": "Release-9"}')
        self.new_patch()
        calls = []
        with self.noting(calls):
            self.assertEqual(self.setup(), 2)
        self.assertEqual(calls, [])
        self.assertIn("was last used by Release-9, newer than Release-1", self.stderr.getvalue())
        self.assertNotIn("remove", self.stderr.getvalue())
        self.assertEqual(self.runner_bytes(), old)

    def test_too_little_space_is_said_before_anything_is_built(self):
        self.assertEqual(self.setup(), 0)
        self.new_patch()
        before = self.tree()
        self.forbid_work = True
        with patch.object(en.rebuild.shutil, "disk_usage", return_value=SimpleNamespace(free=10**8)):
            self.assertEqual(self.setup(), 2)
        self.assertIn("0.1 GB free; nothing was changed", self.stderr.getvalue())
        self.assertEqual(self.tree(), before)

    def test_a_failed_check_in_place_brings_the_old_runner_back(self):
        self.assertEqual(self.setup(), 0)
        old = self.runner_bytes()
        self.new_patch()
        real, seen = en.RunnerSet.current, []
        def current(self_set):
            seen.append(self_set.root)
            if len(seen) == 2:
                raise ValueError("injected")
            return real(self_set)
        with patch.object(en.RunnerSet, "current", current):
            self.assertEqual(self.setup(), 2)
        self.assertIn("the new runner did not pass its check in place (injected); the one before it is back", self.stderr.getvalue())
        self.assertEqual(self.runner_bytes(), old)
        self.assertEqual(self.setup(), 0, self.stderr.getvalue())
        self.assertFalse((self.data / "rebuild").exists())

    def test_an_unfinished_first_build_is_replaced(self):
        for leftover in ("runner-work/half", "runner-work-unix/half", "runners/patched-pe/half"):
            with self.subTest(leftover=leftover):
                self.put(self.data / leftover, b"half")
                self.assertEqual(self.setup(), 0, self.stderr.getvalue())
                self.assertFalse((self.data / leftover).exists())
                self.assertNotIn("remove", self.stderr.getvalue())
                shutil.rmtree(self.data)

    def test_a_link_or_a_prefix_in_the_place_of_a_runner_folder_is_kept(self):
        self.assertEqual(self.setup(), 0)
        self.new_patch()
        work, outside = self.data / "runner-work", self.root / "outside"
        work.rename(self.root / "real-work")
        self.put(outside / "keep", b"keep")
        work.symlink_to(outside)
        self.forbid_work = True
        self.assertEqual(self.setup(), 2)
        self.assertIn("not a folder setup made", self.stderr.getvalue())
        self.stderr.truncate(0)
        self.assertEqual((outside / "keep").read_bytes(), b"keep")
        self.assertTrue(work.is_symlink())
        work.unlink()
        (self.root / "real-work").rename(work)
        (work / "system.reg").write_text("a prefix")
        self.assertEqual(self.setup(), 2)
        self.forbid_work = False
        self.assertIn("not a folder setup made", self.stderr.getvalue())
        self.assertTrue((work / "system.reg").exists())

    def test_only_one_setup_at_a_time(self):
        with en.rebuild.lock(self.data):
            self.assertEqual(self.setup(), 2)
        self.assertIn("another setup is running", self.stderr.getvalue())

    def test_preflight_refuses_missing_tools_before_work(self):
        self.forbid_work = True
        with patch.object(en.shutil, "which", side_effect=lambda n: None if n in ("autoconf", "autoheader") else "ok"), \
             patch.object(en.runner, "llvm_ar", return_value=None):
            self.assertEqual(self.setup(), 2)
        for name in ("autoconf", "autoheader", "llvm-ar"):
            self.assertIn(name, self.stderr.getvalue())
        self.assertFalse(self.data.exists())

    def test_preflight_names_every_missing_tool_and_package_at_once(self):
        self.forbid_work = True
        with patch.object(en.shutil, "which", side_effect=lambda n: None if n in ("flex", "objdump") else "ok"), \
             patch.object(en.runner, "missing_packages", return_value=["freetype2", "egl"]):
            self.assertEqual(self.setup(), 2)
        errors = self.stderr.getvalue()
        self.assertIn("missing required tools: flex, objdump", errors)
        self.assertIn("missing development packages (pkg-config names): freetype2, egl", errors)
        self.assertIn("README.md", errors)
        self.assertFalse(self.data.exists())

    def test_packages_alone_stop_setup_too(self):
        self.forbid_work = True
        with patch.object(en.runner, "missing_packages", return_value=["freetype2"]):
            self.assertEqual(self.setup(), 2)
        self.assertNotIn("missing required tools", self.stderr.getvalue())
        self.assertIn("freetype2", self.stderr.getvalue())
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
        record = {}
        def new_unix_patch(text):
            unix_patch.write_bytes(text)
            record.update(identity=en.runner.patch_identity(self.pins.read_bytes(), [(unix_patch, ["winex11.drv"])], "unix"),
                          driver_version=111, artifacts={name: en.runner.sha256(unix_patch) for name in en.runner.UNIX_FILES})
        new_unix_patch(b"Unix patch")
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
                    self.put(work / "out" / name, unix_patch.read_bytes())
            else:
                base, out = arg("--base"), arg("--out")
                shutil.copytree(base, out)
                manifest = json.loads((out / "halation-runner.json").read_text())
                manifest["patches"] = sorted(p.name for p, _ in patches)
                files = {}
                for name, relative in en.runner.UNIX_FILES.items():
                    self.put(out / relative, unix_patch.read_bytes())
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
            script = (self.data / "runners/patched/proton").read_text()
            (self.data / "runners/patched/proton").write_text("    " + en.runner.OPENCL_OVERRIDE + "\n")
            self.assertEqual(self.setup(), 2)
            self.assertEqual(calls, [])
            (self.data / "runners/patched/proton").write_text(script)
            new_unix_patch(b"another Unix patch")
            self.assertEqual(self.setup(), 0, self.stderr.getvalue())
            self.assertEqual(calls, [("build", "pe"), ("assemble", "pe"), ("build", "unix"), ("assemble", "unix")])
            self.assertEqual((self.data / "runners/patched" / en.runner.UNIX_FILES["winex11.so"]).read_bytes(), b"another Unix patch")
            self.assertFalse((self.data / "rebuild").exists())

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

    def test_setup_writes_the_settings_again_over_another_runner(self):
        pfx, _ = self.finished()
        self.real_apply()
        marker, note = pfx / en.profiles.MARKER, pfx / ".update-timestamp"
        inf = self.data / "runners/patched/files/share/wine/wine.inf"
        self.put(inf, b"the same in every release")
        os.utime(inf, (1000, 1000))
        def wine(cmd, **kwargs):
            note.write_text(str(int(inf.stat().st_mtime)))
            return self.wine(cmd, **kwargs)
        settings = [["wine", "winecfg", "-v", "win10"], ["wineserver", "-w"]]
        update = [["wine", "wineboot", "-u"], ["wineserver", "-w"]]
        with patch.object(en.prefix.subprocess, "run", side_effect=wine):
            self.assertEqual(self.setup(), 0, self.stderr.getvalue())
            self.assertEqual(self.ran, update + settings)
            for seconds, ran in ((1000, []), (2000, update + settings), (2000, [])):
                with self.subTest(seconds=seconds, steps=len(ran)):
                    os.utime(inf, (seconds, seconds))
                    self.ran.clear()
                    tree = en.profiles.snapshot(pfx / "drive_c/Program Files/Example")
                    self.assertEqual(self.setup(), 0, self.stderr.getvalue())
                    self.assertEqual(self.ran, ran)
                    self.assertEqual(json.loads(marker.read_text())["wine_inf"], seconds)
                    self.assertEqual(en.profiles.snapshot(pfx / "drive_c/Program Files/Example"), tree)
            def release(args):
                code = self.stage_release(args, "Release-2", 300)
                if args[0] == "assemble":
                    new = Path(args[args.index("--out") + 1])
                    self.put(new / "files/bin/wine", b"wine")
                    self.put(new / "files/share/wine/wine.inf", inf.read_bytes())
                    os.utime(new / "files/share/wine/wine.inf", (3000, 3000))
                return code
            with self.subTest("a new pin"):
                self.pins.write_text(json.dumps({"ge_tag": "Release-2", "ge_asset": "Release-2-x86_64"}))
                self.ran.clear()
                with patch.object(en.runner, "main", side_effect=release):
                    self.assertEqual(self.setup(), 0, self.stderr.getvalue())
                self.assertEqual(json.loads((self.data / "runners/patched/halation-runner.json").read_text())["base"], "Release-2")
                self.assertEqual(self.ran, update + settings)
                self.assertEqual(json.loads(marker.read_text())["wine_inf"], 3000)
                self.assertEqual(en.profiles.snapshot(pfx / "drive_c/Program Files/Example"), tree)
                self.ran.clear()
                self.assertEqual((self.setup(), self.ran), (0, []), self.stderr.getvalue())

    def test_run_says_when_a_runner_set_the_settings_anew_and_starts(self):
        self.assertEqual(self.setup(), 0)
        pfx, inf = self.data / "pfx", self.data / "runners/patched/files/share/wine/wine.inf"
        self.put(inf, b"the same in every release")
        os.utime(inf, (1000, 1000))
        self.put(pfx / ".update-timestamp", b"1000\n")
        marker = pfx / en.profiles.MARKER
        plain = {k: v for k, v in json.loads(marker.read_text()).items() if k != "wine_inf"}
        cases = (("as recorded", 1000, b"1000", {"wine_inf": 1000}, False), ("a new runner", 2000, b"1000", {"wine_inf": 1000}, True),
                 ("another runner was in it", 1000, b"1500", {"wine_inf": 1000}, True), ("a marker from before", 2000, b"1000", {}, False),
                 ("another runner and this one again", 1000, b"1000", {"wine_inf": 1000, "wine_inf_noted": 1}, True),
                 ("wine may not update it", 1000, b"disable", {"wine_inf": 1000}, False), ("no record", 2000, b"1000", None, False),
                 ("an unreadable record", 2000, b"1000", "{", False))
        for name, seconds, note, record, said in cases:
            with self.subTest(name=name):
                os.utime(inf, (seconds, seconds))
                (pfx / ".update-timestamp").write_bytes(note)
                marker.unlink(missing_ok=True)
                if isinstance(record, dict):
                    noted = {"wine_inf_noted": en.prefix.noted_inf(pfx)} if record else {}
                    record = json.dumps({**plain, **noted, **record})
                if record is not None:
                    marker.write_text(record)
                before = en.profiles.treehash.snapshot(pfx)
                self.stderr.truncate(0)
                self.stderr.seek(0)
                with patch.object(en.prefix, "main", return_value=7):
                    self.assertEqual(en.run_main(["--data", str(self.data)]), 7)
                self.assertEqual("run ./setup to write them again" in self.stderr.getvalue(), said)
                self.assertEqual(en.profiles.treehash.compare(pfx, before), [])

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

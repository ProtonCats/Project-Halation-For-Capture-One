# SPDX-FileCopyrightText: 2026 ProtonCats and contributors
# SPDX-License-Identifier: MIT
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import setup as st

from tests.test_peversion import pe

REPO = Path(__file__).resolve().parents[1]


class Setup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=REPO)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.prefix, self.runner = self.root / "prefix", self.root / "runner"
        binary = self.runner / "files/bin/wine"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"fixture")
        (self.runner / "version").write_text("200 current\n")
        self.installer, self.profile_file = self.root / "local.exe", self.root / "profile.json"
        self.installer.write_bytes(b"local installer")
        self.url = "https://example.invalid/runtime.exe"
        self.payloads = {self.url: b"runtime"}
        self.profile = {
            "prerequisites": [{"name": "runtime.exe", "url": self.url,
                               "sha256": hashlib.sha256(b"runtime").hexdigest(), "args": ["/quiet"]}],
            "windows_version": "win10", "dll_overrides": {"graphics": "builtin", "optional": "disabled"},
            "registry": [{"key": "HKCU\\Software\\Microsoft\\Avalon.Graphics", "name": "DisableHWAcceleration",
                          "type": "REG_DWORD", "data": 1},
                         {"key": "HKCU\\Software\\Wine\\Direct3D", "name": "OldValue", "type": "REG_SZ", "data": None}],
            "installer_args": ["/quiet"], "install_dir": "drive_c/Program Files/Example",
        }
        self.write_profile()
        self.events = []
        self.registry = {("HKCU\\Software\\Wine\\Direct3D", "OldValue"): "old"}
        self.types = {}
        self.tamper, self.fail_step = None, False
        self.fail_at = self.tamper_program = None
        self.install_files = {}
        self.enterContext(patch.object(st.px.shutil, "which", return_value="available"))
        self.enterContext(patch.object(st.px.subprocess, "run", side_effect=self.process))
        opener = SimpleNamespace(open=self.download)
        self.enterContext(patch.object(st.urllib.request, "build_opener", return_value=opener))
        self.output = self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.errors = self.enterContext(contextlib.redirect_stderr(io.StringIO()))

    @property
    def install(self):
        return self.prefix / self.profile["install_dir"]

    @property
    def cache(self):
        return self.prefix.parent / ".setup-cache"

    def write_profile(self):
        self.profile_file.write_text(json.dumps(self.profile))

    def apply(self, *extra):
        return st.main(["apply", "--profile", str(self.profile_file), "--runner", str(self.runner),
                        "--prefix", str(self.prefix), "--installer", str(self.installer), *extra])

    def download(self, url, **kwargs):
        data = self.payloads[url]
        if isinstance(data, Exception):
            raise data
        return io.BytesIO(data)

    def process(self, cmd, **kwargs):
        if Path(cmd[0]).name == "wineserver":
            return subprocess.CompletedProcess(cmd, 0)
        if cmd[0] == "umu-run":
            self.prefix.mkdir(parents=True, exist_ok=True)
            (self.prefix / "system.reg").write_text('"ProgramFilesDir"="C:\\\\Program Files"\n')
            stage = "create"
        else:
            args = cmd[1:]
            if args[0] == "winecfg":
                self.registry["version"] = args[args.index("-v") + 1]
                stage = "version"
            elif args[0] == "reg":
                key, name = args[2], args[args.index("/v") + 1] if "/v" in args else ""
                if args[1] == "delete":
                    self.registry.pop((key, name), None)
                    self.types.pop((key, name), None)
                else:
                    self.registry[key, name] = args[args.index("/d") + 1]
                    self.types[key, name] = args[args.index("/t") + 1]
                stage = "override" if key.endswith("DllOverrides") else "registry"
            else:
                path = Path(args[2] if args[0] == "msiexec" else args[0])
                stage = "installer" if path.read_bytes() == b"local installer" else "prerequisite"
                if stage == "installer":
                    self.install.mkdir(parents=True, exist_ok=True)
                    (self.install / "program.dll").write_bytes(b"installed")
                    for relative, data in self.install_files.items():
                        (self.install / relative).parent.mkdir(parents=True, exist_ok=True)
                        (self.install / relative).write_bytes(data)
                    self.registry["installer_path"] = str(path)
                    self.registry["at_install"] = list(self.events)
        self.events.append(stage)
        if stage == self.fail_at:
            return subprocess.CompletedProcess(cmd, 9)
        if self.tamper_program and cmd[0] != "umu-run":
            self.put_program(self.tamper_program)
        if stage == self.tamper:
            (self.install / "program.dll").write_bytes(b"tampered")
            if self.fail_step:
                return subprocess.CompletedProcess(cmd, 9)
        return subprocess.CompletedProcess(cmd, 0)

    def assert_pristine_refusal(self, message):
        before = st.treehash.snapshot(self.root)
        self.errors.seek(0)
        self.errors.truncate()
        with self.assertRaises(SystemExit) as exc:
            self.apply()
        self.assertTrue(exc.exception.code)
        self.assertRegex(str(exc.exception) + self.errors.getvalue(), message)
        self.assertEqual(st.treehash.compare(self.root, before), [])
        self.assertEqual(self.events, [])

    def installer_outcomes(self, codes, partial=False):
        outcomes = iter(codes)
        self.attempts, self.waits = [], []
        def execute(cmd, **kwargs):
            if len(cmd) > 1 and cmd[1] == str(self.installer):
                self.assertFalse((self.prefix / st.MARKER).exists())
                code = next(outcomes)
                self.attempts.append((cmd[1:], code))
                if code and not partial:
                    self.events.append("installer")
                    return subprocess.CompletedProcess(cmd, code)
                self.process(cmd, **kwargs)
                return subprocess.CompletedProcess(cmd, code)
            return self.process(cmd, **kwargs)
        def wait(seconds):
            self.assertFalse(self.install.exists())
            self.assertFalse((self.prefix / st.MARKER).exists())
            self.waits.append(seconds)
        self.enterContext(patch.object(st.px.subprocess, "run", side_effect=execute))
        self.enterContext(patch.object(st.time, "sleep", side_effect=wait))
        self.enterContext(patch.object(st.time, "monotonic", side_effect=[100, 104, 114, 120]))

    def test_installer_retry_succeeds_and_records_both_attempts(self):
        self.installer_outcomes([7, 0])
        self.assertEqual(self.apply(), 0)
        self.assertEqual([code for _, code in self.attempts], [7, 0])
        self.assertEqual(self.attempts[0][0], [str(self.installer), "/quiet"])
        self.assertEqual(self.attempts[0][0], self.attempts[1][0])
        self.assertEqual(self.waits, [10])
        self.assertIn("installer attempt 1: exit 7 (4.0s)", self.output.getvalue())
        self.assertIn("installer attempt 2: exit 0 (6.0s)", self.output.getvalue())
        marker = json.loads((self.prefix / st.MARKER).read_text())
        self.assertTrue(st.unchanged(self.install, marker["install_tree"]))

    def test_installer_retry_failure_refuses_completion(self):
        self.installer_outcomes([7, 7])
        with self.assertRaises(SystemExit) as error:
            self.apply()
        self.assertEqual([code for _, code in self.attempts], [7, 7])
        self.assertEqual(self.waits, [10])
        message = str(error.exception)
        for fragment in ("exit codes: 7, 7", f"remove the prefix folder {self.prefix}", "run setup again"):
            self.assertIn(fragment, message)
        self.assertIn("installer attempt 2: exit 7 (6.0s)", self.output.getvalue())
        self.assertFalse((self.prefix / st.MARKER).exists())
        self.assertFalse(self.install.exists())

    def test_installer_partial_failure_is_never_retried(self):
        self.installer_outcomes([7, 0], partial=True)
        with self.assertRaises(SystemExit) as error:
            self.apply()
        self.assertEqual([code for _, code in self.attempts], [7])
        self.assertEqual(self.waits, [])
        self.assertIn("exit codes: 7", str(error.exception))
        self.assertIn(f"remove the prefix folder {self.prefix}", str(error.exception))
        self.assertIn("run setup again", str(error.exception))
        self.assertTrue(self.install.is_dir())
        self.assertFalse((self.prefix / st.MARKER).exists())
        self.assertIn("installer attempt 1: exit 7 (4.0s)", self.output.getvalue())

    def test_installer_first_success_runs_once(self):
        self.installer_outcomes([0, 0])
        self.assertEqual(self.apply(), 0)
        self.assertEqual([code for _, code in self.attempts], [0])
        self.assertEqual(self.waits, [])
        self.assertIn("installer attempt 1: exit 0 (4.0s)", self.output.getvalue())
        self.assertNotIn("installer attempt 2", self.output.getvalue())
        marker = json.loads((self.prefix / st.MARKER).read_text())
        self.assertTrue(st.unchanged(self.install, marker["install_tree"]))

    def test_setup_never_reads_the_network_route(self):
        self.assertEqual(self.apply(), 0)
        self.assertNotIn("ip", [c.args[0][0] for c in st.px.subprocess.run.call_args_list])

    def test_all_checksums_are_verified_before_creating_or_running_anything(self):
        for later in (False, True):
            with self.subTest(later=later):
                if later:
                    item = {**self.profile["prerequisites"][0], "name": "second.exe", "url": self.url + "?second"}
                    self.profile["prerequisites"].append(item)
                    self.payloads[self.url] = b"runtime"
                    self.payloads[item["url"]] = b"incorrect"
                else:
                    self.payloads[self.url] = b"incorrect"
                self.write_profile()
                self.assert_pristine_refusal("checksum mismatch")

    def test_settings_precede_the_local_installer_and_completion_is_recorded(self):
        self.assertEqual(self.apply(), 0)
        self.assertEqual(self.registry["version"], "win10")
        overrides = "HKCU\\Software\\Wine\\DllOverrides"
        self.assertEqual(self.registry[overrides, "graphics"], "builtin")
        self.assertEqual(self.registry[overrides, "optional"], "")
        graphics = "HKCU\\Software\\Microsoft\\Avalon.Graphics"
        self.assertEqual(self.registry[graphics, "DisableHWAcceleration"], "1")
        self.assertEqual(self.types[graphics, "DisableHWAcceleration"], "REG_DWORD")
        self.assertNotIn(("HKCU\\Software\\Wine\\Direct3D", "OldValue"), self.registry)
        self.assertEqual(self.registry["at_install"], ["create", "prerequisite", "version", "override", "override", "registry", "registry"])
        self.assertEqual((self.install / "program.dll").read_bytes(), b"installed")
        marker = json.loads((self.prefix / st.MARKER).read_text())
        self.assertEqual(marker["profile_sha256"], st.runner.sha256(self.profile_file))
        self.assertTrue(st.unchanged(self.install, marker["install_tree"]))

    def test_installer_is_required_and_is_never_copied_into_the_cache(self):
        self.installer.unlink()
        self.assert_pristine_refusal("local installer")
        self.installer.write_bytes(b"local installer")
        original = self.profile["prerequisites"][0]["sha256"]
        self.profile["prerequisites"][0]["sha256"] = st.runner.sha256(self.installer)
        self.write_profile()
        self.assert_pristine_refusal("local installer cannot be downloaded")
        self.profile["prerequisites"][0]["sha256"] = original
        self.write_profile()
        self.assertEqual(self.apply(), 0)
        self.assertEqual(self.registry["installer_path"], str(self.installer))
        self.assertEqual(self.installer.read_bytes(), b"local installer")
        copies = [p for p in self.root.rglob("*") if p.is_file() and p.read_bytes() == b"local installer"]
        self.assertEqual(copies, [self.installer])
        self.assertEqual([p.read_bytes() for p in self.cache.iterdir()], [b"runtime"])

    def test_second_apply_is_read_only_and_reports_a_noop(self):
        self.assertEqual(self.apply(), 0)
        before = st.treehash.snapshot(self.root)
        self.payloads[self.url] = OSError("offline")
        self.route, self.events = "1.1.1.1 dev eth0", []
        self.assertEqual(self.apply(), 0)
        self.assertEqual(st.treehash.compare(self.root, before), [])
        self.assertEqual(self.events, [])
        self.assertIn("already applied; nothing changed", self.output.getvalue())
        (self.install / "program.dll").write_bytes(b"external change")
        self.refused("names no program")
        self.assertEqual(self.events, [])

    def test_dry_run_prints_a_plan_and_writes_nothing(self):
        before = st.treehash.snapshot(self.root)
        self.route = "1.1.1.1 dev eth0"
        self.payloads[self.url] = OSError("offline")
        self.assertEqual(self.apply("--dry-run"), 0)
        self.assertEqual(st.treehash.compare(self.root, before), [])
        self.assertEqual(self.events, [])
        self.assertIn("local installer", self.output.getvalue())

    def test_install_tree_changes_fail_even_when_the_command_itself_fails(self):
        self.prefix.mkdir()
        (self.prefix / "system.reg").write_text('"ProgramFilesDir"="C:\\\\Program Files"\n')
        self.install.mkdir(parents=True)
        for stage in ("create", "prerequisite", "version", "override", "registry", "installer"):
            for failure in (False, True):
                with self.subTest(stage=stage, failure=failure):
                    (self.install / "program.dll").write_bytes(b"original")
                    if stage == "create":
                        (self.prefix / "system.reg").write_text("")
                    self.tamper, self.fail_step, self.events = stage, failure, []
                    with self.assertRaisesRegex(SystemExit, "install folder changed"):
                        self.apply()
                    self.assertFalse((self.prefix / st.MARKER).exists())
                    self.assertEqual(self.events[-1], stage if failure or stage in ("create", "installer") else "registry")

    def test_unlisted_registry_keys_are_refused_before_any_changes(self):
        for key in ("HKLM\\Software\\Wine\\Direct3D", "HKCU\\Software\\Other", "HKCU\\Software\\Microsoft\\Avalon.GraphicsOther"):
            with self.subTest(key=key):
                self.profile["registry"][0]["key"] = key
                self.write_profile()
                self.assert_pristine_refusal("outside the allowlist")

    def test_cached_prerequisites_work_offline_and_corruption_is_removed(self):
        self.assertEqual(self.apply(), 0)
        self.prefix = self.root / "second-prefix"
        self.route = "1.1.1.1 dev eth0"
        self.payloads[self.url] = OSError("offline")
        self.assertEqual(self.apply(), 0)
        self.prefix = self.root / "third-prefix"
        cached = next(self.cache.iterdir())
        cached.write_bytes(b"corrupt")
        self.events = []
        with self.assertRaisesRegex(SystemExit, "checksum mismatch"):
            self.apply()
        self.assertFalse(cached.exists())
        self.assertFalse(self.prefix.exists())
        self.assertEqual(self.events, [])

    def test_profile_cannot_escape_the_prefix_or_automate_authentication(self):
        for field, value in (("install_dir", "../elsewhere"), ("installer_args", ["--activate"]),
                             ("installer_url", "https://example.invalid/forbidden.exe")):
            with self.subTest(field=field):
                original = dict(self.profile)
                self.profile[field] = value
                self.write_profile()
                self.assert_pristine_refusal("inside the prefix|forbidden|unknown profile fields")
                self.profile = original

    def versioned(self, version=(1, 0, 0, 0)):
        """Apply the profile with a program whose version is 1.0.0.0."""
        self.profile["program"] = "bin/app.exe"
        self.write_profile()
        self.assertEqual(self.apply(), 0, self.errors.getvalue())
        self.put_program(version)
        record = json.loads(self.marker.read_text())
        st.write_marker(self.prefix, {**record, "install_tree": st.snapshot(self.install), "program": "bin/app.exe",
                                      "program_version": ".".join(map(str, version))})

    def refused(self, reason):
        """apply stops over a finished prefix: it says why, says to keep it, and writes nothing in it."""
        before = {p.name: p.read_bytes() for p in self.prefix.iterdir() if p.is_file()}
        with self.assertRaises(SystemExit) as stop:
            self.apply()
        message = str(stop.exception.code)
        self.assertIn(reason, message)
        self.assertIn(f"{self.prefix} was left as it is", message)
        self.assertIn("Keep it", message)
        self.assertNotIn("remove", message.lower())
        self.assertEqual({p.name: p.read_bytes() for p in self.prefix.iterdir() if p.is_file()}, before)

    def running(self):
        """A real process of the prefix, until the test ends or stop() is called."""
        child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE,
                                 env={**os.environ, "WINEPREFIX": str(self.prefix)})
        def stop():
            if not child.stdin.closed:
                child.stdin.close()
                child.wait(10)
        self.addCleanup(stop)
        return stop

    def put_program(self, version, extra=b""):
        path = self.install / "bin/app.exe"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(pe(version, extra=extra))

    @property
    def marker(self):
        return self.prefix / st.MARKER

    def test_the_marker_records_the_program_version(self):
        self.profile["program"] = "bin/app.exe"
        self.write_profile()
        self.install_files = {"bin/app.exe": pe((16, 9, 0, 3894))}
        self.assertEqual(self.apply(), 0, self.errors.getvalue())
        record = json.loads(self.marker.read_text())
        self.assertEqual((record["program"], record["program_version"]), ("bin/app.exe", "16.9.0.3894"))

    def test_an_app_update_is_recognised_and_recorded(self):
        self.versioned()
        self.put_program((2, 0, 0, 7))
        (self.install / "added.dll").write_bytes(b"new")
        self.events = []
        self.assertEqual(self.apply(), 0, self.errors.getvalue())
        self.assertIn("the app updated itself: 1.0.0.0 to 2.0.0.7 (2 files changed)", self.output.getvalue())
        record = json.loads(self.marker.read_text())
        self.assertEqual(record["program_version"], "2.0.0.7")
        self.assertTrue(st.unchanged(self.install, record["install_tree"]))
        self.assertEqual(self.events, [])
        self.output.truncate(0)
        self.assertEqual(self.apply(), 0)
        self.assertNotIn("updated itself", self.output.getvalue())

    def test_a_changed_folder_with_the_same_version_is_refused(self):
        self.versioned()
        before = self.marker.read_bytes()
        for change in ("other file", "program bytes", "program gone", "program unreadable"):
            with self.subTest(change=change):
                path = self.install / ("bin/app.exe" if change.startswith("program") else "extra.dll")
                saved = path.read_bytes() if path.exists() else None
                if change == "program gone":
                    path.unlink()
                elif change == "program unreadable":
                    path.write_bytes(b"not a program")
                elif change == "program bytes":
                    self.put_program((1, 0, 0, 0), extra=b"patched")
                else:
                    path.write_bytes(b"changed")
                self.refused({"other file": "version of its program stayed 1.0.0.0", "program bytes": "version of its program stayed 1.0.0.0",
                              "program gone": "its program bin/app.exe is missing",
                              "program unreadable": "the version of its program cannot be read"}[change])
                self.assertEqual(self.marker.read_bytes(), before)
                path.unlink(missing_ok=True) if saved is None else path.write_bytes(saved)

    def test_a_marker_without_a_version_needs_a_changed_program(self):
        self.versioned()
        record = json.loads(self.marker.read_text())
        for field in ("program", "program_version"):
            record.pop(field)
        st.write_marker(self.prefix, record)
        (self.install / "extra.dll").write_bytes(b"changed")
        self.refused("its program did not")
        self.put_program((3, 1, 0, 0))
        self.assertEqual(self.apply(), 0, self.errors.getvalue())
        self.assertIn("the app updated itself: an earlier version to 3.1.0.0", self.output.getvalue())

    def test_a_failed_marker_write_keeps_the_old_marker(self):
        self.versioned()
        before = self.marker.read_bytes()
        self.put_program((2, 0, 0, 0))
        with patch.object(st.os, "replace", side_effect=OSError("injected")), self.assertRaises((OSError, SystemExit)):
            self.apply()
        self.assertEqual(self.marker.read_bytes(), before)
        self.assertEqual([p.name for p in self.prefix.iterdir() if p.name.startswith(".setup-")], [])

    def test_a_program_behind_a_link_is_never_an_update(self):
        self.versioned()
        path = self.install / "bin/app.exe"
        other = self.root / "other.exe"
        other.write_bytes(pe((9, 0, 0, 0)))
        path.unlink()
        path.symlink_to(other)
        self.refused("the version of its program cannot be read")

    def test_only_the_recorded_program_can_show_an_update(self):
        self.versioned()
        before = self.marker.read_bytes()
        (self.install / "tool.exe").write_bytes(pe((5, 0, 0, 0)))
        (self.install / "program.dll").write_bytes(b"patched")
        self.profile["program"] = "tool.exe"
        self.write_profile()
        self.refused("version of its program stayed 1.0.0.0")
        self.assertEqual(self.marker.read_bytes(), before)

    def test_a_program_named_in_another_spelling_is_the_same_file(self):
        self.versioned()
        st.write_marker(self.prefix, {**json.loads(self.marker.read_text()), "program": "./bin//app.exe"})
        self.put_program((2, 0, 0, 0))
        self.assertEqual(self.apply(), 0, self.errors.getvalue())
        self.assertIn("the app updated itself: 1.0.0.0 to 2.0.0.0", self.output.getvalue())

    def test_a_program_that_went_back_is_refused_by_both_versions(self):
        self.versioned((1, 9, 0, 0))
        self.put_program((1, 8, 99, 0))
        self.refused("went back from 1.9.0.0 to 1.8.99.0")
        self.put_program((1, 10, 0, 0))
        self.assertEqual(self.apply(), 0, self.errors.getvalue())
        self.assertIn("the app updated itself: 1.9.0.0 to 1.10.0.0", self.output.getvalue())

    def test_an_unchanged_folder_fills_in_what_an_earlier_marker_lacks(self):
        self.versioned()
        record = json.loads(self.marker.read_text())
        st.write_marker(self.prefix, {k: record[k] for k in ("profile_sha256", "install_dir", "install_tree")})
        self.events = []
        stop = self.running()
        self.assertEqual(self.apply(), 0, self.errors.getvalue())
        stop()
        filled = json.loads(self.marker.read_text())
        self.assertEqual(filled, record)
        self.assertEqual(self.events, [])
        self.put_program((1, 0, 0, 0), extra=b"patched")
        self.refused("version of its program stayed 1.0.0.0")
        self.put_program((1, 0, 0, 0))
        before = self.marker.read_bytes()
        self.assertEqual(self.apply(), 0)
        self.assertEqual(self.marker.read_bytes(), before)

    def test_a_folder_that_is_still_changing_is_not_recorded(self):
        self.versioned()
        self.put_program((2, 0, 0, 0))
        before, real, looks = self.marker.read_bytes(), st.snapshot, []
        def snapshot(path):
            tree = real(path)
            looks.append(path)
            (self.install / f"copying-{len(looks)}.dll").write_bytes(b"more")
            return tree
        with patch.object(st, "snapshot", side_effect=snapshot), self.assertRaises(SystemExit) as stop:
            self.apply()
        self.assertIn("still changing", str(stop.exception.code))
        self.assertEqual(self.marker.read_bytes(), before)

    def test_the_version_counts_only_from_the_bytes_the_snapshot_records(self):
        self.versioned()
        self.put_program((1, 0, 0, 0), extra=b"half")
        before = self.marker.read_bytes()
        with patch.object(st.peversion, "program_version", return_value=("2.0.0.0", "0" * 64)):
            self.refused("the version of its program cannot be read")
        self.assertEqual(self.marker.read_bytes(), before)

    def test_nothing_is_recorded_or_applied_while_the_app_runs(self):
        self.versioned()
        stop = self.running()
        with self.subTest("nothing to do"):
            self.assertEqual(self.apply(), 0, self.errors.getvalue())
        with self.subTest("a changed profile"):
            self.profile["registry"][0]["data"] = 0
            self.write_profile()
            self.events = []
            self.refused("close the app and run setup again")
            self.assertEqual(self.events, [])
        with self.subTest("an updated app"):
            self.put_program((2, 0, 0, 0))
            self.refused("close the app and run setup again")
        stop()
        self.assertEqual(self.reapply(), 0, self.errors.getvalue())
        self.assertEqual(json.loads(self.marker.read_text())["program_version"], "2.0.0.0")

    def test_a_file_that_vanishes_while_the_folder_is_read_is_named(self):
        self.versioned()
        with patch.object(st.treehash, "snapshot", side_effect=FileNotFoundError(2, "gone", "app.tmp")):
            self.refused("a file vanished while its install folder was read")
        with patch.object(st.treehash, "snapshot", side_effect=PermissionError(13, "denied", "app.exe")):
            self.refused("its install folder cannot be read")

    def test_an_app_that_starts_during_a_download_stops_the_change(self):
        self.versioned()
        self.second_prerequisite()
        data, stops = self.payloads["https://example.invalid/second.exe"], []
        def download(url, **kwargs):
            stops.append(self.running())
            return io.BytesIO(data)
        self.write_profile()
        self.events, before = [], self.marker.read_bytes()
        commands, real = [], self.process
        def process(cmd, **kwargs):
            commands.append(Path(cmd[0]).name)
            return real(cmd, **kwargs)
        with patch.object(st.urllib.request, "build_opener", return_value=SimpleNamespace(open=download)), \
                patch.object(st.px.subprocess, "run", side_effect=process), \
                self.assertRaisesRegex(SystemExit, "close the app and run setup again"):
            self.apply()
        stops[0]()
        self.assertEqual((commands, self.marker.read_bytes()), ([], before))

    def test_a_change_that_cannot_be_remembered_still_names_itself(self):
        self.versioned()
        self.tamper_program = (2, 0, 0, 0)
        self.profile["registry"][0]["data"] = 0
        real = st.write_marker
        def write(prefix, record):
            if "refused" in record:
                raise OSError(28, "No space left on device")
            real(prefix, record)
        with patch.object(st, "write_marker", side_effect=write), \
                self.assertRaisesRegex(SystemExit, "changed during a Windows step.*No space left"):
            self.reapply()

    def test_a_restored_folder_clears_the_note_without_a_profile_change(self):
        self.versioned()
        record = json.loads(self.marker.read_text())
        st.write_marker(self.prefix, {**record, "refused": {"program.dll": None}})
        self.assertEqual(self.apply(), 0, self.errors.getvalue())
        self.assertEqual(json.loads(self.marker.read_text()), record)

    def test_an_unreadable_runner_record_is_named(self):
        self.versioned()
        profile, runner = st.load_profile(self.profile_file.read_bytes()), self.prefix / st.px.MARKER
        runner.write_text("{")
        with self.assertRaisesRegex(st.Refused, "its runner record .runner.json cannot be read"):
            st.examine(self.prefix, profile, "", self.runner)

    def test_an_unreadable_record_names_the_one_before_it(self):
        self.versioned()
        for text in ("", "{", "[]", "{}", '{"profile_sha256": 1}', '{"profile_sha256": "", "install_dir": "x", "install_tree": {}}',
                     *(json.dumps({**json.loads(self.marker.read_text()), **odd}) for odd in
                       ({"program_version": "new"}, {"program_version": 2}, {"refused": []}, {"prerequisites": {}}))):
            with self.subTest(text=text[:40]):
                self.marker.write_text(text)
                self.refused(f"its record {st.MARKER} cannot be read; the one before it is in {st.PREVIOUS}")

    def test_the_marker_is_flushed_and_the_one_before_it_kept(self):
        self.versioned()
        before, synced = self.marker.read_bytes(), []
        real = os.fsync
        def fsync(fd):
            synced.append(os.readlink(f"/proc/self/fd/{fd}"))
            return real(fd)
        with patch.object(st.os, "fsync", side_effect=fsync):
            st.write_marker(self.prefix, {**json.loads(before), "example": 1})
        self.assertEqual((self.prefix / st.PREVIOUS).read_bytes(), before)
        self.assertEqual(json.loads(self.marker.read_text())["example"], 1)
        self.assertEqual(synced[-1], str(self.prefix))
        self.assertEqual(len([p for p in synced if Path(p).name.startswith(".setup-")]), 2)
        outside = self.root / "outside"
        outside.write_bytes(b"keep")
        (self.prefix / st.PREVIOUS).unlink()
        (self.prefix / st.PREVIOUS).symlink_to(outside)
        st.write_marker(self.prefix, json.loads(before))
        self.assertEqual(outside.read_bytes(), b"keep")

    def reapply(self, *extra):
        self.write_profile()
        self.events = []
        self.output.truncate(0)
        self.output.seek(0)
        return self.apply(*extra)

    def test_a_changed_profile_is_applied_in_place(self):
        self.assertEqual(self.apply(), 0, self.errors.getvalue())
        tree = st.snapshot(self.install)
        self.profile["registry"][0]["data"] = 0
        self.profile["registry"].append({"key": "HKCU\\Software\\Wine\\Fonts\\Replacements", "name": "Example UI",
                                         "type": "REG_SZ", "data": "Example"})
        self.profile["dll_overrides"]["graphics"] = "native"
        self.profile["windows_version"] = "win11"
        self.assertEqual(self.reapply(), 0, self.errors.getvalue())
        self.assertNotIn("installer", self.events)
        self.assertNotIn("prerequisite", self.events)
        self.assertNotIn("create", self.events)
        self.assertEqual(self.registry["HKCU\\Software\\Microsoft\\Avalon.Graphics", "DisableHWAcceleration"], "0")
        self.assertEqual(self.registry["HKCU\\Software\\Wine\\Fonts\\Replacements", "Example UI"], "Example")
        self.assertEqual(self.registry["HKCU\\Software\\Wine\\DllOverrides", "graphics"], "native")
        self.assertEqual(self.registry["version"], "win11")
        self.assertEqual(st.snapshot(self.install), tree)
        self.assertIn("profile updated in place", self.output.getvalue())
        self.assertEqual(self.reapply(), 0)
        self.assertEqual(self.events, [])
        self.assertIn("nothing changed", self.output.getvalue())

    def test_each_kind_of_prefix_setting_is_applied_on_its_own(self):
        self.assertEqual(self.apply(), 0)
        def version():
            self.profile["windows_version"] = "win11"
        def override():
            self.profile["dll_overrides"]["graphics"] = "native"
        def value():
            self.profile["registry"][0]["data"] = 0
        wanted = ((version, ("version",), "win11"), (override, ("HKCU\\Software\\Wine\\DllOverrides", "graphics"), "native"),
                  (value, ("HKCU\\Software\\Microsoft\\Avalon.Graphics", "DisableHWAcceleration"), "0"))
        for change, key, expected in wanted:
            with self.subTest(change=change.__name__):
                change()
                self.assertEqual(self.reapply(), 0, self.errors.getvalue())
                self.assertEqual(self.registry[key if len(key) == 2 else key[0]], expected)

    def test_a_prerequisite_changed_after_its_check_never_runs(self):
        bad = self.root / "runtime.exe"
        bad.write_bytes(b"swapped")
        with patch.object(st, "prerequisites", return_value=[bad]), self.assertRaisesRegex(SystemExit, "checksum mismatch"):
            self.apply()
        self.assertNotIn("prerequisite", self.events)
        self.assertFalse(bad.exists())

    def test_a_value_named_null_is_removed_in_place(self):
        self.assertEqual(self.apply(), 0)
        self.profile["registry"][0]["data"] = None
        self.assertEqual(self.reapply(), 0, self.errors.getvalue())
        self.assertNotIn(("HKCU\\Software\\Microsoft\\Avalon.Graphics", "DisableHWAcceleration"), self.registry)

    def test_a_launch_only_change_runs_no_windows_program(self):
        self.assertEqual(self.apply(), 0)
        before = json.loads(self.marker.read_text())
        self.profile["environment"] = {"EXAMPLE_SETTING": "1"}
        self.assertEqual(self.reapply(), 0, self.errors.getvalue())
        self.assertEqual(self.events, [])
        after = json.loads(self.marker.read_text())
        self.assertNotEqual(after["profile_sha256"], before["profile_sha256"])
        self.assertEqual(after["install_tree"], before["install_tree"])

    def test_only_new_prerequisites_run(self):
        self.assertEqual(self.apply(), 0)
        self.assertEqual(self.events.count("prerequisite"), 1)
        url = "https://example.invalid/second.exe"
        self.payloads[url] = b"second"
        self.profile["prerequisites"].append({"name": "second.exe", "url": url, "args": ["/quiet"],
                                              "sha256": hashlib.sha256(b"second").hexdigest()})
        self.assertEqual(self.reapply(), 0, self.errors.getvalue())
        self.assertEqual(self.events.count("prerequisite"), 1)
        self.assertNotIn("installer", self.events)
        self.assertEqual(self.reapply(), 0)
        self.assertEqual(self.events, [])
        self.payloads[url] = b"third"
        self.profile["prerequisites"][1]["sha256"] = hashlib.sha256(b"third").hexdigest()
        (self.cache / "second.exe").unlink(missing_ok=True)
        self.assertEqual(self.reapply(), 0, self.errors.getvalue())
        self.assertEqual(self.events.count("prerequisite"), 1)

    def test_a_moved_install_folder_is_refused(self):
        self.assertEqual(self.apply(), 0)
        before = self.marker.read_bytes()
        install = self.profile["install_dir"]
        self.profile["install_dir"] = "drive_c/Program Files/Elsewhere"
        self.write_profile()
        self.events = []
        self.refused("it is installed in drive_c/Program Files/Example")
        self.profile["install_dir"] = install
        self.assertEqual(self.marker.read_bytes(), before)
        self.assertEqual(self.events, [])

    def test_a_failed_profile_change_is_marked_and_repeated(self):
        self.assertEqual(self.apply(), 0)
        before, old_profile = json.loads(self.marker.read_text()), json.loads(json.dumps(self.profile))
        self.profile["registry"][0]["data"] = 0
        self.profile["dll_overrides"]["graphics"] = "native"
        self.fail_at = "registry"
        with self.assertRaises((SystemExit, RuntimeError)):
            self.reapply()
        self.fail_at = None
        after = json.loads(self.marker.read_text())
        self.assertEqual((after["profile_sha256"], after["shape"]), ("", None))
        self.assertEqual(after["install_tree"], before["install_tree"])
        self.assertEqual(self.registry["HKCU\\Software\\Wine\\DllOverrides", "graphics"], "native")
        with self.subTest("going back to the old profile sets its values again"):
            self.profile = old_profile
            self.assertEqual(self.reapply(), 0, self.errors.getvalue())
            self.assertEqual(self.registry["HKCU\\Software\\Wine\\DllOverrides", "graphics"], "builtin")
            self.assertEqual(self.reapply(), 0)
            self.assertEqual(self.events, [])

    def test_the_marker_vouches_for_nothing_while_windows_steps_run(self):
        self.assertEqual(self.apply(), 0)
        seen, real = [], self.process
        def process(cmd, **kwargs):
            if Path(cmd[0]).name == "wine":
                record = json.loads(self.marker.read_text())
                seen.append((record["profile_sha256"], record["shape"]))
            return real(cmd, **kwargs)
        self.profile["registry"][0]["data"] = 0
        with patch.object(st.px.subprocess, "run", side_effect=process):
            self.assertEqual(self.reapply(), 0, self.errors.getvalue())
        self.assertEqual(set(seen), {("", None)})
        self.assertEqual(json.loads(self.marker.read_text())["shape"], st.shape(st.load_profile(self.profile_file.read_bytes())))

    def test_a_change_a_step_caused_is_never_an_update(self):
        self.versioned()
        self.tamper_program = (2, 0, 0, 0)
        self.profile["registry"][0]["data"] = 0
        with self.assertRaisesRegex(SystemExit, "install folder changed during a Windows step"):
            self.reapply()
        self.tamper_program = None
        for _ in range(2):
            self.refused("as a Halation step left it")
        (self.install / "extra.log").write_bytes(b"the app wrote this")
        self.refused("as a Halation step left it")
        self.put_program((3, 0, 0, 0))
        self.assertEqual(self.reapply(), 0, self.errors.getvalue())
        self.assertIn("the app updated itself: 1.0.0.0 to 3.0.0.0", self.output.getvalue())
        self.assertNotIn("refused", json.loads(self.marker.read_text()))

    def test_an_accepted_update_is_one_write_without_the_note(self):
        self.versioned()
        record = json.loads(self.marker.read_text())
        st.write_marker(self.prefix, {**record, "refused": {"program.dll": None}})
        self.put_program((2, 0, 0, 0))
        with patch.object(st, "write_marker", wraps=st.write_marker) as write:
            self.assertEqual(self.apply(), 0, self.errors.getvalue())
        self.assertEqual(write.call_count, 1)
        self.assertNotIn("refused", json.loads(self.marker.read_text()))

    def test_a_restored_folder_is_accepted_after_a_step_changed_it(self):
        self.versioned()
        saved = (self.install / "bin/app.exe").read_bytes()
        self.tamper_program = (2, 0, 0, 0)
        self.profile["registry"][0]["data"] = 0
        with self.assertRaisesRegex(SystemExit, "install folder changed during a Windows step"):
            self.reapply()
        self.tamper_program = None
        (self.install / "bin/app.exe").write_bytes(saved)
        self.assertEqual(self.reapply(), 0, self.errors.getvalue())
        self.assertNotIn("refused", json.loads(self.marker.read_text()))

    def test_a_step_that_removes_a_file_is_remembered_too(self):
        self.versioned()
        (self.install / "program.dll").write_bytes(b"installed")
        real = self.process
        def process(cmd, **kwargs):
            result = real(cmd, **kwargs)
            if Path(cmd[0]).name == "wine":
                (self.install / "program.dll").unlink(missing_ok=True)
            return result
        self.profile["registry"][0]["data"] = 0
        with patch.object(st.px.subprocess, "run", side_effect=process), \
                self.assertRaisesRegex(SystemExit, "install folder changed during a Windows step"):
            self.reapply()
        self.put_program((2, 0, 0, 0))
        self.refused("program.dll")

    def test_a_marker_of_the_published_version_takes_a_changed_profile(self):
        self.assertEqual(self.apply(), 0)
        record = json.loads(self.marker.read_text())
        st.write_marker(self.prefix, {k: record[k] for k in ("profile_sha256", "install_dir", "install_tree")})
        self.profile["prerequisites"] = []
        self.profile["registry"][0]["data"] = 0
        self.assertEqual(self.reapply(), 0, self.errors.getvalue())
        self.assertEqual(self.registry["HKCU\\Software\\Microsoft\\Avalon.Graphics", "DisableHWAcceleration"], "0")

    def test_an_app_update_and_a_changed_profile_both_take_effect(self):
        self.versioned()
        self.put_program((2, 0, 0, 0))
        self.profile["registry"][0]["data"] = 0
        self.assertEqual(self.reapply(), 0, self.errors.getvalue())
        record = json.loads(self.marker.read_text())
        self.assertEqual(record["program_version"], "2.0.0.0")
        self.assertTrue(st.unchanged(self.install, record["install_tree"]))
        self.assertEqual(self.registry["HKCU\\Software\\Microsoft\\Avalon.Graphics", "DisableHWAcceleration"], "0")
        self.assertEqual(self.reapply(), 0)
        self.assertEqual(self.events, [])

    def test_a_changed_install_folder_still_refuses_a_changed_profile(self):
        self.assertEqual(self.apply(), 0)
        before = self.marker.read_bytes()
        (self.install / "program.dll").write_bytes(b"changed")
        self.profile["registry"][0]["data"] = 0
        self.write_profile()
        self.events = []
        self.refused("names no program")
        self.assertEqual(self.events, [])
        self.assertEqual(self.marker.read_bytes(), before)

    def test_a_dry_run_of_a_changed_profile_changes_nothing(self):
        self.assertEqual(self.apply(), 0)
        before = self.marker.read_bytes()
        self.profile["registry"][0]["data"] = 0
        self.assertEqual(self.reapply("--dry-run"), 0)
        self.assertEqual((self.events, self.marker.read_bytes()), ([], before))
        self.assertIn("registry HKCU", self.output.getvalue())
        self.assertNotIn("run local installer", self.output.getvalue())

    def test_a_dry_run_of_an_app_update_writes_nothing(self):
        self.versioned()
        self.put_program((2, 0, 0, 0))
        before = st.treehash.snapshot(self.root)
        for change in (None, "registry", "environment"):
            with self.subTest(change=change):
                if change == "registry":
                    self.profile["registry"][0]["data"] = 0
                elif change:
                    self.profile["environment"] = {"EXAMPLE_SETTING": "1"}
                self.write_profile()
                before = st.treehash.snapshot(self.root)
                self.assertEqual(self.reapply("--dry-run"), 0, self.errors.getvalue())
                self.assertEqual(st.treehash.compare(self.root, before), [])
                self.assertEqual(self.events, [])
                self.assertIn("the app updated itself: 1.0.0.0 to 2.0.0.0 (1 files changed); its folder would be recorded",
                              self.output.getvalue())

    def test_a_dry_run_says_what_it_would_do_for_a_launch_setting_and_an_old_marker(self):
        self.versioned()
        record = json.loads(self.marker.read_text())
        st.write_marker(self.prefix, {k: record[k] for k in ("profile_sha256", "install_dir", "install_tree")})
        before = self.marker.read_bytes()
        self.assertEqual(self.reapply("--dry-run"), 0)
        self.assertEqual(self.marker.read_bytes(), before)
        self.profile["environment"] = {"EXAMPLE_SETTING": "1"}
        st.write_marker(self.prefix, record)
        before = self.marker.read_bytes()
        self.assertEqual(self.reapply("--dry-run"), 0)
        self.assertEqual(self.marker.read_bytes(), before)
        self.assertIn("launch settings", self.output.getvalue())

    def second_prerequisite(self, name="second.exe", data=b"second"):
        url = f"https://example.invalid/{name}"
        self.payloads[url] = data
        self.profile["prerequisites"].append({"name": name, "url": url, "args": ["/quiet"], "sha256": hashlib.sha256(data).hexdigest()})

    def test_an_update_that_lands_during_a_download_is_not_mixed_into_the_record(self):
        self.versioned()
        self.second_prerequisite()
        real = self.payloads["https://example.invalid/second.exe"]
        def download(url, **kwargs):
            self.put_program((2, 0, 0, 0))
            return io.BytesIO(real)
        with patch.object(st.urllib.request, "build_opener", return_value=SimpleNamespace(open=download)):
            self.write_profile()
            self.events = []
            with self.assertRaisesRegex(SystemExit, "changed after it was checked"):
                self.apply()
        self.assertEqual(self.events, [])
        record = json.loads(self.marker.read_text())
        self.assertEqual(record["program_version"], "1.0.0.0")
        self.assertNotIn("refused", record)
        self.assertEqual(self.reapply(), 0, self.errors.getvalue())
        record = json.loads(self.marker.read_text())
        self.assertEqual(record["program_version"], "2.0.0.0")
        self.assertTrue(st.unchanged(self.install, record["install_tree"]))
        self.assertEqual(self.events.count("prerequisite"), 1)

    def test_a_launch_only_change_checks_the_folder_before_it_writes(self):
        self.versioned()
        self.profile["environment"] = {"EXAMPLE_SETTING": "1"}
        self.write_profile()
        before = self.marker.read_bytes()
        with patch.object(st.px, "in_use", side_effect=lambda prefix: self.put_program((2, 0, 0, 0))), \
                self.assertRaisesRegex(SystemExit, "changed after it was checked"):
            self.apply()
        self.assertEqual(self.marker.read_bytes(), before)
        self.assertEqual(self.reapply(), 0, self.errors.getvalue())
        self.assertEqual(json.loads(self.marker.read_text())["program_version"], "2.0.0.0")

    def test_a_prerequisite_that_ran_is_not_repeated_after_a_later_step_failed(self):
        self.assertEqual(self.apply(), 0)
        self.second_prerequisite()
        self.fail_at = "registry"
        with self.assertRaises((SystemExit, RuntimeError)):
            self.reapply()
        self.assertEqual(self.events.count("prerequisite"), 1)
        self.fail_at = None
        self.assertEqual(self.reapply(), 0, self.errors.getvalue())
        self.assertNotIn("prerequisite", self.events)

    def test_a_prerequisite_that_ran_once_stays_recorded(self):
        self.assertEqual(self.apply(), 0)
        first = list(self.profile["prerequisites"])
        self.second_prerequisite()
        both = list(self.profile["prerequisites"])
        for wanted in (both, first, both):
            self.profile["prerequisites"] = wanted
            self.assertEqual(self.reapply(), 0, self.errors.getvalue())
        self.assertNotIn("prerequisite", self.events)
        self.assertEqual([item["name"] for item in json.loads(self.marker.read_text())["prerequisites"]],
                         ["runtime.exe", "second.exe"])

    def test_a_known_version_is_never_replaced_by_none(self):
        self.versioned()
        (self.install / "plain.exe").write_bytes(b"no version")
        st.write_marker(self.prefix, {**json.loads(self.marker.read_text()), "install_tree": st.snapshot(self.install)})
        self.profile["program"] = "plain.exe"
        self.assertEqual(self.reapply(), 0, self.errors.getvalue())
        record = json.loads(self.marker.read_text())
        self.assertEqual((record["program"], record["program_version"]), ("bin/app.exe", "1.0.0.0"))
        (self.install / "extra.exe").write_bytes(pe((4, 0, 0, 0)))
        st.write_marker(self.prefix, {**record, "install_tree": st.snapshot(self.install)})
        self.profile["program"] = "extra.exe"
        self.assertEqual(self.reapply(), 0, self.errors.getvalue())
        record = json.loads(self.marker.read_text())
        self.assertEqual((record["program"], record["program_version"]), ("extra.exe", "4.0.0.0"))

    def test_a_profile_whose_program_is_not_there_is_refused(self):
        self.versioned()
        self.profile["program"] = "bin/renamed.exe"
        self.write_profile()
        self.refused("the program bin/renamed.exe is not in its install folder")

    def test_an_update_records_the_runner_that_made_it(self):
        self.assertEqual(self.apply(), 0)
        (self.runner / "version").write_text("300 newer\n")
        self.profile["registry"][0]["data"] = 0
        self.assertEqual(self.reapply(), 0, self.errors.getvalue())
        self.assertEqual(st.px.runner_history(self.prefix), {"stamp": 300, "runner": "newer"})

    def test_forward_only_rule_applies_before_downloads(self):
        self.prefix.mkdir()
        st.px.record_runner(self.prefix, 300, "newer")
        self.assert_pristine_refusal("forward only")

    def test_unlisted_redirects_are_refused_and_listed_ones_followed(self):
        handler = st.ProfileRedirect({self.url})
        request = st.urllib.request.Request(self.url)
        with self.assertRaisesRegex(SystemExit, "not listed"):
            handler.redirect_request(request, None, 302, "redirect", {}, "https://elsewhere.invalid/runtime.exe")
        followed = handler.redirect_request(request, None, 302, "redirect", {}, self.url)
        self.assertEqual(followed.full_url, self.url)
        self.assertEqual(self.events, [])

    def test_program_is_relative_and_cannot_escape_install_dir(self):
        for value in ("../outside.exe", "bin/../app.exe", "/app.exe", "", ".", "C:\\app.exe", "bin\\app.exe", 1):
            with self.subTest(value=value):
                self.profile["program"] = value
                with self.assertRaises(ValueError):
                    st.load_profile(json.dumps(self.profile))
        self.profile["program"] = "bin/app.exe"
        self.assertEqual(st.load_profile(json.dumps(self.profile))["program"], "bin/app.exe")

    def test_measured_profile_loads_with_only_the_approved_settings(self):
        profile = st.load_profile((REPO / "profiles/captureone/profile.json").read_bytes())
        self.assertEqual(profile, {
            "install_dir": "drive_c/Program Files/Capture One",
            "program": "Capture One/CaptureOne.exe",
            "installer_args": ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", "/LOG=C:\\halation-install.log"],
            "prerequisites": [], "windows_version": "win10", "dll_overrides": {"wintab32": "disabled"},
            "registry": [
                {"key": "HKCU\\Software\\Microsoft\\Avalon.Graphics", "name": "DisableHWAcceleration", "type": "REG_DWORD", "data": 1},
                {"key": "HKCU\\Software\\Wine\\AppDefaults\\msedgewebview2.exe", "name": "Version", "type": "REG_SZ", "data": "win8"},
            ],
            "environment": {"WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": "--disable-gpu"},
        })

    def test_environment_validation_refuses_invalid_names_and_values(self):
        cases = [[], {"lower": "value"}, {"1NAME": "value"}, {"BAD-NAME": "value"},
                 {"VALID": 1}, {"VALID": None}, {"VALID": "bad\0value"}]
        cases += [{name: "value"} for name in ("WINEPREFIX", "PROTONPATH", "UMU_TEST", "DXVK_TEST")]
        for environment in cases:
            with self.subTest(environment=environment), self.assertRaises(ValueError):
                st.load_profile(json.dumps({**self.profile, "environment": environment}))
        self.assertEqual(st.validate_environment({"VALID_2": ""}), {"VALID_2": ""})

    def save_runtime_registry(self):
        sections = {r"Software\Wine": {"Version": self.registry["version"]}}
        for pair, value in self.registry.items():
            if isinstance(pair, tuple):
                key, name = pair
                sections.setdefault(key.split("\\", 1)[1], {})[name] = value
        lines = ["WINE REGISTRY Version 2"]
        for key, values in sections.items():
            lines.append("\n[" + key.replace("\\", "\\\\") + "]")
            lines.extend(json.dumps(name) + "=" + json.dumps(str(value)) for name, value in values.items())
        (self.prefix / "user.reg").write_text("\n".join(lines) + "\n")

    def prepare_runtime(self, recorded=True):
        self.profile = st.load_profile((REPO / "profiles/captureone/profile.json").read_bytes())
        self.write_profile()
        self.apply()
        if not recorded:
            (self.prefix / st.MARKER).unlink()
        library = self.prefix / "drive_c/windows/Microsoft.NET/Framework64/v4.0.30319/mscorlib.dll"
        library.parent.mkdir(parents=True)
        library.write_bytes(b"framework fixture")
        self.registry["version"] = "win8"
        for dll in ("mscoree", "uiautomationcore", "dxva2", "evr"):
            self.registry["HKCU\\Software\\Wine\\DllOverrides", dll] = "native"
        self.save_runtime_registry()
        self.events = []
        return library

    def runtime_process(self, cmd, **kwargs):
        result = self.process(cmd, **kwargs)
        if Path(cmd[0]).name == "wine":
            self.save_runtime_registry()
        return result

    def finish(self):
        with patch.object(st.px.subprocess, "run", side_effect=self.runtime_process):
            return st.main(["finish-netfx48", "--runner", str(self.runner), "--prefix", str(self.prefix)])

    def test_finish_refuses_missing_framework_or_non_native_override(self):
        library = self.prepare_runtime()
        before = st.treehash.snapshot(self.root)
        with self.assertRaisesRegex(SystemExit, "accepts only"):
            st.main(["finish-netfx48", "--runner", str(self.runner), "--prefix", str(self.prefix), "--dry-run"])
        self.assertEqual(st.treehash.compare(self.root, before), [])
        for missing, override in ((True, "native"), (False, "builtin"), (False, None)):
            with self.subTest(missing=missing, override=override):
                if missing:
                    library.unlink()
                else:
                    library.write_bytes(b"framework fixture")
                key = ("HKCU\\Software\\Wine\\DllOverrides", "mscoree")
                if override is None:
                    self.registry.pop(key)
                else:
                    self.registry[key] = override
                self.save_runtime_registry()
                before = st.treehash.snapshot(self.root)
                with self.assertRaisesRegex(SystemExit, "requires.*mscorlib.dll.*native mscoree"):
                    self.finish()
                self.assertEqual(st.treehash.compare(self.root, before), [])
                self.assertEqual(self.events, [])

    def test_finish_applies_only_the_measured_repairs_and_then_is_a_noop(self):
        for recorded in (True, False):
            with self.subTest(recorded=recorded):
                self.prefix = self.root / ("recorded" if recorded else "manual")
                self.prepare_runtime(recorded)
                install_before = st.snapshot(self.install)
                key = "HKCU\\Software\\Wine\\DllOverrides"
                expected = {**self.registry, "version": "win10"}
                expected.update({(key, dll): "builtin" for dll in ("uiautomationcore", "dxva2", "evr")})
                self.assertEqual(self.finish(), 0)
                self.assertEqual(self.registry, expected)
                self.assertTrue(st.unchanged(self.install, install_before))
                before = st.treehash.snapshot(self.root)
                self.events = []
                self.assertEqual(self.finish(), 0)
                self.assertEqual(st.treehash.compare(self.root, before), [])
                self.assertEqual(self.events, [])
                self.assertIn("already applied; nothing changed", self.output.getvalue())

    def test_finish_has_one_guard_and_one_wait_for_all_repairs(self):
        self.prepare_runtime()
        waits = []
        def process(cmd, **kwargs):
            if Path(cmd[0]).name == "wineserver":
                waits.append(cmd)
            return self.runtime_process(cmd, **kwargs)
        with patch.object(st, "guarded", wraps=st.guarded) as guard, \
                patch.object(st.px.subprocess, "run", side_effect=process):
            args = SimpleNamespace(runner=str(self.runner), prefix=str(self.prefix))
            self.assertEqual(st.finish_netfx48(args), 0)
        self.assertEqual(guard.call_count, 1)
        self.assertEqual(len(waits), 1)

    def test_every_windows_step_and_wait_is_bounded_and_timeouts_refuse_completion(self):
        for target in ("prerequisite", "winecfg", "reg", "installer", "wineserver"):
            self.prefix = self.root / target
            def process(cmd, **kwargs):
                if Path(cmd[0]).name in ("wine", "wineserver"):
                    self.assertGreater(kwargs.get("timeout", 0), 0)
                    name = Path(cmd[0]).name
                    matches = target == "wineserver" and name == "wineserver"
                    if name == "wine":
                        matches = matches or cmd[1] == target
                        if target == "installer":
                            matches = cmd[1] == str(self.installer)
                            if matches: self.assertGreaterEqual(kwargs["timeout"], 1800)
                        if target == "prerequisite":
                            matches = cmd[1].endswith("runtime.exe")
                    if matches:
                        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
                return self.process(cmd, **kwargs)
            with self.subTest(target=target), patch.object(st.px.subprocess, "run", side_effect=process):
                with self.assertRaisesRegex(SystemExit, "timed out"):
                    self.apply()
            self.assertFalse((self.prefix / st.MARKER).exists())

    def test_delete_missing_registry_value_succeeds_but_other_errors_fail(self):
        for missing in (True, False):
            self.prefix = self.root / ("absent" if missing else "denied")
            def process(cmd, **kwargs):
                if len(cmd) > 2 and cmd[1:3] == ["reg", "delete"]:
                    message = "reg: Unable to find the specified registry value" if missing else "reg: Access denied"
                    message = "00ac:fixme:diagnostic\n" + message
                    output = kwargs.get("stdout")
                    if hasattr(output, "write"):
                        output.write(message)
                    return subprocess.CompletedProcess(cmd, 1, stdout=message)
                return self.process(cmd, **kwargs)
            with self.subTest(missing=missing), patch.object(st.px.subprocess, "run", side_effect=process):
                if missing:
                    self.assertEqual(self.apply(), 0)
                else:
                    with self.assertRaises(SystemExit):
                        self.apply()
            self.assertEqual((self.prefix / st.MARKER).exists(), missing)

    def test_finish_refuses_a_change_to_the_installed_tree(self):
        self.prepare_runtime()
        self.tamper = "override"
        with self.assertRaisesRegex(SystemExit, "install folder changed"):
            self.finish()
        self.assertEqual(self.events, ["version", "override", "override", "override"])
        self.assertIn("program.dll", json.loads(self.marker.read_text())["refused"])

    def test_finish_leaves_a_changed_folder_to_setup(self):
        self.prepare_runtime()
        (self.install / "program.dll").write_bytes(b"changed since")
        before = self.marker.read_bytes()
        with self.assertRaisesRegex(SystemExit, "its install folder changed since setup recorded it; run ./setup first"):
            self.finish()
        self.assertEqual((self.events, self.marker.read_bytes()), ([], before))

    def test_finish_works_in_a_prefix_without_a_record(self):
        self.prepare_runtime(recorded=False)
        self.assertEqual(self.finish(), 0, self.errors.getvalue())
        self.assertFalse(self.marker.exists())


if __name__ == "__main__":
    unittest.main()

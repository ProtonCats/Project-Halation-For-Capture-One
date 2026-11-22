# SPDX-FileCopyrightText: 2026 ProtonCats and contributors
# SPDX-License-Identifier: MIT
import contextlib
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import setup as st

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
        self.route, self.events = "1.1.1.1 dev wg0", []
        self.registry = {("HKCU\\Software\\Wine\\Direct3D", "OldValue"): "old"}
        self.types = {}
        self.tamper, self.fail_step = None, False
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
        if cmd[0] == "ip":
            return subprocess.CompletedProcess(cmd, 0, stdout=self.route)
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
                    self.registry["installer_path"] = str(path)
                    self.registry["at_install"] = list(self.events)
        self.events.append(stage)
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

    def test_missing_tunnel_refuses_before_prefix_creation(self):
        self.route = "1.1.1.1 dev eth0"
        self.assert_pristine_refusal("require.*tunnel")

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
        with self.assertRaisesRegex(SystemExit, "recorded install folder changed"):
            self.apply()
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

    def test_forward_only_rule_applies_before_downloads(self):
        self.prefix.mkdir()
        st.px.record_runner(self.prefix, 300, "newer")
        self.assert_pristine_refusal("forward only")

    def test_unlisted_redirects_and_tunnel_loss_are_refused(self):
        handler = st.ProfileRedirect({self.url})
        request = st.urllib.request.Request(self.url)
        with self.assertRaisesRegex(SystemExit, "not listed"):
            handler.redirect_request(request, None, 302, "redirect", {}, "https://elsewhere.invalid/runtime.exe")
        self.route = "1.1.1.1 dev eth0"
        with self.assertRaisesRegex(SystemExit, "tunnel"):
            handler.redirect_request(request, None, 302, "redirect", {}, self.url)
        self.assertEqual(self.events, [])
        self.route = "1.1.1.1 dev wg0"
        def lose_tunnel(url, **kwargs):
            self.route = "1.1.1.1 dev eth0"
            return io.BytesIO(b"runtime")
        with patch.object(st.urllib.request, "build_opener", return_value=SimpleNamespace(open=lose_tunnel)):
            self.assert_pristine_refusal("tunnel")


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


if __name__ == "__main__":
    unittest.main()

# SPDX-FileCopyrightText: 2026 ProtonCats and contributors
# SPDX-License-Identifier: MIT
import importlib.util
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
from core import setup as profiles

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("core.display_profile", REPO / "core/display_profile.py")
dp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dp)

SYSTEM_REG = r'''[System\\ControlSet001\\Control\\Class\\{4D36E96E-E325-11CE-BFC1-08002BE10318}] 1794405697
#time=1

[System\\ControlSet001\\Control\\Class\\{4D36E96E-E325-11CE-BFC1-08002BE10318}\\0000] 1794405697
#time=1
"DriverDesc"="Generic Non-PnP Monitor"
"ICMProfile"=str(7):"sRGB Color Space Profile.icm\0Old.icc\0"

[System\\ControlSet001\\Control\\Class\\{4d36e96e-e325-11ce-bfc1-08002be10318}\\0001] 1794405697
"DriverDesc"="Generic Non-PnP Monitor"

[System\\ControlSet001\\Control\\Class\\{4D36E968-E325-11CE-BFC1-08002BE10318}\\0000] 1
'''

COLORMGR = """Object Path:   /org/freedesktop/ColorManager/devices/xrandr_dell
Type:          display
Device ID:     xrandr-Dell
Profile 1:     icc-1234
               /var/lib/colord/icc/edid-1234.icc
Profile 2:     icc-5678
               /usr/share/color/icc/other.icc

Object Path:   /org/freedesktop/ColorManager/devices/xrandr_second
Type:          display
Profile 1:     icc-9999
               /var/lib/colord/icc/second.icc

Object Path:   /org/freedesktop/ColorManager/devices/xrandr_spaced
Type:          display
Profile 1:     icc-4242
               /var/lib/colord/icc/My Display.icc
"""


class Parsers(unittest.TestCase):
    def test_icc_header(self):
        data = bytearray(128)
        data[12:16], data[36:40] = b"mntr", b"acsp"
        self.assertEqual(dp.is_icc(bytes(data)), "mntr")
        self.assertIsNone(dp.is_icc(bytes(128)))
        self.assertIsNone(dp.is_icc(b"short"))

    def test_colord_first_display_default(self):
        self.assertEqual(dp.colord_profile(COLORMGR), "/var/lib/colord/icc/edid-1234.icc")
        self.assertIsNone(dp.colord_profile("Object Path: /x\nType: display\n"))
        spaced = COLORMGR.split("\n\n")[2]
        self.assertEqual(dp.colord_profile(spaced), "/var/lib/colord/icc/My Display.icc")

    def test_xprop_bytes(self):
        self.assertEqual(dp.xprop_profile("_ICC_PROFILE = 0, 0, 12, 255"), bytes([0, 0, 12, 255]))
        self.assertIsNone(dp.xprop_profile("_ICC_PROFILE:  no such atom on any window."))

    def test_display_indices_only_the_display_class(self):
        self.assertEqual(dp.display_indices(SYSTEM_REG), ["0000", "0001"])

    def test_saved_multi_string(self):
        key = r"System\ControlSet001\Control\Class\{4D36E96E-E325-11CE-BFC1-08002BE10318}\0000"
        self.assertEqual(dp.saved_list(SYSTEM_REG, key), ["sRGB Color Space Profile.icm", "Old.icc"])
        self.assertEqual(dp.saved_list(SYSTEM_REG, key.replace("0000", "0001")), [])
        self.assertEqual(dp.saved_list(SYSTEM_REG, key.replace("0000", "0009")), [])

    def test_keys_keep_the_class_braces_and_map_to_saved_headers(self):
        key, saved = dp.keys_for("system", "0000")
        self.assertEqual(key, r"HKLM\System\CurrentControlSet\Control\Class\{4D36E96E-E325-11CE-BFC1-08002BE10318}\0000")
        self.assertEqual(dp.saved_list(SYSTEM_REG, saved), ["sRGB Color Space Profile.icm", "Old.icc"])
        key, saved = dp.keys_for("user", "0001")
        self.assertTrue(key.startswith(r"HKCU\Software\Microsoft\Windows NT\CurrentVersion\ICM\ProfileAssociations\Display\{"))
        self.assertTrue(saved.startswith("Software\\Microsoft") and saved.endswith("}\\0001"))

    def test_new_profile_becomes_the_last_entry_once(self):
        self.assertEqual(dp.with_default(["a.icc", "B.icc", "c.icc"], "b.icc"), ["a.icc", "c.icc", "b.icc"])
        self.assertEqual(dp.with_default([], "x.icc"), ["x.icc"])


class ProfileOperations(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=REPO)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runner = self.root / "runner"
        binary = self.runner / "files/bin/wine"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"fixture")
        (self.runner / "version").write_text("200 current\n")
        self.prefix = self.root / "prefix"
        self.prefix.mkdir()
        self.install = self.prefix / "install"
        self.install.mkdir()
        (self.install / "program.dll").write_bytes(b"installed")
        (self.prefix / profiles.MARKER).write_text(json.dumps({"install_dir": "install", "install_tree": profiles.snapshot(self.install)}))
        (self.prefix / "system.reg").write_text(SYSTEM_REG)
        _, saved = dp.keys_for("user", "0000")
        (self.prefix / "user.reg").write_text('[' + saved.replace('\\', '\\\\') + ']\n'
                                               '"ICMProfile"=str(7):"Old.icc\\0"\n'
                                               '"UsePerUserProfiles"=dword:00000001\n')
        data = bytearray(128)
        data[12:16], data[36:40] = b"mntr", b"acsp"
        self.data = bytes(data)
        self.profile = self.root / "Display Profile.icc"
        self.profile.write_bytes(self.data)
        self.args = SimpleNamespace(runner=str(self.runner), prefix=str(self.prefix), profile=str(self.profile),
                                    scope="user", dry_run=False)
        self.command = self.enterContext(patch.object(dp.subprocess, "run"))
        self.command.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))

    def test_host_profile_explicit_and_colord_paths_with_spaces(self):
        self.assertEqual(dp.host_profile(str(self.profile)), (self.profile.name, self.data, "--profile"))
        self.command.assert_not_called()
        self.command.return_value.stdout = f"Profile 1: icc-123\n    {self.profile}\n"
        with patch.object(dp.shutil, "which", return_value="available"):
            self.assertEqual(dp.host_profile(None), (self.profile.name, self.data, "colord"))
        self.assertEqual(self.command.call_args.args[0], ["colormgr", "get-devices-by-kind", "display"])

    def test_host_fallback_and_absent_profile(self):
        with patch.object(dp.shutil, "which", side_effect=lambda name: name == "xprop"), \
                patch.dict(dp.os.environ, {"DISPLAY": ":99"}):
            self.command.return_value.stdout = "_ICC_PROFILE = 1, 2, 255"
            self.assertEqual(dp.host_profile(None), ("x11-root-display.icc", bytes([1, 2, 255]), "X11 _ICC_PROFILE"))
            self.command.return_value.returncode = 1
            self.assertIsNone(dp.host_profile(None))
        with self.assertRaises(SystemExit) as exc:
            dp.host_profile(str(self.root / "missing.icc"))
        self.assertEqual(exc.exception.code, 2)

    def test_link_preserves_lists_copies_spaces_and_sets_each_display_flag(self):
        before = {p.name: p.read_bytes() for p in self.prefix.iterdir() if p.is_file()}
        with patch.dict(dp.os.environ, {"DISPLAY": ":99", "WAYLAND_DISPLAY": "socket"}):
            self.assertEqual(dp.link(self.args), 0)
        self.assertEqual((self.prefix / dp.COLOUR_DIR / self.profile.name).read_bytes(), self.data)
        for name, data in before.items():
            self.assertEqual((self.prefix / name).read_bytes(), data)
        calls = [call for call in self.command.call_args_list if Path(call.args[0][0]).name == "wine"]
        self.assertEqual(len(calls), 4)
        for index, expected in zip(("0000", "0001"), ("Old.icc\\0Display Profile.icc", "Display Profile.icc")):
            call = calls[int(index)]
            self.assertEqual(call.args[0][1:], ["reg", "add", dp.keys_for("user", index)[0], "/v", "ICMProfile",
                                              "/t", "REG_MULTI_SZ", "/d", expected, "/f"])
            self.assertEqual(call.kwargs["env"]["WINEPREFIX"], str(self.prefix))
            self.assertNotIn("DISPLAY", call.kwargs["env"])
            self.assertNotIn("WAYLAND_DISPLAY", call.kwargs["env"])
            self.assertEqual(call.kwargs["timeout"], 120)
        for call, index in zip(calls[2:4], ("0000", "0001")):
            self.assertEqual(call.args[0][3:], [dp.keys_for("user", index)[0], "/v", "UsePerUserProfiles",
                                              "/t", "REG_DWORD", "/d", "1", "/f"])
        self.assertEqual(self.command.call_args_list[-1].args[0], [str(self.runner / "files/bin/wineserver"), "-w"])

    def test_hash_destination_with_wrong_bytes_is_refused(self):
        dest = self.prefix / dp.COLOUR_DIR / self.profile.name
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"original")
        fallback = dest.with_name(dest.stem + "-" + hashlib.sha256(self.data).hexdigest()[:8] + dest.suffix)
        fallback.write_bytes(b"unrelated")
        with self.assertRaises(SystemExit):
            dp.link(self.args)
        self.assertEqual(dest.read_bytes(), b"original")
        self.assertEqual(fallback.read_bytes(), b"unrelated")
        self.command.assert_not_called()
        fallback.write_bytes(self.data)
        self.assertEqual(dp.link(self.args), 0)

    def test_existing_profile_with_different_bytes_is_retained(self):
        dest = self.prefix / dp.COLOUR_DIR / self.profile.name
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"previous profile")
        self.assertEqual(dp.link(self.args), 0)
        self.assertEqual(dest.read_bytes(), b"previous profile")
        renamed = f"Display Profile-{hashlib.sha256(self.data).hexdigest()[:8]}.icc"
        self.assertEqual((dest.parent / renamed).read_bytes(), self.data)
        self.assertIn(renamed, self.command.call_args_list[0].args[0][-2])

    def test_dry_run_and_no_host_profile_change_nothing(self):
        self.args.dry_run = True
        self.assertEqual(dp.link(self.args), 0)
        self.assertFalse((self.prefix / dp.COLOUR_DIR).exists())
        self.command.assert_not_called()
        self.args.dry_run = False
        with patch.object(dp, "host_profile", return_value=None):
            self.assertEqual(dp.link(self.args), 0)
        self.assertFalse((self.prefix / dp.COLOUR_DIR).exists())
        self.command.assert_not_called()

    def test_invalid_profile_or_missing_display_prevents_writes(self):
        self.profile.write_bytes(b"invalid")
        with self.assertRaises(SystemExit):
            dp.link(self.args)
        self.profile.write_bytes(self.data)
        (self.prefix / "system.reg").write_text("WINE REGISTRY Version 2\n")
        with self.assertRaises(SystemExit):
            dp.link(self.args)
        self.command.assert_not_called()
        self.assertFalse((self.prefix / dp.COLOUR_DIR).exists())

    def test_link_refuses_an_older_runner_before_writes_or_windows_commands(self):
        profiles.px.record_runner(self.prefix, 300, "newer")
        before = profiles.snapshot(self.prefix)
        self.command.side_effect = AssertionError("Windows code ran with an older runner")
        with self.assertRaises(SystemExit) as error:
            dp.link(self.args)
        self.assertTrue(error.exception.code)
        self.assertTrue(profiles.unchanged(self.prefix, before))

    def test_link_rejects_install_changes_from_commands_and_cleanup(self):
        for stage in ("wine", "wineserver"):
            with self.subTest(stage=stage):
                (self.install / "program.dll").write_bytes(b"installed")
                def execute(cmd, **kwargs):
                    if Path(cmd[0]).name == stage:
                        (self.install / "program.dll").write_bytes(b"changed")
                    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
                self.command.side_effect = execute
                with self.assertRaisesRegex(SystemExit, "install folder changed"):
                    dp.link(self.args)
                self.assertEqual((self.install / "program.dll").read_bytes(), b"changed")

    def test_manual_prefix_uses_shared_bundled_install_path_fallback(self):
        (self.prefix / profiles.MARKER).unlink()
        bundled = profiles.load_profile((REPO / "profiles/captureone/profile.json").read_bytes())["install_dir"]
        manual = self.prefix / bundled
        manual.mkdir(parents=True)
        (manual / "program.dll").write_bytes(b"installed")
        with patch.object(profiles, "guarded", wraps=profiles.guarded) as guard:
            self.assertEqual(dp.link(self.args), 0)
        self.assertEqual(guard.call_args.args[1], bundled)
        self.assertFalse((self.prefix / profiles.MARKER).exists())

    def test_link_guards_and_waits_once_for_the_whole_batch(self):
        self.args.scope = "both"
        with patch.object(profiles, "guarded", wraps=profiles.guarded) as guard:
            self.assertEqual(dp.link(self.args), 0)
        self.assertEqual(guard.call_count, 1)
        waits = [c for c in self.command.call_args_list if Path(c.args[0][0]).name == "wineserver"]
        self.assertEqual(len(waits), 1)

    def test_busy_prefix_waits_once_and_reports_one_warning(self):
        self.args.scope = "both"
        def execute(cmd, **kwargs):
            if Path(cmd[0]).name == "wineserver":
                raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        self.command.side_effect = execute
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(dp.link(self.args), 0)
        self.assertEqual(errors.getvalue().count("prefix is still in use"), 1)
        self.assertEqual(len([c for c in self.command.call_args_list if Path(c.args[0][0]).name == "wine"]), 6)

    def test_registry_failure_stops_further_associations(self):
        self.command.return_value = subprocess.CompletedProcess([], 5, stdout="denied", stderr="")
        with self.assertRaises(SystemExit) as exc:
            dp.link(self.args)
        self.assertEqual(exc.exception.code, 1)
        self.assertEqual(len([call for call in self.command.call_args_list if Path(call.args[0][0]).name == "wine"]), 1)

    def test_scopes_control_registry_targets_and_flag_writes(self):
        for scope, count in (("system", 2), ("both", 6)):
            with self.subTest(scope=scope):
                self.args.scope = scope
                self.command.reset_mock()
                self.assertEqual(dp.link(self.args), 0)
                calls = [call for call in self.command.call_args_list if Path(call.args[0][0]).name == "wine"]
                self.assertEqual(len(calls), count)
                self.assertEqual(calls[0].args[0][3], dp.keys_for("system", "0000")[0])
                self.assertIn("Old.icc", calls[0].args[0][-2])
                flags = [c for c in calls if "UsePerUserProfiles" in c.args[0]]
                self.assertEqual(len(flags), 0 if scope == "system" else 2)

    def test_show_and_main_do_not_write_or_launch(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(dp.main(["show", "--prefix", str(self.prefix)]), 0)
        self.assertIn("display 0000 lookups use: user scope", out.getvalue())
        self.assertIn("display 0001 lookups use: system scope", out.getvalue())
        self.command.assert_not_called()
        with self.assertRaises(SystemExit):
            dp.main(["link", "--prefix", str(self.prefix)])


if __name__ == "__main__":
    unittest.main()

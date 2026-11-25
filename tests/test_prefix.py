# SPDX-FileCopyrightText: 2026 ProtonCats and contributors
# SPDX-License-Identifier: MIT
import importlib.util
import contextlib
import io
import fcntl
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from core import treehash
from tests.test_peversion import pe

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("prefix", REPO / "core/prefix.py")
px = importlib.util.module_from_spec(spec)
spec.loader.exec_module(px)


class Prefix(unittest.TestCase):
    def test_runner_version(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "version").write_text("1793687165 GE-Proton11-12\n")
            self.assertEqual(px.runner_version(d), (1793687165, "GE-Proton11-12"))
            (Path(d) / "version").write_text("garbage\n")
            with self.assertRaises(SystemExit):
                px.runner_version(d)

    def test_invalid_runner_history_is_refused_and_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            marker = Path(d) / px.MARKER
            for raw in ('{}', '[]', 'null', '{', '{"stamp": -1, "runner": "x"}',
                        '{"stamp": true, "runner": "x"}', '{"stamp": 1.5, "runner": "x"}',
                        '{"stamp": "1", "runner": "x"}', '{"stamp": 1}',
                        '{"stamp": 1, "runner": 2}'):
                marker.write_text(raw)
                for action in (lambda: px.forward_ok(d, 1), lambda: px.record_runner(d, 1, "x")):
                    with self.subTest(raw=raw), self.assertRaises(SystemExit):
                        action()
                    self.assertEqual(marker.read_text(), raw)
            marker.write_bytes(b"\xff")
            with self.assertRaises(SystemExit):
                px.forward_ok(d, 1)
            with patch.object(Path, "read_text", side_effect=PermissionError("unreadable")), \
                    self.assertRaises(SystemExit):
                px.forward_ok(d, 1)
            marker.unlink()
            marker.mkdir()
            with self.assertRaises(SystemExit):
                px.forward_ok(d, 1)

    def test_forward_only(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(px.forward_ok(d, 100))
            px.record_runner(d, 200, "newer")
            self.assertTrue(px.forward_ok(d, 200))
            self.assertTrue(px.forward_ok(d, 300))
            self.assertFalse(px.forward_ok(d, 100))
            px.record_runner(d, 100, "older")
            self.assertFalse(px.forward_ok(d, 100))
            self.assertTrue(px.forward_ok(d, 200))

    def test_old_temporary_symlink_cannot_overwrite_a_file_outside_the_prefix(self):
        with tempfile.TemporaryDirectory(dir=REPO) as directory:
            root = Path(directory)
            prefix = root / "prefix"
            prefix.mkdir()
            victim = root / "outside"
            victim.write_bytes(b"unchanged")
            old_temp = prefix / (px.MARKER + ".tmp")
            old_temp.symlink_to(victim)
            px.record_runner(prefix, 200, "newer")
            marker = prefix / px.MARKER
            self.assertEqual(victim.read_bytes(), b"unchanged")
            self.assertTrue(old_temp.is_symlink())
            self.assertTrue(marker.is_file())
            self.assertFalse(marker.is_symlink())
            self.assertEqual(json.loads(marker.read_text()), {"stamp": 200, "runner": "newer"})
            px.record_runner(prefix, 100, "older")
            self.assertEqual(json.loads(marker.read_text()), {"stamp": 200, "runner": "newer"})
            self.assertEqual(set(prefix.iterdir()), {marker, old_temp, prefix / (px.MARKER + ".lock")})

    def test_symlinked_runner_marker_is_refused_without_touching_its_target(self):
        with tempfile.TemporaryDirectory(dir=REPO) as directory:
            root = Path(directory)
            prefix = root / "prefix"
            prefix.mkdir()
            victim = root / "outside"
            contents = b'{"stamp": 100, "runner": "existing"}\n'
            victim.write_bytes(contents)
            marker = prefix / px.MARKER
            marker.symlink_to(victim)
            for operation in (lambda: px.forward_ok(prefix, 200), lambda: px.record_runner(prefix, 200, "newer")):
                with self.subTest(operation=operation), contextlib.redirect_stderr(io.StringIO()) as errors:
                    with self.assertRaises(SystemExit) as error:
                        operation()
                    self.assertEqual(error.exception.code, 2)
                    self.assertIn("symlink", errors.getvalue())
                self.assertTrue(marker.is_symlink())
                self.assertEqual(victim.read_bytes(), contents)
                self.assertEqual(list(prefix.iterdir()), [marker])

    def test_marker_publication_failure_preserves_history_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory(dir=REPO) as directory:
            prefix = Path(directory)
            px.record_runner(prefix, 200, "existing")
            marker = prefix / px.MARKER
            before = marker.read_bytes()
            with patch.object(px.os, "replace", side_effect=OSError("publication failed")):
                with self.assertRaisesRegex(OSError, "publication failed"):
                    px.record_runner(prefix, 300, "newer")
            self.assertEqual(marker.read_bytes(), before)
            self.assertEqual(set(prefix.iterdir()), {marker, prefix / (px.MARKER + ".lock")})

    def test_concurrent_records_preserve_the_newest_stamp(self):
        with tempfile.TemporaryDirectory(dir=REPO) as directory:
            prefix = Path(directory)
            px.record_runner(prefix, 100, "initial")
            paused, release = threading.Event(), threading.Event()
            errors = []
            original = Path.read_text
            def read(path, *args, **kwargs):
                contents = original(path, *args, **kwargs)
                if threading.current_thread().name == "older-record" and path == prefix / px.MARKER:
                    paused.set()
                    if not release.wait(5):
                        raise RuntimeError("writer coordination timed out")
                return contents
            def older():
                try:
                    px.record_runner(prefix, 200, "older")
                except BaseException as error:
                    errors.append(error)
            with patch.object(Path, "read_text", read):
                worker = threading.Thread(target=older, name="older-record")
                worker.start()
                try:
                    self.assertTrue(paused.wait(5))
                    lock_path = prefix / (px.MARKER + ".lock")
                    held = False
                    if lock_path.exists():
                        with lock_path.open("r+") as probe:
                            try:
                                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            except BlockingIOError:
                                held = True
                    if held:
                        release.set()
                        worker.join(5)
                        px.record_runner(prefix, 300, "newest")
                    else:
                        px.record_runner(prefix, 300, "newest")
                        release.set()
                finally:
                    release.set()
                    worker.join(5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
            self.assertEqual(json.loads((prefix / px.MARKER).read_text()), {"stamp": 300, "runner": "newest"})
            self.assertFalse(px.forward_ok(prefix, 250))

    def test_ready_needs_the_program_files_entry(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(px.ready(d))
            (Path(d) / "system.reg").write_text("WINE REGISTRY Version 2\n")
            self.assertFalse(px.ready(d))
            (Path(d) / "system.reg").write_text('"ProgramFilesDir"="C:\\\\Program Files"\n')
            self.assertTrue(px.ready(d))

    def test_environment_drops_inherited_wine_settings(self):
        env = px.environment(Path("/r"), Path("/p"), base={"WINEDEBUG": "+all", "PROTON_USE_WINED3D": "1",
                                                          "DXVK_HUD": "1", "HOME": "/h"})
        self.assertEqual(env["HOME"], "/h")
        self.assertNotIn("WINEDEBUG", env)
        self.assertNotIn("DXVK_HUD", env)
        self.assertNotIn("PROTON_USE_WINED3D", env)
        self.assertEqual((env["WINEPREFIX"], env["PROTONPATH"], env["UMU_RUNTIME_UPDATE"]), ("/p", "/r", "0"))
        self.assertEqual(px.environment(Path("/r"), Path("/p"), True, base={})["PROTON_USE_WINED3D"], "1")

    def test_options_before_and_program_after_the_separator(self):
        seen = {}
        with patch.object(px, "run", side_effect=lambda a: seen.update(vars(a)) or 0):
            self.assertEqual(px.main(["run", "--runner", "/r", "--prefix", "/p", "--watch", "/w", "--",
                                      "prog.exe", "--prefix", "x"]), 0)
        self.assertEqual((seen["runner"], seen["prefix"], seen["watch"]), ("/r", "/p", "/w"))
        self.assertEqual(seen["program"], ["prog.exe", "--prefix", "x"])

    def test_profile_cannot_override_runner_or_loader_environment(self):
        names = {"GAMEID", "VKD3D_SHADER_CACHE_PATH", "LD_PRELOAD", "LD_LIBRARY_PATH", "PATH",
                 "STEAM_COMPAT_DATA_PATH", *px.environment("runner", "prefix", base={})}
        for name in names:
            with self.subTest(name=name), self.assertRaises(ValueError):
                px.validate_environment({name: "override"})

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=REPO)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runner = self.root / "runner"
        self.runner.mkdir()
        (self.runner / "version").write_text("200 current\n")
        self.prefix = self.root / "prefix"
        self.prefix.mkdir()
        self.args = SimpleNamespace(runner=str(self.runner), prefix=str(self.prefix), program=["sample.exe", "a b"],
                                    watch=None, wined3d=False)
        self.command = self.enterContext(patch.object(px.subprocess, "run"))
        self.command.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        self.enterContext(patch.object(px.shutil, "which", return_value="available"))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.stderr = self.enterContext(contextlib.redirect_stderr(io.StringIO()))

    def make_ready(self):
        (self.prefix / "system.reg").write_text('"ProgramFilesDir"="C:\\\\Program Files"\n')

    def test_checked_runner_refuses_downgrade_and_missing_launcher(self):
        px.record_runner(self.prefix, 300, "newest")
        before = (self.prefix / px.MARKER).read_bytes()
        for operation in (px.checked_runner, px.create, px.run):
            with self.subTest(operation=operation.__name__), self.assertRaises(SystemExit) as exc:
                operation(self.args)
            self.assertEqual(exc.exception.code, 2)
            self.assertEqual((self.prefix / px.MARKER).read_bytes(), before)
        self.command.assert_not_called()
        with patch.object(px.shutil, "which", return_value=None), self.assertRaises(SystemExit):
            px.checked_runner(self.args)

    def test_create_ready_prefix_does_not_reinitialize_it(self):
        self.make_ready()
        before = (self.prefix / "system.reg").read_bytes()
        self.assertEqual(px.create(self.args), 0)
        self.command.assert_not_called()
        self.assertEqual((self.prefix / "system.reg").read_bytes(), before)
        self.assertEqual(json.loads((self.prefix / px.MARKER).read_text()), {"stamp": 200, "runner": "current"})

    def test_create_checks_registry_after_waiting_and_ignores_boot_exit(self):
        def execute(cmd, **kw):
            if cmd[-1] == "-w":
                self.make_ready()
            return subprocess.CompletedProcess(cmd, 1)
        self.command.side_effect = execute
        with patch.dict(px.os.environ, {"DISPLAY": ":99", "WAYLAND_DISPLAY": "socket"}):
            self.assertEqual(px.create(self.args), 0)
        boot, wait = self.command.call_args_list
        self.assertEqual(boot.args[0], ["umu-run", "wineboot", "-u"])
        self.assertEqual(wait.args[0], [str(self.runner / "files/bin/wineserver"), "-w"])
        self.assertNotIn("DISPLAY", boot.kwargs["env"])
        self.assertNotIn("WAYLAND_DISPLAY", boot.kwargs["env"])
        self.assertEqual(boot.kwargs["env"]["WINEPREFIX"], str(self.prefix))
        self.assertTrue((self.prefix / px.MARKER).is_file())

    def test_failed_creation_never_records_success(self):
        with self.assertRaises(SystemExit) as exc:
            px.create(self.args)
        self.assertEqual(exc.exception.code, 1)
        self.assertFalse((self.prefix / px.MARKER).exists())

    def test_run_rejects_missing_program_and_unready_prefix(self):
        self.args.program = []
        with self.assertRaises(SystemExit):
            px.run(self.args)
        self.args.program = ["sample.exe"]
        with self.assertRaises(SystemExit):
            px.run(self.args)
        self.command.assert_not_called()
        self.assertFalse((self.prefix / px.MARKER).exists())

    def test_run_has_no_tunnel_option_and_never_reads_the_route(self):
        self.make_ready()
        with self.assertRaises(SystemExit) as exc:
            px.main(["run", "--runner", str(self.runner), "--prefix", str(self.prefix), "--require-tunnel",
                     "--", "sample.exe"])
        self.assertEqual(exc.exception.code, 2)
        self.command.assert_not_called()
        self.assertEqual(px.run(self.args), 0)
        self.assertEqual([c.args[0][0] for c in self.command.call_args_list], ["umu-run"])

    def test_watch_checks_even_a_failed_program_and_returns_change_exit(self):
        self.make_ready()
        self.args.watch = str(self.root / "install")
        self.command.side_effect = [
            subprocess.CompletedProcess([], 0),
            subprocess.CompletedProcess([], 7),
            subprocess.CompletedProcess([], 1, stdout="changed sample.dll sha256\n"),
        ]
        with patch.object(px.tempfile, "tempdir", str(self.root)), self.assertRaises(SystemExit) as exc:
            px.run(self.args)
        self.assertEqual(exc.exception.code, 3)
        snap, launch, compare = self.command.call_args_list
        self.assertEqual(snap.args[0][2:4], ["snapshot", self.args.watch])
        self.assertEqual(compare.args[0][2:4], ["compare", self.args.watch])
        self.assertEqual(snap.args[0][-1], compare.args[0][-1])
        self.assertEqual(launch.args[0], ["umu-run", "sample.exe", "a b"])
        self.assertEqual(launch.kwargs["env"]["UMU_RUNTIME_UPDATE"], "0")
        self.assertIn("changed sample.dll sha256", self.stderr.getvalue())

    def test_failed_snapshot_prevents_launch(self):
        self.make_ready()
        self.args.watch = str(self.root / "install")
        self.command.return_value = subprocess.CompletedProcess([], 2)
        with patch.object(px.tempfile, "tempdir", str(self.root)), self.assertRaises(SystemExit) as exc:
            px.run(self.args)
        self.assertEqual(exc.exception.code, 2)
        self.assertEqual(self.command.call_count, 1)
        self.assertEqual(self.command.call_args.args[0][2], "snapshot")

    def test_unchanged_watch_preserves_program_exit_and_status_is_read_only(self):
        self.make_ready()
        self.args.watch = str(self.root / "install")
        self.command.side_effect = [subprocess.CompletedProcess([], code) for code in (0, 7, 0)]
        with patch.object(px.tempfile, "tempdir", str(self.root)):
            self.assertEqual(px.run(self.args), 7)
        before = {p.name: p.read_bytes() for p in self.prefix.iterdir()}
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(px.status(self.args), 0)
        self.assertIn("current", out.getvalue())
        self.assertIn("True", out.getvalue())
        self.assertEqual({p.name: p.read_bytes() for p in self.prefix.iterdir()}, before)


    def launch_with_profile(self, profile=None, display=None):
        received = self.root / "received.json"
        (self.prefix / "install").mkdir(exist_ok=True)
        def program(cmd, **kwargs):
            if cmd[0] != "umu-run":
                return self.hash_command(cmd)
            received.write_text(json.dumps(kwargs["env"]))
            return subprocess.CompletedProcess(cmd, 0)
        self.command.side_effect = program
        argv = ["run", "--runner", str(self.runner), "--prefix", str(self.prefix)]
        if display is not None:
            argv += ["--display", display]
        if profile is not None:
            path = self.root / "launch-profile.json"
            path.write_text(json.dumps({"install_dir": "install", **profile}))
            argv += ["--profile", str(path)]
        self.assertEqual(px.main(argv + ["--", "sample.exe"]), 0)
        return json.loads(received.read_text())

    def hash_command(self, cmd):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            code = treehash.main(cmd[2:])
        return subprocess.CompletedProcess(cmd, code, stdout=out.getvalue())

    def watched_profile(self):
        self.make_ready()
        install = self.prefix / "drive_c/Program Files/Example"
        install.mkdir(parents=True)
        (install / "program.dll").write_bytes(b"original")
        profile = self.root / "watched-profile.json"
        profile.write_text(json.dumps({"install_dir": "drive_c/Program Files/Example"}))
        self.args.profile = str(profile)
        return install

    def test_guarded_launch_detects_root_chmod(self):
        install = self.watched_profile()
        install.chmod(0o755)
        def program(cmd, **kwargs):
            if cmd[0] != "umu-run":
                return self.hash_command(cmd)
            install.chmod(0o700)
            return subprocess.CompletedProcess(cmd, 0)
        self.command.side_effect = program
        with self.assertRaises(SystemExit) as exc:
            px.run(self.args)
        self.assertEqual(exc.exception.code, 3)

    def test_profile_install_tree_is_watched_on_failure_and_exception(self):
        install = self.watched_profile()
        for raises in (False, True):
            with self.subTest(raises=raises):
                (install / "program.dll").write_bytes(b"original")
                def program(cmd, **kwargs):
                    if cmd[0] != "umu-run":
                        return self.hash_command(cmd)
                    (install / "program.dll").write_bytes(b"changed")
                    if raises:
                        raise OSError("launch interrupted")
                    return subprocess.CompletedProcess(cmd, 7)
                self.command.side_effect = program
                with self.assertRaises(SystemExit) as exc:
                    px.run(self.args)
                self.assertEqual(exc.exception.code, 3)
                self.assertIn("changed program.dll sha256", self.stderr.getvalue())

    def run_with_change(self, change, program="app.exe", before=(1, 9, 0, 0)):
        """A run during which change(install) happens; its exit code, or the code it stopped with."""
        if not getattr(self.args, "profile", None):
            self.install = self.watched_profile()
        install, profile = self.install, Path(self.args.profile)
        profile.write_text(json.dumps({**json.loads(profile.read_text()), "program": program}))
        (install / "app.exe").unlink(missing_ok=True)
        (install / "app.exe").write_bytes(pe(before))
        (install / "program.dll").write_bytes(b"original")
        self.stderr.truncate(0)
        self.stderr.seek(0)
        def execute(cmd, **kwargs):
            if cmd[0] != "umu-run":
                return self.hash_command(cmd)
            change(install)
            return subprocess.CompletedProcess(cmd, 7)
        self.command.side_effect = execute
        try:
            return px.run(self.args)
        except SystemExit as stop:
            return stop.code

    def test_an_update_during_the_run_is_reported_and_keeps_the_exit_code(self):
        def update(install):
            (install / "app.exe").write_bytes(pe((1, 10, 0, 0)))
            (install / "added.dll").write_bytes(b"new")
        self.assertEqual(self.run_with_change(update), 7)
        self.assertIn("the app updated itself: 1.9.0.0 to 1.10.0.0", self.stderr.getvalue())
        for path in ("added added.dll", "changed app.exe sha256"):
            self.assertIn(path, self.stderr.getvalue())

    def test_any_other_change_during_the_run_fails_it(self):
        outside = self.root / "outside.exe"
        def link(install):
            (install / "app.exe").unlink()
            (install / "app.exe").symlink_to(outside)
        def other(install):
            (install / "program.dll").write_bytes(b"changed")
            outside.write_bytes(pe((9, 0, 0, 0)))
        changes = {"same version": lambda i: (i / "app.exe").write_bytes(pe((1, 9, 0, 0), extra=b"x")),
                   "lower version": lambda i: (i / "app.exe").write_bytes(pe((1, 8, 0, 0))),
                   "no version": lambda i: (i / "app.exe").write_bytes(b"MZ"),
                   "gone": lambda i: (i / "app.exe").unlink(), "link": link}
        for name, change in changes.items():
            with self.subTest(name=name):
                outside.write_bytes(pe((9, 0, 0, 0)))
                self.assertEqual(self.run_with_change(change), 3)
                self.assertNotIn("updated itself", self.stderr.getvalue())
        for program in ("../outside.exe", str(outside)):
            with self.subTest(program=program):
                outside.write_bytes(pe((1, 0, 0, 0)))
                self.assertEqual(self.run_with_change(other, program), 3)

    def fake_process(self, proc, pid, parent, comm=b"app.exe", **environment):
        folder = proc / str(pid)
        folder.mkdir(parents=True)
        (folder / "stat").write_bytes(b"%d (%s) S %d 1 1" % (pid, comm, parent))
        (folder / "comm").write_bytes(comm + b"\n")
        (folder / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in environment.items()) + b"\0")

    def test_a_process_of_the_prefix_is_found_by_its_environment(self):
        (self.prefix / "pfx").symlink_to(".")
        other = self.root / "prefix2"
        other.mkdir()
        mine, shell = px.os.getpid(), 10
        cases = [({"WINEPREFIX": f"{self.prefix}/pfx/"}, True), ({"STEAM_COMPAT_DATA_PATH": str(self.prefix)}, True),
                 ({"WINEPREFIX": str(self.prefix)}, True), ({"WINEPREFIX": str(other)}, False), ({"WINEPREFIX": ""}, False),
                 ({"OTHER": str(self.prefix)}, False), ({"WINEPREFIX": px.os.path.relpath(self.prefix)}, False), ({}, False)]
        for number, (environment, found) in enumerate(cases):
            with self.subTest(environment=environment):
                proc = self.root / f"proc{number}"
                self.fake_process(proc, mine, shell, WINEPREFIX=str(self.prefix))
                self.fake_process(proc, shell, 1, comm=b"a) shell", WINEPREFIX=str(self.prefix))
                self.fake_process(proc, 999, 1, **environment)
                (proc / "1234/environ").mkdir(parents=True)
                (proc / "version").write_text("not a process")
                self.assertEqual(px.in_use(self.prefix, proc), "app.exe, process 999" if found else None)
        with patch.object(px.os, "getuid", return_value=px.os.getuid() + 1):
            self.assertIsNone(px.in_use(self.prefix, self.root / "proc0"))

    def test_a_real_process_of_the_prefix_is_found(self):
        self.assertIsNone(px.in_use(self.prefix))
        child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE,
                                 env={**px.os.environ, "WINEPREFIX": str(self.prefix)})
        self.addCleanup(child.wait, 10)
        self.addCleanup(child.stdin.close)
        self.assertIn(f"process {child.pid}", px.in_use(self.prefix) or "")

    def test_an_explicit_watch_never_accepts_an_update(self):
        install = self.watched_profile()
        profile = Path(self.args.profile)
        profile.write_text(json.dumps({**json.loads(profile.read_text()), "program": "app.exe"}))
        (install / "app.exe").write_bytes(pe((1, 0, 0, 0)))
        self.args.watch = str(install)
        def program(cmd, **kwargs):
            if cmd[0] != "umu-run":
                return self.hash_command(cmd)
            (install / "app.exe").write_bytes(pe((2, 0, 0, 0)))
            return subprocess.CompletedProcess(cmd, 0)
        self.command.side_effect = program
        with self.assertRaises(SystemExit) as exc:
            px.run(self.args)
        self.assertEqual(exc.exception.code, 3)

    def test_explicit_watch_overrides_the_profile_install_tree(self):
        install = self.watched_profile()
        override = self.root / "override"
        override.mkdir()
        watched = override / "keep"
        watched.write_bytes(b"original")
        self.args.watch = str(override)
        for change in (False, True):
            with self.subTest(change=change):
                def program(cmd, **kwargs):
                    if cmd[0] != "umu-run":
                        return self.hash_command(cmd)
                    (install / "program.dll").write_bytes(b"changed")
                    if change:
                        watched.write_bytes(b"changed")
                    return subprocess.CompletedProcess(cmd, 7)
                self.command.side_effect = program
                if change:
                    with self.assertRaises(SystemExit) as exc:
                        px.run(self.args)
                    self.assertEqual(exc.exception.code, 3)
                else:
                    self.assertEqual(px.run(self.args), 7)
                    self.assertEqual(watched.read_bytes(), b"original")

    def test_unchanged_profile_tree_preserves_program_exit(self):
        install = self.watched_profile()
        before = treehash.snapshot(install)
        self.command.side_effect = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 7) if cmd[0] == "umu-run" else self.hash_command(cmd)
        self.assertEqual(px.run(self.args), 7)
        self.assertEqual(treehash.compare(install, before), [])

    def test_invalid_profile_install_paths_are_refused_before_launch(self):
        self.make_ready()
        profile = self.root / "bad-profile.json"
        self.args.profile = str(profile)
        for relative in (None, "../outside", str(self.root), "."):
            with self.subTest(relative=relative):
                profile.write_text(json.dumps({"install_dir": relative}))
                with self.assertRaises(SystemExit) as exc:
                    px.run(self.args)
                self.assertEqual(exc.exception.code, 2)
                self.assertFalse((self.prefix / px.MARKER).exists())

    def test_opencl_vendors_reach_the_launch_unless_already_set(self):
        self.make_ready()
        self.args.opencl_vendors = "/data/opencl-vendors"
        with patch.dict(px.os.environ, {}, clear=False):
            px.os.environ.pop("OCL_ICD_VENDORS", None)
            self.assertEqual(self.launch_with_opencl()["OCL_ICD_VENDORS"], "/data/opencl-vendors")
            px.os.environ["OCL_ICD_VENDORS"] = "/own/list"
            self.assertEqual(self.launch_with_opencl()["OCL_ICD_VENDORS"], "/own/list")

    def launch_with_opencl(self):
        received = self.root / "opencl-env.json"
        def program(cmd, **kwargs):
            if cmd[0] != "umu-run":
                return self.hash_command(cmd)
            received.write_text(json.dumps(kwargs["env"]))
            return subprocess.CompletedProcess(cmd, 0)
        self.command.side_effect = program
        self.args.program = ["sample.exe"]
        self.assertEqual(px.run(self.args), 0)
        return json.loads(received.read_text())

    def test_wayland_launch_sets_driver_and_preserves_display_variables(self):
        self.make_ready()
        with patch.dict(px.os.environ, {"WAYLAND_DISPLAY": "wayland-fixture", "DISPLAY": ":42", "PROTON_ENABLE_WAYLAND": "0"}):
            env = self.launch_with_profile(display="wayland")
        self.assertEqual(env["PROTON_ENABLE_WAYLAND"], "1")
        self.assertEqual(env["WAYLAND_DISPLAY"], "wayland-fixture")
        self.assertEqual(env["DISPLAY"], ":42")

    def test_default_and_x11_launch_drop_inherited_wayland_switch(self):
        self.make_ready()
        with patch.dict(px.os.environ, {"PROTON_ENABLE_WAYLAND": "1", "WAYLAND_DISPLAY": "wayland-fixture", "DISPLAY": ":42"}):
            for display in (None, "x11"):
                with self.subTest(display=display):
                    env = self.launch_with_profile(display=display)
                    self.assertNotIn("PROTON_ENABLE_WAYLAND", env)
                    self.assertEqual(env["WAYLAND_DISPLAY"], "wayland-fixture")
                    self.assertEqual(env["DISPLAY"], ":42")

    def test_invalid_display_is_refused_by_parser(self):
        before = treehash.snapshot(self.root)
        with self.assertRaises(SystemExit) as error:
            px.main(["run", "--prefix", str(self.prefix), "--display", "invalid"])
        self.assertEqual(error.exception.code, 2)
        self.assertIn("invalid choice", self.stderr.getvalue())
        self.assertEqual(treehash.compare(self.root, before), [])

    def test_run_passes_the_validated_profile_environment(self):
        self.make_ready()
        with patch.dict(px.os.environ, {"WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": "previous", "BASE_SETTING": "keep"}):
            env = self.launch_with_profile({"environment": {"WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": "--disable-gpu"}})
        self.assertEqual(env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"], "--disable-gpu")
        self.assertEqual(env["BASE_SETTING"], "keep")
        self.assertEqual(env["WINEPREFIX"], str(self.prefix))
        self.assertEqual(env["PROTONPATH"], str(self.runner))

    def test_profile_without_environment_preserves_the_launch_environment(self):
        self.make_ready()
        with patch.dict(px.os.environ, {"BASE_SETTING": "keep"}):
            baseline = self.launch_with_profile()
            for profile in ({}, {"environment": {}}):
                with self.subTest(profile=profile):
                    self.assertEqual(self.launch_with_profile(profile), baseline)

    def test_invalid_launch_environment_refuses_before_changing_the_prefix(self):
        self.make_ready()
        profile = self.root / "launch-profile.json"
        profile.write_text(json.dumps({"environment": {"WINEPREFIX": "other"}}))
        before = {p.name: p.read_bytes() for p in self.prefix.iterdir()}
        with self.assertRaises(SystemExit) as exc:
            px.main(["run", "--runner", str(self.runner), "--prefix", str(self.prefix), "--profile", str(profile), "--", "sample.exe"])
        self.assertEqual(exc.exception.code, 2)
        self.assertIn("reserved environment name", self.stderr.getvalue())
        self.assertEqual({p.name: p.read_bytes() for p in self.prefix.iterdir()}, before)


if __name__ == "__main__":
    unittest.main()

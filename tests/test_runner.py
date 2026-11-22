# SPDX-FileCopyrightText: 2026 ProtonCats and contributors
# SPDX-License-Identifier: MIT
import importlib.util
import contextlib
import hashlib
import io
import json
import shutil
import struct
import subprocess
import tarfile
import unittest
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch
from pathlib import Path
from core import setup as profiles


REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("core.runner", REPO / "core/runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class OurPatches(unittest.TestCase):
    def test_each_patch_maps_to_the_dlls_it_changes_outside_tests(self):
        mapping = {p.name[:4]: dlls for p, dlls in runner.our_patches()}
        self.assertEqual(mapping["0001"], ["mscms"])
        self.assertEqual(mapping["0002"], ["comdlg32"])
        self.assertEqual(mapping["0003"], ["shell32"])
        self.assertEqual(mapping["0004"], ["shell32"])
        self.assertEqual(mapping["0012"], ["wined3d"])

    def test_every_patched_dll_has_base_patches_in_each_pins_file(self):
        dlls = {d for _, ds in runner.mode_patches(runner.our_patches(), "pe") for d in ds}
        for pins in (REPO / "core/runner").glob("*.json"):
            self.assertLessEqual(dlls, set(json.loads(pins.read_text())["base_patches"]), pins.name)


    def test_identity_uses_only_each_modes_semantic_pin_fields(self):
        pins = json.loads((REPO / "core/runner/ge-proton11-12.json").read_bytes())
        for mode in ("pe", "unix"):
            expected = runner.patch_identity(json.dumps(pins).encode(), [], mode)
            reformatted = json.loads(json.dumps(pins))
            reformatted["comment"] = "different description"
            self.assertEqual(expected, runner.patch_identity(json.dumps(reformatted, indent=4).encode(), [], mode))
            other_mode = json.loads(json.dumps(pins))
            if mode == "pe":
                other_mode["unix"]["driver_version"] += 1
            else:
                other_mode["base_patches"]["mscms"] = [{"tree": "ge", "path": "changed"}]
            self.assertEqual(expected, runner.patch_identity(json.dumps(other_mode).encode(), [], mode))
            relevant = json.loads(json.dumps(pins))
            relevant["wine_commit"] = "a" * 40
            self.assertNotEqual(expected, runner.patch_identity(json.dumps(relevant).encode(), [], mode))


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=REPO)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))


class PatchedIdentity(Sandbox):
    def setUp(self):
        super().setUp()
        self.work = self.root
        self.pins, self.patch = self.work / "pins.json", self.work / "0001.patch"
        self.pins.write_text(json.dumps({"base_patches": {"mscms": []}}))
        self.patch.write_bytes(b"patch bytes")
        self.args = SimpleNamespace(pins=str(self.pins), work=str(self.work), jobs=1)
        self.marker, self.applied, self.prepared = (self.work / n for n in (".patched", "applied", "prepared"))
        self.applied.write_bytes(b"")
        def prepare(*args):
            self.prepared.touch()
            return self.work, self.work, self.work
        for name, kw in {
            "check_tools": {"return_value": ({}, "ar")},
            "prepare": {"side_effect": prepare},
            "our_patches": {"return_value": [(self.patch, ["mscms"])]},
            "apply": {"side_effect": lambda p, *args: self.applied.write_bytes(self.applied.read_bytes() + p.read_bytes())},
            "run": {}, "fix_ucrtbase": {"side_effect": RuntimeError("reached build")},
        }.items():
            self.enterContext(patch.object(runner, name, **kw))

    def build_to_marker(self):
        with self.assertRaisesRegex(RuntimeError, "reached build"):
            runner.build(self.args)

    def test_matching_marker_keeps_the_applied_tree(self):
        self.build_to_marker()
        marker = self.marker.read_bytes()
        self.assertTrue(marker.strip())
        self.assertEqual(self.applied.read_bytes(), b"patch bytes")
        self.build_to_marker()
        self.assertEqual(self.marker.read_bytes(), marker)
        self.assertEqual(self.applied.read_bytes(), b"patch bytes")

    def test_changed_inputs_and_empty_marker_refused_without_preparation(self):
        self.build_to_marker()
        originals = [p.read_bytes() for p in (self.pins, self.patch, self.marker)]
        for target in (self.pins, self.patch, self.marker):
            with self.subTest(target=target.name):
                for path, data in zip((self.pins, self.patch, self.marker), originals):
                    path.write_bytes(data)
                if target == self.pins:
                    data = json.loads(target.read_bytes())
                    data["wine_commit"] = "changed"
                    target.write_text(json.dumps(data))
                else:
                    target.write_bytes(b"" if target == self.marker else target.read_bytes() + b"\n")
                self.prepared.unlink()
                with self.assertRaisesRegex(SystemExit, "fresh --work directory") as exc:
                    runner.build(self.args)
                self.assertTrue(exc.exception.code)
                self.assertFalse(self.prepared.exists())
                self.assertEqual(self.applied.read_bytes(), b"patch bytes")
                self.prepared.touch()


class RunnerOperations(Sandbox):
    def setUp(self):
        super().setUp()
        self.env, self.log = {"PATH": str(self.root / "bin")}, self.root / "build.log"

    def test_command_failure_preserves_diagnostics(self):
        def fail(cmd, **kwargs):
            kwargs["stdout"].write("command diagnostic\n")
            return subprocess.CompletedProcess(cmd, 4)
        with patch.object(runner.subprocess, "run", side_effect=fail):
            with self.assertRaisesRegex(SystemExit, "failed \\(4\\).*see") as exc:
                runner.run(["command", "argument"], cwd=self.root, env=self.env, log=self.log)
        self.assertIn(str(self.log), str(exc.exception))
        self.assertEqual(self.log.read_text(), "command diagnostic\n")

    def test_requirements_refuse_missing_dependencies_or_create_a_local_alias(self):
        with patch.object(runner.shutil, "which", side_effect=lambda n: "archive" if n == "llvm-ar-19" else None):
            self.assertEqual(runner.llvm_ar(), "archive")
        work = self.root / "work"
        with patch.object(runner.shutil, "which", return_value=None):
            self.assertIsNone(runner.llvm_ar())
            with self.assertRaisesRegex(SystemExit, "missing tools:"):
                runner.check_tools(work)
        self.assertFalse(work.exists())
        archive = self.root / "archive"
        archive.write_bytes(b"fixture")
        with patch.object(runner.shutil, "which", return_value=str(archive)), \
                patch.dict(runner.os.environ, self.env, clear=True):
            env, ar = runner.check_tools(work)
        self.assertEqual(ar, str(archive))
        self.assertEqual((work / "bin/dlltool").readlink(), archive)
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["PATH"].split(":"), [str(work / "bin"), self.env["PATH"]])
        self.assertEqual(archive.read_bytes(), b"fixture")

    def test_source_fetch_keeps_the_pin_and_reuses_present_objects(self):
        dest, history = self.root / "source", self.root / "fetches"
        def execute(cmd, cwd, **kwargs):
            (cwd / ".git").mkdir(exist_ok=True)
            if "fetch" in cmd:
                self.assertEqual(cmd[-1], "pinned-commit")
                history.write_text((history.read_text() if history.exists() else "") + cmd[-1] + "\n")
        with patch.object(runner, "run", side_effect=execute), \
                patch.object(runner.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            runner.fetch("fixture-source", "pinned-commit", dest, self.env, depth=2)
            runner.fetch("fixture-source", "pinned-commit", dest, self.env, depth=2)
        self.assertEqual(history.read_text(), "pinned-commit\n")

    def test_preparation_records_completion_and_preserves_a_prepared_tree(self):
        pins = json.loads((REPO / "core/runner/ge-proton11-12.json").read_text())
        wine, staging, ge = (self.root / n for n in ("wine", "wine-staging", "runner-src"))
        (ge / ".git").mkdir(parents=True)
        state, marker = self.root / "source-state", self.root / ".prepared"
        state.write_text("original")
        with patch.object(runner, "fetch"), patch.object(runner, "run", side_effect=SystemExit("failed")):
            with self.assertRaisesRegex(SystemExit, "failed"):
                runner.prepare(pins, self.root, self.env, self.log)
        self.assertFalse(marker.exists())
        self.assertEqual(state.read_text(), "original")
        def execute(cmd, cwd=None, **kwargs):
            if "checkout" in cmd:
                self.assertEqual(cmd[-1], pins["wine_commit" if cwd == wine else "staging_commit"])
                if cwd == wine:
                    state.write_text("reset")
            if "revert" in cmd:
                self.assertEqual(cmd[-1], pins["wine_revert"])
                self.assertIn("--no-commit", cmd)
        with patch.object(runner, "fetch"), patch.object(runner, "run", side_effect=execute):
            self.assertEqual(runner.prepare(pins, self.root, self.env, self.log), (wine, staging, ge))
            self.assertTrue(marker.exists())
            state.write_text("prepared edits")
            runner.prepare(pins, self.root, self.env, self.log)
        self.assertEqual(state.read_text(), "prepared edits")

    def test_pe_preparation_ignores_unix_pins_and_comments(self):
        pins = json.loads((REPO / "core/runner/ge-proton11-12.json").read_bytes())
        (self.root / "runner-src/.git").mkdir(parents=True)
        with patch.object(runner, "fetch"), patch.object(runner, "run") as command:
            runner.prepare(pins, self.root, self.env, self.log)
            command.reset_mock()
            pins["comment"] = "changed description"
            pins["unix"]["driver_version"] += 1
            runner.prepare(pins, self.root, self.env, self.log)
            command.assert_not_called()

    def test_prepared_identity_refuses_changed_pins_before_touching_sources(self):
        pins = json.loads((REPO / "core/runner/ge-proton11-12.json").read_bytes())
        (self.root / "runner-src/.git").mkdir(parents=True)
        state = self.root / "wine-state"
        def execute(cmd, cwd=None, **kwargs):
            if cmd[:2] == ["git", "checkout"] and cwd == self.root / "wine":
                state.write_text(cmd[-1])
        with patch.object(runner, "fetch"), patch.object(runner, "run", side_effect=execute):
            runner.prepare(pins, self.root, self.env, self.log)
        original_marker = (self.root / ".prepared").read_bytes()
        original_state = state.read_bytes()
        unexpected = self.root / "unexpected-source-write"
        def changed_source(*args, **kwargs):
            unexpected.write_text("source operation ran")
        for changed in ({**pins, "wine_commit": "different-pin"}, {**pins, "ge_tag": "different-tag"}):
            with self.subTest(commit=changed["wine_commit"], tag=changed["ge_tag"]), \
                 patch.object(runner, "fetch", side_effect=changed_source), patch.object(runner, "run", side_effect=changed_source):
                with self.assertRaisesRegex(SystemExit, "fresh --work"):
                    runner.prepare(changed, self.root, self.env, self.log)
                self.assertEqual(state.read_bytes(), original_state)
                self.assertEqual((self.root / ".prepared").read_bytes(), original_marker)
                self.assertFalse(unexpected.exists())
        (self.root / ".prepared").write_bytes(b"")
        with patch.object(runner, "fetch", side_effect=changed_source), patch.object(runner, "run", side_effect=changed_source):
            with self.assertRaisesRegex(SystemExit, "fresh --work"):
                runner.prepare(pins, self.root, self.env, self.log)
        self.assertEqual(state.read_bytes(), original_state)
        self.assertFalse(unexpected.exists())

    def test_patch_filters_order_and_failure_preserve_unrelated_sources(self):
        directory, state = self.root / "patches", self.root / "source"
        directory.mkdir()
        state.write_text("one")
        for name, text in (("a.patch", "one two"), ("b.patch", "two three"), ("ignored.txt", "three bad")):
            (directory / name).write_text(text)
        def apply(cmd, **kwargs):
            self.assertIn("--include=dlls/sample/*", cmd)
            old, new = Path(cmd[-1]).read_text().split()
            if state.read_text() != old:
                raise SystemExit("context mismatch")
            state.write_text(new)
        with patch.object(runner, "run", side_effect=apply):
            runner.apply(directory, self.root, self.env, self.log, ("dlls/sample/*",))
            self.assertEqual(state.read_text(), "three")
            (directory / "a.patch").write_text("wrong bad")
            with self.assertRaisesRegex(SystemExit, "context mismatch"):
                runner.apply(directory, self.root, self.env, self.log, ("dlls/sample/*",))
        self.assertEqual(state.read_text(), "three")

    def test_import_names_rejects_unmapped_rvas_and_unterminated_tables(self):
        data = bytearray(1024)
        pe, section, raw, va = 64, 64 + 24 + 240, 512, 4096
        struct.pack_into("<I", data, 0x3c, pe)
        struct.pack_into("<H", data, pe + 6, 1)
        struct.pack_into("<H", data, pe + 20, 240)
        struct.pack_into("<II", data, pe + 24 + 120, va, 40)
        struct.pack_into("<8sIIII", data, section, b".rdata", 512, va, 512, raw)
        struct.pack_into("<I", data, raw + 12, va + 64)
        data[raw + 64:raw + 75] = b"sample.dll\0"
        dll = self.root / "sample.dll"
        dll.write_bytes(data)
        self.assertEqual(runner.import_names(dll), ["sample.dll"])
        for offset, value, message in ((raw + 12, va + 1024, "outside every section"),
                                       (pe + 24 + 124, 20, "no terminator")):
            with self.subTest(message=message):
                damaged = bytearray(data)
                struct.pack_into("<I", damaged, offset, value)
                dll.write_bytes(damaged)
                with self.assertRaisesRegex(ValueError, message):
                    runner.import_names(dll)

    def test_archive_rebuild_retains_original_on_failure_and_includes_objects(self):
        build = self.root / "build"
        lib = build / "dlls/ucrtbase/x86_64-windows/libucrtbase.a"
        lib.parent.mkdir(parents=True)
        lib.write_bytes(b"original archive")
        makefile = build / "Makefile"
        makefile.write_text("unrelated:\n")
        with patch.object(runner, "run"), self.assertRaisesRegex(SystemExit, "no libucrtbase.a rule"):
            runner.fix_ucrtbase(build, self.env, "archive", self.log)
        self.assertEqual(lib.read_bytes(), b"original archive")
        obj = "dlls/ucrtbase/x86_64-windows/"
        for name in ("a", "b"):
            (build / (obj + name + ".o")).write_text(name)
        makefile.write_text(f"{lib.relative_to(build)}: {obj}b.o " + "\\\n" +
                            f" {obj}a.o {obj}b.o\nunrelated: other.o\n")
        def execute(cmd, **kwargs):
            if "--def" in cmd:
                raise SystemExit("definition failed")
        with patch.object(runner, "run", side_effect=execute), self.assertRaisesRegex(SystemExit, "definition failed"):
            runner.fix_ucrtbase(build, self.env, "archive", self.log)
        self.assertEqual(lib.read_bytes(), b"original archive")
        for stage in ("dlltool", "archive"):
            def fail(cmd, **kwargs):
                if cmd[0] == "dlltool":
                    Path(cmd[-1]).write_bytes(b"partial")
                if cmd[0] == stage:
                    raise RuntimeError("injected failure")
            with self.subTest(stage=stage), patch.object(runner, "run", side_effect=fail):
                with self.assertRaisesRegex(RuntimeError, "injected failure"):
                    runner.fix_ucrtbase(build, self.env, "archive", self.log)
                self.assertEqual(lib.read_bytes(), b"original archive")
                self.assertEqual(list(lib.parent.glob("*.tmp")), [])
        def rebuild(cmd, cwd, **kwargs):
            if cmd[0] == "archive":
                Path(cmd[2]).write_bytes(b"".join((cwd / p).read_bytes() for p in cmd[3:]))
        with patch.object(runner, "run", side_effect=rebuild):
            runner.fix_ucrtbase(build, self.env, "archive", self.log)
        self.assertEqual(lib.read_bytes(), b"ab")

    def assembly_fixture(self):
        work, base, out = (self.root / name for name in ("work", "base", "assembled"))
        (work / "out").mkdir(parents=True)
        (work / "out/sample.dll").write_bytes(b"replacement")
        lib = Path("files/lib/wine/x86_64-windows")
        (base / lib).mkdir(parents=True)
        (base / lib / "sample.dll").write_bytes(b"original")
        (base / "untouched").write_bytes(b"keep")
        pins = self.root / "pins.json"
        pins.write_text(json.dumps({"ge_tag": "pinned"}))
        patch_file = self.root / "assembly.patch"
        patch_file.write_bytes(b"fixture patch")
        self.enterContext(patch.object(runner, "our_patches", return_value=[(patch_file, ["sample"])]))
        (work / ".patched").write_text(runner.patch_identity(pins.read_bytes(), runner.our_patches()) + "\n")
        return SimpleNamespace(pins=str(pins), work=str(work), base=str(base), out=str(out)), work, base, out, lib

    def test_assemble_refuses_existing_output_or_missing_inputs_without_copying(self):
        args, work, base, out, lib = self.assembly_fixture()
        out.mkdir()
        sentinel = out / "keep"
        sentinel.write_bytes(b"existing")
        with patch.object(runner, "run", side_effect=lambda cmd: shutil.copytree(cmd[-2], cmd[-1])):
            with self.assertRaisesRegex(SystemExit, "refusing to overwrite"):
                runner.assemble(args)
            self.assertEqual(sentinel.read_bytes(), b"existing")
            args.out = str(self.root / "absent-output")
            (work / "out/sample.dll").unlink()
            with self.assertRaisesRegex(SystemExit, "nothing built"):
                runner.assemble(args)
            (work / "out/sample.dll").write_bytes(b"replacement")
            args.base = str(self.root / "not-a-runner")
            with self.assertRaisesRegex(SystemExit, "not a Proton runner"):
                runner.assemble(args)
        self.assertFalse(Path(args.out).exists())

    def test_assemble_refuses_changed_or_missing_build_identity_before_copying(self):
        args, work, base, out, lib = self.assembly_fixture()
        patch_file = self.root / "current.patch"
        patch_file.write_bytes(b"original patch")
        patches = [(patch_file, ["sample"])]
        paths = [Path(args.pins), patch_file, work / ".patched"]
        paths[2].write_text(runner.patch_identity(paths[0].read_bytes(), patches))
        originals = [path.read_bytes() for path in paths]
        for index, change in enumerate(("pins", "patch", "empty", "missing")):
            with self.subTest(change=change):
                for path, contents in zip(paths, originals):
                    path.write_bytes(contents)
                if change in ("pins", "patch"):
                    path = paths[0 if change == "pins" else 1]
                    if change == "pins":
                        data = json.loads(path.read_bytes())
                        data["wine_commit"] = "changed"
                        path.write_text(json.dumps(data))
                    else:
                        path.write_bytes(path.read_bytes() + b"\n")
                elif change == "empty":
                    paths[2].write_bytes(b"")
                else:
                    paths[2].unlink()
                args.out = str(self.root / f"output-{index}")
                with patch.object(runner, "our_patches", return_value=patches), \
                     patch.object(runner, "run", side_effect=lambda cmd: shutil.copytree(cmd[-2], cmd[-1], symlinks=True)):
                    with self.assertRaisesRegex(SystemExit, "fresh --work"):
                        runner.assemble(args)
                self.assertFalse(Path(args.out).exists())
                self.assertEqual((base / lib / "sample.dll").read_bytes(), b"original")

    def test_assemble_refuses_partial_build_outputs(self):
        args, work, base, out, lib = self.assembly_fixture()
        patch_file = self.root / "assembly.patch"
        patches = [(patch_file, ["sample", "other"])]
        (work / ".patched").write_text(runner.patch_identity(Path(args.pins).read_bytes(), patches))
        with patch.object(runner, "our_patches", return_value=patches):
            with self.assertRaisesRegex(SystemExit, "incomplete"):
                runner.assemble(args)
        self.assertFalse(out.exists())
        self.assertEqual((base / lib / "sample.dll").read_bytes(), b"original")

    def test_assemble_refuses_library_directory_aliases_without_changing_the_base(self):
        args, work, base, out, lib = self.assembly_fixture()
        shared = self.root / "shared"
        (base / lib).rename(shared)
        (base / lib).symlink_to(shared, target_is_directory=True)
        with patch.object(runner, "run", side_effect=lambda cmd: shutil.copytree(cmd[-2], cmd[-1], symlinks=True)):
            with self.assertRaisesRegex(SystemExit, "symlink"):
                runner.assemble(args)
        self.assertEqual((shared / "sample.dll").read_bytes(), b"original")
        self.assertFalse(out.exists())
        (base / lib).unlink()
        shared.rename(base / lib)
        def copy_with_alias(cmd):
            shutil.copytree(cmd[-2], cmd[-1], symlinks=True)
            shutil.rmtree(out / lib)
            (out / lib).symlink_to(base / lib, target_is_directory=True)
        with patch.object(runner, "run", side_effect=copy_with_alias):
            with self.assertRaisesRegex(SystemExit, "symlink"):
                runner.assemble(args)
        self.assertEqual((base / lib / "sample.dll").read_bytes(), b"original")
        self.assertFalse((out / "halation-runner.json").exists())
        shutil.rmtree(out)
        def copy_root_alias(cmd):
            out.symlink_to(base, target_is_directory=True)
        with patch.object(runner, "run", side_effect=copy_root_alias):
            with self.assertRaisesRegex(SystemExit, "symlink"):
                runner.assemble(args)
        self.assertEqual((base / lib / "sample.dll").read_bytes(), b"original")
        self.assertFalse((base / "halation-runner.json").exists())

    def test_assemble_replaces_only_copied_dll_and_records_both_hashes(self):
        args, work, base, out, lib = self.assembly_fixture()
        target = base / lib / "original.dll"
        (base / lib / "sample.dll").rename(target)
        (base / lib / "sample.dll").symlink_to("original.dll")
        def copy(cmd):
            shutil.copytree(cmd[-2], cmd[-1], symlinks=True)
        with patch.object(runner, "run", side_effect=copy):
            self.assertEqual(runner.assemble(args), 0)
        self.assertFalse((out / lib / "sample.dll").is_symlink())
        self.assertEqual((out / lib / "sample.dll").read_bytes(), b"replacement")
        self.assertEqual((base / lib / "sample.dll").read_bytes(), b"original")
        self.assertEqual((out / lib / "original.dll").read_bytes(), b"original")
        self.assertEqual((out / "untouched").read_bytes(), b"keep")
        self.assertEqual((out / lib / "sample.dll").stat().st_mode & 0o777, 0o755)
        manifest = json.loads((out / "halation-runner.json").read_text())
        self.assertEqual(manifest["base"], "pinned")
        self.assertEqual(manifest["dlls"]["sample.dll"], {
            "sha256": hashlib.sha256(b"replacement").hexdigest(),
            "replaces": hashlib.sha256(b"original").hexdigest()})


class SourceDownloads(Sandbox):
    def test_source_fetch_never_reads_the_network_route(self):
        with patch.object(profiles.px.subprocess, "run", side_effect=AssertionError("route read")), \
             patch.object(runner, "run") as run:
            runner.fetch("fixture-source", "pin", self.root / "wine", {})
        self.assertIn(["git", "fetch", "-q", "--depth=1", "fixture-source", "pin"], [c.args[0] for c in run.call_args_list])


class FetchRunner(Sandbox):
    def setUp(self):
        super().setUp()
        self.dest, self.pins = self.root / "runners", self.root / "pins.json"
        self.dest.mkdir()
        self.tag = "GE-Proton11-12"
        self.base = f"https://github.com/example/runner/releases/download/{self.tag}"
        self.set_asset(self.tag + "-x86_64")
        self.use_archive()
        self.opener = Mock(side_effect=self.open_release)
        self.enterContext(patch.object(runner.urllib.request, "build_opener",
                                      return_value=SimpleNamespace(open=self.opener)))

    def set_asset(self, asset=None):
        self.asset = asset or self.tag
        self.target = self.dest / self.asset
        pins = {"ge_tag": self.tag, "ge_url": "https://github.com/example/runner/"}
        if asset:
            pins["ge_asset"] = asset
        self.pins.write_text(json.dumps(pins))

    def use_archive(self, extra=(), binary=False):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            for member in [self.member(self.asset + "/files/bin/start"), *extra]:
                archive.addfile(member, io.BytesIO(b"payload") if member.isfile() else None)
        self.archive = stream.getvalue()
        self.checksum = f"{hashlib.sha512(self.archive).hexdigest()}  {self.asset}.tar.gz\n".encode()
        if binary:
            self.checksum = self.checksum.upper().split(b"  ")[0] + f" *./{self.asset}.tar.gz\n".encode()

    def member(self, name, link=None, hard=False):
        member = tarfile.TarInfo(name)
        member.mode = 0o755
        if link is None:
            member.size = len(b"payload")
        else:
            member.type, member.linkname = (tarfile.LNKTYPE if hard else tarfile.SYMTYPE), link
        return member

    def open_release(self, url, **kwargs):
        data = {f"{self.base}/{self.asset}.sha512sum": self.checksum,
                f"{self.base}/{self.asset}.tar.gz": self.archive}[url]
        if isinstance(data, Exception):
            raise data
        return data if hasattr(data, "read") else io.BytesIO(data)

    def fetch(self):
        return runner.main(["fetch", "--pins", str(self.pins), "--dest", str(self.dest)])

    def test_fetch_never_reads_the_network_route(self):
        with patch.object(profiles.px.subprocess, "run", side_effect=AssertionError("route read")):
            self.assertEqual(self.fetch(), 0)

    def assert_refused(self, message):
        with self.assertRaisesRegex(SystemExit, message) as exc:
            self.fetch()
        self.assertTrue(exc.exception.code)
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_verified_install_preserves_links_permissions_and_checksum_formats(self):
        for asset, binary in ((None, False), (self.tag + "-x86_64", True)):
            with self.subTest(asset=asset):
                self.set_asset(asset)
                base = self.asset + "/files/bin/"
                self.use_archive([self.member(base + "link", "../bin/start"),
                                  self.member(base + "hard", base + "start", hard=True)], binary=binary)
                self.assertEqual(self.fetch(), 0)
                self.assertEqual(list(self.dest.iterdir()), [self.target])
                program, link, hard = (self.target / "files/bin" / n for n in ("start", "link", "hard"))
                self.assertEqual(program.read_bytes(), b"payload")
                self.assertEqual(program.stat().st_mode & 0o777, 0o755)
                self.assertTrue(link.is_symlink())
                self.assertEqual(link.read_bytes(), b"payload")
                self.assertEqual(hard.stat().st_ino, program.stat().st_ino)
                shutil.rmtree(self.target)

    def test_bad_checksums_are_refused_before_archive_parsing(self):
        self.archive = b"not an archive"
        for checksum, message in ((f"{'0' * 128}  {self.asset}.tar.gz", "checksum mismatch"),
                                  (f"{hashlib.sha512(self.archive).hexdigest()}  other.tar.gz", "missing.*checksum line")):
            with self.subTest(message=message):
                self.checksum = checksum.encode()
                self.assert_refused(message)

    def test_existing_directory_and_dangling_link_survive_offline_refusal(self):
        self.opener.side_effect = OSError("offline")
        for dangling in (False, True):
            with self.subTest(dangling=dangling), tempfile.TemporaryDirectory(dir=self.root) as dest:
                self.dest, self.target = Path(dest), Path(dest) / self.asset
                if dangling:
                    self.target.symlink_to(self.root / "absent")
                else:
                    self.target.mkdir()
                    (self.target / "keep").write_bytes(b"original")
                with self.assertRaisesRegex(SystemExit, "exists; refusing to overwrite"):
                    self.fetch()
                self.assertEqual(list(self.dest.iterdir()), [self.target])
                if dangling:
                    self.assertEqual(self.target.readlink(), self.root / "absent")
                else:
                    self.assertEqual((self.target / "keep").read_bytes(), b"original")

    def test_unsafe_archive_paths_and_links_leave_no_files(self):
        escaped = self.root / "escaped"
        escaped.write_bytes(b"original")
        base = self.asset + "/files/bin/"
        cases = [("absolute member", self.member(str(escaped)), "member"),
                 ("parent member", self.member(self.asset + "/files/../extra"), "member")]
        for hard in (False, True):
            for absolute in (False, True):
                target = str(escaped) if absolute else "../../../escaped"
                cases.append((f"{'absolute' if absolute else 'relative'} {'hardlink' if hard else 'symlink'}",
                              self.member(base + "link", target, hard=hard), "link"))
        for name, member, error in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory(dir=self.root) as dest:
                self.dest, self.target = Path(dest), Path(dest) / self.asset
                escaped.write_bytes(b"original")
                self.use_archive([member])
                self.assert_refused("unsafe archive " + error)
                self.assertEqual(escaped.read_bytes(), b"original")

    def test_fallback_extraction_allows_internal_links_and_refuses_escape(self):
        original = tarfile.TarFile.extractall
        def unfiltered(archive, path, members):
            return original(archive, path, members=members, filter=lambda member, dest: member)
        with patch.object(runner.tarfile, "data_filter", None), patch.object(runner.tarfile.TarFile, "extractall", unfiltered):
            self.use_archive([self.member(self.asset + "/files/bin/link", "start")])
            self.assertEqual(self.fetch(), 0)
            self.assertEqual((self.target / "files/bin/link").read_bytes(), b"payload")
            shutil.rmtree(self.target)
            self.use_archive([self.member(self.asset + "/files/bin/link", "../../../escaped")])
            self.assert_refused("unsafe archive link")

    def test_download_archive_and_publish_errors_remove_partial_files(self):
        class Interrupted(io.BytesIO):
            def read(self, size=-1):
                if self.tell():
                    raise OSError("interrupted download")
                return super().read(8)
        for stage in ("checksum", "stream", "archive", "rename"):
            with self.subTest(stage=stage):
                self.use_archive()
                if stage == "checksum":
                    self.checksum = OSError("checksum unavailable")
                elif stage == "stream":
                    self.archive = Interrupted(self.archive)
                elif stage == "archive":
                    self.archive = b"not an archive"
                    self.checksum = f"{hashlib.sha512(self.archive).hexdigest()}  {self.asset}.tar.gz".encode()
                guard = patch.object(runner.os, "rename", side_effect=OSError("rename denied"))
                with guard if stage == "rename" else contextlib.nullcontext():
                    self.assert_refused("fetch failed:")
                if stage == "stream":
                    self.assertTrue(self.archive.closed)

    def test_required_cli_options_are_refused(self):
        cases = [(cmd, [], f"needs {flag}") for cmd, flag in
                 (("fetch", "--dest"), ("build", "--work"), ("assemble", "--work"))]
        cases.append(("assemble", ["--work", str(self.root)], "assemble needs --base and --out"))
        for cmd, extra, message in cases:
            with self.subTest(cmd=cmd, extra=extra), self.assertRaisesRegex(SystemExit, message):
                runner.main([cmd, "--pins", str(self.pins)] + extra)
        self.assertEqual(list(self.dest.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

class UnixMode(Sandbox):
    def setUp(self):
        super().setUp()
        self.pins_path = self.root / "pins.json"
        self.pins = json.loads((REPO / "core/runner/ge-proton11-12.json").read_bytes())
        self.base, self.work, self.out = (self.root / p for p in ("base", "work", "assembled"))
        self.base.mkdir()
        self.work.mkdir()
        self.files = {**runner.UNIX_FILES, "win32u.so": "files/lib/wine/x86_64-unix/win32u.so"}
        for name, relative in self.files.items():
            path = self.base / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("original " + name).encode())
            self.pins["unix"]["base_sha256"][relative] = runner.sha256(path)
        self.script = "    def setup(self):\n        " + runner.OPENCL_OVERRIDE + "\n        pass\n"
        (self.base / "proton").write_text(self.script)
        self.pins["unix"]["base_sha256"]["proton"] = runner.sha256(self.base / "proton")
        self.opencl_imports = True
        (self.base / "version").write_text("200 GE-Proton11-12\n")
        self.pins_path.write_text(json.dumps(self.pins))
        self.patch_file = self.root / "0006.patch"
        self.patch_file.write_bytes(b"Unix patch")
        self.patches = [(self.patch_file, ["winex11.drv"])]
        self.enterContext(patch.object(runner, "our_patches", return_value=self.patches))
        self.args = SimpleNamespace(mode="unix", pins=str(self.pins_path), work=str(self.work),
                                    base=str(self.base), out=str(self.out), jobs=2)
        self.header = self.work / "source/wine/include/wine/gdi_driver.h"
        self.header.parent.mkdir(parents=True)
        self.header.write_text("#define WINE_GDI_DRIVER_VERSION 111\n")
        for name, relative in runner.UNIX_TARGETS.items():
            path = self.work / "build64" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("built " + name).encode())
        self.built_version = 111
        self.base_version = 111
        self.wayland_version = 111
        self.commands = []
        def command(cmd, **kwargs):
            self.commands.append(cmd)
            if cmd[0] == "cp":
                shutil.copytree(cmd[-2], cmd[-1], symlinks=True)
        self.run = self.enterContext(patch.object(runner, "run", side_effect=command))
        self.enterContext(patch.object(runner, "check_tools", return_value=({}, "ar")))
        self.enterContext(patch.object(runner.shutil, "which", return_value="available"))
        self.prepare = self.enterContext(patch.object(runner, "prepare_unix", return_value=self.work / "source/wine"))
        self.enterContext(patch.object(runner, "apply"))
        def disassemble(cmd, **kwargs):
            self.assertEqual(cmd[0], "objdump")
            if cmd[1] == "-T":
                self.assertTrue(cmd[2].endswith("opencl.so"))
                und = "0000000000000000      DF *UND*\t0000000000000000 (OPENCL_1.0) clGetPlatformIDs\n"
                return subprocess.CompletedProcess(cmd, 0, stdout=und if self.opencl_imports else "")
            if cmd[3].endswith("=waylanddrv_unix_init"):
                load, call = f"mov    $0x{self.wayland_version:x},%esi", "call   910 <__wine_set_user_driver@plt>"
                return subprocess.CompletedProcess(cmd, 0, stdout=f"00000001 <waylanddrv_unix_init>:\n  1:\t{load}\n"
                                                   f"  2:\t{call}\n  3:\t{load}\n  4:\t{call}\n")
            base = "__wine_set_user_driver" in cmd[3]
            symbol = "__wine_set_user_driver" if base else "init_user_driver"
            instruction, value = ("cmp", self.base_version) if base else ("mov", self.built_version)
            return subprocess.CompletedProcess(cmd, 0, stdout=f"00000001 <{symbol}>:\n  1: {instruction} $0x{value:x},%esi\n")
        self.decode = self.enterContext(patch.object(runner.subprocess, "run", side_effect=disassemble))

    def built(self):
        self.assertEqual(runner.build(self.args), 0)
        return (self.work / "unix-build.json").read_bytes()

    def test_matched_pair_is_published_with_hashes_and_no_extra_dependencies(self):
        self.built()
        unrelated = self.base / "keep"
        unrelated.write_bytes(b"keep")
        self.assertEqual(runner.assemble(self.args), 0)
        manifest = json.loads((self.out / "halation-runner.json").read_text())
        self.assertEqual(manifest["patches"], ["0006.patch"])
        self.assertEqual(manifest["unix"]["driver_version"], 111)
        self.assertEqual((self.out / "keep").read_bytes(), b"keep")
        for name, relative in runner.UNIX_FILES.items():
            self.assertEqual((self.out / relative).read_bytes(), ("built " + name).encode())
            self.assertEqual((self.base / relative).read_bytes(), ("original " + name).encode())
        self.assertEqual((self.out / self.files["win32u.so"]).read_bytes(), b"original win32u.so")

    def test_opencl_built_without_the_host_loader_is_refused(self):
        self.opencl_imports = False
        with self.assertRaisesRegex(SystemExit, "without the host OpenCL loader"):
            runner.build(self.args)
        self.assertFalse((self.work / "unix-build.json").exists())

    def test_assembled_proton_script_allows_opencl_and_records_both_hashes(self):
        self.built()
        self.assertEqual(runner.assemble(self.args), 0)
        script = (self.out / "proton").read_text()
        self.assertNotIn(runner.OPENCL_OVERRIDE, script)
        self.assertIn("        pass  # opencl", script)
        self.assertEqual(script.count("\n"), self.script.count("\n"))
        self.assertEqual((self.base / "proton").read_text(), self.script)
        self.assertEqual((self.out / "proton").stat().st_mode & 0o777, 0o755)
        manifest = json.loads((self.out / "halation-runner.json").read_text())
        self.assertEqual(manifest["unix"]["proton"], {"sha256": runner.sha256(self.out / "proton"),
                                                      "replaces": runner.sha256(self.base / "proton")})

    def test_proton_script_needs_exactly_one_override_line(self):
        for count in (0, 2):
            with self.subTest(count=count), self.assertRaisesRegex(SystemExit, f"found {count}"):
                runner.allow_opencl("x = 1\n" + ("        " + runner.OPENCL_OVERRIDE + "\n") * count)

    def test_missing_pins_refuse_before_commands_or_source_preparation(self):
        for key in ("ge_commit", "prep_path", "prep_sha256", "wineopenxr_tree", "xml_sha256", "base_sha256", "driver_version"):
            pins = json.loads(json.dumps(self.pins))
            del pins["unix"][key]
            self.pins_path.write_text(json.dumps(pins))
            with self.subTest(key=key), self.assertRaises(SystemExit):
                runner.build(self.args)
            self.prepare.assert_not_called()
            self.run.assert_not_called()
            self.assertFalse((self.work / "out").exists())

    def test_version_mismatch_refuses_assembly_without_any_writes(self):
        self.built()
        original = self.header.read_text()
        for change in ("header", "built", "wayland", "base"):
            self.header.write_text(original)
            self.built_version = self.base_version = self.wayland_version = 111
            if change == "header":
                self.header.write_text("#define WINE_GDI_DRIVER_VERSION 108\n")
            elif change == "built":
                self.built_version = 108
            elif change == "wayland":
                self.wayland_version = 108
            else:
                self.base_version = 108
            self.run.reset_mock()
            with self.subTest(change=change), self.assertRaisesRegex(SystemExit, "version mismatch"):
                runner.assemble(self.args)
            self.run.assert_not_called()
            self.assertFalse(self.out.exists())

    def test_unknown_or_ambiguous_symbol_decode_refuses(self):
        self.built()
        for text in ("", "00000001 <wrong>:\n  1: mov $0x6f,%esi\n",
                     "00000001 <init_user_driver>:\n  1: mov $0x6f,%esi\n  2: mov $0x6c,%esi\n"):
            self.decode.return_value = subprocess.CompletedProcess([], 0, stdout=text)
            self.decode.side_effect = None
            self.run.reset_mock()
            with self.subTest(text=text), self.assertRaisesRegex(SystemExit, "driver version"):
                runner.assemble(self.args)
            self.assertFalse(self.out.exists())
            self.run.assert_not_called()

    def test_driver_call_decode_needs_one_known_value_at_every_call(self):
        call = "  9:\tcall   910 <__wine_set_user_driver@plt>\n"
        cases = {"  1:\tmov    $0x6f,%esi\n" + call + "  5:\tmov    $0x6f,%esi\n" + call: 111,
                 "  1:\tmov    $0x6f,%esi\n  2:\tlea    0x10(%rip),%rdi        # 20 <funcs>\n"
                 "  3:\tjmp    910 <__wine_set_user_driver@plt>\n": 111,
                 "  1:\tmov    $0x6f,%esi\n" + call + "  5:\tmov    $0x6c,%esi\n" + call: None,
                 "  1:\tmov    $0x6f,%esi\n  2:\tlea    0x31687(%rip),%rsi        # 550e0 <dbch>\n" + call: None,
                 "  1:\tmov    $0x6f,%esi\n  2:\tcall   a90 <getenv@plt>\n" + call: None,
                 "  1:\tmov    $0x6f,%esi\n": None, call: None}
        for body, expected in cases.items():
            self.decode.side_effect = None
            self.decode.return_value = subprocess.CompletedProcess([], 0, stdout="00000001 <init>:\n" + body)
            with self.subTest(body=body):
                if expected is None:
                    with self.assertRaisesRegex(SystemExit, "driver version"):
                        runner.call_version(self.root / "driver.so", "init")
                else:
                    self.assertEqual(runner.call_version(self.root / "driver.so", "init"), expected)

    def test_stale_identity_refuses_pins_patch_mode_or_feature_changes(self):
        self.built()
        original = self.pins_path.read_bytes()
        for change in ("pins", "patch", "features", "mode"):
            self.pins_path.write_bytes(original)
            self.patch_file.write_bytes(b"Unix patch")
            features = runner.UNIX_CONFIGURE + ["--without-x"] if change == "features" else runner.UNIX_CONFIGURE
            if change == "pins":
                changed = json.loads(original)
                changed["wine_commit"] = "a" * 40
                self.pins_path.write_text(json.dumps(changed))
            if change == "patch":
                self.patch_file.write_bytes(b"changed patch")
            if change == "mode":
                (self.work / ".patched").write_text(runner.patch_identity(original, self.patches, "pe"))
            with self.subTest(change=change), patch.object(runner, "UNIX_CONFIGURE", features):
                with self.assertRaisesRegex(SystemExit, "fresh --work"):
                    runner.assemble(self.args)
            self.assertFalse(self.out.exists())

    def test_changed_base_hash_refuses_before_copy(self):
        self.built()
        for relative in self.files.values():
            path = self.base / relative
            original = path.read_bytes()
            path.write_bytes(b"changed")
            self.run.reset_mock()
            with self.subTest(relative=relative), self.assertRaisesRegex(SystemExit, "base hash mismatch"):
                runner.assemble(self.args)
            self.assertFalse(self.out.exists())
            self.run.assert_not_called()
            path.write_bytes(original)

    def test_incomplete_unexpected_or_changed_outputs_refuse_before_copy(self):
        self.built()
        for change in ("extra", "missing", "changed"):
            path = self.work / "out" / ("unexpected.so" if change == "extra" else "winex11.so")
            original = path.read_bytes() if path.exists() else None
            if change == "missing":
                path.unlink()
            else:
                path.write_bytes(b"changed")
            self.run.reset_mock()
            with self.subTest(change=change), self.assertRaisesRegex(SystemExit, "output|hash mismatch"):
                runner.assemble(self.args)
            self.assertFalse(self.out.exists())
            self.run.assert_not_called()
            if original is None:
                path.unlink()
            else:
                path.write_bytes(original)

    def test_unix_library_alias_refuses_without_publication_or_base_changes(self):
        self.built()
        directory = self.base / "files/lib/wine/x86_64-unix"
        shared = self.root / "shared"
        directory.rename(shared)
        directory.symlink_to(shared, target_is_directory=True)
        before = {p.name: p.read_bytes() for p in shared.iterdir()}
        self.run.reset_mock()
        with self.assertRaisesRegex(SystemExit, "symlink"):
            runner.assemble(self.args)
        self.assertFalse(self.out.exists())
        self.assertEqual(before, {p.name: p.read_bytes() for p in shared.iterdir()})
        self.run.assert_not_called()

    def test_missing_xinput_packages_refuse_before_prepare(self):
        self.run.side_effect = SystemExit("missing xi")
        with self.assertRaisesRegex(SystemExit, "missing xi"):
            runner.build(self.args)
        self.prepare.assert_not_called()
        self.assertFalse((self.work / "out").exists())

    def test_pe_mode_still_builds_only_pe_targets_with_unix_patch_present(self):
        pe_patch = self.root / "0001.patch"
        pe_patch.write_bytes(b"PE patch")
        patches = [(pe_patch, ["mscms"]), *self.patches]
        pins = {**self.pins, "base_patches": {"mscms": []}}
        self.pins_path.write_text(json.dumps(pins))
        pe_work = self.root / "pe"
        pe_work.mkdir()
        self.args.mode, self.args.work = "pe", str(pe_work)
        built = pe_work / "build64/dlls/mscms/x86_64-windows/mscms.dll"
        built.parent.mkdir(parents=True)
        built.write_bytes(b"unchanged PE output")
        with patch.object(runner, "our_patches", return_value=patches), \
                patch.object(runner, "prepare", return_value=(self.root, self.root, self.root)), \
                patch.object(runner, "fix_ucrtbase"), patch.object(runner, "import_names", return_value=[]):
            self.assertEqual(runner.build(self.args), 0)
        self.assertEqual({p.name for p in (pe_work / "out").iterdir()}, {"mscms.dll"})
        self.assertEqual((pe_work / "out/mscms.dll").read_bytes(), b"unchanged PE output")
        self.assertFalse(any("winex11" in str(arg) for cmd in self.commands for arg in cmd))

    def test_patch_classification_keeps_unix_out_of_pe_build(self):
        pe = (self.root / "0001.patch", ["mscms"])
        pe[0].write_bytes(b"PE patch")
        patches = [pe, *self.patches]
        self.assertEqual(runner.mode_patches(patches, "pe"), [pe])
        self.assertEqual(runner.mode_patches(patches, "unix"), self.patches)
        self.assertEqual(runner.patch_identity(b"{}", [pe]),
                         runner.patch_identity(b"{}", runner.mode_patches(patches, "pe")))
        with self.assertRaisesRegex(SystemExit, "mixed"):
            runner.mode_patches([(self.patch_file, ["winex11.drv", "mscms"])], "pe")


class UnixPreparation(Sandbox):
    def setUp(self):
        super().setUp()
        self.pins = json.loads((REPO / "core/runner/ge-proton11-12.json").read_bytes())
        self.source = self.root / "source"
        self.script = self.source / self.pins["unix"]["prep_path"]
        self.script.parent.mkdir(parents=True)
        self.script.write_text('#!/bin/bash\napply_all_in_dir() { :; }\n### (1) PREP SECTION ###\n'
                               'exit 99\n### (2) WINE PATCHING ###\npushd wine\n'
                               f'git revert --no-commit {self.pins["wine_revert"]}\nautoreconf -f\n'
                               './tools/make_requests\npopd\n### END WINE PATCHING ###\n')
        self.pins["unix"]["prep_sha256"] = runner.sha256(self.script)
        self.wine = self.source / "wine"
        self.wine.mkdir()
        self.calls = []
        def execute(cmd, cwd=None, **kwargs):
            self.calls.append(cmd)
            if cmd[0] == "curl":
                (cwd / cmd[3]).write_bytes(b"xml fixture")
        self.command = self.enterContext(patch.object(runner, "run", side_effect=execute))
        self.fetch = self.enterContext(patch.object(runner, "fetch"))
        self.enterContext(patch.object(runner.subprocess, "run", return_value=subprocess.CompletedProcess(
            [], 0, stdout=self.pins["unix"]["wineopenxr_tree"] + "\n")))
        self.pins["unix"]["xml_sha256"] = {name: hashlib.sha256(b"xml fixture").hexdigest()
                                              for name in ("vk.xml", "video.xml")}

    def prepare(self):
        return runner.prepare_unix(self.pins, self.root, {}, self.root / "log", "identity")

    def test_full_wine_section_is_fail_fast_and_reused_only_with_matching_identity(self):
        self.assertEqual(self.prepare(), self.wine)
        script = (self.root / "prep-wine.sh").read_text()
        self.assertIn("set -Eeuo pipefail", script)
        self.assertIn('apply_all_in_dir "../patches/wineopenxr/"', script)
        self.assertIn("autoconf -f && autoheader -f", script)
        self.assertIn("./tools/make_requests", script)
        self.assertNotIn("exit 99", script)
        self.assertEqual((self.root / ".prepared").read_text(), "identity\n")
        self.command.reset_mock()
        self.prepare()
        self.command.assert_not_called()
        with self.assertRaisesRegex(SystemExit, "fresh --work"):
            runner.prepare_unix(self.pins, self.root, {}, self.root / "log", "changed")

    def test_prep_revert_must_match_the_pin(self):
        self.pins["wine_revert"] = "a" * 40
        with self.assertRaisesRegex(SystemExit, "revert pin mismatch"):
            self.prepare()
        self.assertFalse((self.root / ".prepared").exists())

    def test_changed_script_or_xml_and_failed_prep_never_publish_completion(self):
        original = self.script.read_bytes()
        self.script.write_bytes(original + b"\n")
        with self.assertRaisesRegex(SystemExit, "script hash"):
            self.prepare()
        self.assertFalse((self.root / ".prepared").exists())
        self.script.write_bytes(original)
        self.pins["unix"]["xml_sha256"]["vk.xml"] = "0" * 64
        with self.assertRaisesRegex(SystemExit, "XML hash"):
            self.prepare()
        self.assertFalse((self.root / ".prepared").exists())
        self.command.side_effect = SystemExit("prep failed")
        with self.assertRaisesRegex(SystemExit, "prep failed"):
            self.prepare()
        self.assertFalse((self.root / ".prepared").exists())

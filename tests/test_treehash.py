# SPDX-FileCopyrightText: 2026 ProtonCats and contributors
# SPDX-License-Identifier: MIT
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))
import treehash  # noqa: E402


def run(*args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = treehash.main([str(a) for a in args])
    return code, out.getvalue().splitlines()


class TreeHashTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "app"
        self.root.mkdir()
        (self.root / "bin").mkdir()
        (self.root / "bin" / "a.dll").write_bytes(b"one")
        (self.root / "readme.txt").write_text("hello")
        os.symlink("bin/a.dll", self.root / "link")
        (base / "outside").write_text("x")
        os.symlink(base / "outside", self.root / "out")
        self.manifest = base / "manifest.jsonl"
        self.assertEqual(run("snapshot", self.root, self.manifest)[0], 0)

    def tearDown(self):
        self.tmp.cleanup()

    def entries(self):
        return {e["path"]: e for e in map(json.loads, self.manifest.read_text().splitlines())}

    def test_manifest_fields(self):
        e = self.entries()
        self.assertEqual(list(e), sorted(e))
        self.assertEqual(e["bin"]["type"], "dir")
        f = e["bin/a.dll"]
        self.assertEqual((f["type"], f["size"]), ("file", 3))
        self.assertEqual(f["sha256"], "7692c3ad3540bb803c020b3aee66cd8887123234ea0c6e7143c0add73ff431ed")
        self.assertEqual((e["link"]["type"], e["link"]["target"]), ("link", "bin/a.dll"))
        self.assertNotIn("sha256", e["out"])

    def test_clean_tree_passes(self):
        self.assertEqual(run("compare", self.root, self.manifest), (0, []))

    def test_byte_change_with_mtime_restored(self):
        p = self.root / "bin" / "a.dll"
        st = p.stat()
        p.write_bytes(b"two")
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertEqual(run("compare", self.root, self.manifest), (1, ["changed bin/a.dll sha256"]))

    def test_mode_change(self):
        os.chmod(self.root / "readme.txt", 0o755)
        self.assertEqual(run("compare", self.root, self.manifest), (1, ["changed readme.txt mode"]))

    def test_manifest_without_root_entry_still_compares_clean(self):
        lines = [l for l in self.manifest.read_text().splitlines() if json.loads(l)["path"] != "."]
        self.manifest.write_text("\n".join(lines) + "\n")
        self.assertEqual(run("compare", self.root, self.manifest), (0, []))
        os.chmod(self.root / "readme.txt", 0o755)
        self.assertEqual(run("compare", self.root, self.manifest), (1, ["changed readme.txt mode"]))

    def test_link_retargeted(self):
        (self.root / "link").unlink()
        os.symlink("readme.txt", self.root / "link")
        self.assertEqual(run("compare", self.root, self.manifest), (1, ["changed link target"]))

    def test_link_replaced_by_file(self):
        (self.root / "link").unlink()
        (self.root / "link").write_bytes(b"one")
        code, lines = run("compare", self.root, self.manifest)
        self.assertEqual(code, 1)
        self.assertIn("changed link type", lines)

    def test_case_twin_added(self):
        (self.root / "bin" / "A.dll").write_bytes(b"one")
        self.assertEqual(run("compare", self.root, self.manifest), (1, ["added bin/A.dll"]))

    def test_removed_and_added(self):
        (self.root / "readme.txt").unlink()
        (self.root / "bin" / "new.dll").write_bytes(b"")
        code, lines = run("compare", self.root, self.manifest)
        self.assertEqual((code, lines), (1, ["added bin/new.dll", "removed readme.txt"]))

    def test_outside_link_not_followed(self):
        (Path(self.tmp.name) / "outside").write_text("changed outside the tree")
        self.assertEqual(run("compare", self.root, self.manifest), (0, []))

    @unittest.skipIf(os.geteuid() == 0, "root reads mode 000 files")
    def test_unreadable_file_is_an_error(self):
        p = self.root / "readme.txt"
        os.chmod(p, 0)
        try:
            self.assertEqual(run("snapshot", self.root, self.manifest)[0], 2)
        finally:
            os.chmod(p, 0o644)

    def test_bad_manifest_is_an_error(self):
        self.manifest.write_text("not json\n")
        self.assertEqual(run("compare", self.root, self.manifest)[0], 2)

    def test_missing_root_is_an_error(self):
        self.assertEqual(run("snapshot", self.root / "nope", self.manifest)[0], 2)


if __name__ == "__main__":
    unittest.main()

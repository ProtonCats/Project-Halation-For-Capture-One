# SPDX-FileCopyrightText: 2026 ProtonCats and contributors
# SPDX-License-Identifier: MIT
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECK = ROOT / "tools" / "prepush.py"
PUSH = ROOT / "tools" / "push"
OWN = ["tools/prepush.py", "tools/push", "tools/install-hooks", "tests/test_prepush.py"]
TERM = "zorb" + "lax"  # split so this file passes its own check
USER = "fixture" + "user"
ZERO = "0" * 40


class BlobPaths(unittest.TestCase):
    def test_duplicate_bytes_keep_path_rules_for_commit_and_tree_targets(self):
        import importlib.util
        from unittest.mock import patch
        spec = importlib.util.spec_from_file_location("prepush", CHECK)
        gate_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate_module)
        sha = "a" * 40
        target = "b" * 40
        for kind in ("commit", "tree"):
            def git(*args, **kwargs):
                if args[0] == "ls-tree":
                    return (f"100644 blob {sha}\tdocs/a.txt\0"
                            f"100644 blob {sha}\tdocs/model.onnx\0").encode()
                if args[0] == "rev-list":
                    return target.encode()
                if args[:2] == ("cat-file", "commit"):
                    return b"tree fixture\n\nclean"
                if args[0] == "diff-tree":
                    return (f":000000 100644 {ZERO} {sha} A\0docs/a.txt\0"
                            f":000000 100644 {ZERO} {sha} A\0docs/model.onnx\0").encode()
                return b""
            with self.subTest(kind=kind), patch.object(gate_module, "git", side_effect=git), \
                    patch.object(gate_module, "obj_type", return_value=kind), \
                    patch.object(gate_module, "read_blobs", return_value=[(sha, 5, b"plain")]) as read:
                gate = gate_module.Gate(True, 1024)
                gate_module.check_push(gate, [f"refs/heads/main {target} refs/heads/main {ZERO}"])
                read.assert_called_once_with([sha], 1024)
                self.assertTrue(any(f[0] == "binary" and "model.onnx" in f[1]
                                    for f in gate.findings), gate.findings)


class PrepushTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home = base / "home"
        self.home.mkdir()
        self.list = base / "forbidden.txt"
        self.list.write_text(f"# test list\n{TERM}\n")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(HOME=str(self.home), USER=USER, LOGNAME=USER, GIT_CONFIG_NOSYSTEM="1",
                        GIT_CONFIG_GLOBAL=os.devnull, HALATION_FORBIDDEN=str(self.list))
        self.repo = base / "repo"
        self.git("init", "-q", "-b", "main", str(self.repo), cwd=base)
        self.git("config", "user.name", "t")
        self.git("config", "user.email", "t@users.noreply.github.com")
        self.commit({p: (ROOT / p).read_bytes() for p in OWN}, "Add the leak check")
        self.base = self.head()

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args, cwd=None):
        r = subprocess.run(["git", *args], cwd=cwd or self.repo, env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def head(self):
        return self.git("rev-parse", "HEAD")

    def commit(self, files=None, msg="Change", delete=()):
        for name, data in (files or {}).items():
            p = self.repo / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data if isinstance(data, bytes) else data.encode())
            if name.startswith("tools/"):
                p.chmod(0o755)
        for name in delete:
            self.git("rm", "-q", name)
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", msg)

    def run_check(self, *flags, ref="refs/heads/main", remote=ZERO, stdin=None, env=None, cwd=None):
        line = stdin if stdin is not None else f"{ref} {self.head()} {ref} {remote}\n"
        r = subprocess.run([sys.executable, str(CHECK), *flags], cwd=cwd or self.repo, env=env or self.env,
                           input=line, capture_output=True, text=True)
        self.assertNotIn(TERM, r.stdout + r.stderr)
        return r.returncode, r.stdout

    def assertFinding(self, rule, reason=None, **kw):
        code, out = self.run_check(**kw)
        self.assertEqual(code, 1, out)
        hits = [l for l in out.splitlines() if l.startswith(rule + ": ") and (reason is None or l.endswith(reason))]
        self.assertTrue(hits, out)

    def test_clean_push_passes(self):
        self.commit({"docs/a.md": "plain text\n", "README.md": "# x\n"})
        self.assertEqual(self.run_check(), (0, ""))

    def test_term_in_file_deleted_two_commits_back(self):
        self.commit({"docs/old.md": f"a line\nsee {TERM}.Run here\n"})
        self.commit({"docs/b.md": "b\n"})
        self.commit(delete=["docs/old.md"])
        self.assertFalse((self.repo / "docs/old.md").exists())
        code, out = self.run_check()
        self.assertEqual(code, 1)
        self.assertRegex(out, r"term: \w{12}:docs/old\.md:2: term 2")

    def test_term_in_message(self):
        self.commit(msg=f"Fix the {TERM} path")
        self.assertFinding("term", "term 2")

    def test_term_in_branch_name(self):
        self.git("switch", "-q", "-c", f"fix-{TERM}")
        self.assertFinding("term", "term 2", ref=f"refs/heads/fix-{TERM}")

    def test_term_in_destination_ref_only(self):
        code, out = self.run_check(stdin=f"refs/heads/main {self.head()} refs/heads/fix-{TERM} {ZERO}\n")
        self.assertEqual((code, out), (1, "term: ref refs/heads/fix-<term 2>:1: term 2\n"))

    def test_term_in_path(self):
        self.commit({f"docs/{TERM}.md": "x\n"})
        self.assertFinding("term", "term 2")

    def test_term_in_annotated_tag(self):
        self.git("tag", "-a", "v0", "-m", f"tag {TERM}")
        tag = self.git("rev-parse", "v0")
        code, out = self.run_check(stdin=f"refs/tags/v0 {tag} refs/tags/v0 {ZERO}\n")
        self.assertEqual(code, 1)
        self.assertIn("term: tag ", out)

    def test_range_stops_at_remote(self):
        self.commit({"docs/a.md": f"{TERM}\n"})
        remote = self.head()
        self.commit({"docs/b.md": "clean\n"})
        self.assertEqual(self.run_check(remote=remote), (0, ""))
        self.assertFinding("term", remote=self.base)

    def test_unknown_remote_sha_scans_everything(self):
        self.commit({"docs/a.md": f"{TERM}\n"})
        self.commit({"docs/b.md": "clean\n"})
        self.assertFinding("term", remote="1" * 40)

    def test_term_added_by_merge_commit_itself(self):
        self.git("switch", "-q", "-c", "side")
        self.commit({"docs/side.md": "s\n"})
        self.git("switch", "-q", "main")
        self.commit({"docs/main.md": "m\n"})
        self.git("merge", "-q", "--no-commit", "side")
        self.commit({"docs/merged.md": f"{TERM}\n"}, msg="Merge side")
        self.assertEqual(len(self.git("log", "-1", "--format=%P").split()), 2)
        self.assertFinding("term", remote=self.base)

    def test_path_outside_allow_list(self):
        self.commit({"notes/x.md": "x\n"})
        self.assertFinding("path", "outside the allow-list")

    def test_top_level_allow_list(self):
        self.commit({n: "x\n" for n in ("CHANGELOG.md", "SECURITY.md", "setup", "run", "REUSE.toml")})
        self.assertEqual(self.run_check(), (0, ""))
        self.commit({"Makefile": "x\n"})
        self.assertFinding("path")

    def test_list_missing_or_empty(self):
        env = dict(self.env)
        del env["HALATION_FORBIDDEN"]
        code, out = self.run_check(env=env)
        self.assertEqual(code, 1)
        self.assertIn("list: $HALATION_FORBIDDEN: list not run", out)
        code, out = self.run_check("--ci", env=env)
        self.assertEqual(code, 0)
        self.assertIn("list not run", out)
        self.list.write_text("# only a comment\n\n")
        self.assertFinding("list")

    def test_binaries_by_magic(self):
        cases = {
            "pe": b"MZ\x90", "elf": b"\x7fELF\x02", "tiff_le": b"II*\x00", "tiff_be": b"MM\x00*",
            "raf": b"FUJIFILMCCD-RAW 0201", "icc": b"x" * 36 + b"acsp", "png": b"\x89PNG\r\n\x1a\n",
            "jpeg": b"\xff\xd8\xff\xe0", "gif": b"GIF89a", "webp": b"RIFF\x10\x00\x00\x00WEBPVP8 ",
            "zip": b"PK\x03\x04", "gzip": b"\x1f\x8b\x08", "7z": b"7z\xbc\xaf\x27\x1c",
            "xz": b"\xfd7zXZ\x00", "zstd": b"\x28\xb5\x2f\xfd", "onnx": b"\x08\x07\x12\x07pytorch",
            "nul": b"plain text then \x00 a NUL",
        }
        for name, magic in cases.items():
            with self.subTest(name):
                self.git("reset", "-q", "--hard", self.base)
                self.commit({"docs/file.txt": magic + b" padding text\n"})
                self.assertFinding("binary")
        self.git("reset", "-q", "--hard", self.base)
        self.commit({"repro/model.onnx": "text\n"})
        self.assertFinding("binary", "ONNX model")

    def test_secrets(self):
        cases = {
            "pem": "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
            "pgp": "-----BEGIN " + "PGP PRIVATE KEY BLOCK-----",
            "ghp": "gh" + "p_" + "a1" * 18,
            "pat": "github" + "_pat_" + "A" * 30,
            "glpat": "gl" + "pat-" + "x" * 20,
            "slack": "xo" + "xb-" + "1234567890-abc",
            "aws": "AK" + "IA" + "ABCDEFGHIJKLMNOP",
            "bearer": "Authorization: Bear" + "er " + "a" * 24,
        }
        for name, text in cases.items():
            with self.subTest(name):
                self.git("reset", "-q", "--hard", self.base)
                self.commit({"docs/log.txt": f"line one\n{text}\n"})
                self.assertFinding("secret")

    def test_user_name_and_home_path(self):
        self.commit({"docs/a.md": f"owner {USER} ran it\n"})
        self.assertFinding("identity", "user name")
        self.git("reset", "-q", "--hard", self.base)
        self.commit({"docs/a.md": f"log: {self.home}/x/y.txt\n"})
        self.assertFinding("identity", "home path")

    def test_identity_skipped_in_ci_mode(self):
        self.commit({"docs/a.md": f"owner {USER} at {self.home}/x\n"})
        self.assertEqual(self.run_check("--ci")[0], 0)

    def test_git_mail_in_author_field(self):
        self.git("config", "user.email", "person@example.com")
        self.commit({"docs/a.md": "x\n"})
        self.assertFinding("identity", "git user mail")

    def test_size(self):
        self.commit({"docs/big.txt": b"a" * ((1 << 20) + 1)})
        self.assertFinding("size")
        self.assertEqual(self.run_check("--max-bytes", str(2 << 20))[0], 0)

    def test_tree_mode(self):
        self.assertEqual(self.run_check("--tree", stdin=""), (0, ""))
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "untracked.md").write_text(f"{TERM}\n")
        code, out = self.run_check("--tree", stdin="")
        self.assertEqual((code, out), (1, "term: docs/untracked.md:1: term 2\n"))

    def test_bad_input_is_an_error(self):
        self.assertEqual(self.run_check(stdin="only three fields\n")[0], 2)

    def test_message_lines_that_start_like_headers(self):
        for msg in (f"Fix thing\n\ntree {TERM} is the cause", f"Fix thing\n\nparent {TERM} too", f"tree {TERM} subject"):
            with self.subTest(msg):
                self.git("reset", "-q", "--hard", self.base)
                self.commit({"docs/a.md": "clean\n"}, msg=msg)
                self.assertFinding("term", "term 2")

    def test_list_with_byte_order_mark(self):
        self.list.write_text(f"\ufeff{TERM}\n")
        self.commit({"docs/a.md": f"{TERM}\n"})
        self.assertFinding("term", "term 1")

    def test_tag_chain_and_tag_of_blob(self):
        self.git("tag", "-a", "inner", "-m", f"inner {TERM}")
        self.git("tag", "-a", "outer", "-m", "outer", "inner")
        outer = self.git("rev-parse", "outer")
        code, out = self.run_check(stdin=f"refs/tags/outer {outer} refs/tags/outer {ZERO}\n")
        self.assertEqual(code, 1, out)
        blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=self.repo, env=self.env, input=f"{TERM}\n",
                              capture_output=True, text=True).stdout.strip()
        self.git("tag", "-a", "blobtag", "-m", "clean", blob)
        tag = self.git("rev-parse", "blobtag")
        code, out = self.run_check(stdin=f"refs/tags/blobtag {tag} refs/tags/blobtag {ZERO}\n")
        self.assertEqual(code, 1, out)
        self.assertIn("term: tag target ", out)

    def test_tree_mode_from_subdirectory(self):
        (self.repo / "docs").mkdir()
        (self.repo / "secret.md").write_text(f"{TERM}\n")
        code, out = self.run_check("--tree", stdin="", cwd=self.repo / "docs")
        self.assertEqual((code, out), (1, "path: secret.md: outside the allow-list\nterm: secret.md:1: term 2\n"))

    def test_tree_mode_nested_repository(self):
        self.git("init", "-q", str(self.repo / "tools" / "nested"))
        self.assertEqual(self.run_check("--tree", stdin="")[0], 0)

    @unittest.skipIf(os.geteuid() == 0, "root reads mode 000 files")
    def test_error_output_redacted(self):
        (self.repo / "docs").mkdir()
        p = self.repo / "docs" / f"{TERM}.md"
        p.write_text("x\n")
        p.chmod(0)
        try:
            self.assertEqual(self.run_check("--tree", stdin="")[0], 2)  # run_check asserts the term is not printed
        finally:
            p.chmod(0o644)

    def test_overlapping_terms_redacted(self):
        self.list.write_text(f"{TERM}.core\ncore\n")
        self.commit({f"docs/{TERM}.core": "x\n"})
        for seed in range(6):
            with self.subTest(seed=seed):
                code, out = self.run_check(env={**self.env, "PYTHONHASHSEED": str(seed)})
                self.assertEqual(code, 1)

    def push(self, *args):
        return subprocess.run([sys.executable, str(PUSH), *args], cwd=self.repo, env=self.env,
                              capture_output=True, text=True)

    def remote(self):
        bare = Path(self.tmp.name) / "remote.git"
        self.git("init", "-q", "--bare", str(bare), cwd=self.tmp.name)
        self.git("remote", "add", "origin", str(bare))
        return bare

    def remote_head(self, bare):
        r = subprocess.run(["git", "--git-dir", str(bare), "rev-parse", "--verify", "-q", "refs/heads/main"],
                           env=self.env, capture_output=True, text=True)
        return r.stdout.strip() or None

    def test_push_refuses_planted_finding(self):
        bare = self.remote()
        self.commit({"docs/a.md": f"{TERM}\n"})
        r = self.push("origin", "main")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("term: ", r.stdout)
        self.assertNotIn(TERM, r.stdout + r.stderr)
        self.assertIsNone(self.remote_head(bare))

    def test_push_clean_then_checks_only_new_range(self):
        bare = self.remote()
        r = self.push("origin", "main")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.remote_head(bare), self.base)
        self.commit({"notes/x.md": "x\n"})
        self.assertEqual(self.push("origin", "main").returncode, 1)
        self.assertEqual(self.remote_head(bare), self.base)

    def test_installed_hook_blocks_plain_push(self):
        bare = self.remote()
        r = subprocess.run(["sh", str(self.repo / "tools" / "install-hooks")], cwd=self.repo, env=self.env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.commit({"docs/a.md": f"{TERM}\n"})
        r = subprocess.run(["git", "push", "-q", "origin", "main"], cwd=self.repo, env=self.env,
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIsNone(self.remote_head(bare))

    def test_push_takes_no_extra_arguments(self):
        bare = self.remote()
        self.commit({"docs/a.md": f"{TERM}\n"})
        for extra in ("--tree", "--ci", "-h", "--help"):
            with self.subTest(extra):
                self.assertEqual(self.push("origin", "main", extra).returncode, 2)
        self.assertEqual(self.push("origin", "nope").returncode, 2)
        self.assertIsNone(self.remote_head(bare))

    def test_push_does_not_follow_tags(self):
        bare = self.remote()
        self.git("config", "push.followTags", "true")
        self.git("tag", "-a", "v1", "-m", f"tag {TERM}")
        r = self.push("origin", "main")
        self.assertEqual(r.returncode, 0, r.stderr)
        tags = subprocess.run(["git", "--git-dir", str(bare), "tag"], env=self.env, capture_output=True, text=True)
        self.assertEqual(tags.stdout.strip(), "")

    def test_hook_refuses_when_check_is_missing(self):
        bare = self.remote()
        r = subprocess.run(["sh", str(self.repo / "tools" / "install-hooks")], cwd=self.repo, env=self.env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        (self.repo / "tools" / "prepush.py").unlink()
        r = subprocess.run(["git", "push", "-q", "origin", "main"], cwd=self.repo, env=self.env,
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIsNone(self.remote_head(bare))


if __name__ == "__main__":
    unittest.main()

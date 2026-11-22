#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 ProtonCats and contributors
# SPDX-License-Identifier: MIT
"""Refuse a push that carries anything private.

Usage: prepush.py [--ci] [--max-bytes N] < pre-push lines
       prepush.py --tree [--ci] [--max-bytes N]
Stdin lines are what git gives a pre-push hook: <local ref> <local sha> <remote ref> <remote sha>.
Every commit in the pushed range is checked: each added or changed path and blob, each commit
message, author and committer, and the ref names. --tree checks the working tree instead.
Forbidden terms come from the file named by $HALATION_FORBIDDEN and print as numbers only.
--ci: a missing list is reported without failing, and the runner's own user name and home path
are not checked (they describe the CI machine, not the author).
Exit 0 clean, 1 on any finding (one "rule: where: reason" line each), 2 on its own error.
"""
import argparse
import os
import re
import subprocess
import sys

ZERO = "0" * 40
SCAN_HEAD = 8192
ALLOWED_DIRS = ("core/", "profiles/", "patches/", "repro/", "tests/", "docs/", "tools/",
                ".github/", "LICENSES/")
ALLOWED_TOP = {"README.md", "LICENSE", "REUSE.toml", "CHANGELOG.md", "CONTRIBUTING.md",
               "REVIEW.md", "SECURITY.md", "setup", "run", ".gitignore"}
MAGIC = [
    (0, b"MZ", "PE executable"),
    (0, b"\x7fELF", "ELF executable"),
    (0, b"II*\x00", "TIFF or TIFF-based RAW"),
    (0, b"MM\x00*", "TIFF or TIFF-based RAW"),
    (0, b"FUJIFILMCCD-RAW", "Fujifilm RAF"),
    (36, b"acsp", "ICC colour profile"),
    (0, b"\x89PNG\r\n\x1a\n", "PNG image"),
    (0, b"\xff\xd8\xff", "JPEG image"),
    (0, b"GIF87a", "GIF image"),
    (0, b"GIF89a", "GIF image"),
    (0, b"PK\x03\x04", "zip archive"),
    (0, b"PK\x05\x06", "zip archive"),
    (0, b"PK\x07\x08", "zip archive"),
    (0, b"\x1f\x8b", "gzip archive"),
    (0, b"7z\xbc\xaf\x27\x1c", "7z archive"),
    (0, b"\xfd7zXZ\x00", "xz archive"),
    (0, b"\x28\xb5\x2f\xfd", "zstd archive"),
]
SECRETS = [
    (re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"), "private key"),
    (re.compile(r"\bghp_[A-Za-z0-9]{36}"), "GitHub token"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}"), "GitHub token"),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"), "GitLab token"),
    (re.compile(r"\bxox[abp]-[A-Za-z0-9-]{10,}"), "Slack token"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key"),
    (re.compile(r"\bBearer [A-Za-z0-9._~+/=-]{20,}"), "bearer token"),
]


class GateError(Exception):
    pass


def git(*args, data=None):
    r = subprocess.run(["git", *args], input=data, capture_output=True)
    if r.returncode:
        raise GateError(f"git {' '.join(args)}: {r.stderr.decode(errors='replace').strip()}")
    return r.stdout


def tokens(line):
    low = line.lower()
    return set(re.findall(r"[a-z0-9_]+", low)) | {t.strip(".-") for t in re.findall(r"[a-z0-9_][a-z0-9_.-]*", low)}


def load_terms(ci):
    """Return ({term: line number}, finding or None)."""
    path = os.environ.get("HALATION_FORBIDDEN", "")
    words = {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            for n, t in enumerate(f, 1):
                t = t.strip().lower()
                if t and not t.startswith("#"):
                    words.setdefault(t, n)
    except OSError:
        pass
    if words:
        return words, None
    why = "list not run (HALATION_FORBIDDEN unset)" if not path else "list not run (missing or empty)"
    return words, ("list", "$HALATION_FORBIDDEN", why, not ci)


class Gate:
    def __init__(self, ci, max_bytes):
        self.terms, list_finding = load_terms(ci)
        self.findings = []  # (rule, where, reason, fails)
        if list_finding:
            self.findings.append(list_finding)
        self.max_bytes = max_bytes
        self.ident = []  # (kind, value, match by token)
        if not ci:
            for var in ("USER", "LOGNAME"):
                v = os.environ.get(var, "").lower()
                if len(v) > 1 and ("user name", v, True) not in self.ident:
                    self.ident.append(("user name", v, True))
            home = os.environ.get("HOME", "").rstrip("/").lower()
            if home:
                self.ident.append(("home path", home, False))
        try:
            mail = git("config", "user.email").decode().strip().lower()
        except GateError:
            mail = ""
        if mail and "noreply" not in mail:
            self.ident.append(("git user mail", mail, False))

    def add(self, rule, where, reason):
        self.findings.append((rule, where, reason, True))

    def text(self, where, text):
        for i, line in enumerate(text.splitlines(), 1):
            at = f"{where}:{i}"
            toks = tokens(line) if self.terms or self.ident else ()
            for n in sorted({self.terms[t] for t in toks if t in self.terms}):
                self.add("term", at, f"term {n}")
            low = line.lower()
            for kind, value, by_token in self.ident:
                if (value in toks) if by_token else (value in low):
                    self.add("identity", at, kind)
            for rx, kind in SECRETS:
                if rx.search(line):
                    self.add("secret", at, kind)

    def path(self, where, path):
        if not (path in ALLOWED_TOP or path.startswith(ALLOWED_DIRS)):
            self.add("path", where, "outside the allow-list")
        self.text(where, path)

    def blob(self, where, path, data, size):
        if size > self.max_bytes:
            self.add("size", where, f"{size} bytes over {self.max_bytes}")
            return
        head = data[:SCAN_HEAD]
        kind = next((k for off, m, k in MAGIC if head[off:off + len(m)] == m), None)
        if kind is None and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            kind = "WebP image"
        if kind is None and path.lower().endswith(".onnx"):
            kind = "ONNX model"
        if kind is None and len(head) > 2 and head[0] == 0x08 and head[1] < 0x80 and head[2] == 0x12:
            kind = "ONNX model"  # ModelProto: ir_version varint, then producer_name
        if kind is None and b"\x00" in head:
            kind = "binary (NUL byte)"
        if kind:
            self.add("binary", where, kind)
            return
        self.text(where, data.decode("utf-8", errors="replace"))

    def redact(self, where):
        """Paths and ref names can hold the very thing found; never print it."""
        for t in sorted(tokens(where), key=len, reverse=True):  # a dotted term before its parts
            if t in self.terms:
                where = re.sub(rf"(?<![a-z0-9_]){re.escape(t)}(?![a-z0-9_])", f"<term {self.terms[t]}>", where, flags=re.I)
        for kind, value, _ in self.ident:
            where = re.sub(re.escape(value), f"<{kind}>", where, flags=re.I)
        for rx, kind in SECRETS:
            where = rx.sub(f"<{kind}>", where)
        return where

    def report(self):
        for rule, where, reason, _ in self.findings:
            print(f"{rule}: {self.redact(where)}: {reason}")
        return 1 if any(f[3] for f in self.findings) else 0


def read_blobs(shas, max_bytes):
    """Yield (sha, size, data) per blob; data is empty for blobs over max_bytes, never loaded."""
    sizes = {}
    for line in git("cat-file", "--batch-check", data="".join(s + "\n" for s in shas).encode()).decode().splitlines():
        head = line.split()
        if len(head) != 3:
            raise GateError(f"cat-file: {line}")
        sizes[head[0]] = int(head[2])
    small = [s for s in shas if sizes[s] <= max_bytes]
    out = git("cat-file", "--batch", data="".join(s + "\n" for s in small).encode()) if small else b""
    pos = 0
    for sha in small:
        nl = out.index(b"\n", pos)
        yield sha, sizes[sha], out[nl + 1:nl + 1 + sizes[sha]]
        pos = nl + 1 + sizes[sha] + 1
    for sha in shas:
        if sizes[sha] > max_bytes:
            yield sha, sizes[sha], b""


def exists(sha):
    return subprocess.run(["git", "cat-file", "-e", sha], capture_output=True).returncode == 0


def obj_type(sha):
    return git("cat-file", "-t", sha).decode().strip()


def check_push(gate, lines):
    blobs = {}  # sha -> all (where, path) associations
    for raw in lines:
        if not raw.strip():
            continue
        parts = raw.split()
        if len(parts) != 4:
            raise GateError(f"bad input line: {raw.strip()}")
        _, local, remote_ref, remote = parts
        gate.text(f"ref {remote_ref}", remote_ref)  # the only ref name that reaches the remote
        if local == ZERO:
            continue  # deletion: nothing new leaves
        target = local
        while obj_type(target) == "tag":  # tags can point at tags; every one in the chain is pushed
            raw_tag = git("cat-file", "tag", target).decode(errors="replace")
            gate.text(f"tag {target[:12]}", raw_tag)
            target = raw_tag.split("\n", 1)[0].split()[1]  # "object <sha>"
        kind = obj_type(target)
        if kind == "blob":
            blobs.setdefault(target, []).append((f"tag target {target[:12]}", ""))
            continue
        if kind == "tree":
            for entry in git("ls-tree", "-r", "-z", target).split(b"\x00"):
                if entry:
                    info, path = entry.decode(errors="replace").split("\t", 1)
                    mode, _, sha = info.split()
                    where = f"tree {target[:12]}:{path}"
                    gate.path(where, path)
                    if mode != "160000":
                        blobs.setdefault(sha, []).append((where, path))
            continue
        rng = [local] if remote == ZERO or not exists(remote) else [local, "^" + remote]
        for c in git("rev-list", *rng).decode().split():
            raw_commit = git("cat-file", "commit", c).decode(errors="replace")
            header, _, message = raw_commit.partition("\n\n")
            meta = "\n".join(l for l in header.split("\n") if not l.startswith(("tree ", "parent ")))
            gate.text(f"commit {c[:12]}", meta + "\n\n" + message)
            diff = git("diff-tree", "-r", "-z", "-m", "--root", "--no-renames",
                       "--no-commit-id", c).split(b"\x00")
            for i in range(0, len(diff) - 1, 2):
                _, new_mode, _, new_sha, status = diff[i].decode().split(" ")
                path = diff[i + 1].decode(errors="replace")
                if status == "D":
                    continue
                where = f"{c[:12]}:{path}"
                gate.path(where, path)
                if new_mode != "160000":
                    blobs.setdefault(new_sha, []).append((where, path))
    for sha, size, data in read_blobs(list(blobs), gate.max_bytes):
        for where, path in blobs[sha]:
            gate.blob(where, path, data, size)


def check_tree(gate):
    top = git("rev-parse", "--show-toplevel").decode().strip()
    for p in git("-C", top, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split(b"\x00"):
        if not p:
            continue
        path = p.decode(errors="replace")
        full = os.path.join(top, os.fsdecode(p))
        if not os.path.lexists(full):
            continue  # tracked but deleted in the working tree: nothing to push from here
        gate.path(path, path)
        if os.path.islink(full):
            gate.text(path, os.readlink(full))
            continue
        if os.path.isdir(full):
            continue  # a submodule or nested repository: its contents are not pushed with this repo
        try:
            size = os.path.getsize(full)
            with open(full, "rb") as f:
                data = f.read(gate.max_bytes + 1)
        except OSError as e:
            raise GateError(f"{path}: {e.strerror}")
        gate.blob(path, path, data, size)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Refuse a push that carries anything private.")
    ap.add_argument("--tree", action="store_true", help="check the working tree, not a push")
    ap.add_argument("--ci", action="store_true", help="CI mode: a missing list does not fail")
    ap.add_argument("--max-bytes", type=int, default=1 << 20)
    ap.add_argument("hook_args", nargs="*", help="remote name and url, given by git; unused")
    args = ap.parse_args(argv)
    gate = None
    try:
        gate = Gate(args.ci, args.max_bytes)
        if args.tree:
            check_tree(gate)
        else:
            check_push(gate, sys.stdin.read().splitlines())
    except (GateError, OSError, ValueError) as e:
        print(f"error: {gate.redact(str(e)) if gate else e}", file=sys.stderr)
        return 2
    return gate.report()


if __name__ == "__main__":
    sys.exit(main())

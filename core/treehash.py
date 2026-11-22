#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 ProtonCats and contributors
# SPDX-License-Identifier: MIT
"""Prove a directory is unchanged: snapshot it, then compare a later state.

Usage: treehash.py snapshot <dir> <manifest>
       treehash.py compare <dir> <manifest>
Exit 0 unchanged, 1 changed (one line per difference), 2 error.
Links are recorded, never followed; mtime is never compared.
"""
import hashlib
import json
import os
import stat
import sys

CHUNK = 1 << 20
FIELDS = ("type", "mode", "size", "sha256", "target")


def _entry(path, rel):
    st = os.lstat(path)
    e = {"path": rel, "mode": stat.S_IMODE(st.st_mode)}
    if stat.S_ISLNK(st.st_mode):
        e.update(type="link", target=os.readlink(path))
    elif stat.S_ISDIR(st.st_mode):
        e["type"] = "dir"
    elif stat.S_ISREG(st.st_mode):
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while block := f.read(CHUNK):
                h.update(block)
        e.update(type="file", size=st.st_size, sha256=h.hexdigest())
    else:
        e["type"] = "other"
    return e


def snapshot(root):
    """Return entries for root and its descendants, sorted by path."""
    if not os.path.isdir(root) or os.path.islink(root):
        raise NotADirectoryError(f"not a directory: {root}")
    out = [_entry(root, ".")]

    def walk(d, prefix):
        with os.scandir(d) as it:
            names = sorted(x.name for x in it)
        for name in names:
            path = os.path.join(d, name)
            rel = prefix + name
            e = _entry(path, rel)
            out.append(e)
            if e["type"] == "dir":
                walk(path, rel + "/")

    walk(root, "")
    return sorted(out, key=lambda e: e["path"])


def compare(root, entries):
    """Return difference lines between root now and the given entries."""
    old = {e["path"]: e for e in entries}
    new = {e["path"]: e for e in snapshot(root)}
    if "." not in old:  # manifests written before the root entry existed
        new.pop(".", None)
    lines = []
    for p in sorted(old.keys() | new.keys()):
        if p not in old:
            lines.append(f"added {p}")
        elif p not in new:
            lines.append(f"removed {p}")
        else:
            lines += [f"changed {p} {k}" for k in FIELDS if old[p].get(k) != new[p].get(k)]
    return lines


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 3 or argv[0] not in ("snapshot", "compare"):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    cmd, root, manifest = argv
    try:
        if cmd == "snapshot":
            entries = snapshot(root)
            with open(manifest, "w") as f:
                f.writelines(json.dumps(e, sort_keys=True) + "\n" for e in entries)
            return 0
        with open(manifest) as f:
            entries = [json.loads(line) for line in f if line.strip()]
        lines = compare(root, entries)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for line in lines:
        print(line)
    return 1 if lines else 0


if __name__ == "__main__":
    sys.exit(main())

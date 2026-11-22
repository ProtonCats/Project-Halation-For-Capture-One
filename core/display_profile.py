#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 ProtonCats and contributors
# SPDX-License-Identifier: MIT
"""Give a Wine prefix the host's monitor colour profile, stored the way Windows stores display profile associations.

  display_profile.py link --runner DIR --prefix DIR [--profile FILE] [--scope system|user|both] [--dry-run]
  display_profile.py show --prefix DIR

link takes --profile, else colord's default profile for the first display, else the X11 root window's
_ICC_PROFILE. With none it changes nothing, and Windows programs keep the standard sRGB profile. The profile file
goes into the prefix's colour directory and is appended, as the default (last) entry, to the REG_MULTI_SZ value
ICMProfile of every display device in the prefix, under the user's ProfileAssociations\\Display key, with
UsePerUserProfiles set to 1 there so lookups take the user's list (user scope, the default). --scope system writes
the device's class key instead, as Windows does for all users, but Wine rebuilds that key at every start, so the
value lasts only until the next start. Writes go through the runner's own wine reg. One profile is given to every
display; matching host monitors to Wine's display indices is not done.
Link guards the install directory from setup metadata or the bundled profile for manual prefixes.
"""
import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

if __package__:
    from . import prefix as px
else:
    import prefix as px

DISPLAY_CLASS = "{4D36E96E-E325-11CE-BFC1-08002BE10318}"
SYSTEM_ROOT = "HKLM\\System\\CurrentControlSet\\Control\\Class\\" + DISPLAY_CLASS + "\\"
USER_ROOT = "HKCU\\Software\\Microsoft\\Windows NT\\CurrentVersion\\ICM\\ProfileAssociations\\Display\\" + DISPLAY_CLASS + "\\"
COLOUR_DIR = Path("drive_c/windows/system32/spool/drivers/color")


def die(msg, code=2):
    print(f"display_profile.py: {msg}", file=sys.stderr)
    sys.exit(code)


def is_icc(data):
    """An ICC profile carries 'acsp' at byte 36; returns its device class ('mntr' for displays) or None."""
    if len(data) < 128 or data[36:40] != b"acsp":
        return None
    return data[12:16].decode("ascii", "replace")


def colord_profile(text):
    """The file of the first display's default profile in `colormgr get-devices-by-kind display` output."""
    for block in re.split(r"\n\s*\n", text):
        m = re.search(r"^Profile 1:.*?(/[^\n]+?\.ic[cm])\s*$", block, re.M | re.S)
        if m:
            return m.group(1)
    return None


def xprop_profile(text):
    """The bytes of `xprop -root -notype _ICC_PROFILE` output, or None when the atom is unset."""
    m = re.search(r"_ICC_PROFILE\s*=\s*([\d,\s]+)$", text.strip())
    if not m:
        return None
    return bytes(int(v) for v in m.group(1).split(",") if v.strip())


def host_profile(given):
    """(name, bytes, source) of the profile to use, or None."""
    if given:
        p = Path(given)
        if not p.is_file():
            die(f"{given}: no such file")
        return p.name, p.read_bytes(), "--profile"
    if shutil.which("colormgr"):
        r = subprocess.run(["colormgr", "get-devices-by-kind", "display"], capture_output=True, text=True)
        path = colord_profile(r.stdout) if r.returncode == 0 else None
        if path and Path(path).is_file():
            return Path(path).name, Path(path).read_bytes(), "colord"
    if shutil.which("xprop") and os.environ.get("DISPLAY"):
        r = subprocess.run(["xprop", "-root", "-notype", "_ICC_PROFILE"], capture_output=True, text=True)
        data = xprop_profile(r.stdout) if r.returncode == 0 else None
        if data:
            return "x11-root-display.icc", data, "X11 _ICC_PROFILE"
    return None


def display_indices(system_reg):
    """Indices ("0000", ...) of the display device keys saved in a prefix's system.reg."""
    pat = re.compile(r"^\[System\\\\ControlSet001\\\\Control\\\\Class\\\\" + re.escape(DISPLAY_CLASS).replace("\\-", "-")
                     + r"\\\\(\d{4})\]", re.I | re.M)
    return sorted(set(pat.findall(system_reg)))


def section(reg_text, key_path):
    """The lines of one key in a saved registry file; key_path as in the file's headers, single backslashes."""
    head = "[" + key_path.replace("\\", "\\\\") + "]"
    start = reg_text.lower().find(head.lower())
    if start < 0:
        return ""
    end = reg_text.find("\n[", start + 1)
    return reg_text[start:end if end > 0 else len(reg_text)]


def saved_list(reg_text, key_path, value="ICMProfile"):
    """A REG_MULTI_SZ value from a saved registry file (str(7):"a\\0b\\0" form)."""
    m = re.search(r'^"' + value + r'"=str\(7\):"(.*)"$', section(reg_text, key_path), re.M)
    if not m:
        return []
    return [s for s in m.group(1).replace('\\"', '"').replace("\\\\", "\\").split("\\0") if s]


def keys_for(scope, index):
    """(key for wine reg, the same key as headed in the saved registry file) for a display index."""
    key = (SYSTEM_ROOT if scope == "system" else USER_ROOT) + index
    return key, key.split("\\", 1)[1].replace("CurrentControlSet", "ControlSet001")


def with_default(current, name):
    """The association list with name moved to the end, where Windows takes the default from."""
    return [n for n in current if n.lower() != name.lower()] + [name]


def checked_prefix(runner, prefix):
    if __package__:
        from .setup import protected_install
    else:
        from setup import protected_install
    stamp, name = px.checked_version(runner, prefix)
    relative, _ = protected_install(prefix)
    return relative, stamp, name


def wine(runner, prefix, args, timeout=120):
    env = px.environment(runner, prefix)
    env["WINEDEBUG"] = "-all"
    for var in ("WAYLAND_DISPLAY", "DISPLAY"):
        env.pop(var, None)
    try:
        return subprocess.run([str(runner / "files/bin/wine")] + args, env=env, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        die(f"registry step timed out ({timeout}s)")


def link(a):
    runner, prefix = Path(a.runner).resolve(), Path(a.prefix).resolve()
    if not (runner / "files/bin/wine").is_file():
        die(f"{runner} has no files/bin/wine")
    if not (prefix / "system.reg").is_file():
        die(f"{prefix} has no system.reg; create the prefix first")
    relative, stamp, runner_name = checked_prefix(runner, prefix)
    found = host_profile(a.profile)
    if not found:
        print("no host monitor profile (none given, colord has none, X11 root has none); nothing changed")
        return 0
    name, data, source = found
    kind = is_icc(data)
    if not kind:
        die(f"{name} from {source} is not an ICC profile")
    if kind != "mntr":
        print(f"warning: {name} is an ICC '{kind}' profile, not a display ('mntr') profile")
    indices = display_indices((prefix / "system.reg").read_text(errors="replace"))
    if not indices:
        die("no display device keys in the prefix yet; start any program in it once, then link again")
    dest = prefix / COLOUR_DIR / name
    if dest.exists() and dest.read_bytes() != data:
        stem, suffix = os.path.splitext(name)
        name = f"{stem}-{hashlib.sha256(data).hexdigest()[:8]}{suffix}"
        dest = prefix / COLOUR_DIR / name
    if dest.exists() and dest.read_bytes() != data:
        die(f"profile destination contains different bytes: {dest}")
    scopes = {"system": ["system"], "user": ["user"], "both": ["system", "user"]}[a.scope]
    plan = []
    for index in indices:
        for scope in scopes:
            key, saved = keys_for(scope, index)
            reg_file = "system.reg" if scope == "system" else "user.reg"
            current = saved_list((prefix / reg_file).read_text(errors="replace"), saved)
            plan.append((key, with_default(current, name)))
    print(f"profile {name} from {source} -> {dest}")
    for key, names in plan:
        print(f"  {key}\\ICMProfile = {' | '.join(names)}")
    if "user" in scopes:
        for index in indices:
            print(f"  {keys_for('user', index)[0]}\\UsePerUserProfiles = 1")
    if "system" in scopes:
        print("note: Wine rebuilds display class keys at every start; the system-scope value lasts until then")
    if a.dry_run:
        return 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        dest.write_bytes(data)
    if __package__:
        from .setup import guarded
    else:
        from setup import guarded
    def execute():
        px.record_runner(prefix, stamp, runner_name)
        try:
            for key, names in plan:
                r = wine(runner, prefix, ["reg", "add", key, "/v", "ICMProfile", "/t", "REG_MULTI_SZ", "/d", "\\0".join(names), "/f"])
                if r.returncode:
                    die(f"wine reg add {key} failed ({r.returncode}): {r.stdout.strip()} {r.stderr.strip()}"[:400], 1)
            for index in indices if "user" in scopes else []:
                key = keys_for("user", index)[0]
                r = wine(runner, prefix, ["reg", "add", key, "/v", "UsePerUserProfiles", "/t", "REG_DWORD", "/d", "1", "/f"])
                if r.returncode:
                    die(f"wine reg add {key} UsePerUserProfiles failed ({r.returncode})", 1)
        finally:
            env = px.environment(runner, prefix)
            for var in ("WAYLAND_DISPLAY", "DISPLAY"):
                env.pop(var, None)
            try:
                subprocess.run([str(runner / "files/bin/wineserver"), "-w"], env=env, timeout=120)
            except subprocess.TimeoutExpired:
                print("the prefix is still in use; the values are live and are saved when it exits", file=sys.stderr)
        return 0
    return guarded(prefix, relative, execute)


def show(a):
    prefix = Path(a.prefix).resolve()
    system = (prefix / "system.reg").read_text(errors="replace")
    user = (prefix / "user.reg").read_text(errors="replace") if (prefix / "user.reg").exists() else ""
    for index in display_indices(system):
        for label, text in (("system", system), ("user", user)):
            names = saved_list(text, keys_for(label, index)[1])
            print(f"display {index} {label}: {' | '.join(names) if names else '(none)'}")
        per_user = re.search(r'"UsePerUserProfiles"=dword:0*1\b', section(user, keys_for("user", index)[1]))
        print(f"display {index} lookups use: {'user' if per_user else 'system'} scope")
    return 0


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=["link", "show"])
    ap.add_argument("--runner")
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--profile")
    ap.add_argument("--scope", choices=["system", "user", "both"], default="user")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "link" and not a.runner:
        die("link needs --runner")
    return link(a) if a.cmd == "link" else show(a)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

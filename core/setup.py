#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 ProtonCats and contributors
# SPDX-License-Identifier: MIT
"""Apply a local installer and a trusted prerequisite/configuration profile to a prefix.

setup.py apply --profile FILE --runner DIR --prefix DIR --installer FILE [--dry-run]
setup.py finish-netfx48 --runner DIR --prefix DIR
JSON: prerequisites=[{name,url,sha256,args}], registry=[{key,name,type,data}],
dll_overrides={dll: builtin|native|disabled}, windows_version="win10",
environment={NAME: "value"} optionally adds launch variables; uppercase names cannot
override runner or loader settings (see prefix.validate_environment). Values are strings.
installer_args=[], install_dir="drive_c/Program Files/Example". Only install_dir is
required; optional program is relative inside install_dir without "..".
Other collections default empty. Prerequisite args must supply silent switches.
Registry data is a string or integer; null deletes the named value. Registry keys must
start with HKCU\\Software\\Wine\\ or HKCU\\Software\\Microsoft\\Avalon.Graphics.
Prerequisites use HTTPS; redirects must also be listed in the profile.
Profiles cannot supply installer URLs or authentication/licensing directives.
All prerequisites are verified before prefix creation. Only the installer may create
an absent install_dir; no step may change an existing one. Completed profiles are
recorded in .setup.json, the record before in .setup.json.previous. A changed profile is
applied to the existing prefix: its registry values, overrides, Windows version and new
prerequisites; the installer does not run again, and a changed install_dir is refused.
A changed folder is taken for the app's own update, and recorded, only when the recorded
program has a higher version and nothing runs in the prefix. --dry-run writes nothing.
"""
import argparse
from collections import namedtuple
import hashlib
import http.client
import json
import os
import re
import sys
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath

if __package__:
    from . import peversion, prefix as px, runner, treehash, display_profile
else:
    import peversion
    import prefix as px
    import runner
    import treehash
    import display_profile

MARKER = ".setup.json"
PREVIOUS = ".setup.json.previous"
INTERRUPTED = "if an update was interrupted, start the app, let it finish, close the app and run setup again"
INSTALLER_RETRIES = 1
WINDOWS_TIMEOUT = 900
INSTALLER_TIMEOUT = 1800
SERVER_TIMEOUT = 300
AUTH = re.compile(r"licen[cs]|activat|sign[-_ ]?in|log[-_ ]?in|product[-_ ]?key", re.I)
REGISTRY_PREFIXES = ("hkcu\\software\\wine\\", "hkcu\\software\\microsoft\\avalon.graphics")
NETFX48_BUILTINS = ("uiautomationcore", "dxva2", "evr")


class Refused(ValueError):
    """Why a finished prefix is left as it is; never a reason to remove it."""


# What a finished prefix holds and needs: record is its marker, tree its install folder now, version the new one of an
# app that updated itself, outdated whether apply has something to write.
State = namedtuple("State", "record tree version outdated")


def die(message):
    raise SystemExit(f"setup.py: {message}")


def refuse(prefix, reason):
    raise Refused(f"{prefix} was left as it is: {reason}. Keep it: it holds the installed app, its settings and its activation")


def text(value):
    if not isinstance(value, str) or "\0" in value:
        raise ValueError("profile strings must contain no NUL")
    return value


def arguments(values):
    if not isinstance(values, list):
        raise ValueError("arguments must be a list")
    for value in values:
        if AUTH.search(text(value)):
            raise ValueError("authentication and licensing directives are forbidden")
    return values


def allowed_key(key):
    key = key.lower()
    return key.startswith(REGISTRY_PREFIXES[0]) or key == REGISTRY_PREFIXES[1] \
        or key.startswith(REGISTRY_PREFIXES[1] + "\\")


def validate_environment(values):
    return px.validate_environment(values)


def load_profile(raw):
    profile = json.loads(raw)
    fields = {"prerequisites", "registry", "dll_overrides", "windows_version", "installer_args", "install_dir", "environment", "program"}
    if not isinstance(profile, dict) or set(profile) - fields:
        raise ValueError("unknown profile fields; installer downloads are not supported")
    for field, default in (("prerequisites", []), ("registry", []), ("dll_overrides", {}),
                           ("windows_version", "win10"), ("installer_args", [])):
        profile.setdefault(field, default)
    arguments(profile["installer_args"])
    validate_environment(profile.get("environment", {}))
    if not re.fullmatch(r"win(?:[0-9]+|xp)", text(profile["windows_version"])):
        raise ValueError("invalid Windows version")
    for field in ("registry", "prerequisites"):
        if not isinstance(profile[field], list):
            raise ValueError(f"{field} must be a list")
    names = set()
    for item in profile["prerequisites"]:
        if not isinstance(item, dict) or set(item) != {"name", "url", "sha256", "args"}:
            raise ValueError("invalid prerequisite fields")
        name = text(item["name"])
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) or name in names:
            raise ValueError("invalid or duplicate prerequisite name")
        if AUTH.search(name):
            raise ValueError("authentication and licensing directives are forbidden")
        names.add(name)
        url = urllib.parse.urlsplit(text(item["url"]))
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment:
            raise ValueError("prerequisite URLs must be HTTPS without credentials or fragments")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", text(item["sha256"])):
            raise ValueError("each prerequisite needs a SHA-256")
        item["sha256"] = item["sha256"].lower()
        if not arguments(item["args"]):
            raise ValueError("prerequisite args must include silent switches")
    for item in profile["registry"]:
        if not isinstance(item, dict) or set(item) != {"key", "name", "type", "data"}:
            raise ValueError("invalid registry fields")
        key = text(item["key"])
        if not allowed_key(key):
            raise ValueError(f"registry key is outside the allowlist: {key}")
        if AUTH.search(key + "\\" + text(item["name"])):
            raise ValueError("authentication and licensing directives are forbidden")
        if item["type"] not in {"REG_SZ", "REG_EXPAND_SZ", "REG_MULTI_SZ", "REG_DWORD", "REG_QWORD", "REG_BINARY"}:
            raise ValueError("invalid registry type")
        if item["data"] is not None and type(item["data"]) not in (str, int):
            raise ValueError("registry data must be text, an integer or null")
        if isinstance(item["data"], str):
            text(item["data"])
    if not isinstance(profile["dll_overrides"], dict):
        raise ValueError("dll_overrides must be an object")
    for dll, mode in profile["dll_overrides"].items():
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", dll) or mode not in {"builtin", "native", "disabled"}:
            raise ValueError("invalid DLL override")
        if AUTH.search(dll):
            raise ValueError("authentication and licensing directives are forbidden")
    if "program" in profile:
        value = text(profile["program"])
        path = PurePosixPath(value)
        if not path.parts or path.is_absolute() or ".." in path.parts or "\\" in value or ":" in value:
            raise ValueError("program must be relative inside install_dir without '..'")
    text(profile["install_dir"])
    return profile


def install_path(prefix, relative):
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0].startswith(".setup"):
        raise ValueError("install_dir must be a relative directory inside the prefix")
    target = prefix / relative
    if target.is_symlink() or not target.resolve().is_relative_to(prefix) or target.resolve() == prefix:
        raise ValueError("install_dir must stay inside the prefix without a root symlink")
    return target


def snapshot(path):
    if not os.path.lexists(path):
        return None
    return {"entries": treehash.snapshot(path)}


def unchanged(path, before):
    if before is None:
        return not os.path.lexists(path)
    return path.is_dir() and not path.is_symlink() and not treehash.compare(path, before["entries"])


def replace_file(path, data):
    """Put data at path in one step that survives a power cut."""
    fd, temp = tempfile.mkstemp(prefix=".setup-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def write_marker(prefix, record):
    """Replace the marker and keep the one before it beside it."""
    marker = prefix / MARKER
    if marker.is_file() and not marker.is_symlink():
        replace_file(prefix / PREVIOUS, marker.read_bytes())
    replace_file(marker, (json.dumps(record) + "\n").encode())
    folder = os.open(prefix, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(folder)
    finally:
        os.close(folder)


def read_marker(prefix):
    marker = prefix / MARKER
    try:
        record = json.loads(marker.read_bytes())
        if marker.is_symlink() or not isinstance(record["profile_sha256"], str) \
                or not isinstance(record["install_tree"]["entries"], list) \
                or not isinstance(record.get("refused", {}), dict) or not isinstance(record.get("prerequisites", []), list):
            raise ValueError("unexpected content")
        install_path(prefix, record["install_dir"])
        peversion.parts(record.get("program_version") or "0")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        before = f"; the one before it is in {PREVIOUS}" if (prefix / PREVIOUS).is_file() else ""
        refuse(prefix, f"its record {MARKER} cannot be read{before}")
    return record


def by_path(tree):
    return {entry["path"]: entry for entry in tree["entries"]}


def differing(before, now):
    """Paths that differ between two snapshots; one from before the root entry existed lacks it."""
    before, now = by_path(before), by_path(now)
    if "." not in before:
        now.pop(".", None)
    return sorted(path for path in before.keys() | now.keys() if before.get(path) != now.get(path))


def bound_version(install, relative, tree):
    """The program's version, read from the very bytes the snapshot records; None otherwise."""
    version, digest = peversion.program_version(install, relative)
    entry = by_path(tree).get(str(PurePosixPath(relative)), {})
    return version if digest and entry.get("sha256") == digest else None


def with_version(record, install, profile):
    """The record with the profile's program and its version; a program without one changes nothing."""
    version = bound_version(install, profile["program"], record["install_tree"]) if profile.get("program") else None
    return {**record, "program": profile["program"], "program_version": version} if version else record


def refuse_running(prefix):
    process = px.in_use(prefix)
    if process:
        refuse(prefix, f"a program still runs in it ({process}); close the app and run setup again")


def updated(prefix, record, program, tree, changed):
    """The new version of an app whose changed folder is its own update; Refused says why it is not one."""
    refuse_running(prefix)
    before, now = by_path(record["install_tree"]), by_path(tree)
    ours = [path for path, entry in record.get("refused", {}).items() if now.get(path) == entry]
    if ours:
        refuse(prefix, f"{ours[0]} is still as a Halation step left it, so its folder is not taken for an update")
    if not record.get("program") and not program:
        refuse(prefix, "its install folder changed and the profile names no program to judge that by")
    relative, old = str(PurePosixPath(record.get("program") or program)), record.get("program_version")
    if relative not in now:
        refuse(prefix, f"its program {relative} is missing; {INTERRUPTED}")
    new = bound_version(install_path(prefix, record["install_dir"]), relative, tree)
    if not new:
        refuse(prefix, f"the version of its program cannot be read; {INTERRUPTED}")
    if old is None:
        if relative not in before or before[relative].get("sha256") == now[relative]["sha256"]:
            refuse(prefix, f"its install folder changed, but its program did not (for example {changed[0]}); {INTERRUPTED}")
    elif peversion.parts(new) < peversion.parts(old):
        refuse(prefix, f"its program went back from {old} to {new}")
    elif new == old:
        refuse(prefix, f"{len(changed)} of its files changed while the version of its program stayed {old} "
                       f"(for example {changed[0]}); {INTERRUPTED}")
    return new


def examine(prefix, profile, digest, runner_dir):
    """What a finished prefix needs, without writing anything; Refused when setup must leave it as it is."""
    record = read_marker(prefix)
    if record["install_dir"] != profile["install_dir"]:
        refuse(prefix, f"the profile expects the app in {profile['install_dir']}, but it is installed in "
                       f"{record['install_dir']}, and an installed app is never moved")
    if not px.ready(prefix):
        refuse(prefix, "its registry (system.reg) is missing or incomplete")
    stamp, name = px.runner_version(runner_dir)
    try:
        history = px.runner_history(prefix)
    except SystemExit:
        refuse(prefix, f"its runner record {px.MARKER} cannot be read")
    if history and history["stamp"] > stamp:
        refuse(prefix, f"it was last used by {history['runner']}, newer than {name}; "
                       "runners only move forward, so update Halation")
    install, program = install_path(prefix, record["install_dir"]), profile.get("program")
    try:
        tree = snapshot(install)
    except FileNotFoundError as error:
        refuse(prefix, f"a file vanished while its install folder was read ({error.filename}); let the app finish, "
                       "close it and run setup again")
    except OSError as error:
        refuse(prefix, f"its install folder cannot be read ({error})")
    if tree is None:
        refuse(prefix, "its install folder is missing")
    changed = differing(record["install_tree"], tree)
    version = updated(prefix, record, program, tree, changed) if changed else None
    if program and not ((install / program).is_file() and (install / program).resolve().is_relative_to(install.resolve())):
        refuse(prefix, f"the program {program} is not in its install folder")
    same = record["profile_sha256"] == digest
    return State(record, tree, version, bool(version) or not same or filled(record, install, profile) != record)


def filled(record, install, profile):
    """The marker of an applied profile and an unchanged folder, with what earlier versions did not record."""
    record = {key: value for key, value in record.items() if key != "refused"}
    if not record.get("program_version"):
        record = with_version(record, install, profile)
    return {"shape": shape(profile), "prerequisites": recorded(profile["prerequisites"]), **record}


def guarded(prefix, relative, action, allow_creation=False, record=None):
    """Run a step and refuse any change of the install folder. With a record its tree is what the folder must be, and
    a change is remembered there, so that the next setup does not take it for the app's own update."""
    path = install_path(prefix, relative)
    before = record["install_tree"] if record else snapshot(path)
    if record and not unchanged(path, before):
        refuse(prefix, "its install folder changed after it was checked; run setup again")
    try:
        return action()
    finally:
        path = install_path(prefix, relative)
        if not (allow_creation and before is None) and not unchanged(path, before):
            note = ""
            if record:
                try:
                    now = snapshot(path) or {"entries": []}
                    left = by_path(now)
                    record["refused"] = {**record.get("refused", {}), **{p: left.get(p) for p in differing(before, now)}}
                    write_marker(prefix, record)
                except OSError as error:
                    note = f", and setup could not note it: {error}"
            die(f"install folder changed during a Windows step{note}")


class ProfileRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed):
        self.allowed = allowed

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if newurl not in self.allowed:
            die("download redirect is not listed in the profile")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def prerequisites(items, cache):
    if cache.is_symlink() or (cache.exists() and not cache.is_dir()):
        die("unsafe prerequisite cache")
    paths = [cache / (item["sha256"] + "-" + item["name"]) for item in items]
    missing = []
    for item, path in zip(items, paths):
        if path.is_symlink():
            die("unsafe cached prerequisite")
        if path.exists():
            if runner.sha256(path) != item["sha256"]:
                path.unlink()
                die(f"checksum mismatch for {item['name']}")
        else:
            missing.append((item, path))
    if not missing:
        return paths
    created = []
    parent = cache.parent
    while not parent.exists():
        created.append(parent)
        parent = parent.parent
    try:
        for directory in reversed(created):
            directory.mkdir()
        with tempfile.TemporaryDirectory(prefix=".setup-download-", dir=cache.parent) as tmp:
            opener = urllib.request.build_opener(ProfileRedirect({item["url"] for item in items}))
            staged = []
            for item, path in missing:
                partial = Path(tmp) / path.name
                with opener.open(item["url"], timeout=60) as response, partial.open("wb") as out:
                    while True:
                        block = response.read(1 << 20)
                        if not block:
                            break
                        out.write(block)
                if runner.sha256(partial) != item["sha256"]:
                    partial.unlink()
                    die(f"checksum mismatch for {item['name']}")
                staged.append((partial, path))
            cache.mkdir(exist_ok=True)
            for partial, path in staged:
                os.replace(partial, path)
        return paths
    finally:
        for directory in created:
            if directory.exists() and not any(directory.iterdir()):
                directory.rmdir()


def windows(runner_dir, prefix, relative, action, allow_creation=False, record=None):
    """Guard a batch, bound every command, and wait for its server once."""
    env = px.environment(runner_dir, prefix)
    log = prefix / ".setup.log"
    if log.is_symlink():
        die("unsafe setup log")
    def step(args, check=True, timeout=WINDOWS_TIMEOUT, missing_ok=False):
        command = [str(runner_dir / "files/bin/wine")] + args
        if missing_ok:
            try:
                result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                die(f"Windows step timed out ({timeout}s): {' '.join(args)}")
            output = (result.stdout or "") + (result.stderr or "")
            with log.open("a") as stream:
                stream.write(output)
            diagnostics = [line.strip() for line in output.splitlines()
                           if line.strip().startswith(("reg:", "ERROR:"))]
            missing_messages = (
                "reg: Unable to find the specified registry value",
                "reg: Unable to find the specified registry key",
                "ERROR: The system was unable to find the specified registry key or value.")
            missing = bool(diagnostics) and all(line in missing_messages for line in diagnostics)
            if result.returncode and not (result.returncode == 1 and missing):
                die(f"registry delete failed ({result.returncode}); see {log}")
            return 0
        return runner.run(command, env=env, log=log, check=check, timeout=timeout)
    def execute():
        try:
            return action(step)
        finally:
            runner.run([str(runner_dir / "files/bin/wineserver"), "-w"], env=env, log=log, timeout=SERVER_TIMEOUT)
    return guarded(prefix, relative, execute, allow_creation, record)


def protected_install(prefix):
    """Use recorded install metadata, or the bundled profile for a manual prefix."""
    marker = prefix / MARKER
    if marker.is_symlink():
        die("unsafe setup metadata location")
    if not marker.exists():
        bundled = Path(__file__).resolve().parents[1] / "profiles/captureone/profile.json"
        relative, recorded = load_profile(bundled.read_bytes())["install_dir"], {}
    else:
        try:
            recorded = json.loads(marker.read_bytes())
            if not isinstance(recorded, dict) or not isinstance(recorded.get("install_dir"), str):
                raise ValueError("install_dir must be text")
            relative = recorded["install_dir"]
        except (OSError, ValueError, TypeError) as error:
            die(f"invalid setup metadata: {error}")
    install_path(prefix, relative)
    return relative, recorded


def configure(profile, items, paths, begin=lambda: None, done=lambda item: None):
    """The Windows steps that shape a prefix: the given prerequisites, then version, overrides and registry values."""
    def steps(step):
        begin()
        for item, path in zip(items, paths):
            if runner.sha256(path) != item["sha256"]:
                path.unlink()
                die(f"checksum mismatch for {item['name']}")
            step(program(path, item["args"]))
            done(item)
        step(["winecfg", "-v", profile["windows_version"]])
        for dll, mode in profile["dll_overrides"].items():
            step(["reg", "add", "HKCU\\Software\\Wine\\DllOverrides", "/v", dll,
                                                  "/t", "REG_SZ", "/d", "" if mode == "disabled" else mode, "/f"])
        for item in profile["registry"]:
            value = ["/v", item["name"]] if item["name"] else ["/ve"]
            command = ["reg", "delete" if item["data"] is None else "add", item["key"]] + value
            if item["data"] is not None:
                command += ["/t", item["type"], "/d", str(item["data"])]
            step(command + ["/f"], missing_ok=item["data"] is None)
    return steps


def announce(profile, items):
    for item in items:
        print(f"verify {item['url']} SHA-256 {item['sha256']}; run {item['name']} {item['args']}")
    for dll, mode in profile["dll_overrides"].items():
        print(f"override {dll}: {mode}")
    for item in profile["registry"]:
        print(f"registry {item['key']} {item['name']}: {item['type']} {item['data']!r}")


def shape(profile):
    """A digest of the settings that live in the prefix; launch settings are read at each run instead."""
    return hashlib.sha256(json.dumps([profile["windows_version"], profile["dll_overrides"], profile["registry"]],
                                     sort_keys=True).encode()).hexdigest()


def recorded(items, before=()):
    """What a marker says of prerequisites that ran: the ones before, then each of items once."""
    kept = [{"name": item["name"], "sha256": item["sha256"]} for item in before]
    for item in items:
        entry = {"name": item["name"], "sha256": item["sha256"]}
        if entry not in kept:
            kept.append(entry)
    return kept


def applied_record(record, profile, digest):
    record = {key: value for key, value in record.items() if key != "refused"}
    return {**record, "profile_sha256": digest, "shape": shape(profile),
            "prerequisites": recorded(profile["prerequisites"], record.get("prerequisites", []))}


def bring_up_to_date(args, profile, digest, runner_dir, prefix, cache):
    """Record an app that updated itself, then apply what changed in the profile."""
    state = examine(prefix, profile, digest, runner_dir)
    record, install = state.record, install_path(prefix, state.record["install_dir"])
    if state.version:
        old, program = record.get("program_version") or "an earlier version", record.get("program") or profile["program"]
        files = [path for path in differing(record["install_tree"], state.tree)
                 if (by_path(state.tree).get(path) or by_path(record["install_tree"])[path])["type"] != "dir"]
        record = {key: value for key, value in record.items() if key != "refused"}
        record.update(install_tree=state.tree, program=program, program_version=state.version)
        if not args.dry_run:
            try:
                again = snapshot(install)
            except OSError:
                again = None
            if again != state.tree or bound_version(install, program, again) != state.version:
                refuse(prefix, "its install folder is still changing; let the app finish, close it and run setup again")
            refuse_running(prefix)
            write_marker(prefix, record)
        print(f"the app updated itself: {old} to {state.version} ({len(files)} files changed); its folder "
              f"{'would be' if args.dry_run else 'is'} recorded as the known-good one", flush=True)
    if record["profile_sha256"] != digest:
        return update_profile(args, profile, digest, record, cache)
    complete = filled(record, install, profile)
    if complete != record and not args.dry_run:
        write_marker(prefix, complete)
    print("profile already applied" + ("" if state.outdated else "; nothing changed"))
    return 0


def update_profile(args, profile, digest, record, cache):
    """Apply a changed profile to a prefix whose install folder was just found to be the recorded one."""
    runner_dir, prefix, stamp, name = px.checked_runner(args)
    relative, install = record["install_dir"], install_path(prefix, record["install_dir"])
    ran = recorded(record.get("prerequisites", []))
    items = [item for item in profile["prerequisites"] if recorded([item])[0] not in ran]
    steps = items or record.get("shape") != shape(profile)
    if steps:
        print(f"update prefix {prefix}; set Windows version {profile['windows_version']}")
        announce(profile, items)
    else:
        print(f"update prefix {prefix}; only launch settings changed")
    if args.dry_run:
        return 0
    refuse_running(prefix)
    record = dict(record)
    if steps:
        paths = prerequisites(items, cache)
        refuse_running(prefix)
        def begin():
            px.record_runner(prefix, stamp, name)
            record.update(profile_sha256="", shape=None)
            write_marker(prefix, record)
        def done(item):
            record["prerequisites"] = recorded([item], record.get("prerequisites", []))
            write_marker(prefix, record)
        windows(runner_dir, prefix, relative, configure(profile, items, paths, begin, done), record=record)
    if not unchanged(install, record["install_tree"]):
        refuse(prefix, "its install folder changed after it was checked; run setup again")
    write_marker(prefix, with_version(applied_record(record, profile, digest), install, profile))
    print("profile updated in place; the app and its folder were not touched")
    return 0


def program(path, args):
    return (["msiexec", "/i", str(path)] if path.suffix.lower() == ".msi" else [str(path)]) + args


def apply(args):
    raw = Path(args.profile).read_bytes()
    profile, digest = load_profile(raw), hashlib.sha256(raw).hexdigest()
    installer = Path(args.installer).resolve()
    if not installer.is_file():
        die("a local installer file is required")
    installer_digest = runner.sha256(installer)
    if any(item["sha256"] == installer_digest for item in profile["prerequisites"]):
        die("the local installer cannot be downloaded as a prerequisite")
    runner_dir, prefix, _, _ = px.checked_runner(args)
    if not (runner_dir / "files/bin/wine").is_file():
        die("runner has no files/bin/wine")
    relative = profile["install_dir"]
    install = install_path(prefix, relative)
    marker, cache = prefix / MARKER, prefix.parent / ".setup-cache"
    if marker.is_symlink() or cache == prefix:
        die("unsafe setup metadata location")
    if os.path.lexists(marker):
        return bring_up_to_date(args, profile, digest, runner_dir, prefix, cache)
    print(f"create prefix {prefix}; set Windows version {profile['windows_version']}")
    announce(profile, profile["prerequisites"])
    print(f"run local installer {installer} {profile['installer_args']}; snapshot {install}")
    if args.dry_run:
        return 0
    paths = prerequisites(profile["prerequisites"], cache)
    guarded(prefix, relative, lambda: px.create(args))
    windows(runner_dir, prefix, relative, configure(profile, profile["prerequisites"], paths))
    codes = []
    command = program(installer, profile["installer_args"])
    for attempt in range(INSTALLER_RETRIES + 1):
        start = time.monotonic()
        code = windows(runner_dir, prefix, relative,
                       lambda step: step(command, check=False, timeout=INSTALLER_TIMEOUT), allow_creation=True)
        codes.append(code)
        print(f"installer attempt {attempt + 1}: exit {code} ({time.monotonic() - start:.1f}s)", flush=True)
        if code == 0:
            break
        if os.path.lexists(install) or attempt == INSTALLER_RETRIES:
            die(f"installer failed; exit codes: {', '.join(map(str, codes))}; "
                f"remove the prefix folder {prefix} and run setup again")
        time.sleep(10)
    final = snapshot(install_path(prefix, relative))
    if final is None:
        die("installer did not create install_dir")
    record = applied_record({"install_dir": relative, "install_tree": final}, profile, digest)
    write_marker(prefix, with_version(record, install, profile))
    print("profile applied; install folder recorded")
    return 0


def registry_values(prefix, key):
    saved = prefix / "user.reg"
    section = display_profile.section(saved.read_text(errors="replace"), key) if saved.is_file() else ""
    return {name.lower(): value for name, value in re.findall(r'^"([^"]+)"="([^"]*)"\s*$', section, re.M)}


def finish_netfx48(args):
    runner_dir, prefix, stamp, name = px.checked_runner(args)
    library = prefix / "drive_c/windows/Microsoft.NET/Framework64/v4.0.30319/mscorlib.dll"
    overrides = registry_values(prefix, r"Software\Wine\DllOverrides")
    if not px.ready(prefix) or not library.is_file() or not library.resolve().is_relative_to(prefix) \
            or overrides.get("mscoree", "").lower() != "native":
        die("finish-netfx48 requires an existing Framework64 runtime with mscorlib.dll and a native mscoree override")
    relative, marked = protected_install(prefix)
    install = install_path(prefix, relative)
    if marked and not unchanged(install, marked["install_tree"]):
        refuse(prefix, "its install folder changed since setup recorded it; run ./setup first")
    version = registry_values(prefix, r"Software\Wine").get("version")
    if version == "win10" and all(overrides.get(dll) == "builtin" for dll in NETFX48_BUILTINS):
        print("plugin runtime configuration already applied; nothing changed")
        return 0
    if not (runner_dir / "files/bin/wine").is_file():
        die("runner has no files/bin/wine")
    px.record_runner(prefix, stamp, name)
    def builtins(step):
        step(["winecfg", "-v", "win10"])
        for dll in NETFX48_BUILTINS:
            step(["reg", "add", "HKCU\\Software\\Wine\\DllOverrides", "/v", dll,
                                                  "/t", "REG_SZ", "/d", "builtin", "/f"])
    windows(runner_dir, prefix, relative, builtins, record=marked or None)
    print("plugin runtime configuration applied")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["apply", "finish-netfx48"])
    for name in ("runner", "prefix"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--profile")
    parser.add_argument("--installer")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "finish-netfx48":
            if args.profile or args.installer or args.dry_run:
                die("finish-netfx48 accepts only --runner and --prefix")
            return finish_netfx48(args)
        if not args.profile or not args.installer:
            die("apply needs --profile and --installer")
        return apply(args)
    except subprocess.TimeoutExpired as error:
        die(f"Windows step timed out ({error.timeout}s): {error.cmd}")
    except (OSError, ValueError, TypeError, KeyError, http.client.HTTPException) as ex:
        die(str(ex))


if __name__ == "__main__":
    sys.exit(main())

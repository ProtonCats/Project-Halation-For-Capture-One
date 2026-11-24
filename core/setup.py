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
an absent install_dir; every existing install tree is immutable. Completed profiles
are recorded in .setup.json; changing a completed profile requires a fresh prefix.
"""
import argparse
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
INSTALLER_RETRIES = 1
WINDOWS_TIMEOUT = 900
INSTALLER_TIMEOUT = 1800
SERVER_TIMEOUT = 300
AUTH = re.compile(r"licen[cs]|activat|sign[-_ ]?in|log[-_ ]?in|product[-_ ]?key", re.I)
REGISTRY_PREFIXES = ("hkcu\\software\\wine\\", "hkcu\\software\\microsoft\\avalon.graphics")
NETFX48_BUILTINS = ("uiautomationcore", "dxva2", "evr")


def die(message):
    raise SystemExit(f"setup.py: {message}")


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


def write_marker(prefix, record):
    fd, temp = tempfile.mkstemp(prefix=".setup-", dir=prefix)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(record, out)
            out.write("\n")
        os.replace(temp, prefix / MARKER)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def program_version(install, relative):
    path = install / relative
    if path.is_symlink() or not path.resolve().is_relative_to(install.resolve()):
        return None
    return peversion.file_version(path)


def accept_update(prefix, record, relative):
    """Record the folder of an app that updated itself; False when nothing shows that it did.

    The program's own version must differ from the recorded one. A marker from before versions were recorded counts
    only when the program file itself changed.
    """
    install = install_path(prefix, record["install_dir"])
    if not relative or not install.is_dir():
        return False
    new, old = program_version(install, relative), record.get("program_version")
    if not new or new == old:
        return False
    if old is None:
        entry = next((e for e in record["install_tree"]["entries"] if e["path"] == relative), None)
        if entry is None or entry.get("sha256") == runner.sha256(install / relative):
            return False
    tree = snapshot(install)
    before, after = ({e["path"]: e for e in entries} for entries in (record["install_tree"]["entries"], tree["entries"]))
    count = sum(before.get(path) != after.get(path) for path in before.keys() | after.keys()
                if (before.get(path) or after[path])["type"] != "dir")
    write_marker(prefix, {**record, "install_tree": tree, "program": relative, "program_version": new})
    print(f"the app updated itself: {old or 'an earlier version'} to {new} ({count} files changed); "
          "its folder is recorded as the known-good one", flush=True)
    return True


def guarded(prefix, relative, action, allow_creation=False):
    path = install_path(prefix, relative)
    before = snapshot(path)
    try:
        return action()
    finally:
        path = install_path(prefix, relative)
        if not (allow_creation and before is None) and not unchanged(path, before):
            die("install folder changed during a Windows step")


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


def windows(runner_dir, prefix, relative, action, allow_creation=False):
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
    return guarded(prefix, relative, execute, allow_creation)


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
    if marker.exists():
        old = json.loads(marker.read_text())
        if old["profile_sha256"] != digest:
            die("a different profile is already applied; use a fresh prefix")
        if not px.ready(prefix) or not (unchanged(install, old["install_tree"])
                                        or accept_update(prefix, old, profile.get("program"))):
            die("recorded install folder changed or prefix is not ready")
        print("profile already applied; nothing changed")
        return 0
    for item in profile["prerequisites"]:
        print(f"verify {item['url']} SHA-256 {item['sha256']}; run {item['name']} {item['args']}")
    print(f"create prefix {prefix}; set Windows version {profile['windows_version']}")
    for dll, mode in profile["dll_overrides"].items():
        print(f"override {dll}: {mode}")
    for item in profile["registry"]:
        print(f"registry {item['key']} {item['name']}: {item['type']} {item['data']!r}")
    print(f"run local installer {installer} {profile['installer_args']}; snapshot {install}")
    if args.dry_run:
        return 0
    paths = prerequisites(profile["prerequisites"], cache)
    guarded(prefix, relative, lambda: px.create(args))
    def configure(step):
        for item, path in zip(profile["prerequisites"], paths):
            if runner.sha256(path) != item["sha256"]:
                path.unlink()
                die(f"checksum mismatch for {item['name']}")
            step(program(path, item["args"]))
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
    windows(runner_dir, prefix, relative, configure)
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
    record = {"profile_sha256": digest, "install_dir": relative, "install_tree": final}
    if profile.get("program"):
        record.update(program=profile["program"], program_version=program_version(install, profile["program"]))
    write_marker(prefix, record)
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
    relative, recorded = protected_install(prefix)
    install = install_path(prefix, relative)
    if recorded and not (unchanged(install, recorded["install_tree"])
                         or accept_update(prefix, recorded, recorded.get("program"))):
        die("recorded install folder changed")
    version = registry_values(prefix, r"Software\Wine").get("version")
    if version == "win10" and all(overrides.get(dll) == "builtin" for dll in NETFX48_BUILTINS):
        print("plugin runtime configuration already applied; nothing changed")
        return 0
    if not (runner_dir / "files/bin/wine").is_file():
        die("runner has no files/bin/wine")
    px.record_runner(prefix, stamp, name)
    def configure(step):
        step(["winecfg", "-v", "win10"])
        for dll in NETFX48_BUILTINS:
            step(["reg", "add", "HKCU\\Software\\Wine\\DllOverrides", "/v", dll,
                                                  "/t", "REG_SZ", "/d", "builtin", "/f"])
    windows(runner_dir, prefix, relative, configure)
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

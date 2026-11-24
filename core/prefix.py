#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 ProtonCats and contributors
# SPDX-License-Identifier: MIT
"""Create and use one persistent Wine prefix with a Proton runner, through umu.

  prefix.py create --runner DIR --prefix DIR
  prefix.py run    --runner DIR --prefix DIR [--profile FILE] [--watch DIR] [--wined3d] -- PROGRAM [ARGS...]
  prefix.py status --prefix DIR

The prefix records the runner that last used it; a runner older than that is refused, because an older Proton
rewrites a newer prefix. A prefix counts as ready once its registry holds the Program Files path; Proton's own
creation exits 1 by design, so exit codes decide nothing there. run starts the program with umu-run, the given
runner and no runtime update, on X11; --watch hashes a directory before and after with treehash.py and fails the
run when anything in it changed.
With --profile, install_dir inside the prefix is watched by default; --watch overrides it.
"""
import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

if __package__:
    from . import peversion
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import peversion

HERE = Path(__file__).resolve().parent
MARKER = ".runner.json"


def die(msg, code=2):
    print(f"prefix.py: {msg}", file=sys.stderr)
    sys.exit(code)


def runner_version(runner):
    """(build stamp, name) from a Proton runner's version file, e.g. "1793687165 GE-Proton11-12"."""
    path = Path(runner) / "version"
    if not path.is_file():
        die(f"{runner} has no version file; not a Proton runner")
    parts = path.read_text().split()
    if len(parts) < 2 or not parts[0].isdigit():
        die(f"{path}: unexpected content {path.read_text().strip()!r}")
    return int(parts[0]), parts[1]


def runner_history(prefix):
    """Read valid history; only an absent marker means a new prefix."""
    marker = Path(prefix) / MARKER
    if marker.is_symlink():
        die("runner marker must not be a symlink")
    try:
        raw = marker.read_text()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as error:
        die(f"invalid runner marker: {error}")
    try:
        history = json.loads(raw)
        if not isinstance(history, dict) or type(history.get("stamp")) is not int \
                or history["stamp"] < 0 or not isinstance(history.get("runner"), str):
            raise ValueError("expected an object with integer stamp >= 0 and string runner")
    except (ValueError, UnicodeError) as error:
        die(f"invalid runner marker: {error}")
    return history


def forward_ok(prefix, stamp):
    """False when the prefix was last used by a newer runner than stamp."""
    history = runner_history(prefix)
    return history is None or history["stamp"] <= stamp


def record_runner(prefix, stamp, name):
    marker = Path(prefix) / MARKER
    if marker.is_symlink():
        die("runner marker must not be a symlink")
    marker.parent.mkdir(parents=True, exist_ok=True)
    lock_path = marker.with_name(MARKER + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if marker.is_symlink():
            die("runner marker must not be a symlink")
        old = runner_history(prefix)
        if old is not None and old["stamp"] > stamp:
            stamp, name = old["stamp"], old["runner"]
        fd, tmp = tempfile.mkstemp(prefix=MARKER + "-", suffix=".tmp", dir=marker.parent)
        try:
            with os.fdopen(fd, "w") as out:
                out.write(json.dumps({"stamp": stamp, "runner": name}) + "\n")
            if marker.is_symlink():
                die("runner marker must not be a symlink")
            os.replace(tmp, marker)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def ready(prefix):
    reg = Path(prefix) / "system.reg"
    return reg.is_file() and "ProgramFilesDir" in reg.read_text(errors="replace")


def environment(runner, prefix, wined3d=False, base=None):
    """The environment for umu-run: inherited Wine, Proton, umu and DXVK settings dropped, ours set."""
    env = {k: v for k, v in (base if base is not None else os.environ).items()
           if not k.startswith(("WINE", "PROTON", "UMU", "DXVK"))}
    env.update(WINEPREFIX=str(prefix), PROTONPATH=str(runner), GAMEID="umu-default", UMU_RUNTIME_UPDATE="0",
               VKD3D_SHADER_CACHE_PATH=str(prefix))
    if wined3d:
        env["PROTON_USE_WINED3D"] = "1"
    return env


def validate_environment(values):
    if not isinstance(values, dict):
        raise ValueError("environment must be an object")
    for name, value in values.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) \
                or name.startswith(("WINE", "PROTON", "UMU", "DXVK", "STEAM_COMPAT_")) \
                or name in set(environment("", "", base={})) | {"PATH", "LD_PRELOAD", "LD_LIBRARY_PATH"}:
            raise ValueError("invalid or reserved environment name")
        if not isinstance(value, str) or "\0" in value:
            raise ValueError("environment values must be strings without NUL")
    return values


def checked_version(runner, prefix):
    stamp, name = runner_version(runner)
    if not forward_ok(prefix, stamp):
        old = json.loads((prefix / MARKER).read_text())
        die(f"{prefix} was last used by {old.get('runner')}, newer than {name}; runner moves are forward only")
    return stamp, name


def checked_runner(a):
    runner, prefix = Path(a.runner).resolve(), Path(a.prefix).resolve()
    if not shutil.which("umu-run"):
        die("umu-run not found on PATH")
    stamp, name = checked_version(runner, prefix)
    return runner, prefix, stamp, name


def create(a):
    runner, prefix, stamp, name = checked_runner(a)
    if ready(prefix):
        record_runner(prefix, stamp, name)
        print(f"{prefix} is ready ({name})")
        return 0
    prefix.mkdir(parents=True, exist_ok=True)
    env = environment(runner, prefix)
    for var in ("DISPLAY", "WAYLAND_DISPLAY"):
        env.pop(var, None)
    subprocess.run(["umu-run", "wineboot", "-u"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=900)
    subprocess.run([str(runner / "files/bin/wineserver"), "-w"], env=env, timeout=300)
    if not ready(prefix):
        die(f"{prefix} did not finish building", 1)
    record_runner(prefix, stamp, name)
    print(f"{prefix} created with {name}")
    return 0


def run(a):
    runner, prefix, stamp, name = checked_runner(a)
    if not a.program:
        die("run needs a program after --")
    if not ready(prefix):
        die(f"{prefix} is not a ready prefix; run create first")
    extra, program = {}, None
    watch = a.watch
    if getattr(a, "profile", None):
        try:
            profile = json.loads(Path(a.profile).read_bytes())
            if not isinstance(profile, dict):
                raise ValueError("profile must be an object")
            extra = validate_environment(profile.get("environment", {}))
            if not watch:
                relative = profile.get("install_dir")
                if not isinstance(relative, str) or not relative:
                    raise ValueError("profile needs install_dir when --watch is absent")
                path = Path(relative)
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError("install_dir must be relative to the prefix")
                path = (prefix / path).resolve()
                if path == prefix or not path.is_relative_to(prefix):
                    raise ValueError("install_dir must stay inside the prefix")
                watch = str(path)
                if isinstance(profile.get("program"), str):
                    program = path / profile["program"]
        except (OSError, ValueError, TypeError) as ex:
            die(f"invalid launch profile: {ex}")
    record_runner(prefix, stamp, name)
    env = environment(runner, prefix, a.wined3d)
    env.update(extra)
    if getattr(a, "display", "x11") == "wayland":
        env["PROTON_ENABLE_WAYLAND"] = "1"
    if getattr(a, "opencl_vendors", None):
        env.setdefault("OCL_ICD_VENDORS", a.opencl_vendors)
    version = peversion.file_version(program) if program else None
    with tempfile.TemporaryDirectory() as tmp:
        manifest = Path(tmp) / "tree.json"
        if watch:
            snap = subprocess.run([sys.executable, str(HERE / "treehash.py"), "snapshot", watch, str(manifest)])
            if snap.returncode:
                die(f"could not hash {watch}")
        try:
            code = subprocess.run(["umu-run"] + a.program, env=env).returncode
        finally:
            if watch:
                cmp = subprocess.run([sys.executable, str(HERE / "treehash.py"), "compare", watch, str(manifest)],
                                     capture_output=True, text=True)
                now = peversion.file_version(program) if program and cmp.returncode == 1 else None
                if version and now and version != now:
                    print(f"the app updated itself: {version} to {now} ({len(cmp.stdout.splitlines())} changes); "
                          "the next setup records its folder", file=sys.stderr)
                elif cmp.returncode:
                    print(cmp.stdout, end="", file=sys.stderr)
                    die(f"{watch} changed during the run", 3)
    return code


def status(a):
    prefix = Path(a.prefix).resolve()
    last = runner_history(prefix) or {}
    print(f"prefix  {prefix}\nready   {ready(prefix)}\nrunner  {last.get('runner', '(none recorded)')}")
    return 0


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=["create", "run", "status"])
    ap.add_argument("--runner")
    ap.add_argument("--profile")
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--display", choices=["x11", "wayland"], default="x11")
    ap.add_argument("--watch")
    ap.add_argument("--wined3d", action="store_true")
    ap.add_argument("--opencl-vendors", help="run: OCL_ICD_VENDORS for the run, unless already set")
    cut = argv.index("--") if "--" in argv else len(argv)
    a = ap.parse_args(argv[:cut])
    a.program = argv[cut + 1:]
    if a.cmd in ("create", "run") and not a.runner:
        die(f"{a.cmd} needs --runner")
    return {"create": create, "run": run, "status": status}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

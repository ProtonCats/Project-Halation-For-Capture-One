# SPDX-FileCopyrightText: 2026 ProtonCats and contributors
# SPDX-License-Identifier: MIT
"""One-command setup and profile launch."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import struct
import time

from . import prefix, runner, setup as profiles

PROJECT_SLUG = "halation-layer"
REPO = Path(__file__).resolve().parents[1]
PINS = REPO / "core/runner/ge-proton11-12.json"
PROFILE = REPO / "profiles/captureone/profile.json"
LIB = Path("files/lib/wine/x86_64-windows")

OPENCL_HOST = Path("/etc/OpenCL/vendors")

def data_directory(value):
    if value:
        return Path(value).resolve()
    base = Path(os.environ["XDG_DATA_HOME"]) if os.environ.get("XDG_DATA_HOME") else Path.home() / ".local/share"
    return (base / PROJECT_SLUG).resolve()


def partial(folder):
    if os.path.lexists(folder / profiles.MARKER):
        raise ValueError(f"{folder} is not as setup left it, for example after the app updated itself. Nothing was changed. "
                         "Keep it: it holds the installed app, its settings and its activation. Start the app with ./run")
    raise ValueError(f"incomplete or changed step; remove {folder} before retrying")


def executable(pfx, profile):
    root = profiles.install_path(pfx, profile["install_dir"]).resolve()
    target = (root / profile["program"]).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise ValueError("profile program must be an existing file inside install_dir")
    return target


def completed(folder, check):
    if not os.path.lexists(folder):
        return False
    try:
        if folder.is_symlink() or not folder.is_dir() or not check():
            partial(folder)
    except (OSError, ValueError, KeyError, TypeError, SystemExit, struct.error, AttributeError):
        partial(folder)
    return True


def setup_main(argv=None):
    parser = argparse.ArgumentParser(description="Prepare the pinned runner and apply the profile")
    parser.add_argument("installer")
    parser.add_argument("--data")
    args = parser.parse_args(argv)
    try:
        missing = [name for name in runner.TOOLS + ["umu-run"] if not shutil.which(name)]
        if not runner.llvm_ar():
            missing.append("llvm-ar")
        if missing:
            raise ValueError("missing required tools: " + ", ".join(missing))
        installer = Path(args.installer).resolve()
        if not installer.is_file():
            raise ValueError("a local installer file is required")
        data = data_directory(args.data)
        pins = json.loads(PINS.read_bytes())
        profile = profiles.load_profile(PROFILE.read_bytes())
        base = data / "runners" / pins.get("ge_asset", pins["ge_tag"])
        work, assembled, pfx = data / "runner-work", data / "runners/patched", data / "pfx"
        patches = runner.our_patches()
        pe_patches = runner.mode_patches(patches, "pe")
        unix_patches = runner.mode_patches(patches, "unix")
        dlls = {f"{dll}.dll" for _, names in pe_patches for dll in names}
        pe_runner = data / "runners/patched-pe" if unix_patches else assembled
        unix_work = data / "runner-work-unix"
        if unix_patches:
            runner.unix_pins(pins)

        def fetched():
            return prefix.runner_version(base)[1] == pins["ge_tag"]

        def built():
            if (work / ".patched").read_text().strip() != runner.patch_identity(PINS.read_bytes(), pe_patches):
                return False
            for dll in dlls:
                runner.import_names(work / "out" / dll)
            return bool(dlls)

        def assembled_ok():
            manifest = json.loads((pe_runner / "halation-runner.json").read_bytes())
            if manifest["base"] != pins["ge_tag"] or manifest["patches"] != [p.name for p, _ in pe_patches]:
                return False
            if set(manifest["dlls"]) != dlls or prefix.runner_version(pe_runner) != prefix.runner_version(base):
                return False
            return all(item == {"sha256": runner.sha256(pe_runner / LIB / dll),
                                "replaces": runner.sha256(base / LIB / dll)}
                       and item["sha256"] == runner.sha256(work / "out" / dll)
                       for dll, item in manifest["dlls"].items())

        def unix_built():
            runner.verified_unix(PINS, unix_work, pe_runner)
            return True

        def unix_assembled():
            _, _, record = runner.verified_unix(PINS, unix_work, pe_runner)
            manifest = json.loads((assembled / "halation-runner.json").read_bytes())
            pe_manifest = json.loads((pe_runner / "halation-runner.json").read_bytes())
            if manifest["base"] != pins["ge_tag"] or manifest["dlls"] != pe_manifest["dlls"] \
                    or manifest["patches"] != sorted(p.name for p, _ in patches) \
                    or prefix.runner_version(assembled) != prefix.runner_version(base):
                return False
            if not all(runner.sha256(assembled / LIB / dll) == runner.sha256(pe_runner / LIB / dll) for dll in dlls):
                return False
            files = {relative: {"sha256": record["artifacts"][name], "replaces": runner.sha256(pe_runner / relative)}
                     for name, relative in runner.UNIX_FILES.items()}
            script = runner.allow_opencl((pe_runner / "proton").read_text()).encode()
            proton = {"sha256": hashlib.sha256(script).hexdigest(), "replaces": runner.sha256(pe_runner / "proton")}
            return manifest["unix"] == {**record, "files": files, "proton": proton} and all(
                runner.sha256(assembled / relative) == item["sha256"] for relative, item in files.items()) \
                and runner.sha256(assembled / "proton") == proton["sha256"]

        def applied():
            marker = json.loads((pfx / profiles.MARKER).read_bytes())
            executable(pfx, profile)
            return (marker["profile_sha256"] == runner.sha256(PROFILE) and prefix.ready(pfx)
                    and prefix.forward_ok(pfx, prefix.runner_version(assembled)[0])
                    and profiles.unchanged(profiles.install_path(pfx, profile["install_dir"]), marker["install_tree"]))

        steps = [
            ("fetch", base, fetched, runner.main, ["fetch", "--pins", str(PINS), "--dest", str(base.parent)]),
            ("build", work, built, runner.main, ["build", "--pins", str(PINS), "--work", str(work)]),
            ("assemble", pe_runner, assembled_ok, runner.main,
             ["assemble", "--pins", str(PINS), "--work", str(work), "--base", str(base), "--out", str(pe_runner)]),
        ]
        if unix_patches:
            steps.extend([
                ("build-unix", unix_work, unix_built, runner.main,
                 ["build", "--mode", "unix", "--pins", str(PINS), "--work", str(unix_work), "--base", str(pe_runner)]),
                ("assemble-unix", assembled, unix_assembled, runner.main,
                 ["assemble", "--mode", "unix", "--pins", str(PINS), "--work", str(unix_work),
                  "--base", str(pe_runner), "--out", str(assembled)]),
            ])
        steps.extend([
            ("prefix", pfx, applied, profiles.main,
             ["apply", "--profile", str(PROFILE), "--runner", str(assembled), "--prefix", str(pfx),
              "--installer", str(installer)]),
        ])
        checked = {}
        for index, (name, folder, check, action, command) in enumerate(steps):
            checked[index] = completed(folder, check)
            if not checked[index]:
                break
        for index, (name, folder, check, action, command) in enumerate(steps):
            start = time.monotonic()
            try:
                skipped = checked[index] if index in checked else completed(folder, check)
                if not skipped:
                    code = action(command)
                    if code:
                        raise ValueError(f"{name} failed with exit {code}; remove {folder} before retrying")
                    if not completed(folder, check):
                        partial(folder)
            except BaseException:
                print(f"{name}: failed ({time.monotonic() - start:.1f}s)", file=sys.stderr)
                raise
            print(f"{name}: {'verified, skipped' if skipped else 'complete'} ({time.monotonic() - start:.1f}s)", flush=True)
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"setup: {exc}", file=sys.stderr)
        return 2


def opencl_vendors(data):
    """A fresh copy of the host's OpenCL ICD list, Mesa's rusticl left out; None when the host lists none.

    The runtime container Proton starts in has no ICD list of its own, so Wine's opencl finds no platform without it.
    """
    icds = sorted(p for p in OPENCL_HOST.glob("*.icd") if p.is_file() and not p.name.startswith("rusticl")) \
        if OPENCL_HOST.is_dir() else []
    target = data / "opencl-vendors"
    if target.is_symlink():
        raise ValueError(f"{target} must not be a symlink")
    target.mkdir(parents=True, exist_ok=True)
    for old in target.glob("*.icd"):
        old.unlink()
    for icd in icds:
        (target / icd.name).write_bytes(icd.read_bytes())
    return target if icds else None


def run_main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    boundary = argv.index("--") if "--" in argv else len(argv)
    parser = argparse.ArgumentParser(description="Launch the installed profile")
    parser.add_argument("--data")
    parser.add_argument("--display", choices=["x11", "wayland"], default="x11")
    parser.add_argument("--renderer", choices=["default", "wined3d"], default="default")
    args = parser.parse_args(argv[:boundary])
    try:
        data = data_directory(args.data)
        profile = profiles.load_profile(PROFILE.read_bytes())
        program = executable(data / "pfx", profile)
        vendors = opencl_vendors(data)
        if vendors is None:
            print("run: the host lists no OpenCL driver (/etc/OpenCL/vendors); image processing stays on the CPU",
                  file=sys.stderr)
        return prefix.main(["run", "--runner", str(data / "runners/patched"), "--prefix", str(data / "pfx"),
                            "--profile", str(PROFILE), "--display", args.display,
                            *(["--opencl-vendors", str(vendors)] if vendors else []),
                            *(["--wined3d"] if args.renderer == "wined3d" else []),
                            "--", str(program), *argv[boundary + 1:]])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"run: {exc}", file=sys.stderr)
        return 2

# SPDX-FileCopyrightText: 2026 ProtonCats and contributors
# SPDX-License-Identifier: MIT
"""One-command setup and profile launch."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import struct
import time

from . import prefix, rebuild, runner, setup as profiles

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


class Outdated(Exception):
    """A finished step whose inputs changed since; its action brings it up to date."""


def partial(folder, reason="it is not as setup left it"):
    """Stop over a step that is not complete; a finished prefix is never named for removal."""
    if os.path.lexists(folder / profiles.MARKER):
        profiles.refuse(folder, reason)
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
    except Outdated:
        return False
    except profiles.Refused:
        raise
    except (OSError, ValueError, KeyError, TypeError, SystemExit, struct.error, AttributeError) as error:
        partial(folder, f"it could not be checked: {error}")
    return True


class RunnerSet:
    """The folders of one built runner under root: how each is made, how it is checked, and what it was made from."""

    def __init__(self, root, base, pins):
        self.root, self.base, self.pins = root, base, pins
        self.patches = runner.our_patches()
        self.pe_patches = runner.mode_patches(self.patches, "pe")
        self.unix_patches = runner.mode_patches(self.patches, "unix")
        self.dlls = {f"{dll}.dll" for _, names in self.pe_patches for dll in names}
        self.work, self.unix_work = root / "runner-work", root / "runner-work-unix"
        self.assembled = root / "runners/patched"
        self.pe_runner = root / "runners/patched-pe" if self.unix_patches else self.assembled
        if self.unix_patches:
            runner.unix_pins(pins)
        self.checked = []

    def inputs(self):
        contents = PINS.read_bytes()
        return {"base": self.pins["ge_tag"], "pe": runner.patch_identity(contents, self.pe_patches),
                "unix": runner.patch_identity(contents, self.unix_patches, "unix") if self.unix_patches else None}

    def recorded(self):
        """What the finished runner says it was made from; None when there is none, or part of the record is gone."""
        manifest, marker = self.assembled / "halation-runner.json", self.work / ".patched"
        if not manifest.is_file() or not marker.is_file():
            return None
        pe = marker.read_text().strip()
        if not re.fullmatch("[0-9a-f]{64}", pe):
            partial(self.work)
        try:
            record = json.loads(manifest.read_bytes())
            made = {"base": record["base"], "pe": pe, "unix": record["unix"]["identity"] if "unix" in record else None}
            if not isinstance(made["base"], str) or not (made["unix"] is None or re.fullmatch("[0-9a-f]{64}", made["unix"])):
                raise ValueError("unexpected content")
        except (ValueError, KeyError, TypeError):
            partial(self.assembled)
        return made

    def current(self):
        """True when the set is whole and made from today's inputs; a set that only differs from its record is refused."""
        self.checked = []
        if self.recorded() != self.inputs():
            return False
        for name, folder, check, _, _ in self.steps():
            start = time.monotonic()
            if not completed(folder, check):
                return False
            self.checked.append((name, time.monotonic() - start))
        return True

    def names(self):
        return [name for name in rebuild.NAMES if os.path.lexists(self.root / name)]

    def built(self):
        if (self.work / ".patched").read_text().strip() != runner.patch_identity(PINS.read_bytes(), self.pe_patches):
            return False
        for dll in self.dlls:
            runner.import_names(self.work / "out" / dll)
        return bool(self.dlls)

    def assembled_ok(self):
        manifest = json.loads((self.pe_runner / "halation-runner.json").read_bytes())
        if manifest["base"] != self.pins["ge_tag"] or manifest["patches"] != [p.name for p, _ in self.pe_patches]:
            return False
        if set(manifest["dlls"]) != self.dlls or prefix.runner_version(self.pe_runner) != prefix.runner_version(self.base):
            return False
        return all(item == {"sha256": runner.sha256(self.pe_runner / LIB / dll),
                            "replaces": runner.sha256(self.base / LIB / dll)}
                   and item["sha256"] == runner.sha256(self.work / "out" / dll)
                   for dll, item in manifest["dlls"].items())

    def unix_built(self):
        runner.verified_unix(PINS, self.unix_work, self.pe_runner)
        return True

    def unix_assembled(self):
        _, _, record = runner.verified_unix(PINS, self.unix_work, self.pe_runner)
        manifest = json.loads((self.assembled / "halation-runner.json").read_bytes())
        pe_manifest = json.loads((self.pe_runner / "halation-runner.json").read_bytes())
        if manifest["base"] != self.pins["ge_tag"] or manifest["dlls"] != pe_manifest["dlls"] \
                or manifest["patches"] != sorted(p.name for p, _ in self.patches) \
                or prefix.runner_version(self.assembled) != prefix.runner_version(self.base):
            return False
        if not all(runner.sha256(self.assembled / LIB / dll) == runner.sha256(self.pe_runner / LIB / dll) for dll in self.dlls):
            return False
        files = {relative: {"sha256": record["artifacts"][name], "replaces": runner.sha256(self.pe_runner / relative)}
                 for name, relative in runner.UNIX_FILES.items()}
        script = runner.allow_opencl((self.pe_runner / "proton").read_text()).encode()
        proton = {"sha256": hashlib.sha256(script).hexdigest(), "replaces": runner.sha256(self.pe_runner / "proton")}
        return manifest["unix"] == {**record, "files": files, "proton": proton} and all(
            runner.sha256(self.assembled / relative) == item["sha256"] for relative, item in files.items()) \
            and runner.sha256(self.assembled / "proton") == proton["sha256"]

    def steps(self):
        steps = [
            ("build", self.work, self.built, runner.main, ["build", "--pins", str(PINS), "--work", str(self.work)]),
            ("assemble", self.pe_runner, self.assembled_ok, runner.main,
             ["assemble", "--pins", str(PINS), "--work", str(self.work), "--base", str(self.base), "--out", str(self.pe_runner)]),
        ]
        if self.unix_patches:
            steps.extend([
                ("build-unix", self.unix_work, self.unix_built, runner.main,
                 ["build", "--mode", "unix", "--pins", str(PINS), "--work", str(self.unix_work), "--base", str(self.pe_runner)]),
                ("assemble-unix", self.assembled, self.unix_assembled, runner.main,
                 ["assemble", "--mode", "unix", "--pins", str(PINS), "--work", str(self.unix_work),
                  "--base", str(self.pe_runner), "--out", str(self.assembled)]),
            ])
        return steps


def run_steps(steps, clear=None, advice=None):
    """Take each step that its check does not find complete.

    clear deletes an unfinished folder of a staged set, so that its step can be taken again; advice ends what is said
    when that fails. Without them an unfinished folder stops setup.
    """
    for name, folder, check, action, command in steps:
        start = time.monotonic()
        try:
            try:
                skipped = completed(folder, check)
            except ValueError:
                if not clear:
                    raise
                clear(folder)
                skipped = False
            if not skipped:
                code = action(command)
                finished = os.path.lexists(folder / profiles.MARKER)
                if code:
                    end = f"; {advice}" if advice else "" if finished else f"; remove {folder} before retrying"
                    raise ValueError(f"{name} failed with exit {code}{end}")
                if clear and not whole(folder, check):
                    raise ValueError(f"{name} did not pass its check; {advice}")
                if not completed(folder, check):
                    if finished:
                        raise ValueError(f"{folder} changed again while setup ran; run setup again")
                    partial(folder)
        except BaseException:
            print(f"{name}: failed ({time.monotonic() - start:.1f}s)", file=sys.stderr)
            raise
        print(f"{name}: {'verified, skipped' if skipped else 'complete'} ({time.monotonic() - start:.1f}s)", flush=True)


def whole(folder, check):
    try:
        return completed(folder, check)
    except ValueError:
        return False


def renew(data, live, pfx):
    """Build today's runner beside the one in use and put it in its place."""
    rebuild.check_folders(data)
    before, (stamp, name) = live.recorded(), prefix.runner_version(live.base)
    if os.path.lexists(pfx / profiles.MARKER) and not prefix.forward_ok(pfx, stamp):
        last = prefix.runner_history(pfx)["runner"]
        raise ValueError(f"{pfx} was last used by {last}, newer than {name}; the runner in use is unchanged, update Halation")
    staged, journal = RunnerSet(data / rebuild.STAGING, live.base, live.pins), rebuild.state(data)
    resumed = journal and journal["state"] in ("building", "ready") and journal["inputs"] == live.inputs()
    if not resumed:
        rebuild.discard(data)
    rebuild.check_space(data)
    if resumed:
        rebuild.resume(data)
    else:
        rebuild.begin(data, live.inputs())
    run_steps(staged.steps(), lambda folder: rebuild.clear(data, str(folder.relative_to(staged.root))),
              "the runner in use is unchanged, run setup again" if live.assembled.is_dir() else "run setup again")
    rebuild.ready(data, staged.names())
    process = prefix.in_use(pfx)
    if process:
        raise ValueError(f"the new runner is ready, but a program still runs in the prefix ({process}); close the app and run setup again")
    start = time.monotonic()
    rebuild.switch(data)
    try:
        if not live.current():
            raise ValueError("it is not whole")
    except (OSError, ValueError, KeyError, TypeError, SystemExit) as error:
        rebuild.undo(data)
        raise ValueError(f"the new runner did not pass its check in place ({error}); the one before it is back") from None
    rebuild.discard(data)
    print(f"switch: complete ({time.monotonic() - start:.1f}s)", flush=True)
    if before and before["base"] != name:
        for folder in sorted((data / "runners").iterdir()):
            if folder not in (live.base, live.pe_runner, live.assembled) and release(folder) == before["base"]:
                print(f"{folder} ({before['base']}) is no longer used and can be deleted", flush=True)


def release(folder):
    """The release an unpacked runner says it is, None for any other folder."""
    try:
        return (folder / "version").read_text().split()[1]
    except (OSError, ValueError, IndexError):
        return None


def setup_main(argv=None):
    parser = argparse.ArgumentParser(description="Prepare the pinned runner and apply the profile")
    parser.add_argument("installer")
    parser.add_argument("--data")
    args = parser.parse_args(argv)
    try:
        missing = [name for name in runner.TOOLS + runner.UNIX_TOOLS + ["umu-run"] if not shutil.which(name)]
        if not runner.llvm_ar():
            missing.append("llvm-ar")
        lacking = runner.missing_packages()
        if missing or lacking:
            raise ValueError("; ".join(["missing required tools: " + ", ".join(missing)] * bool(missing)
                                       + ["missing development packages (pkg-config names): " + ", ".join(lacking)] * bool(lacking))
                             + ". README.md lists one command per distribution that installs them all")
        installer = Path(args.installer).resolve()
        if not installer.is_file():
            raise ValueError("a local installer file is required")
        data = data_directory(args.data)
        pins = json.loads(PINS.read_bytes())
        raw = PROFILE.read_bytes()
        profile, digest = profiles.load_profile(raw), hashlib.sha256(raw).hexdigest()
        base, pfx = data / "runners" / pins.get("ge_asset", pins["ge_tag"]), data / "pfx"

        def fetched():
            return prefix.runner_version(base)[1] == pins["ge_tag"]

        def applied():
            if not os.path.lexists(pfx / profiles.MARKER):
                return False
            if profiles.examine(pfx, profile, digest, live.assembled).outdated:
                raise Outdated
            return True

        live = RunnerSet(data, base, pins)
        with rebuild.lock(data):
            rebuild.recover(data)
            run_steps([("fetch", base, fetched, runner.main, ["fetch", "--pins", str(PINS), "--dest", str(base.parent)])])
            if live.current():
                for name, seconds in live.checked:
                    print(f"{name}: verified, skipped ({seconds:.1f}s)", flush=True)
                rebuild.discard(data)
            else:
                renew(data, live, pfx)
            run_steps([("prefix", pfx, applied, profiles.main,
                        ["apply", "--profile", str(PROFILE), "--runner", str(live.assembled), "--prefix", str(pfx),
                         "--installer", str(installer)])])
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
        if not (data / "runners/patched").is_dir() and os.path.lexists(data / rebuild.STAGING):
            with rebuild.lock(data):  # setup was stopped in the middle of a switch
                rebuild.recover(data)
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

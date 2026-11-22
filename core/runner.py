#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 ProtonCats and contributors
# SPDX-License-Identifier: MIT
"""Rebuild a Proton runner's Wine DLLs with the patches in patches/, and overlay them on that runner.

  runner.py fetch    --pins core/runner/ge-proton11-12.json --dest DIR
  runner.py build    --pins core/runner/ge-proton11-12.json --work DIR [--jobs N]
  runner.py assemble --pins ... --work DIR --base <installed runner> --out <new runner dir>

build fetches the pinned Wine, wine-staging and runner trees, prepares Wine the way the runner's own build does,
applies the runner's own patches for each DLL our patches touch and then ours, and builds those DLLs for x86_64
with clang and lld. Every built DLL must have an import table whose names all resolve. Tools are checked, never
installed: git, curl, autoconf, autoheader, perl, bison, flex, make, gcc, clang, lld-link, llvm-ar.
"""
import argparse
import copy
import fnmatch
import hashlib
import http.client
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parents[1]
TOOLS = ["git", "curl", "autoconf", "autoheader", "perl", "bison", "flex", "make", "gcc", "clang", "lld-link"]
CONFIGURE = ["--enable-archs=x86_64", "--disable-tests", "--without-x", "--without-freetype", "--without-wayland",
             "--without-vulkan", "--without-gstreamer", "--without-pulse", "--without-alsa", "--without-cups",
             "--without-sane", "--without-gphoto", "--without-v4l2", "--without-krb5", "--without-netapi",
             "--without-usb", "--without-pcap", "--without-opencl", "--without-sdl", "--without-capi",
             "--without-dbus", "--without-udev", "--without-unwind", "--without-ffmpeg"]


# Built from GE's fully prepared tree (GE changes them beyond what PE mode can replay), Unix side or not.
UNIX_MODULES = {"winex11.drv", "winewayland.drv", "wined3d"}
UNIX_CONFIGURE = ["--enable-archs=x86_64", "--enable-win64", "--disable-tests", "--with-x",
                  "--with-vulkan", "--with-wayland", "--with-opencl", "--without-ffmpeg",
                  # This Wine's configure.ac lacks the Linux libOpenCL check, so opencl.so would link without it.
                  "OPENCL_LIBS=-lOpenCL"]
UNIX_FILES = {"winex11.so": "files/lib/wine/x86_64-unix/winex11.so",
              "winex11.drv": "files/lib/wine/x86_64-windows/winex11.drv",
              "winewayland.so": "files/lib/wine/x86_64-unix/winewayland.so",
              "winewayland.drv": "files/lib/wine/x86_64-windows/winewayland.drv",
              "opencl.so": "files/lib/wine/x86_64-unix/opencl.so",
              "opencl.dll": "files/lib/wine/x86_64-windows/opencl.dll",
              "wined3d.dll": "files/lib/wine/x86_64-windows/wined3d.dll"}
UNIX_TARGETS = {"winex11.so": "dlls/winex11.drv/winex11.so",
                "winex11.drv": "dlls/winex11.drv/x86_64-windows/winex11.drv",
                "winewayland.so": "dlls/winewayland.drv/winewayland.so",
                "winewayland.drv": "dlls/winewayland.drv/x86_64-windows/winewayland.drv",
                "opencl.so": "dlls/opencl/opencl.so",
                "opencl.dll": "dlls/opencl/x86_64-windows/opencl.dll",
                "wined3d.dll": "dlls/wined3d/x86_64-windows/wined3d.dll"}
# GE's proton script forces opencl to "n,d" (disabled unless a native DLL exists); this line is made a no-op.
OPENCL_OVERRIDE = 'self.dlloverrides["opencl"] = "n,d"'



def die(msg):
    sys.exit(f"runner.py: {msg}")


def run(cmd, cwd=None, env=None, log=None, check=True, timeout=None):
    with open(log, "a") if log else open(os.devnull, "w") as out:
        try:
            r = subprocess.run(cmd, cwd=cwd, env=env, stdout=out, stderr=subprocess.STDOUT, timeout=timeout)
        except subprocess.TimeoutExpired:
            die(f"timed out ({timeout}s): {' '.join(map(str, cmd))}" + (f"; see {log}" if log else ""))
    if check and r.returncode:
        die(f"failed ({r.returncode}): {' '.join(map(str, cmd))}" + (f"; see {log}" if log else ""))

    return r.returncode


def network_request(action, *args, **kwargs):
    if __package__:
        from .setup import require_tunnel
    else:
        from setup import require_tunnel
    require_tunnel()
    return action(*args, **kwargs)


class TunnelRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return network_request(super().redirect_request, req, fp, code, msg, headers, newurl)


def llvm_ar():
    for name in ["llvm-ar"] + [f"llvm-ar-{v}" for v in range(30, 14, -1)]:
        if shutil.which(name):
            return shutil.which(name)
    return None


def check_tools(work):
    missing = [t for t in TOOLS if not shutil.which(t)]
    ar = llvm_ar()
    if not ar:
        missing.append("llvm-ar")
    if missing:
        die("missing tools: " + ", ".join(missing))
    # winebuild calls "dlltool"; llvm-ar acts as llvm-dlltool when called by that name
    bindir = work / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    link = bindir / "dlltool"
    if not link.exists():
        link.symlink_to(ar)
    return {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "GIT_TERMINAL_PROMPT": "0"}, ar


def fetch(url, commit, dest, env, depth=1):
    if (dest / ".git").exists() and subprocess.run(["git", "cat-file", "-e", commit], cwd=dest).returncode == 0:
        return
    dest.mkdir(parents=True, exist_ok=True)
    run(["git", "init", "-q"], cwd=dest, env=env)
    network_request(run, ["git", "fetch", "-q", f"--depth={depth}", url, commit], cwd=dest, env=env)


def extract_runner(archive, tree, tag):
    root = tree / tag

    def checked(member):
        name = PurePosixPath(member.name)
        if name.is_absolute() or ".." in name.parts or not name.parts or name.parts[0] != tag:
            raise ValueError(f"unsafe archive member: {member.name}")
        if name == PurePosixPath(tag) and not member.isdir():
            raise ValueError(f"unsafe archive root: {member.name}")
        path = tree / member.name
        if not path.resolve().is_relative_to(root):
            raise ValueError(f"unsafe archive member: {member.name}")
        if member.issym() or member.islnk():
            target = (path.parent if member.issym() else tree) / member.linkname
            if not target.resolve().is_relative_to(root):
                raise ValueError(f"unsafe archive link: {member.name} -> {member.linkname}")
        elif not member.isfile() and not member.isdir():
            raise ValueError(f"unsafe archive member type: {member.name}")
        member = copy.copy(member)
        member.mode &= 0o755
        member.uid = member.gid = member.uname = member.gname = None
        return member

    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        for member in members:
            checked(member)
        filtered = (checked(member) for member in members)
        data_filter = getattr(tarfile, "data_filter", None)
        if data_filter is not None:
            tar.extractall(tree, members=filtered, filter=data_filter)
        else:
            tar.extractall(tree, members=filtered)
    if not root.is_dir() or root.is_symlink():
        raise ValueError(f"archive has no {tag} directory")


def fetch_runner(args):
    try:
        pins = json.loads(Path(args.pins).read_text())
        tag = pins["ge_tag"]
        if not isinstance(tag, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", tag):
            raise ValueError("invalid runner tag")
        name = pins.get("ge_asset", tag)
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
            raise ValueError("invalid runner asset name")
        dest = Path(args.dest).resolve()
        target = dest / name
        if os.path.lexists(target):
            die(f"{target} exists; refusing to overwrite")
        archive_name = f"{name}.tar.gz"
        base = f"{pins['ge_url'].rstrip('/')}/releases/download/{tag}"
        opener = urllib.request.build_opener(TunnelRedirect())
        with network_request(opener.open, f"{base}/{name}.sha512sum", timeout=60) as response:
            lines = response.read().decode("ascii").splitlines()
        sums = []
        for line in lines:
            match = re.fullmatch(r"([0-9a-fA-F]{128}) [ *](.+)", line)
            if match and match[2] in (archive_name, "./" + archive_name):
                sums.append(match[1].lower())
        if len(sums) != 1:
            die(f"missing or ambiguous checksum line for {archive_name}")
        dest.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".fetch-", dir=dest) as tmp:
            work = Path(tmp)
            partial = work / "archive.part"
            digest = hashlib.sha512()
            with network_request(opener.open, f"{base}/{archive_name}", timeout=60) as response, partial.open("wb") as out:
                while block := response.read(1 << 20):
                    out.write(block)
                    digest.update(block)
            if digest.hexdigest() != sums[0]:
                die(f"checksum mismatch for {archive_name}")
            archive = work / archive_name
            os.replace(partial, archive)
            tree = work / "tree"
            tree.mkdir()
            extract_runner(archive, tree, name)
            if os.path.lexists(target):
                die(f"{target} exists; refusing to overwrite")
            os.rename(tree / name, target)
        print(f"fetched {target}")
        return 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, tarfile.TarError, EOFError,
            http.client.HTTPException) as ex:
        die(f"fetch failed: {ex}")


def prepare(pins, work, env, log):
    wine, staging, ge = work / "wine", work / "wine-staging", work / "runner-src"
    marker = work / ".prepared"
    identity = patch_identity(json.dumps(pins).encode(), [])
    check_identity(marker, identity)
    if marker.exists():
        return wine, staging, ge
    fetch(pins["wine_url"], pins["wine_commit"], wine, env)
    fetch(pins["wine_url"], pins["wine_revert"], wine, env, depth=2)
    fetch(pins["staging_url"], pins["staging_commit"], staging, env)
    run(["git", "checkout", "-q", pins["staging_commit"]], cwd=staging, env=env)
    if not (ge / ".git").exists():
        network_request(run, ["git", "clone", "-q", "--depth", "1", "--branch", pins["ge_tag"], "--no-recurse-submodules",
             pins["ge_url"], str(ge)], env=env)
    run(["git", "checkout", "-q", "-f", pins["wine_commit"]], cwd=wine, env=env)
    run(["git", "revert", "--no-commit", pins["wine_revert"]], cwd=wine, env=env, log=log)
    run(["autoconf", "-f"], cwd=wine, env=env, log=log)
    run(["autoheader", "-f"], cwd=wine, env=env, log=log)
    for xml in ("vk.xml", "video.xml"):
        network_request(run, ["curl", "-sSf", "-o", xml, f"{pins['vulkan_docs']}/{xml}"], cwd=wine, env=env, log=log)
    run([sys.executable, "dlls/winevulkan/make_vulkan", "-x", "vk.xml", "-X", "video.xml"], cwd=wine,
        env={**env, "XDG_CACHE_HOME": str(work / "cache")}, log=log)
    run(["tools/make_specfiles"], cwd=wine, env=env, log=log)
    marker.write_text(identity + "\n")
    return wine, staging, ge


def our_patches():
    """patches/*.patch in order, each with the DLLs whose non-test sources it changes."""
    out = []
    for p in sorted((REPO / "patches").glob("*.patch")):
        dlls = sorted(set(re.findall(r"^\+\+\+ b/dlls/([^/]+)/(?!tests/)", p.read_text(errors="replace"), re.M)))
        out.append((p, dlls))
    return out


def apply(patch, cwd, env, log, include=()):
    files = sorted(patch.glob("*.patch")) if patch.is_dir() else [patch]
    for f in files:
        run(["git", "apply"] + [f"--include={g}" for g in include] + [str(f)], cwd=cwd, env=env, log=log)


def import_names(dll):
    """Names in a PE import table; raises ValueError when an entry does not resolve inside the file."""
    b = dll.read_bytes()
    pe = struct.unpack_from("<I", b, 0x3C)[0]
    nsec, optsize = struct.unpack_from("<H", b, pe + 6)[0], struct.unpack_from("<H", b, pe + 20)[0]
    rva, size = struct.unpack_from("<II", b, pe + 24 + 112 + 8)
    secs = [struct.unpack_from("<8sIIII", b, pe + 24 + optsize + i * 40) for i in range(nsec)]

    def off(r):
        for _, vsize, va, rsize, raw in secs:
            if va <= r < va + max(vsize, rsize):
                return r - va + raw
        raise ValueError(f"rva {r:#x} outside every section")

    names, o = [], off(rva)
    for i in range(size // 20):
        name_rva = struct.unpack_from("<5I", b, o + 20 * i)[3]
        if not name_rva:
            return names
        n = off(name_rva)
        names.append(b[n:b.index(b"\0", n)].decode("ascii"))
    raise ValueError("import table has no terminator")


def fix_ucrtbase(build, env, ar, log):
    """winebuild's archive step corrupts an llvm-dlltool import library it adds objects to; rebuild that one."""
    lib = build / "dlls/ucrtbase/x86_64-windows/libucrtbase.a"
    run(["make", str(lib.relative_to(build))], cwd=build, env=env, log=log)
    text = (build / "Makefile").read_text()
    rule = re.search(r"^dlls/ucrtbase/x86_64-windows/libucrtbase\.a:(.*?)(?<!\\)\n", text, re.S | re.M)
    if not rule:
        die("no libucrtbase.a rule in the Makefile")
    objs = sorted(set(re.findall(r"dlls/ucrtbase/x86_64-windows/[\w.]+\.o", rule.group(1))))
    deffile = build / "ucrtbase.def"
    run(["tools/winebuild/winebuild", "-w", "--def", "-o", str(deffile), "-b", "x86_64-windows",
         "--export", str(build.parent / "wine/dlls/ucrtbase/ucrtbase.spec")], cwd=build, env=env, log=log)
    fd, name = tempfile.mkstemp(prefix=lib.name + "-", suffix=".tmp", dir=lib.parent)
    os.close(fd)
    replacement = Path(name)
    try:
        run(["dlltool", "-m", "i386:x86-64", "-d", str(deffile), "-l", str(replacement)],
            cwd=build, env=env, log=log)
        run([ar, "rs", str(replacement)] + objs, cwd=build, env=env, log=log)
        os.replace(replacement, lib)
    finally:
        replacement.unlink(missing_ok=True)


def identity_pins(pins, mode):
    shared = ("wine_url", "wine_commit", "wine_revert", "staging_url", "staging_commit", "ge_url", "vulkan_docs", "ge_tag")
    keys = shared + (("unix",) if mode == "unix" else ("base_patches",))
    return {key: pins[key] for key in keys if key in pins}


def patch_identity(pins_bytes, patches, mode="pe"):
    return hashlib.sha256(json.dumps(
        {"mode": mode, "configure": UNIX_CONFIGURE if mode == "unix" else CONFIGURE,
         "pins": identity_pins(json.loads(pins_bytes), mode), "patches": [(p.name, sha256(p)) for p, _ in patches]},
        sort_keys=True).encode()).hexdigest()


def check_identity(marker, identity, required=False):
    if (required and not marker.is_file()) or (marker.exists() and marker.read_text().strip() != identity):
        die("pins or patch identity missing or changed; use a fresh --work directory")


def safe_library(root, lib):
    if root.is_symlink():
        die(f"unsafe runner library directory symlink: {root}")
    path = root
    for part in lib.parts:
        path = path / part
        if path.is_symlink():
            die(f"unsafe runner library directory symlink: {path}")
    if not path.is_dir():
        die(f"{root} is not a Proton runner (no {lib})")
    return path


def build(args):
    pins_bytes = Path(args.pins).read_bytes()
    work = Path(args.work).resolve()
    if getattr(args, "mode", "pe") == "unix":
        return build_unix(args)
    patches = mode_patches(our_patches(), "pe")
    identity = patch_identity(pins_bytes, patches)
    marker = work / ".patched"
    check_identity(marker, identity)
    pins = json.loads(pins_bytes)
    log = work / "build.log"
    env, ar = check_tools(work)
    wine, staging, ge = prepare(pins, work, env, log)
    dlls = sorted({d for _, ds in patches for d in ds})
    unknown = [d for d in dlls if d not in pins["base_patches"]]
    if unknown:
        die(f"no base patch list in {args.pins} for: {', '.join(unknown)}")
    if not marker.exists():
        for dll in dlls:
            for bp in pins["base_patches"][dll]:
                root = ge if bp["tree"] == "ge" else staging
                apply(root / bp["path"], wine, env, log, bp.get("include", ()))
        for p, _ in patches:
            apply(p, wine, env, log)
        marker.write_text(identity + "\n")
    b = work / "build64"
    b.mkdir(exist_ok=True)
    if not (b / "Makefile").exists():
        run([str(wine / "configure")] + CONFIGURE, cwd=b, env=env, log=log)
    jobs = str(args.jobs or os.cpu_count() or 4)
    fix_ucrtbase(b, env, ar, log)
    targets = [f"dlls/{d}/x86_64-windows/{d}.dll" for d in dlls]
    run(["make", "-j", jobs] + targets, cwd=b, env=env, log=log)
    out = work / "out"
    out.mkdir(exist_ok=True)
    for d, t in zip(dlls, targets):
        names = import_names(b / t)
        shutil.copy2(b / t, out / f"{d}.dll")
        print(f"{d}.dll  {sha256(out / f'{d}.dll')[:12]}  imports {', '.join(names)}")
    return 0


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def assemble(args):
    if getattr(args, "mode", "pe") == "unix":
        return assemble_unix(args)
    pins_bytes = Path(args.pins).read_bytes()
    pins = json.loads(pins_bytes)
    work, base, out = Path(args.work).resolve(), Path(args.base).resolve(), Path(args.out).resolve()
    built = sorted((work / "out").glob("*.dll"))
    if not built:
        die(f"nothing built in {work / 'out'}; run build first")
    if out.exists():
        die(f"{out} exists; refusing to overwrite")
    lib = Path("files/lib/wine/x86_64-windows")
    safe_library(base, lib)
    if out.is_relative_to(base):
        die("output must be outside the base runner")
    patches = mode_patches(our_patches(), "pe")
    check_identity(work / ".patched", patch_identity(pins_bytes, patches), required=True)
    expected = {f"{dll}.dll" for _, dlls in patches for dll in dlls}
    if {dll.name for dll in built} != expected:
        die("build output incomplete or unexpected; run build first")
    run(["cp", "-a", "--reflink=auto", str(base), str(out)])
    safe_library(out, lib)
    manifest = {"base": pins["ge_tag"], "patches": [p.name for p, _ in patches], "dlls": {}}
    for dll in built:
        dest = out / lib / dll.name
        if not dest.exists():
            die(f"{dll.name} is not in the base runner")
        dest.unlink()
        shutil.copyfile(dll, dest)
        dest.chmod(0o755)
        manifest["dlls"][dll.name] = {"sha256": sha256(dest), "replaces": sha256(base / lib / dll.name)}
    (out / "halation-runner.json").write_text(json.dumps(manifest, indent=1) + "\n")
    print(f"assembled {out}: {', '.join(manifest['dlls'])}")
    return 0


def mode_patches(patches, mode):
    selected = []
    for patch, modules in patches:
        unix = bool(set(modules) & UNIX_MODULES)
        if unix and set(modules) - UNIX_MODULES:
            die(f"split mixed PE/Unix patch: {patch.name}")
        if unix == (mode == "unix"):
            selected.append((patch, modules))
    return selected


def unix_pins(pins):
    """Validate the complete Unix input contract before doing work."""
    def required(obj, key, pattern=None):
        value = obj.get(key)
        if not isinstance(value, str) or not value or (pattern and not re.fullmatch(pattern, value)):
            die(f"missing or invalid Unix pin: {key}")
        return value
    for key in ("wine_url", "staging_url", "ge_url", "vulkan_docs", "ge_tag"):
        required(pins, key)
    for key in ("wine_commit", "wine_revert", "staging_commit"):
        required(pins, key, r"[0-9a-f]{40}")
    spec = pins.get("unix")
    if not isinstance(spec, dict):
        die("missing Unix pins")
    for key in ("ge_commit", "wineopenxr_tree"):
        required(spec, key, r"[0-9a-f]{40}")
    path = PurePosixPath(required(spec, "prep_path"))
    if path.is_absolute() or ".." in path.parts:
        die("invalid Unix prep path")
    required(spec, "prep_sha256", r"[0-9a-f]{64}")
    if type(spec.get("driver_version")) is not int or spec["driver_version"] < 0:
        die("missing or invalid Unix pin: driver_version")
    for group, keys in (("xml_sha256", ("vk.xml", "video.xml")),
                        ("base_sha256", (*UNIX_FILES.values(), "files/lib/wine/x86_64-unix/win32u.so", "proton"))):
        if not isinstance(spec.get(group), dict):
            die(f"missing Unix pin: {group}")
        for key in keys:
            required(spec[group], key, r"[0-9a-f]{64}")
    return spec


def unix_base(pins, base):
    spec = unix_pins(pins)
    for relative, expected in spec["base_sha256"].items():
        path = base / relative
        safe_library(base, Path(relative).parent)
        if path.is_symlink() or not path.is_file() or sha256(path) != expected:
            die(f"Unix base hash mismatch: {relative}")


def disassembly(path, symbol):
    result = subprocess.run(["objdump", "-d", "--no-show-raw-insn", f"--disassemble={symbol}", str(path)],
                            capture_output=True, text=True)
    if result.returncode:
        die(f"cannot decode driver version: {path}: {symbol}")
    body = re.search(rf"^[0-9a-f]+ <{re.escape(symbol)}>:\n(.*?)(?=^[0-9a-f]+ <|\Z)",
                     result.stdout, re.M | re.S)
    return body[1] if body else ""


def symbol_version(path, symbol, instruction):
    """Decode only the named x86_64 function, refusing unknown instruction forms."""
    values = re.findall(rf"\b{instruction}\s+\$(0x[0-9a-f]+|[0-9]+),%esi\b", disassembly(path, symbol))
    if len(values) != 1:
        die(f"ambiguous or absent driver version: {path}: {symbol}")
    return int(values[0], 0)


def call_version(path, symbol, callee="__wine_set_user_driver"):
    """The constant in %esi at every call to callee inside symbol; unknown or differing values refuse."""
    values, esi = [], None
    for line in disassembly(path, symbol).splitlines():
        code = line.split("#", 1)[0].rstrip()
        if re.search(rf"\b(?:call|jmp)\s+[0-9a-f]+ <{callee}(?:@plt)?>$", code):
            values.append(esi)
        elif m := re.search(r"\bmov\s+\$(0x[0-9a-f]+|[0-9]+),%esi$", code):
            esi = int(m[1], 0)
        elif re.search(r"\b(?:call|jmp)\b|%(?:rsi|esi|si|sil)$", code):
            esi = None  # another call, or any other write to the register
    if not values or None in values or len(set(values)) != 1:
        die(f"ambiguous or absent driver version: {path}: {symbol}")
    return values[0]


def unix_versions(work, base, pins):
    text = (work / "source/wine/include/wine/gdi_driver.h").read_text()
    values = re.findall(r"^#define WINE_GDI_DRIVER_VERSION\s+([0-9]+)\s*$", text, re.M)
    if len(values) != 1:
        die("absent or ambiguous driver header version")
    versions = [int(values[0]),
                symbol_version(work / "out/winex11.so", "init_user_driver", "mov"),
                call_version(work / "out/winewayland.so", "waylanddrv_unix_init"),
                symbol_version(base / "files/lib/wine/x86_64-unix/win32u.so", "__wine_set_user_driver", "cmp")]
    if any(value != pins["unix"]["driver_version"] for value in versions):
        die(f"driver version mismatch: header/built/base {versions}")
    return versions[0]


def prepare_unix(pins, work, env, log, identity):
    spec = unix_pins(pins)
    marker = work / ".prepared"
    check_identity(marker, identity)
    source = work / "source"
    wine, staging = source / "wine", source / "wine-staging"
    if marker.exists():
        return wine
    # Everything reset or cleaned by GE's script is inside this disposable checkout.
    fetch(pins["ge_url"], spec["ge_commit"], source, env)
    run(["git", "checkout", "--detach", "-f", spec["ge_commit"]], cwd=source, env=env, log=log)
    script = source / spec["prep_path"]
    if sha256(script) != spec["prep_sha256"]:
        die("Unix prep script hash mismatch")
    tree = subprocess.run(["git", "rev-parse", "HEAD:wineopenxr"], cwd=source, capture_output=True, text=True)
    if tree.returncode or tree.stdout.strip() != spec["wineopenxr_tree"]:
        die("Unix wineopenxr tree mismatch")
    for url, commit, dest in ((pins["wine_url"], pins["wine_commit"], wine),
                              (pins["staging_url"], pins["staging_commit"], staging)):
        fetch(url, commit, dest, env)
        run(["git", "checkout", "--detach", "-f", commit], cwd=dest, env=env, log=log)
    fetch(pins["wine_url"], pins["wine_revert"], wine, env, depth=2)
    text = script.read_text()
    start, end = "### (2) WINE PATCHING ###", "### END WINE PATCHING ###"
    if text.count(start) != 1 or text.count(end) != 1:
        die("Unix prep section missing or ambiguous")
    # GE's unrelated prep sections download Mono and alter other projects; run only Wine's inputs.
    header = text.split("### (1) PREP SECTION ###", 1)[0]
    body = text.split(start, 1)[1].split(end, 1)[0]
    if f"git revert --no-commit {pins['wine_revert']}" not in body:
        die("Unix prep revert pin mismatch")
    # Match the working Job 17 generator sequence without autoreconf's extra project regeneration.
    body = body.replace("autoreconf -f", "autoconf -f && autoheader -f")
    wrapper = work / "prep-wine.sh"
    wrapper.write_text("set -Eeuo pipefail\n" + header +
                       '\npushd wineopenxr\napply_all_in_dir "../patches/wineopenxr/"\npopd\n' + body)
    run(["bash", str(wrapper)], cwd=source, env=env, log=log)
    for xml, expected in spec["xml_sha256"].items():
        network_request(run, ["curl", "-sSf", "-o", xml, f"{pins['vulkan_docs']}/{xml}"],
                        cwd=wine, env=env, log=log)
        if sha256(wine / xml) != expected:
            die(f"Unix XML hash mismatch: {xml}")
    run([sys.executable, "dlls/winevulkan/make_vulkan", "-x", "vk.xml", "-X", "video.xml"],
        cwd=wine, env={**env, "XDG_CACHE_HOME": str(work / "cache")}, log=log)
    run(["tools/make_specfiles"], cwd=wine, env=env, log=log)
    marker.write_text(identity + "\n")
    return wine


def unix_identity(pins_path):
    contents = Path(pins_path).read_bytes()
    pins = json.loads(contents)
    unix_pins(pins)
    patches = mode_patches(our_patches(), "unix")
    if not patches:
        die("Unix mode requires winex11.drv or winewayland.drv patches")
    return pins, patches, patch_identity(contents, patches, "unix")


def undefined_symbols(path):
    r = subprocess.run(["objdump", "-T", str(path)], capture_output=True, text=True)
    if r.returncode:
        die(f"objdump -T failed on {path.name}")
    return {line.split()[-1] for line in r.stdout.splitlines() if "*UND*" in line}


def allow_opencl(script):
    """GE's proton script with its opencl override line turned into a no-op; exactly one match or refuse."""
    lines = script.splitlines(keepends=True)
    hits = [i for i, line in enumerate(lines) if line.strip() == OPENCL_OVERRIDE]
    if len(hits) != 1:
        die(f"proton script: expected one opencl override line, found {len(hits)}")
    line = lines[hits[0]]
    indent = line[:len(line) - len(line.lstrip())]
    lines[hits[0]] = indent + "pass  # opencl left to the builtin, which loads the host ICDs\n"
    return "".join(lines)


def build_unix(args):
    pins, patches, identity = unix_identity(args.pins)
    work, base = Path(args.work).resolve(), Path(args.base).resolve()
    check_identity(work / ".patched", identity)
    unix_base(pins, base)
    required = ["bash", "patch", "pkg-config", "objdump"]
    missing = [tool for tool in required if not shutil.which(tool)]
    if missing:
        die("missing Unix tools: " + ", ".join(missing))
    # Refuse silently disabled input support before generating any build output.
    packages = ["x11", "xi", "xext", "xrandr", "xrender", "xfixes", "xcursor", "xcomposite", "xinerama", "vulkan",
                "wayland-client", "xkbcommon", "xkbregistry", "OpenCL"]
    run(["pkg-config", "--exists", *packages])
    work.mkdir(parents=True, exist_ok=True)
    log = work / "build.log"
    env, _ = check_tools(work)
    wine = prepare_unix(pins, work, env, log, identity)
    if not (work / ".patched").exists():
        for patch, _ in patches:
            apply(patch, wine, env, log)
        (work / ".patched").write_text(identity + "\n")
    build = work / "build64"
    build.mkdir(exist_ok=True)
    if not (build / "Makefile").exists():
        run([str(wine / "configure"), *UNIX_CONFIGURE], cwd=build, env=env, log=log)
    run(["make", "-j", str(args.jobs or os.cpu_count() or 4), *UNIX_TARGETS.values()], cwd=build, env=env, log=log)
    out = work / "out"
    out.mkdir(exist_ok=True)
    for name, target in UNIX_TARGETS.items():
        shutil.copyfile(build / target, out / name)
    if "clGetPlatformIDs" not in undefined_symbols(out / "opencl.so"):
        die("opencl.so was built without the host OpenCL loader (no clGetPlatformIDs import)")
    version = unix_versions(work, base, pins)
    record = {"identity": identity, "driver_version": version,
              "artifacts": {name: sha256(out / name) for name in UNIX_FILES}}
    (work / "unix-build.json").write_text(json.dumps(record, sort_keys=True) + "\n")
    return 0


def verified_unix(pins_path, work, base):
    pins, patches, identity = unix_identity(pins_path)
    check_identity(work / ".patched", identity, required=True)
    if {p.name for p in (work / "out").iterdir()} != set(UNIX_FILES):
        die("Unix build output incomplete or unexpected")
    unix_base(pins, base)
    record = json.loads((work / "unix-build.json").read_text())
    expected = {"identity": identity, "driver_version": unix_versions(work, base, pins),
                "artifacts": {name: sha256(work / "out" / name) for name in UNIX_FILES}}
    if record != expected:
        die("Unix build provenance or artifact hash mismatch")
    return pins, patches, record


def assemble_unix(args):
    work, base, out = Path(args.work).resolve(), Path(args.base).resolve(), Path(args.out).resolve()
    if os.path.lexists(out):
        die(f"{out} exists; refusing to overwrite")
    if out.is_relative_to(base):
        die("output must be outside the base runner")
    pins, patches, record = verified_unix(args.pins, work, base)
    # All refusal checks precede copying; publish only a completed replacement tree.
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".assemble-unix-", dir=out.parent) as temporary:
        tree = Path(temporary) / "runner"
        run(["cp", "-a", "--reflink=auto", str(base), str(tree)])
        manifest_path = tree / "halation-runner.json"
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"base": pins["ge_tag"], "patches": [], "dlls": {}}
        manifest["patches"] = sorted(set(manifest["patches"]) | {p.name for p, _ in patches})
        manifest["unix"] = {**record, "files": {}}
        for name, relative in UNIX_FILES.items():
            safe_library(tree, Path(relative).parent)
            dest = tree / relative
            dest.unlink()
            shutil.copyfile(work / "out" / name, dest)
            dest.chmod(0o755)
            manifest["unix"]["files"][relative] = {"sha256": sha256(dest), "replaces": sha256(base / relative)}
        script = tree / "proton"
        if script.is_symlink() or not script.is_file():
            die("proton script missing or a link")
        text = allow_opencl(script.read_text())
        script.unlink()
        script.write_text(text)
        script.chmod(0o755)
        manifest["unix"]["proton"] = {"sha256": sha256(script), "replaces": sha256(base / "proton")}
        # Unlink first so a copied metadata symlink cannot redirect the manifest write.
        manifest_path.unlink(missing_ok=True)
        manifest_path.write_text(json.dumps(manifest, indent=1) + "\n")
        os.rename(tree, out)
    print(f"assembled Unix modules {out}")
    return 0


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=["fetch", "build", "assemble"])
    ap.add_argument("--pins", required=True)
    ap.add_argument("--mode", choices=["pe", "unix"], default="pe")
    ap.add_argument("--work")
    ap.add_argument("--dest")
    ap.add_argument("--jobs", type=int)
    ap.add_argument("--base")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    if a.cmd == "fetch":
        if not a.dest:
            die("fetch needs --dest")
        return fetch_runner(a)
    if not a.work:
        die(f"{a.cmd} needs --work")
    if a.cmd == "assemble" and not (a.base and a.out):
        die("assemble needs --base and --out")
    if a.mode == "unix" and not a.base:
        die("Unix mode needs --base for compatibility checks")
    return build(a) if a.cmd == "build" else assemble(a)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

# Reference

How each piece of Halation works, for contributors and anyone curious. The front page is the [README](../README.md).

## Setup and run

- **Data folder.** `$XDG_DATA_HOME/halation-layer`, or the local data directory `halation-layer` when that variable is unset; `--data <dir>` selects another location for both commands.
- **Before starting**, setup checks the required build dependencies. It reports each step's time and skips steps it has already verified.
- **A partial or changed step** is refused, naming the folder to remove before retrying. A finished prefix never is: setup says why it left it as it is, because it holds the app, its settings and its activation.
- **Launch** checks the installed files before and after; arguments after `./run --` are passed to the program.

## Repro programs

`repro/p1c-wpf-hw-window/` holds five plain WPF geometry phases (`--hold` seconds each, default 4) for external screenshots and records descendant HWNDs.

`repro/p1d-wpf-input/` logs input as JSON lines from a plain WPF window:

- `--hold` seconds (default 30) and `--owner none|hidden|shown-small|popup`. When the hold ends it writes the events, counts and any errors to the shared probe result file (`HALATION_RESULT`).
- `--mode wheel` shows a 300-pixel tool ScrollViewer with 30 label and slider rows and an outside text area, and records routed wheel events, raw wheel messages, scroll offsets and distinct deltas. `--mode input` remains the default.
- `--rawinput mouse` registers the window for raw mouse input (WM_INPUT) and records the registration result and the message count.
- `--stall MILLISECONDS` makes P1d block its UI thread that long between 300 ms of normal pumping, so input waits unread.

**Device churn.** `repro/p1d-wpf-input/device_churn.py --display :N` clicks through short-lived XInput devices on a private X server. Started 2 s after any Wine program launches there, while Wine's desktop thread (the only one that reads raw XInput events) is still busy, it makes that thread open a device that is gone.

**Device churn result.** Without `0006` the session ends with XI_BadDevice (2 of 3 runs); with it, 0 of 3.

**Black GPU-drawn window.** `--autotype MILLISECONDS` makes P1d type into its own text box. Under the Wayland driver with Wine's Direct3D for WPF, the window stays black except the strip that last changed, and Wine logs `Using GDI present` once; with `0012` the whole window draws.

`repro/p8-layered-alpha/` shows a white layered window at a constant alpha (`--alpha 0..255`, `--mode slwa|ulw`, `--hold` seconds, `--then` a second alpha mid-hold); compare its pixels with the background to read the alpha the compositor applied. It shows the bugs behind `0008` (X11), `0009` (Wayland) and `0010` (alpha below 8).

`repro/p9-show-hide/` shows a captioned window, paints it and hides it within `--gap` milliseconds, then hides it again (`--rehide 1`, the default; `0` skips it) and holds for `--hold` seconds. Windows never leaves it on screen; without `0011` the X11 driver leaves it mapped after the second hide.

## Building the runner

`core/runner.py` rebuilds the Wine DLLs that `patches/` changes, for the Proton runner pinned in `core/runner/`, and puts them on top of an installed copy of that runner:

```sh
core/runner.py fetch    --pins core/runner/ge-proton11-12.json --dest runners
core/runner.py build    --pins core/runner/ge-proton11-12.json --work /tmp/runner-work
core/runner.py assemble --pins core/runner/ge-proton11-12.json --work /tmp/runner-work \
    --base runners/GE-Proton11-12-x86_64 --out runners/patched
```

`fetch` downloads the pinned release and its published SHA-512 checksum, verifies it, and installs it under `--dest` in a folder named for the tag. It refuses an existing folder or an unsafe archive and removes partial downloads on failure.

Unix patches (the window drivers, `0006` and `0008` to `0011`, and `wined3d`, `0012`, which GE changes beyond what PE mode replays) use a separate work directory and GE's full pinned Wine preparation. `./setup` builds and assembles the PE mode first, then overlays the Unix mode when required:

```sh
core/runner.py build --mode unix --pins core/runner/ge-proton11-12.json --work /tmp/runner-work-unix --base runners/patched-pe
core/runner.py assemble --mode unix --pins core/runner/ge-proton11-12.json --work /tmp/runner-work-unix --base runners/patched-pe --out runners/patched
```

**Tools and packages.** Unix mode requires bash, patch, pkg-config and GNU objdump in addition to the PE tools. Install development packages for X11, Xi, Xext, Xrandr, Xrender, Xfixes, Xcursor, Xcomposite, Xinerama, Vulkan, Wayland client, xkbcommon, xkbregistry, OpenCL (the ICD loader, `pkg-config OpenCL`), FreeType, fontconfig, D-Bus and EGL, plus libc development files. `./setup` names every missing one before any work.

**Build environment.** Job 17 used autoconf 2.72 and LLVM 22. Build in a Proton-compatible runtime/sysroot; arbitrary host builds can require newer system libraries.

**Pinned inputs.** The mode pins GE's prep script and source tree, staging, Wine, WineOpenXR tree and Vulkan XML bytes, and runs only GE's Wine preparation in a disposable checkout. It uses Job 17's `autoconf -f && autoheader -f` regeneration.

**Outputs.** It builds `winex11.so`, `winex11.drv`, `winewayland.so`, `winewayland.drv`, `opencl.so` and `opencl.dll`, retaining dependency builds privately.

**The OpenCL library.** GE ships `opencl.so` built without OpenCL. This Wine's `configure.ac` has no Linux check for the OpenCL library, so Unix mode passes `OPENCL_LIBS=-lOpenCL` to configure, and the build fails unless the new `opencl.so` imports `clGetPlatformIDs` from the loader.

**Assembly checks.** Assembly verifies base and output hashes, provenance, and matching driver versions in the header, both built drivers and base win32u before publishing. Missing or ambiguous version decoding is refused.

**GPU image processing.** GE's `proton` script sets `opencl` to `n,d` for every program, which turns Wine's builtin OpenCL off.

**The override line.** Assembly replaces exactly that line in the copied runner with a no-op and refuses if it appears zero or several times; the base runner's script is pinned. `halation-runner.json` records the script's hash before and after.

**Rebuilds.** Use a fresh work directory after changing mode, configure features, relevant source pins or patches. Comments, JSON formatting and pins used only by the other mode leave build identities unchanged.

**Build tools.** Building needs git, curl, autoconf, autoheader, perl, bison, flex, make, gcc, clang, lld and llvm-ar, and installs none of them. The entry points also require umu-run.

**Pins.** The pins file names the Wine and wine-staging commits the runner was built from and the runner's own patches for each DLL we rebuild, so the result differs from the runner only by `patches/`.

**Work and output folders.** `build` records the pins and patch contents in its work directory and refuses to reuse it if either changes; use a fresh `--work` directory after editing pins or patches. `assemble` refuses an existing output folder and writes `halation-runner.json` with each replaced DLL's hash before and after.

## Applying a profile

`python3 core/setup.py apply --profile <file> --runner <runner> --prefix <dir> --installer <local file>` applies a profile using the supplied installer. The minimal profile schema is documented in `core/setup.py`.

- **Prerequisites** come only from profile URLs and must match their recorded SHA-256 before the prefix is created or any Windows code runs.
- **Redirects** are followed only to URLs the profile lists; verified cached prerequisites can be reused offline.
- **`--dry-run`** prints the plan without writing anything.

The measured profile is `profiles/captureone/profile.json`; its setting notes are beside it. Setup applies the Windows version, DLL overrides and registry values before running the local installer:

- **Registry directives** are limited to `HKCU\Software\Wine\` and `HKCU\Software\Microsoft\Avalon.Graphics` and its subkeys; authentication and licensing directives are refused.
- **The install folder** is checked before and after every Windows step, and a change fails it. The change is noted, so the next setup does not take it for the app's own update. Only the initial installer may create an absent install folder.
- **A failed installer** with the install folder still absent is retried once after 10 seconds with the same arguments; each attempt reports its exit code and duration. A second failure or a partial install refuses completion and asks for the prefix folder to be removed before running setup again.
- **The record.** Setup records the install tree, the profile's hash and settings, and the program's version in `.setup.json`. It is written in one step and flushed; the record before it stays beside it as `.setup.json.previous`. Applying the same profile again changes nothing.
- **An earlier record** gets the fields it lacks at the next setup, once its folder is found unchanged.

### A changed profile

Setup applies the difference to the existing prefix: registry values, overrides, the Windows version and prerequisites that have not run there.

- **What does not happen.** The app's installer does not run again, and a profile that moves `install_dir` is refused. A value the profile no longer names stays; naming it with `null` removes it.
- **The app must be closed.** Setup looks for the prefix in the environment of your own processes and stops when one uses it.
- **A change that fails** is repeated by the next setup, also when you have gone back to the old profile: until every step is done, the record says that no profile is applied. Prerequisites are recorded as they finish.

### An app that updated itself

Setup takes a changed install folder for the app's own update, prints both versions and records the new folder, when all of this holds:

- **A higher version.** The recorded program is a regular file in the folder, and its version is higher than the recorded one, compared as numbers.
- **The same bytes.** That version comes from the bytes the new record holds, and a second look at the whole folder and the version agrees.
- **Nothing runs** in the prefix.

Anything else is refused and left as it is, with its reason: the same or a lower version, a missing or unreadable program, or files one of setup's own steps changed.

- **Once only:** a record made before versions were recorded, over a folder that has changed since, counts a changed program file instead.
- **Not told apart:** an update that stopped after it replaced the program looks like a finished one. Finishing it later changes files under a recorded version, which is refused.
- **Tested** with stand-in programs. No real app update has gone through it yet.

## Prefix and launch

- **Create.** `core/prefix.py create --runner <runner> --prefix <dir>` builds one persistent prefix with a Proton runner through umu.
- **Run.** `run --runner <runner> --prefix <dir> -- <program>` starts a program in it (umu-run with that runner, no runtime update, X11). The program's working folder is the one `run` is started from.
- **Runner versions.** The prefix remembers the newest runner it has seen and refuses an older one, since an older Proton rewrites a newer prefix. Recording an older runner never lowers that saved version.
- **Watch.** `run --watch <dir>` hashes a folder before and after with `treehash.py` and exits 3 if anything in it changed; pointed at the app's install folder, that is the "install folder untouched" check.

Add `--profile profiles/captureone/profile.json` to `run` to apply its launch environment, including the web view's `--disable-gpu` argument. A profile without an `environment` entry leaves the launch environment unchanged; profiles cannot override names beginning with WINE, PROTON, UMU, DXVK or STEAM_COMPAT_, any variable set by the runner environment, or GAMEID, VKD3D_SHADER_CACHE_PATH, PATH, LD_PRELOAD and LD_LIBRARY_PATH.

Profile launches also watch `install_dir`, resolved inside the prefix, before and after execution. A change exits 3, unless the app's program has a higher version afterwards: then `run` lists the changed paths, says that the app updated itself and returns the program's own exit code. `--watch` overrides that directory and has no such exception.

**OpenCL at launch.** Proton runs programs in a runtime container that has no OpenCL driver list, so Wine's OpenCL would find no platform.

**The driver list.** `./run` copies the host's `/etc/OpenCL/vendors/*.icd` into `<data>/opencl-vendors` at every start, leaving out Mesa's rusticl, and passes it as `OCL_ICD_VENDORS` (`core/prefix.py run --opencl-vendors <dir>`); a value already set in the environment wins. With no host driver it prints a note and the app processes on the CPU.

`./run --display wayland` (or `core/prefix.py run --display wayland`) opts into the Wayland driver; this was measured on the window probe only, not yet on the application. The default remains X11.

`./run --display wayland --renderer wined3d` selects Wine's own Direct3D. It reaches WPF only when the prefix lets WPF use the GPU; the Capture One profile draws WPF in software (`DisableHWAcceleration=1`). Application rendering through `./run` remains untested; the measured run used the lab harness.

## Optional: the plugin runtime

The .NET Framework 4.8 supplemental licence permits use only by Windows licence holders. If eligible, add it yourself with the runner's own binaries, for example:

```sh
WINEPREFIX="<prefix>" WINE="<runner>/files/bin/wine" WINESERVER="<runner>/files/bin/wineserver" winetricks -q dotnet48
python3 core/setup.py finish-netfx48 --runner "<runner>" --prefix "<prefix>"
```

The project never downloads or runs the runtime installer. `finish-netfx48` requires the existing framework files and a native `mscoree` override, then restores `win10` and builtin `uiautomationcore`, `dxva2` and `evr` without changing the install folder. Repeating it changes nothing. After an app update, run `./setup` first.

## Display colour profile

`core/display_profile.py link --runner <runner> --prefix <prefix>` gives Windows programs in the prefix the host's monitor profile.

- **Which profile:** a file you name with `--profile`, else colord's default profile for the first display, else the X11 root window's `_ICC_PROFILE`. Quote a path with spaces, for example `--profile "profiles/Display Profile.icc"`; detected paths with spaces work too.
- **Where it goes:** into the prefix's colour directory, associated with every display as the default in the per-user place Windows uses (`ProfileAssociations\Display`, with `UsePerUserProfiles` set), because Wine rebuilds the system-wide display keys at every start.
- **No host profile:** nothing changes and programs keep sRGB.
- **Checking it:** `show --prefix <prefix>` prints what is associated. Programs read it through the WCS functions, which `patches/0005` implements.

## Pushing

Push only through the leak check:

```sh
tools/install-hooks                                      # a plain git push runs the check too
HALATION_FORBIDDEN=<term list> tools/push origin main    # checks exactly what it pushes, then pushes
```

`tools/prepush.py --tree` checks the working tree instead.

- **It refuses** paths outside the allow-list, listed terms (printed as numbers, never the term), binaries by magic bytes, key and token shapes, the local user name, home path and a non-noreply git mail, and files over 1 MiB.
- **Exit codes:** 0 clean, 1 on a finding, 2 on its own error. `--ci` reports a missing term list without failing.

## Tests

```sh
python3 -m unittest discover -s tests
```

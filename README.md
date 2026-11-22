<!-- rumdl-disable MD041 -->
![Halation: a Linux compatibility layer for Capture One. Clone, setup, run.](docs/banner.svg)

# Run Capture One on Linux

**[Quick start](#quick-start)** · **[How it works](#how-it-works)** · **[Ground rules](#ground-rules)** · **[Status](#status)** · **[Contributing](#contributing)** · **[Reference](docs/reference.md)**

Halation runs the Windows edition of Capture One on a Linux desktop, through Proton and Wine. You bring the installer you downloaded; `setup` builds a pinned GE-Proton runner with a small set of Wine fixes, creates a Windows prefix and installs the app into it, and `run` starts it.

The app itself is never modified, and every Wine fix comes with a small program that shows the bug without Capture One.

- **Your install stays untouched.** Setup and every launch hash the install folder before and after; one changed file fails the run.
- **Exports match Windows.** Three RAW files exported to TIFF on the CPU were pixel-identical to the same machine's Windows install: 0 differing pixels.
- **Fixes, not workarounds.** Eleven Wine patches fix what the app trips over: colour profiles, file dialogs, input devices, helper windows and GPU-drawn windows. Each one has a repro program in `repro/`.
- **Pinned and checked.** Setup downloads one pinned GE-Proton release, checks its published SHA-512 and refuses anything that does not match.
- **Open to build on.** The generic parts live in `core/`, so the same layer can carry other Windows apps through their own profile.

```console
$ ./setup "Capture One installer.exe"
fetch: complete (38.5s)
build: complete (59.0s)
assemble: complete (0.3s)
build-unix: complete (68.9s)
assemble-unix: complete (0.4s)
prefix: complete (164.6s)
$ ./run
```

*A fresh clone on the maintainer's PC: 335 s from clone to an installed app.*

**Early, unreleased, provided as is.** It has been tested on one machine so far, and reports from other distributions and GPUs are the most useful help right now.

> [!NOTE]
> Halation is an independent project, not affiliated with Capture One A/S, Phase One or Fujifilm. You need your own licensed copy of Capture One; see [Legal](#legal).

## Contents

- [Quick start](#quick-start)
- [How it works](#how-it-works)
- [Ground rules](#ground-rules)
- [Wine patches](#wine-patches)
- [Status](#status)
- [Questions](#questions)
- [Contributing](#contributing)
- [Layout](#layout)
- [Legal](#legal)

## Quick start

**You need:**

- x86-64 Linux with a Vulkan driver (developed on NVIDIA);
- [umu-launcher](https://github.com/Open-Wine-Components/umu-launcher) (`umu-run`) and Python 3;
- the build tools for the Wine fixes: git, curl, autoconf, autoheader, perl, bison, flex, make, gcc, clang, lld and llvm-ar, plus the development packages listed in the [reference](docs/reference.md#building-the-runner);
- the Capture One installer for Windows, downloaded from the official site.

**Then:**

```sh
git clone <repository-url> halation
cd halation
./setup "<path to the Capture One installer>"
./run
```

Setup keeps everything in `$XDG_DATA_HOME/halation-layer` (`--data <dir>` picks another place). It skips steps it has already verified, so running it again is safe. Arguments after `./run --` go to the app.

![Setup, step by step, drawn as a darkroom test strip: fetch 38.5 s (upstream runner), build 59 s, assemble 0.3 s, build-unix 68.9 s and assemble-unix 0.4 s (Halation's Wine fixes), prefix 164.6 s (your installer, in a new prefix); 335 s in all on one fresh clone.](docs/setup.svg)

## How it works

![The layer stack, drawn as a cross-section of colour film. From top: 1, Capture One, your licensed copy, never modified; 2, the Windows prefix, built by setup from the profile; 3, GE-Proton 11-12, upstream and pinned; 4, Halation's 11 Wine fixes as the anti-halation layer; 5, your Linux desktop as the base.](docs/layers.svg)

- **`setup`** fetches the pinned GE-Proton release, rebuilds only the Wine libraries that `patches/` changes, and lays them over the runner. It then creates one Windows prefix, applies the app's profile (Windows version, library overrides, a few registry values) and runs your installer silently.
- **`run`** starts the app through `umu-run` with the profile's environment, passes the host's OpenCL drivers in, and checks the install folder before and after.
- **Everything else is upstream:** Wine, DXVK and vkd3d-proton come with GE-Proton; Halation pins a release and changes only what its patches name.

The [reference](docs/reference.md) covers each step in detail: the runner build, profiles, the prefix, the display colour profile and the optional .NET Framework runtime for the app's plugins.

## Ground rules

![Where the fixes live. Changed by Halation: the Wine runner, the Windows prefix and the launch environment. Never touched: the Capture One install, hashed before and after every step; sign-in and activation; your licence.](docs/boundary.svg)

| | Rule | What it means | Enforced by |
|---|---|---|---|
| 1 | **The install folder is never modified** | Fixes live in the runner, the prefix or the launch environment, never in the app's files. | A hash of the install tree before and after every setup step and launch (`core/treehash.py`); a change fails the run. |
| 2 | **Licensing is never touched** | Sign-in, trial, activation and machine checks run exactly as shipped. No code reads, emulates, skips or patches them. | Profile entries that touch authentication or licensing are refused (`core/setup.py`). |
| 3 | **You bring the software** | Setup installs the copy you downloaded. Halation never downloads, mirrors or links it. | `./setup` takes a local installer and nothing else. |
| 4 | **Every fix stands on its own** | Each Wine fix ships with a small Windows program that shows the bug without Capture One, so anyone can check it, and Wine can take it upstream. | `repro/`, one program per behaviour. |
| 5 | **Nothing private leaves** | Logs, traces and machine details stay on the machine that made them. | Every push runs a leak check over each commit, message, path and ref (`tools/prepush.py`). |

## Wine patches

![Contact sheet of the eleven Wine patches, one frame each with its component and the repro program that shows it; a twelfth open frame invites your fix. The table below lists the same patches as text.](docs/patches.svg)

| Patch | What it fixes | Shown by |
|---|---|---|
| `0001` mscms | `GetStandardColorSpaceProfileW` reports the size a caller must allocate | `repro/p4-colour` |
| `0002` comdlg32 | The file dialog returns its folder view through `QueryService` | `repro/p5b-dialog-com` |
| `0003` shell32 | `IFolderView2::GetSelection`, so a picked folder comes back to the app | `repro/p5b-dialog-com` |
| `0004` shell32 | `SHCreateItemWithParent` | `repro/p5b-dialog-com` |
| `0005` mscms | The WCS default display profile lookups, so programs see the monitor's colour profile | `repro/p4-colour` |
| `0006` winex11 | No crash when an input device disappears while Wine reads its button map | `repro/p1d-wpf-input` (device churn) |
| `0008` winex11 | Constant layered alpha applied in the window surface, so helper windows are see-through on X11 | `repro/p8-layered-alpha` |
| `0009` winewayland | The layered alpha survives a change of surface role on Wayland | `repro/p8-layered-alpha` |
| `0010` winex11, winewayland | Near-invisible layered windows (constant alpha below 8) stay unmapped, so they take no pointer input and draw no blurred band | `repro/p8-layered-alpha` |
| `0011` winex11 | A window hidden right after it is shown stays hidden (backport of Wine `2b05f63811`) | `repro/p9-show-hide` |
| `0012` wined3d | Partial copy presents are shown as full frames, so GPU-drawn windows draw completely | `repro/p1d-wpf-input` (`--autotype`) |

Patches are LGPL-2.1-or-later, as Wine is, and are written to go upstream to WineHQ.

## Status

**Early, unreleased, provided as is.** Tested on one machine; other setups are untested.

![Tested on: Nobara 44, an NVIDIA RTX 3090, Hyprland through XWayland, Capture One 16.9 and GE-Proton 11-12. Provided as is.](docs/tested.svg)

**Known issues:**

- File dialogs are Wine's own, not your desktop's.
- Edits show more slowly than on Windows: 250 to 395 ms from a slider change to the preview.
- True fullscreen can flicker (seen with adaptive sync on).

## Questions

### Is there a Linux version of Capture One?

Not a native one: Capture One's desktop editions are for Windows and macOS. Halation runs the Windows edition on Linux through Proton and Wine, with no virtual machine and no Windows installation.

### Do I need a licence?

Yes, your own licensed copy of Capture One. Halation installs the copy you downloaded and leaves sign-in and activation to the app.

### Why not plain Wine?

Capture One trips over several Wine bugs: colour profile lookups, file dialogs, helper windows and input devices. Halation pins a GE-Proton release, adds fixes for those bugs, and sets up the prefix the way the app needs it.

### Which distributions and desktops work?

Tested so far on Nobara 44 (Fedora-based) with an NVIDIA GPU, on Hyprland through XWayland. Other x86-64 distributions with a Vulkan driver, and desktops such as KDE Plasma and GNOME, are untested; a report from yours helps.

## Contributing

Halation is meant to be built on. Branches, fixes, profiles for other apps and plain test reports are all welcome; [CONTRIBUTING.md](CONTRIBUTING.md) says how, and every commit carries a DCO sign-off. Everyone follows the [Code of Conduct](.github/CODE_OF_CONDUCT.md).

- **Test it on your machine.** A run on another distribution, desktop or GPU is the most useful report right now.
- **Fix a Wine bug.** Add the patch with a repro program that shows the bug on its own, and a test.
- **Bring another app.** `core/` is generic; a new app needs only its own profile.

## Layout

```text
core/       setup, launch and runner pieces any Windows app can use
profiles/   what is specific to one app (captureone/)
patches/    Wine patches (LGPL-2.1-or-later)
repro/      small Windows programs that show a Wine behaviour on their own
tests/      tests for everything above (python3 -m unittest discover -s tests)
tools/      the leak check and push helpers
docs/       the reference and the images on this page
LICENSES/   licence texts
```

## Legal

**Disclaimer.** *Draft wording, pending the maintainer's sign-off.*

- **Independent.** Halation is a user-made project. It is not affiliated with, endorsed by or sponsored by Capture One A/S, Phase One or Fujifilm.
- **Your own licensed copy.** You need your own licensed copy of Capture One. Halation never downloads, mirrors or links the software, and never touches sign-in, trial or activation.
- **No Capture One code.** The repository contains no installer, library, model, profile or decompiled text from Capture One.
- **Why it exists.** So licensed customers on Linux can run the copy they paid for (interoperability).

**Licence.** Own code is MIT; Wine patches are LGPL-2.1-or-later, as Wine is; the Code of Conduct is CC BY 4.0. Each file carries an SPDX header, and `REUSE.toml` covers the rest.

**Trademarks.** Capture One is a trademark of its owner, used here only to say what Halation runs. Halation's name and logo carry no Capture One, Phase One or Fujifilm marks; the repository name uses "Capture One" only to say what Halation is for.

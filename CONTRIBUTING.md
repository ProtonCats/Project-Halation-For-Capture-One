# Contributing to Halation

**Halation is meant to be built on.** Fork it, branch it, improve it. Fixes, profiles for other apps, documentation and plain test reports are all welcome, and so are long-running community branches for bigger experiments.

## Ground rules for every contribution

These keep Halation clean and open. A pull request that breaks one is closed, however good the code.

- **No Capture One files.** No installer, library, model, colour profile or icon, and no screenshot that shows an account or a catalog.
- **No reverse-engineered material.** No decompiled or disassembled Capture One code, and no notes taken from it. Fixes come from public Microsoft and Wine documentation, open-source code under a compatible licence, and what you observe a program do on Windows and under Wine.
- **Licensing stays untouched.** Nothing may read, emulate, skip or patch sign-in, trial, activation or machine checks.
- **The install folder stays untouched.** A fix lives in the runner, the prefix registry or the launch environment.
- **Every Wine fix stands on its own.** It comes with a small program in `repro/` that shows the behaviour without Capture One, on Windows and under Wine.

## Report a test run

A run on a machine unlike the maintainer's is the most useful thing anyone can send. Open an issue with:

- the distribution and version, the desktop or compositor, and the session type (Wayland or X11);
- the GPU and its driver version;
- the Capture One version and the output of `./setup`;
- what you did, what you expected and what you saw.

Remove user names, home paths, serial numbers and account details before posting.

## Fix a Wine bug

1. **Search first.** WineHQ Bugzilla, Wine merge requests and the GE-Proton issues often have a fix or a lead; a backport of an upstream commit beats new code.
2. **Show the bug on its own.** Add a program under `repro/` (the existing ones are small self-contained .NET 8 programs) that behaves one way on Windows and another under Wine.
3. **Write the patch** in `patches/` as `NNNN-component-Summary.patch`, with an SPDX header (LGPL-2.1-or-later) and a message that says what Windows does, what Wine did, and which repro program shows it.
4. **Add a test** under `tests/` and run `python3 -m unittest discover -s tests`.

## Bring another app

`core/` knows nothing about Capture One. A new app needs only `profiles/<app>/profile.json` and a `README.md` beside it that explains each setting and what it fixes, as `profiles/captureone/` does. The profile schema is documented in `core/setup.py`.

## Code and commits

- Every file carries an SPDX header: MIT for own code, LGPL-2.1-or-later for Wine patches.
- Keep commits small, with messages that say what changed and why.
- The test suite passes before a pull request.
- Run `tools/install-hooks` once; every push then runs the leak check (`tools/prepush.py`), which refuses files outside the allow-list, binaries, key and token shapes and oversized files.

## Sign your commits

Every commit carries a `Signed-off-by` line (`git commit -s`). It certifies the [Developer Certificate of Origin 1.1](https://developercertificate.org/): you wrote the change, or have the right to submit it under the project's licences. Your sign-off also confirms the change follows the ground rules above.

## Conduct

Everyone taking part follows the [Code of Conduct](.github/CODE_OF_CONDUCT.md).

## Branches

Pull requests from forks are welcome. A larger experiment (a Wayland-first runner, tuning for AMD or Intel GPUs, another app's profile) can live as a community branch: open an issue to propose it, and it gets its own branch here.

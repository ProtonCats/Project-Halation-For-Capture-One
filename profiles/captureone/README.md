<!-- SPDX-FileCopyrightText: 2026 ProtonCats and contributors -->
<!-- SPDX-License-Identifier: MIT -->

# Profile settings

Each setting in `profile.json` and what it fixes.

- Install folder: `drive_c/Program Files/Capture One` identifies the protected install tree.
- Installer switches: `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-` select the public silent installation mode; `/LOG=C:\halation-install.log` keeps the installer's own log inside the prefix (`drive_c/halation-install.log`), since the installer sometimes stops in its preparing stage.
- Prerequisites: none; the installer supplies its own runtimes.
- Windows version: `win10` selects the measured base configuration.
- `wintab32=disabled`: avoids a fatal X error during the tablet scan.
- `Avalon.Graphics\DisableHWAcceleration=1`: forces software WPF rendering because GPU rendering is black or white.
- `msedgewebview2.exe\Version=win8`: avoids the missing GPU swap chain in the web view helper.
- `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--disable-gpu`: disables GPU use in the web view at launch.

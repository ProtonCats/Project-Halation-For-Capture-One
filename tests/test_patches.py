# SPDX-FileCopyrightText: 2026 ProtonCats and contributors
# SPDX-License-Identifier: MIT
import re
import unittest
from pathlib import Path

PATCHES = sorted((Path(__file__).resolve().parents[1] / "patches").glob("*.patch"))
HUNK = re.compile(r"@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")


def hunk_errors(text):
    """Hunks whose header line counts disagree with the lines that follow it."""
    errors, lines = [], text.splitlines()
    for i, line in enumerate(lines):
        m = HUNK.match(line)
        if not m:
            continue
        old, new = int(m[1] or 1), int(m[2] or 1)
        o = n = 0
        for body in lines[i + 1:]:
            if o == old and n == new:
                break
            kind = body[:1]
            if kind in (" ", ""):
                o, n = o + 1, n + 1
            elif kind == "-":
                o += 1
            elif kind == "+":
                n += 1
            elif kind != "\\":
                break
        if (o, n) != (old, new):
            errors.append(f"{line}: body has {o},{n}")
    return errors


class PatchHunks(unittest.TestCase):
    def test_every_hunk_header_matches_its_body(self):
        self.assertTrue(PATCHES)
        for patch in PATCHES:
            with self.subTest(patch=patch.name):
                self.assertEqual(hunk_errors(patch.read_text()), [])

    def test_window_driver_patches_route_only_to_unix_mode(self):
        from core import runner
        patches = runner.our_patches()
        unix = runner.mode_patches(patches, "unix")
        self.assertEqual([(p.name[:4], modules) for p, modules in unix],
                         [("0006", ["winex11.drv"]), ("0007", ["winex11.drv"]), ("0008", ["winex11.drv"]), ("0009", ["winewayland.drv"]),
                          ("0010", ["winewayland.drv", "winex11.drv"]), ("0011", ["winex11.drv"]),
                          ("0012", ["wined3d"]), ("0013", ["win32u"])])
        self.assertFalse(any(p.name.startswith(("0006", "0007", "0008", "0009", "0010", "0011", "0012", "0013"))
                             for p, _ in runner.mode_patches(patches, "pe")))

    def test_a_miscounted_header_is_caught(self):
        good = "@@ -1,2 +1,2 @@\n a\n-b\n+c\n"
        self.assertEqual(hunk_errors(good), [])
        self.assertTrue(hunk_errors(good.replace("+1,2", "+1,3")))


if __name__ == "__main__":
    unittest.main()

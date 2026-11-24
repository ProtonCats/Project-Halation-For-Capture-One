# SPDX-FileCopyrightText: 2026 ProtonCats and contributors
# SPDX-License-Identifier: MIT
import struct
import tempfile
import unittest
from pathlib import Path

from core import peversion

SIGNATURE = 0xFEEF04BD


def pe(version=(1, 2, 3, 4), section=b".rsrc", structure=0x10000, extra=b""):
    """A minimal PE file whose resource section holds one fixed version block."""
    a, b, c, d = version
    block = b"resource data " + struct.pack("<IIII", SIGNATURE, structure, a << 16 | b, c << 16 | d) + extra
    head = bytearray(0x200)
    head[:2] = b"MZ"
    struct.pack_into("<I", head, 0x3C, 0x80)
    head[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<HH", head, 0x84, 0x8664, 1)
    struct.pack_into("<H", head, 0x94, 0xF0)
    struct.pack_into("<8sIIII", head, 0x98 + 0xF0, section, len(block), 0x1000, len(block), 0x200)
    return bytes(head) + block


class FileVersion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / "app.exe"

    def version(self, data):
        self.file.write_bytes(data)
        return peversion.file_version(self.file)

    def test_the_four_parts_are_read_in_order(self):
        self.assertEqual(self.version(pe((16, 9, 0, 3894))), "16.9.0.3894")
        self.assertEqual(self.version(pe((1, 65535, 2, 65535))), "1.65535.2.65535")

    def test_a_signature_outside_the_resource_section_does_not_count(self):
        self.assertIsNone(self.version(pe(section=b".text")))
        self.assertIsNone(self.version(pe(section=b".data") + struct.pack("<IIII", SIGNATURE, 0x10000, 1, 1)))

    def test_damaged_or_foreign_files_have_no_version(self):
        whole = pe()
        for data in (b"", b"program", b"MZ", whole[:0x90], whole[:0x200], whole[:0x20c + 8], pe(structure=0x20000),
                     whole.replace(b"PE\0\0", b"NE\0\0")):
            with self.subTest(size=len(data)):
                self.assertIsNone(self.version(data))

    def test_a_missing_file_or_a_folder_has_no_version(self):
        self.assertIsNone(peversion.file_version(self.file))
        self.assertIsNone(peversion.file_version(self.file.parent))

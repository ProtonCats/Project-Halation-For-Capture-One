# SPDX-FileCopyrightText: 2026 ProtonCats and contributors
# SPDX-License-Identifier: MIT
import hashlib
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


class Version(unittest.TestCase):
    def test_the_four_parts_are_read_in_order(self):
        self.assertEqual(peversion.version_in(pe((16, 9, 0, 3894))), "16.9.0.3894")
        self.assertEqual(peversion.version_in(pe((1, 65535, 2, 65535))), "1.65535.2.65535")

    def test_a_signature_outside_the_resource_section_does_not_count(self):
        self.assertIsNone(peversion.version_in(pe(section=b".text")))
        self.assertIsNone(peversion.version_in(pe(section=b".data") + struct.pack("<IIII", SIGNATURE, 0x10000, 1, 1)))

    def test_a_signature_with_another_structure_number_is_passed_over(self):
        block = struct.pack("<IIII", SIGNATURE, 0x10000, 3 << 16, 4)
        self.assertEqual(peversion.version_in(pe(structure=0x20000, extra=block)), "3.0.0.4")
        self.assertEqual(peversion.version_in(pe(structure=SIGNATURE, extra=b"x" + block)), "3.0.0.4")

    def test_damaged_or_foreign_files_have_no_version(self):
        whole = pe()
        for data in (b"", b"program", b"MZ", whole[:0x90], whole[:0x200], whole[:0x20c + 8], pe(structure=0x20000),
                     whole.replace(b"PE\0\0", b"NE\0\0")):
            with self.subTest(size=len(data)):
                self.assertIsNone(peversion.version_in(data))

    def test_versions_compare_as_numbers(self):
        self.assertGreater(peversion.parts("1.10.0.0"), peversion.parts("1.9.99.99"))
        self.assertEqual(peversion.parts("16.9.0.3894"), (16, 9, 0, 3894))


class ProgramVersion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name) / "app"
        (self.folder / "bin").mkdir(parents=True)

    def test_version_and_hash_come_from_the_same_bytes(self):
        data = pe((2, 0, 0, 1))
        (self.folder / "bin/app.exe").write_bytes(data)
        self.assertEqual(peversion.program_version(self.folder, "bin/app.exe"), ("2.0.0.1", hashlib.sha256(data).hexdigest()))
        (self.folder / "bin/app.exe").write_bytes(b"no program")
        self.assertEqual(peversion.program_version(self.folder, "bin/app.exe"), (None, hashlib.sha256(b"no program").hexdigest()))

    def test_only_a_regular_file_inside_the_folder_is_read(self):
        outside = self.folder.parent / "outside"
        outside.mkdir()
        (outside / "app.exe").write_bytes(pe())
        (self.folder / "link.exe").symlink_to(outside / "app.exe")
        (self.folder / "inner.exe").write_bytes(pe())
        (self.folder / "inner-link.exe").symlink_to("inner.exe")
        (self.folder / "linked").symlink_to(outside)
        for relative in ("absent.exe", "bin", "link.exe", "inner-link.exe", "linked/app.exe", "../outside/app.exe",
                         str(outside / "app.exe")):
            with self.subTest(relative=relative):
                self.assertEqual(peversion.program_version(self.folder, relative), (None, None))

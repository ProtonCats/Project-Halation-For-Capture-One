# SPDX-FileCopyrightText: 2026 ProtonCats and contributors
# SPDX-License-Identifier: MIT
"""The file version a Windows program carries in its version resource."""
import hashlib
import os
from pathlib import Path
import stat
import struct

SIGNATURE = struct.pack("<I", 0xFEEF04BD)


def version_in(data):
    """The four-part version from the fixed version block in a PE file's resource section, or None."""
    try:
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        if data[:2] != b"MZ" or data[pe:pe + 4] != b"PE\0\0":
            return None
        count, optional = struct.unpack_from("<H", data, pe + 6)[0], struct.unpack_from("<H", data, pe + 20)[0]
        for number in range(count):
            name, _, _, size, start = struct.unpack_from("<8sIIII", data, pe + 24 + optional + number * 40)
            if name.rstrip(b"\0") != b".rsrc":
                continue
            section = data[start:start + size]
            at = section.find(SIGNATURE)
            while at >= 0:
                structure, high, low = struct.unpack_from("<III", section, at + 4)
                if structure == 0x10000:
                    return f"{high >> 16}.{high & 0xFFFF}.{low >> 16}.{low & 0xFFFF}"
                at = section.find(SIGNATURE, at + 1)
            return None
    except struct.error:
        return None
    return None


def parts(version):
    """A version as numbers, so 1.10 is higher than 1.9."""
    return tuple(int(part) for part in version.split("."))


def program_version(folder, relative):
    """(version, SHA-256) of a program, both from one read; (None, None) unless it is a regular file inside folder."""
    path = Path(folder) / relative
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode) or not path.resolve().is_relative_to(Path(folder).resolve()):
            return None, None
        data = path.read_bytes()
    except OSError:
        return None, None
    return version_in(data), hashlib.sha256(data).hexdigest()

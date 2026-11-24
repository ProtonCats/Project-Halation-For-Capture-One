# SPDX-FileCopyrightText: 2026 ProtonCats and contributors
# SPDX-License-Identifier: MIT
"""The file version a Windows program carries in its version resource."""
from pathlib import Path
import struct

SIGNATURE = struct.pack("<I", 0xFEEF04BD)


def file_version(path):
    """The four-part version from the fixed version block in a PE file's resource section, or None."""
    try:
        data = Path(path).read_bytes()
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        if data[:2] != b"MZ" or data[pe:pe + 4] != b"PE\0\0":
            return None
        count, optional = struct.unpack_from("<H", data, pe + 6)[0], struct.unpack_from("<H", data, pe + 20)[0]
        for number in range(count):
            name, _, _, size, start = struct.unpack_from("<8sIIII", data, pe + 24 + optional + number * 40)
            if name.rstrip(b"\0") != b".rsrc":
                continue
            before, signature, block = data[start:start + size].partition(SIGNATURE)
            if not signature:
                return None
            structure, high, low = struct.unpack_from("<III", block)
            if structure != 0x10000:
                return None
            return f"{high >> 16}.{high & 0xFFFF}.{low >> 16}.{low & 0xFFFF}"
    except (OSError, struct.error):
        return None
    return None

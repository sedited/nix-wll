"""Small PNG status dots, generated without external image dependencies."""

import struct
import zlib


def _png(color):
    def chunk(kind, data):
        return (struct.pack("!I", len(data)) + kind + data
                + struct.pack("!I", zlib.crc32(kind + data)))

    pixels = bytearray()
    for y in range(16):
        pixels.append(0)  # PNG scanline filter: none.
        for x in range(16):
            inside = (2 * x - 15) ** 2 + (2 * y - 15) ** 2 <= 13 ** 2
            pixels.extend((*color, 255 if inside else 0))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack("!IIBBBBB", 16, 16, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(pixels))
            + chunk(b"IEND", b""))


GREY = _png((128, 128, 128))
GREEN = _png((46, 160, 67))

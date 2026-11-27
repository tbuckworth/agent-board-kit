"""Real images of a chosen size, made in memory for tests: a PNG any decoder opens, and the header of a JPEG, GIF and
each kind of WebP as their files begin, which is all that agentmail.imagefile reads."""
import struct
import zlib


def png(width, height):
    """A grey PNG, width by height pixels, one bit a pixel."""
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    rows = (b"\x00" + bytes((width + 7) // 8)) * height
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 1, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def jpeg(width, height):
    """A JPEG's start: SOI, a JFIF APP0 segment, then a baseline frame header (SOF0) giving the size."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    return b"\xff\xd8" + app0 + b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"


def gif(width, height):
    """A GIF's start: its signature and logical screen descriptor."""
    return b"GIF89a" + struct.pack("<HHBBB", width, height, 0, 0, 0)


def webp(width, height, chunk):
    """A WebP's start with its first chunk `chunk`: VP8 (lossy), VP8L (lossless) or VP8X (extended)."""
    if chunk == "VP8 ":
        body = b"\x00\x00\x00" + b"\x9d\x01\x2a" + struct.pack("<HH", width, height)
    elif chunk == "VP8L":
        body = b"\x2f" + struct.pack("<I", (width - 1) | (height - 1) << 14)
    else:
        body = bytes(4) + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    return b"RIFF" + struct.pack("<I", 4 + 8 + len(body)) + b"WEBP" + chunk.encode() + struct.pack("<I", len(body)) + body

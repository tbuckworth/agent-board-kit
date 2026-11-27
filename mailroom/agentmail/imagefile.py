"""An image agent mail embeds (--image), read from its bytes: its type and its size in pixels. Stdlib only."""
import struct

# The first bytes of each type but WebP, which subtype tells by its RIFF header
SIGNATURES = ((b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpeg"), (b"GIF87a", "gif"), (b"GIF89a", "gif"))
# JPEG's start-of-frame markers, which give the size: C0 to CF but DHT (C4), JPG (C8) and DAC (CC)
_JPEG_FRAMES = set(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


def subtype(data):
    """data's type as a MIME image subtype (png, jpeg, gif or webp), from its first bytes; None for any other."""
    for magic, sub in SIGNATURES:
        if data.startswith(magic):
            return sub
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def size(data):
    """data's (width, height) in pixels, as its header gives them: PNG's IHDR, GIF's screen descriptor, JPEG's frame
    header, WebP's VP8, VP8L or VP8X chunk. None when the header gives none: data is no image of those types, or is
    cut short before the size."""
    try:
        return _size(subtype(data), data)
    except struct.error:                       # a header cut short
        return None


def _size(kind, data):
    if kind == "png":
        return struct.unpack(">II", data[16:24]) if data[12:16] == b"IHDR" else None
    if kind == "gif":
        return struct.unpack("<HH", data[6:10])
    if kind == "jpeg":
        i = 2
        while i + 1 < len(data) and data[i] == 0xFF:
            marker = data[i + 1]
            if marker == 0xFF:                 # a fill byte before a marker
                i += 1
            elif marker == 0x01 or 0xD0 <= marker <= 0xD8:      # markers with no segment
                i += 2
            elif marker in _JPEG_FRAMES:
                height, width = struct.unpack(">HH", data[i + 5:i + 9])
                return width, height
            else:
                i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
        return None
    if kind == "webp":
        chunk = data[12:16]
        if chunk == b"VP8X":
            return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
        if chunk == b"VP8L" and data[20:21] == b"\x2f":
            bits = struct.unpack("<I", data[21:25])[0]
            return 1 + (bits & 0x3FFF), 1 + (bits >> 14 & 0x3FFF)
        if chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
            width, height = struct.unpack("<HH", data[26:30])
            return width & 0x3FFF, height & 0x3FFF
    return None

"""An embedded image's type and size, read from its bytes (agentmail.imagefile)."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import isolate_state  # noqa: E402,F401  (temporary state dirs; before agentmail)
import pictures  # noqa: E402
from agentmail import imagefile  # noqa: E402


class Size(unittest.TestCase):
    def test_each_type_gives_its_size(self):
        cases = [(pictures.png(1600, 900), "png"), (pictures.jpeg(1600, 900), "jpeg"), (pictures.gif(1600, 900), "gif"),
                 (pictures.webp(1600, 900, "VP8 "), "webp"), (pictures.webp(1600, 900, "VP8L"), "webp"),
                 (pictures.webp(1600, 900, "VP8X"), "webp")]
        for data, kind in cases:
            with self.subTest(kind=kind, start=data[12:16]):
                self.assertEqual((imagefile.subtype(data), imagefile.size(data)), (kind, (1600, 900)))

    def test_a_jpeg_s_size_comes_after_its_other_segments_and_fill_bytes(self):
        data = pictures.jpeg(760, 480)
        padded = data[:2] + b"\xff\xe1" + b"\x00\x06EXIF" + b"\xff" + data[2:]     # an APP1 segment, then a fill byte
        self.assertEqual(imagefile.size(padded), (760, 480))

    def test_no_size_from_a_header_that_gives_none(self):
        for data in (b"\x89PNG\r\n\x1a\n" + b"plot" * 50, b"\xff\xd8\xff", pictures.png(10, 10)[:20], b"GIF89a",
                     b"not an image at all"):
            with self.subTest(data=data[:16]):
                self.assertIsNone(imagefile.size(data))


if __name__ == "__main__":
    unittest.main()

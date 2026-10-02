import io, sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from PIL import Image

from motioncraft import security as sec
from motioncraft.engine import brain, render, vision


def png_bytes(w=200, h=300, color=(200, 30, 50)):
    img = Image.new("RGB", (w, h), (235, 235, 235))
    from PIL import ImageDraw
    ImageDraw.Draw(img).ellipse((40, 40, w - 40, h - 40), fill=color)
    b = io.BytesIO(); img.save(b, "PNG"); return b.getvalue()


class Brain(unittest.TestCase):
    def test_extracts_copy(self):
        p = brain.build_plan("Luxury ad for Aurora Perfume, 20% off, order now", 10, "9:16")
        self.assertEqual((p.style, p.offer, p.cta), ("luxury", "20% OFF", "ORDER NOW"))
        self.assertIn("Aurora Perfume", p.product_name)
        self.assertEqual(len(p.shots), 2)

    def test_deterministic_and_seeded(self):
        a = brain.build_plan("neon drink", 15, "16:9", 1).to_dict()
        self.assertEqual(a, brain.build_plan("neon drink", 15, "16:9", 1).to_dict())
        self.assertEqual(len(a["shots"]), 3)

    def test_shot_timeline_covers_duration(self):
        for d in (5, 10, 15):
            p = brain.build_plan("x ad", d, "9:16")
            self.assertAlmostEqual(p.shots[-1].end, d)


class Security(unittest.TestCase):
    def test_rejects_non_images(self):
        with self.assertRaises(sec.UploadError):
            sec.validate_image(b"<svg onload=alert(1)>", 10**6, 10**8)
        with self.assertRaises(sec.UploadError):
            sec.validate_image(b"\x89PNG\r\n\x1a\n" + b"0" * 100, 10**6, 10**8)

    def test_pixel_and_size_limits(self):
        raw = png_bytes(400, 400)
        with self.assertRaises(sec.UploadError):
            sec.validate_image(raw, 10, 10**8)
        with self.assertRaises(sec.UploadError):
            sec.validate_image(raw, 10**7, 1000)

    def test_reencodes_and_strips_trailing_data(self):
        out = sec.validate_image(png_bytes() + b"<script>trailing</script>", 10**7, 10**8)
        self.assertNotIn(b"trailing", out.png)

    def test_clean_text(self):
        self.assertEqual(sec.clean_text("a\x00b\u202e  c", 10), "ab c")
        self.assertEqual(len(sec.clean_text("x" * 500, 20)), 20)

    def test_key_compare(self):
        self.assertTrue(sec.key_ok("abc", ("zzz", "abc")))
        self.assertFalse(sec.key_ok("", ("abc",)))
        self.assertFalse(sec.key_ok("abd", ("abc",)))

    def test_rate_limiter(self):
        r = sec.RateLimiter()
        self.assertTrue(all(r.allow("b", "ip", 3) for _ in range(3)))
        self.assertFalse(r.allow("b", "ip", 3))
        self.assertTrue(r.allow("b", "other", 3))


class Vision(unittest.TestCase):
    def test_subject_isolation(self):
        s = vision.analyze(png_bytes(), (1, 2, 3))
        self.assertIn(s.method, ("keyed", "grabcut"))
        self.assertGreater(s.accent[0], s.accent[2])      # red-ish subject


class EndToEnd(unittest.TestCase):
    def test_renders_playable_mp4(self):
        with tempfile.TemporaryDirectory() as d:
            res = render.render_video(prompt="Fresh water bottle ad, shop now", duration=5, aspect="16:9", quality="draft",
                                      seed=0, overrides={}, image_png=png_bytes(), out_dir=Path(d))
            info = render.probe(res.path)
            kinds = {s["codec_type"]: s for s in info["streams"]}
            self.assertEqual(kinds["video"]["codec_name"], "h264")
            self.assertEqual(kinds["audio"]["codec_name"], "aac")
            self.assertEqual((kinds["video"]["width"], kinds["video"]["height"]), (960, 540))
            self.assertAlmostEqual(float(info["format"]["duration"]), 5.0, delta=0.2)


if __name__ == "__main__":
    unittest.main()

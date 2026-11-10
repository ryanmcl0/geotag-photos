#!/usr/bin/env python3
"""tools/location_pin.py placement: the left corners first, top right (under Instagram's carousel counter) last."""
import importlib.util
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
try:
    import cv2  # noqa: F401 — place() needs it
except ImportError:
    cv2 = None


def load():
    spec = importlib.util.spec_from_file_location('location_pin', ROOT / 'tools' / 'location_pin.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def scene(left, right, W=1080, H=720):
    """Sky over ground, with texture of strength `left` / `right` on each half (seeded, so always the same)."""
    y = np.mgrid[0:H, 0:W][0]
    img = np.stack([np.full((H, W), 120.0), np.full((H, W), 160.0), 210 - y / H * 60], -1)
    img[H // 2:] = [90, 110, 70]
    tex = np.random.default_rng(0).normal(0, 1, (H, W, 1))
    img[:, :W // 2] += tex[:, :W // 2] * left
    img[:, W // 2:] += tex[:, W // 2:] * right
    return Image.fromarray(np.clip(img, 0, 255).astype(np.uint8))


@unittest.skipIf(cv2 is None, 'needs opencv')
class Placement(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.LP = load()

    def corner(self, left, right):
        return self.LP.place(scene(left, right), 260, 90)[0]

    def test_calm_everywhere_goes_top_left(self):
        self.assertEqual(self.corner(0, 0), 'top-left')

    def test_left_a_little_busier_still_goes_left(self):
        self.assertEqual(self.corner(40, 30), 'top-left')

    def test_left_much_busier_goes_bottom_right_not_top_right(self):
        self.assertEqual(self.corner(40, 20), 'bottom-right')
        self.assertEqual(self.corner(8, 0), 'bottom-right')


if __name__ == '__main__':
    unittest.main()

"""tools/reels_local.py /reveal: the edited file behind a hosted photo is found from the trip manifest only.

    python3 -m unittest tests.test_reveal
"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("reels_local", HERE.parent / "tools" / "reels_local.py")
rl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rl)


def _site(tmp, edits):
    web = tmp / "web"
    (web / "trips" / "t1").mkdir(parents=True)
    (web / "trips" / "t1" / "manifest.json").write_text(json.dumps({
        "source": {"photos_path": str(edits)},
        "photos": [{"id": "DSC01", "source_filename": "DSC01.jpg"}, {"id": "GONE", "source_filename": "GONE.jpg"}],
    }))
    rl.WEB = web
    rl._edit_index.clear()


class RevealTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_finds_the_export_in_a_subfolder(self):
        edits = self.tmp / "Edits" / "Trip"
        (edits / "Day 2").mkdir(parents=True)
        (edits / "Day 2" / "DSC01.jpg").write_bytes(b"x")
        _site(self.tmp, edits)
        self.assertEqual(rl.edit_path("t1", "DSC01"), str(edits / "Day 2" / "DSC01.jpg"))

    def test_unknown_or_missing(self):
        edits = self.tmp / "Edits"
        edits.mkdir()
        _site(self.tmp, edits)
        self.assertIsNone(rl.edit_path("t1", "GONE"))        # listed, file not there
        self.assertIsNone(rl.edit_path("t1", "NOPE"))        # not in the manifest
        self.assertIsNone(rl.edit_path("other", "DSC01"))    # no such trip
        self.assertIsNone(rl.edit_path("../t1", "DSC01"))    # no path tricks

    def test_unmounted_folder(self):
        _site(self.tmp, self.tmp / "not-mounted")
        self.assertIsNone(rl.edit_path("t1", "DSC01"))


if __name__ == "__main__":
    unittest.main()

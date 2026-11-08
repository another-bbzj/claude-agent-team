"""Shipped original artwork must remain discoverable and safe to animate."""
import json
import os
import unittest
from unittest import mock

from test_board import BoardEnv


ORIGINAL_PETS = (
    'aurelia-engineer', 'lyra-designer', 'nova-strategist',
    'rune-analyst', 'mika-operator',
)


class CharacterRosterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.environ = mock.patch.dict(os.environ)
        cls.environ.start()
        cls.env = BoardEnv()
        cls.tb = cls.env.load()

    @classmethod
    def tearDownClass(cls):
        cls.env.cleanup()
        cls.environ.stop()

    def test_originals_are_shipped_and_have_complete_bounded_animation_metadata(self):
        info = self.tb.pet_info()
        for pet in ORIGINAL_PETS:
            with self.subTest(pet=pet):
                self.assertIn(pet, self.tb.SHIPPED_PETS)
                self.assertIn(pet, self.tb.pet_list())
                entry = info[pet]
                self.assertTrue(entry['shipped'])
                self.assertEqual(entry['file'], pet + '.png')
                for field in ('displayName', 'description', 'source', 'credit', 'license'):
                    self.assertTrue(entry.get(field), field)
                self.assertEqual(entry['license'], 'MIT')
                frames = entry['frameRects']
                self.assertEqual(len(frames), 9)
                for row in frames:
                    self.assertEqual(len(row), 8)
                    for x, y, width, height in row:
                        self.assertGreaterEqual(x, 0)
                        self.assertGreaterEqual(y, 0)
                        self.assertGreater(width, 0)
                        self.assertGreater(height, 0)
                        self.assertLessEqual(x + width, entry['w'])
                        self.assertLessEqual(y + height, entry['h'])

    def test_out_of_bounds_frame_metadata_is_not_exposed_to_renderer(self):
        path = self.tb.SPRITES / 'lyra-designer.json'
        original = path.read_text(encoding='utf-8')
        meta = json.loads(original)
        meta['frameRects'][0][0] = [0, 0, 100000, 100000]
        try:
            path.write_text(json.dumps(meta), encoding='utf-8')
            self.assertNotIn('frameRects', self.tb.pet_info()['lyra-designer'])
        finally:
            path.write_text(original, encoding='utf-8')


if __name__ == '__main__':
    unittest.main()

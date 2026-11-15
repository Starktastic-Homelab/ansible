"""Offline checks for the storage inventory parser."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'roles/storage_vm/files'))
from homelab_storage import inventory  # noqa: E402


def entry(**kw):
    e = {'id': 'media', 'namespace': 'media', 'claim': 'media-data', 'size': '10Gi', 'class': 'retained', 'origin': 'new'}
    e.update(kw)
    return e


def load(*entries):
    return inventory.load(json.dumps(list(entries)))


class InventoryTest(unittest.TestCase):
    def test_loads(self):
        v = load(entry())[0]
        self.assertEqual((v.id, v.size_bytes, v.cls, v.origin), ('media', 10 * 1024 ** 3, 'retained', 'new'))
        self.assertEqual(inventory.load('[]'), [])

    def test_rejects_bad_id(self):
        for bad in ['Bad', '-a', 'a' * 64, 'snapshot', 'snapshot-x', 'pvmove', 'pvmove1', 'abc\n', '']:
            with self.subTest(bad=bad), self.assertRaises(inventory.InventoryError):
                load(entry(id=bad))
        load(entry(id='a' * 63))

    def test_rejects_duplicate_id(self):
        with self.assertRaises(inventory.InventoryError):
            load(entry(), entry(claim='other'))

    def test_class_picks_vg(self):
        self.assertEqual(inventory.vg_for('retained'), 'retained')
        self.assertEqual(inventory.vg_for('regenerable'), 'regen')
        with self.assertRaises(inventory.InventoryError):
            inventory.vg_for('other')

    def test_parse_size(self):
        self.assertEqual(inventory.parse_size('4Mi'), 4 * 1024 ** 2)
        self.assertEqual(inventory.parse_size('2Gi'), 2 * 1024 ** 3)
        for bad in ['10Mi', '0Gi', '1G', '1Ti', '-1Gi', '1.5Gi', '1Gi\n', 10, '']:
            with self.subTest(bad=bad), self.assertRaises(inventory.InventoryError):
                inventory.parse_size(bad)

    def test_rejects_bad_shape(self):
        for text in ['{}', 'nope', json.dumps([entry(origin='x')]), json.dumps([entry(**{'class': 'x'})]),
                     json.dumps([entry(extra='1')]), json.dumps([{'id': 'a'}]), json.dumps([entry(claim='')])]:
            with self.subTest(text=text), self.assertRaises(inventory.InventoryError):
                inventory.load(text)


if __name__ == '__main__':
    unittest.main()

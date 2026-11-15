"""Offline checks for the LVM tag ledger and the cluster fence tags."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'roles/storage_vm/files'))
from homelab_storage import ledger  # noqa: E402

BOOT = '0f8e6d2a-1b3c-4d5e-8f90-a1b2c3d4e5f6'


class LedgerTest(unittest.TestCase):
    def test_ledger_roundtrip(self):
        tags = [ledger.tag_creating('a'), ledger.tag_created('b'), ledger.tag_tombstone('c'),
                ledger.tag_creating('d'), ledger.tag_created('d'), ledger.HOLD_CREATE, ledger.tag_fenced('w', BOOT)]
        led = ledger.Ledger.from_tags(tags)
        self.assertEqual([led.state(i) for i in 'abcd'], ['creating', 'created', None, 'created'])
        self.assertTrue(led.tombstoned('c'))
        self.assertFalse(led.tombstoned('a'))
        self.assertEqual(ledger.tag_pending_delete(1800000000.9), 'pending-delete:1800000000')

    def test_fenced_tag_parses_uuid(self):
        c = ledger.Cluster.from_tags([ledger.tag_fenced('k3s-worker-01', BOOT), ledger.HOLD_FENCE, 'ledger:a:created'], 'retained')
        self.assertEqual(c.fenced(), {'k3s-worker-01': BOOT})
        self.assertEqual(c.holds(), {'hold_fence'})

    def test_unknown_bootid(self):
        c = ledger.Cluster.from_tags(['fenced:w1:unknown', 'hold_create'], 'retained')
        self.assertEqual(c.fenced(), {'w1': 'unknown'})
        self.assertEqual(c.holds(), {'hold_create'})

    def test_malformed_fence_tag_raises(self):
        for bad in ['fenced:w1', 'fenced:w1:notauuid', 'fenced::' + BOOT]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ledger.Cluster.from_tags([bad], 'retained')

    def test_parse_keeps_valid_fences(self):
        c = ledger.Cluster.parse(['fenced:w1:notauuid', ledger.tag_fenced('w2', BOOT), 'fenced:w3:unknown',
                                  ledger.tag_fenced('w3', BOOT)], 'retained')
        self.assertEqual(c.fenced(), {'w2': BOOT, 'w3': BOOT})
        self.assertEqual(c.errors, ["malformed fence tag 'fenced:w1:notauuid'", 'conflicting fence tags for w3'])

    def test_cluster_tags_ignored_on_regen(self):
        with self.assertRaises(ValueError):
            ledger.Cluster.from_tags([ledger.HOLD_CREATE], 'regen')


if __name__ == '__main__':
    unittest.main()

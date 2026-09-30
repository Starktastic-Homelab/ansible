from pathlib import Path
import shutil
import unittest
import test_maintenance_executor as fixtures
from maintenance_install import finalize
from maintenance_executor import manifest


class InstallTests(unittest.TestCase):
    setUp = fixtures.ExecutorTests.setUp
    refresh = fixtures.ExecutorTests.refresh
    def test_finalize_is_content_addressed_and_existing_release_verified(self):
        releases = self.base/'releases'; releases.mkdir()
        self.manifest.unlink()
        result = finalize(self.release, releases, self.root, 'test-runner', 'a'*40)
        self.assertTrue(result['created'])
        path = Path(result['manifest'])
        self.assertEqual(path.parent.name, result['runtime_id'])
        self.assertEqual(manifest(path), self.data)
        shutil.copytree(path.parent, self.release)
        (self.release/'manifest.json').unlink()
        self.assertFalse(finalize(self.release, releases, self.root, 'test-runner', 'a'*40)['created'])
        (path.parent/'apps/scripts/storage/supervised_readonly.py').write_text('changed')
        with self.assertRaises(ValueError): finalize(self.release, releases, self.root, 'test-runner', 'a'*40)

    def test_unit_has_no_restart_or_boot_activation(self):
        source = (Path(__file__).resolve().parents[2]/'roles/maintenance_runner/templates/homelab-maintenance@.service.j2').read_text()
        for expected in ('Type=exec', 'Restart=no', 'KillMode=control-group', 'TimeoutStopSec=30s', 'RuntimeMaxSec=3600', 'UMask=0077'):
            self.assertIn(expected, source)
        self.assertNotIn('WantedBy=', source)
        self.assertNotIn('sudo', source)

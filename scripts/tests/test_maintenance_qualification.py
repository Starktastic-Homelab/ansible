from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import qualify_maintenance_executor as qualification


class QualificationGateTests(unittest.TestCase):
    def call_gate(self, virtual=True, container=False, machine='a'*32, marker='a'*32, dmi='disposable'):
        def read(path, *args, **kwargs):
            return {'/etc/machine-id':machine, '/etc/homelab-maintenance-disposable':marker,
                    '/sys/class/dmi/id/product_uuid':dmi}[str(path)]
        with patch.object(Path, 'read_text', read), patch.object(Path, 'stat', return_value=SimpleNamespace(st_uid=0,st_mode=0o100644)), \
                patch.object(Path, 'is_symlink', return_value=False), patch.object(Path, 'exists', return_value=False), \
                patch('qualify_maintenance_executor.os.geteuid', return_value=1000), \
                patch('qualify_maintenance_executor.subprocess.run', side_effect=lambda argv, **kwargs: SimpleNamespace(returncode=(0 if container else 1) if '--container' in argv else (0 if virtual else 1))):
            return qualification.gate('a'*32, '/var/tmp/maintenance-qualification-test')

    def test_explicit_disposable_vm_is_required(self):
        self.call_gate()
        for args in [dict(virtual=False),dict(container=True),dict(machine='b'*32),dict(marker='b'*32),dict(dmi=qualification.PRODUCTION_RUNNER)]:
            with self.subTest(args=args), self.assertRaises(ValueError): self.call_gate(**args)

    def test_missing_explicit_id_refuses_before_any_host_probe(self):
        with patch('qualify_maintenance_executor.subprocess.run', side_effect=AssertionError('host probe')):
            with self.assertRaises(ValueError): qualification.gate('', '/var/tmp/maintenance-qualification-test')


if __name__ == '__main__': unittest.main()

"""Exercise actual preflight assertions against synthetic Proxmox responses."""
import copy
from pathlib import Path
import unittest

from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import filter_loader, init_plugin_loader
from ansible.template import Templar

ROOT = Path(__file__).resolve().parents[2]


class PreflightTests(unittest.TestCase):
    def test_stable_identity_passes_but_template_transition_is_refused(self):
        init_plugin_loader()
        filter_loader.add_directory(str(ROOT / 'filter_plugins'))
        play = DataLoader().load_from_file(str(ROOT / 'csi-activation-preflight.yml'), trusted_as_template=True)[0]
        conditions = next(t['ansible.builtin.assert']['that'] for t in play['tasks']
                          if t['name'] == 'Refuse visibility or inventory drift')
        permissions = {'/vms': {'VM.Audit': 1}, '/pool': {'Pool.Audit': 1}, '/access': {'Sys.Audit': 0}}
        resources = [{'id': 'qemu/200', 'vmid': 200, 'type': 'qemu', 'name': 'master',
                      'node': 'pve', 'template': 0, 'cpu': 0.2}]
        variables = {}
        for name, data in [('permissions', permissions), ('acls', []), ('resources', resources)]:
            for suffix in ('', '_after'):
                variables['preflight_' + name + suffix] = {'json': {'data': copy.deepcopy(data)}}
        def accepted():
            return all(Templar(variables=variables).evaluate_conditional(c) for c in conditions)
        self.assertTrue(accepted())
        after = variables['preflight_resources_after']['json']['data'][0]
        after['cpu'] = 0.4
        self.assertTrue(accepted())
        for field, value in [('template', 1), ('node', 'other-node'), ('name', 'renamed'), ('vmid', 201)]:
            previous = after[field]
            after[field] = value
            with self.subTest(field=field):
                self.assertFalse(accepted())
            after[field] = previous


if __name__ == '__main__':
    unittest.main()

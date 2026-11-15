"""Offline checks for the additive storage bridge role."""
from pathlib import Path
import unittest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / 'roles/proxmox_storage_bridge'

def tasks():
    return yaml.safe_load((ROLE / 'tasks/main.yml').read_text())

def render(**values):
    variables = {'proxmox_storage_bridge_name': 'vmbr2', 'ansible_managed': 'managed', **values}
    return Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template((ROLE / 'templates/bridge.j2').read_text()))

def when_state(state):
    return [t for t in tasks() if state in str(t.get('when', ''))]

class StorageBridgeTests(unittest.TestCase):
    def test_refuses_without_interfaces_d_source(self):
        first = tasks()[0]
        argv = first['ansible.builtin.command']['argv']
        self.assertEqual(argv[2], r'^source /etc/network/interfaces\.d/\*')
        self.assertEqual(argv[3], '/etc/network/interfaces')
        self.assertNotIn('failed_when', first)  # grep -q fails the task when the line is missing

    def test_defaults(self):
        d = yaml.safe_load((ROLE / 'defaults/main.yml').read_text())
        self.assertEqual((d['proxmox_storage_bridge_name'], d['proxmox_storage_bridge_state']), ('vmbr2', 'present'))

    def test_renders_uplinkless_bridge(self):
        for name in ('vmbr2', 'vmbr9'):
            out = render(proxmox_storage_bridge_name=name)
            lines = [line.strip() for line in out.splitlines()]
            for want in (f'auto {name}', f'iface {name} inet manual', 'bridge-ports none', 'bridge-stp off', 'bridge-fd 0'):
                self.assertIn(want, lines)
            self.assertNotIn('address', out)

    def test_never_writes_main_interfaces_file(self):
        for t in tasks():
            for module in ('ansible.builtin.template', 'ansible.builtin.file', 'ansible.builtin.copy',
                           'ansible.builtin.lineinfile', 'ansible.builtin.blockinfile'):
                if module in t:
                    self.assertTrue(str(t[module].get('dest', t[module].get('path'))).startswith(
                        '/etc/network/interfaces.d/'), t['name'])
        tpl = next(t for t in tasks() if 'ansible.builtin.template' in t)['ansible.builtin.template']
        self.assertEqual(tpl['mode'], '0644')

    def test_present_runs_ifup_only_this_bridge(self):
        argvs = [t['ansible.builtin.command']['argv'] for t in tasks() if 'ansible.builtin.command' in t]
        self.assertIn(['ifup', '{{ proxmox_storage_bridge_name }}'], argvs)
        self.assertNotIn('ifreload', str(tasks()))

    def test_absent_order(self):
        names = [t['name'] for t in when_state('absent')]
        self.assertEqual(names, [
            'Find guests that use the bridge', 'Refuse to remove a bridge that a guest still uses',
            'Take down only this bridge', 'Remove the bridge file', 'Verify the bridge is gone'])
        by = {t['name']: t for t in tasks()}
        find = by['Find guests that use the bridge']['ansible.builtin.find']
        self.assertEqual(find['paths'], '/etc/pve/qemu-server')
        self.assertIn('bridge={{ proxmox_storage_bridge_name }}', find['contains'])
        self.assertIn('matched == 0', str(by['Refuse to remove a bridge that a guest still uses']))
        self.assertEqual(by['Take down only this bridge']['ansible.builtin.command']['argv'][0], 'ifdown')
        self.assertEqual(by['Remove the bridge file']['ansible.builtin.file']['state'], 'absent')
        last = by['Verify the bridge is gone']
        self.assertEqual(last['ansible.builtin.command']['argv'][:3], ['ip', 'link', 'show'])
        self.assertIn('rc == 0', last['failed_when'])

    def test_host_only(self):
        plays = yaml.safe_load((ROOT / 'proxmox-storage-bridge.yml').read_text())
        self.assertEqual([(p['hosts'], [r['role'] for r in p['roles']]) for p in plays],
                         [('proxmox_hosts', ['proxmox_storage_bridge'])])
        self.assertNotIn('proxmox_storage_bridge', (ROOT / 'k3s.yml').read_text())

if __name__ == '__main__':
    unittest.main()

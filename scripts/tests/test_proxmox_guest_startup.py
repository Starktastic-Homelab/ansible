"""Offline checks for the host crash settings: reboot on a kernel crash, and no crash diagnostics left in the role."""
from pathlib import Path
import unittest
from ansible.parsing.dataloader import DataLoader

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / 'roles/proxmox_guest_startup'


def tasks():
    return DataLoader().load_from_file(str(ROLE / 'tasks/main.yml'), trusted_as_template=True)


class CrashSettings(unittest.TestCase):
    def test_a_kernel_oops_reboots_the_host(self):
        sysctl = next(t for t in tasks() if t['name'] == 'Reboot 10 s after a kernel oops')
        self.assertEqual({i['key']: i['value'] for i in sysctl['loop']},
                         {'kernel.panic_on_oops': '1', 'kernel.panic': '10'})

    def test_nothing_reboots_the_host(self):
        self.assertFalse([t for t in tasks() if 'reboot' in str(t.get('ansible.builtin.command', '')).lower()])
        self.assertFalse((ROLE / 'handlers').exists())

    def test_crash_diagnostics_stay_removed(self):
        # Removed 2026-11-16 (Ben): re-adding any of these is a conscious change, so update this test with it.
        text = '\n'.join(p.read_text() for p in ROLE.rglob('*') if p.is_file()).lower()
        for word in ('kdump', 'kfence', 'rasdaemon', 'ksm', 'crashkernel', '/etc/kernel/cmdline'):
            self.assertNotIn(word, text)


if __name__ == '__main__':
    unittest.main()

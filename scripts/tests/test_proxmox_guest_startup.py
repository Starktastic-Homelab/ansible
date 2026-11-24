"""Offline checks for the host settings: reboot on a kernel crash, no crash diagnostics, ARC cap, guest balloon."""
from pathlib import Path
import unittest
from ansible.parsing.dataloader import DataLoader

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / 'roles/proxmox_guest_startup'


def tasks():
    return DataLoader().load_from_file(str(ROLE / 'tasks/main.yml'), trusted_as_template=True)


def task(name):
    return next(t for t in tasks() if t['name'] == name)


def host_vars():
    return DataLoader().load_from_file(str(ROOT / 'group_vars/proxmox_hosts/guest_startup.yml'))


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


class GuestStartup(unittest.TestCase):
    def test_storage_vm_starts_after_truenas(self):
        # The K3s workers stay unordered, so they start after both and stop first.
        self.assertEqual(host_vars()['proxmox_guest_startup'], {'100': 'order=1,up=180', '101': 'order=2,up=30'})


class HostMemory(unittest.TestCase):
    def test_arc_cap_is_8_gib(self):
        self.assertEqual(host_vars()['proxmox_guest_startup_zfs_arc_max_bytes'], 8 * 1024 ** 3)

    def test_arc_cap_is_set_at_boot(self):
        copy = task('Cap the ZFS ARC')['ansible.builtin.copy']
        self.assertEqual(copy['dest'], '/etc/tmpfiles.d/zfs-arc-max.conf')
        self.assertIn('w /sys/module/zfs/parameters/zfs_arc_max - - - - '
                      '{{ proxmox_guest_startup_zfs_arc_max_bytes }}', copy['content'])

    def test_arc_cap_is_applied_live_only_when_it_differs(self):
        self.assertEqual(task('Read the running ZFS ARC cap')['ansible.builtin.slurp']['src'],
                         '/sys/module/zfs/parameters/zfs_arc_max')
        apply = task('Apply the ZFS ARC cap to the running kernel')
        self.assertEqual(apply['ansible.builtin.command']['argv'],
                         ['systemd-tmpfiles', '--create', '/etc/tmpfiles.d/zfs-arc-max.conf'])
        self.assertIn('proxmox_guest_startup_zfs_arc_max.content', apply['when'])
        self.assertIn('!= proxmox_guest_startup_zfs_arc_max_bytes', apply['when'])

    def test_vm300_balloon_equals_its_memory(self):
        self.assertEqual(host_vars()['proxmox_guest_startup_balloon'], {'300': 4096})

    def test_balloon_is_set_only_when_it_differs(self):
        self.assertIs(task('Read ballooned guest configs')['check_mode'], False)
        balloon = task('Set guest balloon target')
        self.assertEqual(balloon['ansible.builtin.command']['argv'][3], '--balloon')
        self.assertIn('.balloon', balloon['when'])
        self.assertIn('!= item.item.value', balloon['when'])


if __name__ == '__main__':
    unittest.main()

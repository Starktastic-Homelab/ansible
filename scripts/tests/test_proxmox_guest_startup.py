"""Offline checks for the host crash settings: KSM off, and kdump that keeps the pstore record and can mount rpool."""
import base64
from pathlib import Path
import unittest
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / 'roles/proxmox_guest_startup'
HOST_CMDLINE = ('root=ZFS=rpool/ROOT/pve-1 boot=zfs intel_iommu=on i915.enable_guc=3 i915.max_vfs=7 '
                'module_blacklist=xe i915.xelp_enable_ccs=1')


def tasks():
    return DataLoader().load_from_file(str(ROLE / 'tasks/main.yml'), trusted_as_template=True)


def task(name):
    return next(t for t in tasks() if t['name'] == name)


def render(template, cmdline, extra):
    variables = {'proxmox_guest_startup_cmdline': {'content': base64.b64encode(cmdline.encode()).decode()}}
    templar = Templar(loader=DataLoader(), variables=variables)
    for key, value in extra.items():  # task vars reference each other, so resolve them in order
        variables[key] = templar.template(trust_as_template(value))
        templar = Templar(loader=DataLoader(), variables=variables)
    return templar.template(trust_as_template(template))


class CrashSettings(unittest.TestCase):
    def test_ksm_stays_off(self):
        self.assertEqual(task('Stop and disable the KSM tuning daemon')['ansible.builtin.systemd_service'],
                         {'name': 'ksmtuned', 'state': 'stopped', 'enabled': False})
        stop = task('Stop KSM and unmerge shared pages')
        self.assertIn('echo 2 > /sys/kernel/mm/ksm/run', stop['ansible.builtin.command']['argv'][-1])
        self.assertIn("== '1'", stop['when'])

    def test_crash_kernel_can_mount_the_sata_root_pool(self):
        names = [t['name'] for t in tasks()]
        drivers = task("Put the root pool's disk drivers in every initramfs")
        self.assertEqual(drivers['loop'], ['ahci', 'sd_mod'])
        # The kdump initrd is first built when the package installs, so the drivers must be listed before that.
        self.assertLess(names.index(drivers['name']), names.index('Install kdump'))

    def test_crash_kernel_keeps_debian_defaults_and_leaves_passthrough_devices_alone(self):
        config = {i['key']: i['value'] for i in task('Configure kdump')['loop']}
        append = config['KDUMP_CMDLINE_APPEND'].split()
        self.assertEqual(append[:5], ['reset_devices', 'systemd.unit=kdump-tools-dump.service', 'nr_cpus=1',
                                      'irqpoll', 'usbcore.nousb'])
        self.assertIn('module_blacklist=xe,i915,mpt3sas,nvme', append)
        self.assertIn('panic=10', append)
        self.assertIn('oops=panic', append)

    def test_cmdline_gains_crashkernel_and_keeps_the_pstore_record(self):
        copy = task('Reserve memory for the crash kernel')
        content, extra = copy['ansible.builtin.copy']['content'], copy['vars']
        self.assertEqual(render(content, HOST_CMDLINE + '\n', extra),
                         HOST_CMDLINE + ' crashkernel=1G crash_kexec_post_notifiers\n')
        done = HOST_CMDLINE + ' crashkernel=1G crash_kexec_post_notifiers\n'
        self.assertEqual(render(extra['missing'], done, {'current': extra['current']}), [])
        self.assertEqual(copy['notify'], 'Refresh boot tool')

    def test_nothing_reboots_the_host(self):
        handlers = DataLoader().load_from_file(str(ROLE / 'handlers/main.yml'), trusted_as_template=True)
        argvs = [h['ansible.builtin.command']['argv'] for h in handlers]
        self.assertEqual(argvs, [['proxmox-boot-tool', 'refresh'], ['kdump-config', 'try-reload']])


if __name__ == '__main__':
    unittest.main()

"""Offline checks for the host crash settings: KSM off, KFENCE and rasdaemon, and kdump (off by default) that keeps
the pstore record, can mount rpool and reboots when the save fails."""
import base64
from pathlib import Path
import unittest
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / 'roles/proxmox_guest_startup'
HOST_CMDLINE = ('root=ZFS=rpool/ROOT/pve-1 boot=zfs intel_iommu=on i915.enable_guc=3 i915.max_vfs=7 '
                'module_blacklist=xe i915.xelp_enable_ccs=1')
KFENCE = ' kfence.sample_interval=100'


def tasks():
    return DataLoader().load_from_file(str(ROLE / 'tasks/main.yml'), trusted_as_template=True)


def task(name):
    return next(t for t in tasks() if t['name'] == name)


def defaults():
    return DataLoader().load_from_file(str(ROLE / 'defaults/main.yml'), trusted_as_template=True)


def render(template, cmdline, extra, **overrides):
    variables = {**defaults(), **overrides,
                 'proxmox_guest_startup_cmdline': {'content': base64.b64encode(cmdline.encode()).decode()}}
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
        # A stall must reboot too, not hold pve down with every guest.
        self.assertIn('hung_task_panic=1', append)
        self.assertIn('softlockup_panic=1', append)
        self.assertTrue(task('Configure kdump')['ansible.builtin.lineinfile']['create'])

    def test_kdump_is_off_by_default(self):
        self.assertIs(defaults()['proxmox_guest_startup_kdump_enabled'], False)
        service = task('Enable or stop kdump')['ansible.builtin.systemd_service']
        self.assertEqual(render(service['enabled'], '', {}), False)
        self.assertEqual(render(service['state'], '', {}), 'stopped')
        config = {i['key']: i['value'] for i in task('Configure kdump')['loop']}
        self.assertEqual(render(config['USE_KDUMP'], '', {}), '0')
        preseed = {i['question']: i['value'] for i in task('Preseed kdump-tools')['loop']}
        self.assertEqual(render(preseed['kdump-tools/use_kdump'], '', {}), 'false')
        handlers = DataLoader().load_from_file(str(ROLE / 'handlers/main.yml'), trusted_as_template=True)
        reload = next(h for h in handlers if h['name'] == 'Reload kdump')
        self.assertEqual(render('{{ ' + reload['when'] + ' }}', '', {}), False)

    def test_enabled_kdump_is_armed_and_reboots_on_a_failed_save(self):
        on = {'proxmox_guest_startup_kdump_enabled': True}
        service = task('Enable or stop kdump')['ansible.builtin.systemd_service']
        self.assertEqual(render(service['enabled'], '', {}, **on), True)
        config = {i['key']: i['value'] for i in task('Configure kdump')['loop']}
        self.assertEqual(render(config['USE_KDUMP'], '', {}, **on), '1')
        self.assertEqual(config['KDUMP_FAIL_CMD'], 'reboot -f')
        preseed = {i['question']: i['value'] for i in task('Preseed kdump-tools')['loop']}
        self.assertEqual(render(preseed['kdump-tools/use_kdump'], '', {}, **on), 'true')
        drop_in = task('Force a reboot if saving the dump stalls or fails')['ansible.builtin.copy']['content']
        unit = render(drop_in, '', {'item': 'kdump-tools-dump'})
        self.assertIn('FailureAction=reboot-force\n', unit.split('[Service]')[0])
        # kdump-tools.service runs every normal boot; a failure there must not reboot the host.
        self.assertNotIn('FailureAction', render(drop_in, '', {'item': 'kdump-tools'}))

    def test_a_stalled_dump_forces_a_reboot(self):
        names = [t['name'] for t in tasks()]
        drop_in = task('Force a reboot if saving the dump stalls or fails')
        directories = task('Create the kdump unit drop-in directories')
        self.assertEqual(drop_in['loop'], ['kdump-tools', 'kdump-tools-dump'])
        self.assertEqual(directories['loop'], drop_in['loop'])
        self.assertLess(names.index(directories['name']), names.index(drop_in['name']))
        content = drop_in['ansible.builtin.copy']['content']
        for line in ('JobTimeoutSec=30min', 'JobTimeoutAction=reboot-force', 'TimeoutStartSec=30min'):
            self.assertIn(line + '\n', content)
        self.assertEqual(drop_in['notify'], 'Reload systemd')

    def cmdline(self, cmdline, **overrides):
        copy = task('Set the crash kernel memory reservation')
        content, extra = copy['ansible.builtin.copy']['content'], copy['vars']
        self.assertEqual(copy['notify'], 'Refresh boot tool')
        self.assertEqual(copy['when'], 'wanted != current')
        return render(content, cmdline + '\n', extra, **overrides).rstrip('\n')

    def test_disabled_cmdline_drops_crashkernel_and_keeps_the_pstore_record(self):
        live = HOST_CMDLINE + ' crashkernel=1G crash_kexec_post_notifiers'
        want = HOST_CMDLINE + ' crash_kexec_post_notifiers' + KFENCE
        self.assertEqual(self.cmdline(live), want)
        self.assertEqual(self.cmdline(HOST_CMDLINE), want)
        self.assertEqual(self.cmdline(want), want)  # idempotent

    def test_debug_tokens_are_added_once_and_can_be_dropped(self):
        want = HOST_CMDLINE + ' crash_kexec_post_notifiers' + KFENCE
        self.assertEqual(self.cmdline(HOST_CMDLINE + KFENCE + ' crash_kexec_post_notifiers'), want)
        self.assertEqual(self.cmdline(HOST_CMDLINE + ' kfence.sample_interval=50 crash_kexec_post_notifiers'), want)
        self.assertEqual(self.cmdline(HOST_CMDLINE + ' page_poison=1 crash_kexec_post_notifiers',
                                      proxmox_guest_startup_debug_cmdline=['page_poison=1']),
                         HOST_CMDLINE + ' crash_kexec_post_notifiers page_poison=1')
        self.assertEqual(self.cmdline(want, proxmox_guest_startup_debug_cmdline=[]),
                         HOST_CMDLINE + ' crash_kexec_post_notifiers')

    def test_kfence_live_write_is_guarded_and_tolerates_enomem(self):
        write = task('Apply the KFENCE sample interval to the running kernel')
        argv = write['ansible.builtin.command']['argv']
        self.assertEqual(render(argv[-1], '', {}), 'echo 100 > /sys/module/kfence/parameters/sample_interval')
        def runs(value, on_rc, **overrides):
            sysfs = {'content': base64.b64encode(value).decode()}
            return render('{{ ' + write['when'] + ' }}', '', {}, proxmox_guest_startup_kfence=sysfs,
                          proxmox_guest_startup_kfence_on={'rc': on_rc}, **overrides)
        self.assertEqual(argv[:2], ['bash', '-c'])  # dash prints "I/O error" for every errno
        self.assertTrue(runs(b'0\n', 1))
        self.assertFalse(runs(b'100\n', 0))
        self.assertTrue(runs(b'100\n', 1))  # value stored but the late enable failed: retry
        self.assertTrue(runs(b'50\n', 0))
        self.assertFalse(runs(b'0\n', 1, proxmox_guest_startup_kfence_sample_interval=0))
        self.assertTrue(runs(b'100\n', 0, proxmox_guest_startup_kfence_sample_interval=0))
        failed = '{{ (' + ') and ('.join(write['failed_when']) + ') }}'
        result = {'rc': 1, 'stderr': 'bash: line 1: echo: write error: Cannot allocate memory'}
        self.assertFalse(render(failed, '', {}, proxmox_guest_startup_kfence_write=result))
        result = {'rc': 1, 'stderr': 'Permission denied'}
        self.assertTrue(render(failed, '', {}, proxmox_guest_startup_kfence_write=result))

    def test_rasdaemon_runs(self):
        self.assertEqual(task('Install rasdaemon')['ansible.builtin.apt'], {'name': 'rasdaemon', 'state': 'present'})
        self.assertEqual(task('Enable and start rasdaemon')['ansible.builtin.systemd_service'],
                         {'name': 'rasdaemon', 'enabled': True, 'state': 'started'})

    def test_enabled_cmdline_sets_crashkernel_once_from_the_variable(self):
        on = {'proxmox_guest_startup_kdump_enabled': True}
        old = HOST_CMDLINE + ' crashkernel=1G crash_kexec_post_notifiers'
        new = HOST_CMDLINE + ' crash_kexec_post_notifiers' + KFENCE + ' crashkernel=2G'
        self.assertEqual(self.cmdline(old, **on), new)
        self.assertEqual(self.cmdline(new, **on), new)  # idempotent: wanted == current
        self.assertEqual(self.cmdline(HOST_CMDLINE, **on), new)
        self.assertEqual(self.cmdline(old, proxmox_guest_startup_crashkernel='3G', **on),
                         new.replace('2G', '3G'))

    def test_nothing_reboots_the_host(self):
        handlers = DataLoader().load_from_file(str(ROLE / 'handlers/main.yml'), trusted_as_template=True)
        argvs = [h['ansible.builtin.command']['argv'] for h in handlers if 'ansible.builtin.command' in h]
        self.assertEqual(argvs, [['proxmox-boot-tool', 'refresh'], ['kdump-config', 'try-reload']])
        self.assertFalse([h for h in handlers if 'reboot' in str(h).lower()])


if __name__ == '__main__':
    unittest.main()

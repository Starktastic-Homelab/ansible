"""Offline checks for the weekly ZFS trim role on the Proxmox host."""
from pathlib import Path
import configparser
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / 'roles/proxmox_zfs_trim'
SCRIPT = '/usr/lib/zfs-linux/trim'
TIMER = 'zfs-trim-weekly.timer'


def tasks():
    return yaml.safe_load((ROLE / 'tasks/main.yml').read_text())


def unit(name):
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read_string((ROLE / 'files' / name).read_text())
    return parser


def with_module(module):
    return [t for t in tasks() if module in t]


def argv(task):
    return task['ansible.builtin.command']['argv']


def index_of(predicate):
    return next(i for i, t in enumerate(tasks()) if predicate(t))


class ZfsTrimTests(unittest.TestCase):
    def test_defaults_and_group_vars(self):
        self.assertEqual(yaml.safe_load((ROLE / 'defaults/main.yml').read_text()), {'proxmox_zfs_trim_pools': {}})
        self.assertEqual(yaml.safe_load((ROOT / 'group_vars/proxmox_hosts/zfs_trim.yml').read_text()),
                         {'proxmox_zfs_trim_pools': {'rpool': 'enable'}})

    def test_units(self):
        svc, tmr = unit('zfs-trim-weekly.service'), unit(TIMER)
        self.assertEqual((svc['Service']['Type'], svc['Service']['ExecStart']), ('oneshot', SCRIPT))
        self.assertEqual(tmr['Timer']['OnCalendar'], 'Wed *-*-* 01:30:00')
        self.assertEqual(tmr['Timer']['Persistent'], 'false')
        self.assertEqual(tmr['Install']['WantedBy'], 'timers.target')
        for name in ('zfs-trim-weekly.service', TIMER):
            self.assertTrue((ROLE / 'files' / name).read_text().startswith('# Managed by ansible (proxmox_zfs_trim).'))

    def test_asserts_before_any_change(self):
        t = tasks()
        first = t[0]['ansible.builtin.assert']['that']
        text = ' '.join(first)
        self.assertIn('^[A-Za-z0-9][A-Za-z0-9_.:-]*$', text)
        self.assertIn("['auto', 'enable', 'disable']", text)
        self.assertEqual(t[1]['ansible.builtin.stat']['path'], SCRIPT)
        self.assertEqual(t[1]['register'], 'proxmox_zfs_trim_script')
        self.assertIn('proxmox_zfs_trim_script.stat.executable', ' '.join(t[2]['ansible.builtin.assert']['that']))
        first_change = index_of(lambda x: any(m in x for m in (
            'ansible.builtin.copy', 'ansible.builtin.systemd')) or (
            'ansible.builtin.command' in x and argv(x)[:2] == ['zfs', 'set']))
        self.assertGreater(first_change, 2)

    def test_property_set_only_when_different(self):
        cmds = with_module('ansible.builtin.command')
        read = next(c for c in cmds if argv(c)[:2] == ['zfs', 'get'])
        self.assertEqual(argv(read), ['zfs', 'get', '-H', '-o', 'value', 'org.debian:periodic-trim', '{{ item.key }}'])
        self.assertEqual(read['loop'], '{{ proxmox_zfs_trim_pools | dict2items }}')
        self.assertEqual(read['register'], 'proxmox_zfs_trim_current')
        self.assertIs(read['changed_when'], False)
        self.assertNotIn('failed_when', read)  # a pool that does not exist fails the play
        put = next(c for c in cmds if argv(c)[:2] == ['zfs', 'set'])
        self.assertEqual(argv(put), ['zfs', 'set', 'org.debian:periodic-trim={{ item.item.value }}', '{{ item.item.key }}'])
        self.assertEqual(put['loop'], '{{ proxmox_zfs_trim_current.results }}')
        self.assertEqual(put['when'], 'item.stdout != item.item.value')
        self.assertIs(put['changed_when'], True)
        self.assertLess(tasks().index(read), tasks().index(put))

    def test_registers_use_role_prefix(self):
        regs = [t['register'] for t in tasks() if 'register' in t]
        self.assertTrue(regs)
        for r in regs:
            self.assertTrue(r.startswith('proxmox_zfs_trim_'), r)

    def test_units_installed_and_timer_enabled(self):
        copies = [t['ansible.builtin.copy'] for t in with_module('ansible.builtin.copy')]
        self.assertEqual(sorted((c['src'], c['dest']) for c in copies), [
            ('zfs-trim-weekly.service', '/etc/systemd/system/zfs-trim-weekly.service'),
            ('zfs-trim-weekly.timer', '/etc/systemd/system/zfs-trim-weekly.timer')])
        for c in copies:
            self.assertEqual((c['mode'], c['owner'], c['group']), ('0644', 'root', 'root'))
        (sd,) = [t['ansible.builtin.systemd'] for t in with_module('ansible.builtin.systemd')]
        self.assertEqual(sd, {'name': TIMER, 'enabled': True, 'state': 'started', 'daemon_reload': True})
        self.assertEqual(index_of(lambda x: 'ansible.builtin.systemd' in x), len(tasks()) - 1)

    def test_deploy_never_starts_a_trim(self):
        for t in with_module('ansible.builtin.systemd'):
            self.assertEqual(t['ansible.builtin.systemd']['name'], TIMER)
        for t in with_module('ansible.builtin.command'):
            self.assertNotIn('zpool', argv(t))
            self.assertNotIn(SCRIPT, argv(t))
        for t in tasks():
            self.assertFalse({'ansible.builtin.shell', 'ansible.builtin.raw'} & set(t), t['name'])

    def test_no_pool_names_in_role(self):
        text = ''.join(p.read_text() for p in ROLE.rglob('*') if p.is_file())
        for name in ('rpool', 'vm-pool', 'usbbk'):
            self.assertNotIn(name, text)
        self.assertNotIn('cron', text)

    def test_host_only(self):
        plays = yaml.safe_load((ROOT / 'proxmox-zfs-trim.yml').read_text())
        self.assertEqual([(p['hosts'], p['become'], p['gather_facts'], [r['role'] for r in p['roles']]) for p in plays],
                         [('proxmox_hosts', True, False, ['proxmox_zfs_trim'])])
        self.assertNotIn('proxmox_zfs_trim', (ROOT / 'k3s.yml').read_text())

    def test_workflow_and_deploy_ignore(self):
        deploy_ignored = yaml.safe_load((ROOT / '.github/workflows/deploy.yml').read_text())[True]['push']['paths-ignore']
        for p in ('roles/proxmox_zfs_trim/**', 'proxmox-zfs-trim.yml', '.github/workflows/proxmox-zfs-trim.yml',
                  'scripts/tests/test_proxmox_zfs_trim.py', 'group_vars/proxmox_hosts/**'):
            self.assertIn(p, deploy_ignored)
        own = yaml.safe_load((ROOT / '.github/workflows/proxmox-zfs-trim.yml').read_text())
        self.assertEqual(own[True]['push']['branches'], ['main'])
        self.assertIn('workflow_dispatch', own[True])
        self.assertEqual(sorted(own[True]['push']['paths']),
                         ['group_vars/proxmox_hosts/zfs_trim.yml', 'proxmox-zfs-trim.yml', 'roles/proxmox_zfs_trim/**'])
        (job,) = own['jobs'].values()
        self.assertEqual(job['runs-on'], 'self-hosted')
        run = ' '.join(s.get('run', '') for s in job['steps'])
        self.assertIn('-i inventory/hosts.yml', run)
        self.assertIn('proxmox-zfs-trim.yml', run)
        last = job['steps'][-1]
        self.assertEqual((last['if'], last['run'].strip()), ('always()', 'rm -f .vault_pass'))
        syntax = next(line for line in (ROOT / '.github/workflows/validate.yml').read_text().splitlines()
                      if line.strip().startswith('--syntax-check'))
        self.assertIn('proxmox-zfs-trim.yml', syntax.split())


if __name__ == '__main__':
    unittest.main()

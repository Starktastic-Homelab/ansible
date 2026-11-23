"""Offline checks for the storage VM role, storage.yml, storage-expiry.yml and the lab guard (ADR 0017)."""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest
from unittest import mock
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / 'roles/storage_vm'
sys.path.insert(0, str(ROLE / 'files'))
from homelab_storage import config, lio, metrics  # noqa: E402

WORKERS = [{'name': 'k3s-worker-01', 'bridge_address': '10.77.0.21'},
           {'name': 'k3s-worker-02', 'bridge_address': '10.77.0.22'}]
BRIDGE = '10.77.0.10'
# The role's targetctl drop-in, byte for byte.
DEV_UNIT = 'dev-disk-by\\x2dpath-pci\\x2d0000:06:0b.0.device'  # systemd-escape --path of the default retained PV
TARGETCTL_DROPIN = (
    "# managed\n# The only LVM activation at boot (the image turns stock activation off, ADR 0017 F24): retained from "
    "its own PV,\n# no fallback, so a wrong path fails the unit and restores no targets; regen bounded and never "
    "killed (F11).\n# Then re-applies fences, strips fenced ACLs and checks LUN 0 per ledgered LV. A failed "
    "start\n# skips ExecStop (targetctl clear), so a failing check never clears targets.\n[Unit]\n"
    "Wants=network-online.target\nAfter=network-online.target\nAfter=nftables.service\nWants=" + DEV_UNIT + "\nAfter=" + DEV_UNIT + "\n\n[Service]\n"
    "# A failed start or a stop never signals an abandoned (D-state) LVM child.\nKillMode=process\n"
    "ExecStartPre=\n"
    "ExecStartPre=/sbin/vgchange --devices /dev/disk/by-path/pci-0000:06:0b.0 -ay retained\n"
    "ExecStartPre=-/usr/local/sbin/storage-reconcile activate --vg regen\n"
    "ExecStartPre=-/usr/local/sbin/storage-fencer restore-set\n"
    "ExecStartPost=/usr/local/sbin/storage-fencer restore-set\n"
    "ExecStartPost=/usr/local/sbin/storage-reconcile boot-check\n")


def load(path):
    return DataLoader().load_from_file(str(path), trusted_as_template=True)


def imported(play):
    return play.get('ansible.builtin.import_playbook', play.get('import_playbook'))


def plays(name):
    """The plays of a playbook, each import_playbook resolved in place."""
    return [q for p in load(ROOT / name) for q in (plays(imported(p)) if imported(p) else [p])]


GROUP_ALL = load(ROOT / 'group_vars/all/vars.yml')
TOP_PLAYBOOKS = sorted(p.name for p in ROOT.glob('storage*.yml') if not p.name.startswith('storage-play-'))


def role_tasks(name='main'):
    return load(ROLE / 'tasks' / f'{name}.yml')


def defaults(**values):
    variables = load(ROLE / 'defaults/main.yml')
    variables['storage_initiator_iqn_prefix'] = GROUP_ALL['storage_initiator_iqn_prefix']
    variables.update({'vip_address': GROUP_ALL['production_vip_address'], 'argocd_repo_url': 'https://example.invalid/apps.git',
                      'ansible_managed': 'managed', 'storage_vm_workers': WORKERS, 'storage_vm_bridge_address': BRIDGE,
                      'storage_vm_bridge_subnet': '10.77.0.0/24',
                      'storage_vm_ssh_sources': ['10.9.9.0/24', '10.9.8.0/24'], 'storage_vm_bridge_iface': 'eth1',
                      'storage_vm_fenced_addresses': [],
                      'storage_vm_pv_devices': {'retained': '/dev/disk/by-path/pci-0000:06:0b.0',
                                                'regen': '/dev/disk/by-path/pci-0000:06:0c.0'}})
    variables.update(values)
    return variables


def render(name, **values):
    return Templar(loader=DataLoader(), variables=defaults(**values)).template(
        trust_as_template((ROLE / 'templates' / name).read_text()))


def holds(expressions, **values):
    templar = Templar(variables=defaults(**values))
    return all(templar.evaluate_expression(trust_as_template(e) if isinstance(e, str) else e) for e in expressions)


def walk(items):
    """Every task dict, descending into blocks and pre/post tasks."""
    for t in items or []:
        yield t
        for key in ('block', 'rescue', 'always', 'tasks', 'pre_tasks', 'post_tasks', 'handlers'):
            yield from walk(t.get(key))


def all_tasks():
    files = [*sorted(ROOT.glob('storage*.yml')), ROOT / 'tasks/lab-guard.yml',
             *sorted((ROLE / 'tasks').glob('*.yml')), ROLE / 'handlers/main.yml']
    for f in files:
        yield from walk(load(f))


def by_name(items, name):
    return next(t for t in walk(items) if t.get('name') == name)


def module(task, name):
    return task.get(f'ansible.builtin.{name}')


def argv(task):
    cmd = module(task, 'command')
    return cmd.get('argv', []) if isinstance(cmd, dict) else []


def lock_play_index(ps):
    return next(i for i, p in enumerate(ps) if 'lock-verify.yml' in str(p) or 'maintenance_lock.py' in str(p))


def input_chain(text):
    body = text.split('chain input {', 1)[1].split('\n  }', 1)[0]
    return [line.strip() for line in body.splitlines() if line.strip() and not line.strip().startswith('#')]


class InventoryTests(unittest.TestCase):
    def test_storage_group_requires_exact_tag(self):
        expr = load(ROOT / 'inventory/proxmox.yml')['groups']['storage']
        for tags, want in ((['storage'], True), (['lab-storage'], False), (['k3s', 'worker'], False),
                           (['storage', 'other'], True), (['storage-x'], False)):
            got = Templar(variables={'proxmox_tags_parsed': tags}).evaluate_expression(expr)
            self.assertIs(bool(got), want, tags)


class LabGuardTests(unittest.TestCase):
    def guard(self):
        return load(ROOT / 'tasks/lab-guard.yml')[0]

    def guard_passes(self, lab, vip, masters, inventory_file='/any/inventory.yml'):
        t = self.guard()
        hostvars = {h: {'ansible_host': a} for h, a in masters.items()}
        variables = {'inventory_file': inventory_file, 'vip_address': vip, 'hostvars': hostvars,
                     'production_vip_address': GROUP_ALL['production_vip_address'],
                     'groups': {'masters': list(masters), 'all': list(masters)}}
        if lab is not None:
            variables['lab'] = lab
        templar = Templar(variables=variables)
        if not templar.evaluate_conditional(t['when']):
            return None  # skipped
        return all(templar.evaluate_expression(e) for e in module(t, 'assert')['that'])

    def test_lab_guard(self):
        for name in ('k3s.yml', 'storage.yml', 'storage-expiry.yml', 'storage-dry-run.yml', 'storage-host-metrics.yml',
                     'storage-preflight.yml'):
            first = plays(name)[0]
            self.assertEqual(first['name'], 'Lab guard', name)
            self.assertEqual((first['hosts'], first['gather_facts'], first['any_errors_fatal'], first['tags']),
                             ('all', False, True, ['always']), name)
            self.assertEqual(first['tasks'], [{'name': 'Refuse a lab run aimed at production',
                                               'ansible.builtin.import_tasks': 'tasks/lab-guard.yml', 'run_once': True}])
        prod_vip = GROUP_ALL['production_vip_address']
        self.assertEqual(prod_vip, '10.9.9.99')
        self.assertEqual(GROUP_ALL['vip_address'], '{{ production_vip_address }}')
        self.assertFalse(self.guard_passes(True, prod_vip, {'lab-server': prod_vip}))
        self.assertFalse(self.guard_passes(True, '10.9.9.180', {'lab-server': '10.9.9.181'}))
        self.assertTrue(self.guard_passes(True, '10.9.9.181', {'lab-server': '10.9.9.181'}))
        self.assertIsNone(self.guard_passes(None, prod_vip, {'m': '10.9.9.11'}))  # production: lab unset
        self.assertIsNone(self.guard_passes(False, prod_vip, {'m': '10.9.9.11'}))
        self.assertIsNone(self.guard_passes(None, prod_vip, {'m': '10.9.9.11'}, inventory_file=None))
        # A stale lab inventory that lost lab: true is still guarded by its /lab-storage/ path.
        lab_path = '/maintenance/lab-storage/inventory.yml'
        self.assertIs(self.guard_passes(None, prod_vip, {'lab-server': '10.9.9.181'}, lab_path), False)
        self.assertIs(self.guard_passes(False, prod_vip, {'lab-server': '10.9.9.181'}, lab_path), False)
        self.assertIs(self.guard_passes(None, '10.9.9.181', {'lab-server': '10.9.9.181'}, lab_path), True)


class RoleTests(unittest.TestCase):
    def test_dry_run_shows_its_warnings(self):
        tasks = role_tasks('dry-run')
        run = by_name(tasks, 'Dry-run the reconcile against the real ledger')
        show = by_name(tasks, 'Show the dry-run warnings')
        self.assertEqual(run['register'], 'storage_vm_dry_run')
        self.assertEqual(module(show, 'debug'), {'msg': '{{ storage_vm_dry_run.stderr_lines }}'})
        self.assertEqual(show['when'], 'storage_vm_dry_run.stderr | length > 0')
        for stderr, shown in (('', False), ('hold_create set, 2 creates refused', True)):
            self.assertIs(holds([show['when']], storage_vm_dry_run={'stderr': stderr}), shown)

    def test_asserts_come_first(self):
        for name in ('main', 'dry-run'):
            self.assertEqual(role_tasks(name)[0].get('ansible.builtin.import_tasks'), 'assert.yml', name)

    def test_empty_ssh_sources_fails(self):
        that = module(role_tasks('assert')[0], 'assert')['that']
        self.assertTrue(holds(that))
        self.assertFalse(holds(that, storage_vm_ssh_sources=[]))
        self.assertFalse(holds(that, storage_vm_ssh_sources='10.9.9.0/24'))
        self.assertFalse(holds(that, storage_vm_ssh_sources=['10.9.9.0/24; accept']))
        self.assertFalse(holds(that, storage_vm_workers=[]))
        self.assertFalse(holds(that, storage_vm_workers=WORKERS + [WORKERS[0]]))
        self.assertFalse(holds(that, storage_vm_bridge_address=''))

    def test_worker_dropped_from_inventory_refused(self):
        # A Proxmox API timeout drops a worker from its tag groups with only a warning (inventory/proxmox.yml): the
        # shorter list must never reach config.json, nftables.conf or the ACLs unless an approval names that worker.
        tasks = role_tasks('assert')
        names = [t['name'] for t in tasks]
        stat = by_name(tasks, 'Find the installed storage config')
        read = by_name(tasks, 'Read the installed storage config')
        check = by_name(tasks, 'Refuse a worker that left the inventory without an approval naming it')
        self.assertEqual(module(stat, 'stat')['path'], str(config.PATH))
        self.assertEqual(module(read, 'slurp')['src'], str(config.PATH))
        self.assertEqual(read['when'], 'storage_vm_installed_config_stat.stat.exists')
        self.assertLess(names.index(stat['name']), names.index(read['name']))
        self.assertLess(names.index(read['name']), names.index(check['name']))
        self.assertEqual(load(ROLE / 'defaults/main.yml')['storage_vm_remove_workers'], [])

        def installed(workers):
            if workers is None:
                return {'storage_vm_installed_config_stat': {'stat': {'exists': False}},
                        'storage_vm_installed_config': {'skipped': True}}
            raw = workers if isinstance(workers, str) else json.dumps({'workers': workers})
            return {'storage_vm_installed_config_stat': {'stat': {'exists': True}},
                    'storage_vm_installed_config': {'content': base64.b64encode(raw.encode()).decode()}}

        def passes(installed_workers, rendered=WORKERS, approved=()):
            return holds(module(check, 'assert')['that'], **check['vars'], **installed(installed_workers),
                         storage_vm_workers=rendered,
                         storage_vm_remove_workers=list(approved) if not isinstance(approved, str) else approved)

        one = WORKERS[:1]
        self.assertTrue(passes(None))  # first bootstrap: nothing installed yet
        self.assertTrue(passes(WORKERS))
        self.assertTrue(passes(one, rendered=WORKERS))  # a worker added
        self.assertFalse(passes(WORKERS, rendered=one))  # k3s-worker-02 dropped out of the inventory
        self.assertTrue(passes(WORKERS, rendered=one, approved=['k3s-worker-02']))
        self.assertFalse(passes(WORKERS, rendered=one, approved=['k3s-worker-01']))
        self.assertFalse(passes(WORKERS, rendered=one, approved='k3s-worker-02'))  # a string, not a list
        self.assertFalse(passes(WORKERS, rendered=[], approved=['k3s-worker-01']))  # still one approval short
        for broken in ('not json', '{"workers": 7}', '{}'):
            with self.subTest(installed=broken):  # refused (False) or the task errors: never read as no workers
                try:
                    self.assertFalse(passes(broken, rendered=[]))
                except AssertionError:
                    raise
                except Exception:
                    pass

    def test_bridge_addresses_in_subnet_unique(self):
        t = by_name(role_tasks('assert'), 'Require distinct worker bridge addresses inside the bridge subnet')
        that = module(t, 'assert')['that']
        self.assertTrue(holds(that))
        w1 = WORKERS[0]
        for bad in ([w1, {'name': 'k3s-worker-02', 'bridge_address': BRIDGE}],          # the storage VM's address
                    [w1, {'name': 'k3s-worker-02', 'bridge_address': w1['bridge_address']}],  # two on one address
                    [w1, {'name': 'k3s-worker-02', 'bridge_address': '10.77.1.22'}],   # outside the subnet
                    [w1, {'name': 'k3s-worker-02', 'bridge_address': '10.77.0.222.1'}]):
            self.assertFalse(holds(that, storage_vm_workers=bad), bad)
        for subnet in ('', '10.77.0.0/16', '10.77.0.5/24', '10.77.0.0'):  # only an x.y.z.0/24 subnet
            self.assertFalse(holds(that, storage_vm_bridge_subnet=subnet), subnet)
        self.assertTrue(holds(that, storage_vm_bridge_subnet='10.77.0.0/24'))

    def production_vars(self, nics):
        """group_vars/storage/storage_vm.yml templated over a fake inventory: nics maps a worker to its ipconfigN."""
        raw = load(ROOT / 'group_vars/storage/storage_vm.yml')
        hostvars = {w: {f'proxmox_ipconfig{n}': {'ip': ip, 'gw': '10.9.9.1'} if ip else {} for n, ip in c.items()}
                    for w, c in nics.items()}
        for h in hostvars.values():
            h['ansible_host'] = 'unused'
        templar = Templar(loader=DataLoader(), variables={**raw, 'groups': {'workers': list(nics)}, 'hostvars': hostvars})
        return raw, {k: templar.template(v) for k, v in raw.items()}

    def test_production_storage_vars(self):
        prod = {w: {0: f'10.9.9.5{n}/24', 1: f'10.9.8.5{n}/24', 2: f'10.99.2.{20 + n}/24'}
                for w, n in (('kube-worker-02', 2), ('kube-worker-01', 1))}
        raw, v = self.production_vars(prod)
        self.assertEqual(v['storage_vm_workers'], [
            {'name': 'kube-worker-01', 'bridge_address': '10.99.2.21'},
            {'name': 'kube-worker-02', 'bridge_address': '10.99.2.22'}])
        self.assertEqual(v['storage_vm_bridge_address'], '10.99.2.10')
        self.assertEqual(v['storage_vm_bridge_subnet'], '10.99.2.0/24')
        self.assertEqual(v['storage_vm_ssh_sources'], ['10.9.9.0/24'])
        self.assertNotIn('storage_vm_pv_devices', raw)  # the file as loaded; set in Task 3.5
        tasks = role_tasks('assert')
        first = module(tasks[0], 'assert')['that']
        bridge = module(by_name(tasks, 'Require distinct worker bridge addresses inside the bridge subnet'), 'assert')['that']
        self.assertTrue(holds(first + bridge, **v))
        # The one-NIC cohort fixture: the storage NIC is ipconfig1. Selection is by subnet, never by index.
        _, one = self.production_vars({'kube-worker-01': {0: '10.9.9.51/24', 1: '10.99.2.21/24'}})
        self.assertEqual(one['storage_vm_workers'], [{'name': 'kube-worker-01', 'bridge_address': '10.99.2.21'}])
        # No worker, none or two addresses in the subnet, or a NIC without ip: never a shorter list.
        _, empty = self.production_vars({})
        self.assertEqual(empty['storage_vm_workers'], [])
        self.assertFalse(holds(first, **empty))
        for nics in ({0: '10.9.9.51/24', 1: '10.9.8.51/24'}, {0: '10.9.9.51/24', 1: '10.99.2.21/24', 2: '10.99.2.31/24'},
                     {0: '10.9.9.51/24', 1: None, 2: 'dhcp'}):
            _, bad = self.production_vars({'kube-worker-01': nics, 'kube-worker-02': prod['kube-worker-02']})
            self.assertEqual([w['name'] for w in bad['storage_vm_workers']], ['kube-worker-01', 'kube-worker-02'], nics)
            self.assertFalse(holds(first, **bad), nics)

    def test_no_restart_of_target_or_nftables(self):
        for t in all_tasks():
            for key in ('ansible.builtin.systemd', 'ansible.builtin.systemd_service', 'ansible.builtin.service'):
                if key in t and t[key].get('name') in ('rtslib-fb-targetctl', 'nftables'):
                    self.assertNotIn(t[key].get('state'), ('restarted', 'stopped'), t['name'])
            text = ' '.join(argv(t)) + str(module(t, 'shell') or '')
            self.assertNotRegex(text, r'systemctl (restart|stop|try-restart)|nft -f|targetctl (clear|restore)', t.get('name'))
        handlers = load(ROLE / 'handlers/main.yml')
        self.assertEqual([h['name'] for h in handlers], ['Reload systemd', 'Reload nftables', 'Restart storage-fencer',
                                                       'Flush the journal to disk'])
        self.assertEqual(module(handlers[1], 'systemd'), {'name': 'nftables', 'state': 'reloaded'})
        self.assertEqual(module(handlers[2], 'systemd'), {'name': 'storage-fencer', 'state': 'restarted'})
        restarts = [t for t in all_tasks() if 'restarted' in str(t)]
        self.assertEqual([t['name'] for t in restarts], ['Restart storage-fencer'])

    def test_pause_after_refused_outside_lab(self):
        t = next(t for t in role_tasks('assert') if 'pause_after' in json.dumps(t))
        self.assertNotIn('when', t)
        that = module(t, 'assert')['that']
        paused = dict(defaults()['storage_vm_timings'], pause_after=['reconcile:after-create'])
        lab_path = '/maintenance/lab-storage/inventory.yml'
        self.assertTrue(holds(that))
        self.assertFalse(holds(that, storage_vm_timings=paused))  # lab unset is production
        self.assertFalse(holds(that, lab=False, storage_vm_timings=paused))
        self.assertFalse(holds(that, inventory_file=lab_path, storage_vm_timings=paused))  # the path is not the marker
        self.assertTrue(holds(that, lab=True, inventory_file='/any/inventory.yml', storage_vm_timings=paused))

    def test_persistent_journal_on_storage_vm(self):
        t = by_name(role_tasks(), 'Ensure the persistent journal directory exists')
        self.assertEqual(module(t, 'file'), {'path': '/var/log/journal', 'state': 'directory', 'owner': 'root',
                                             'group': 'systemd-journal', 'mode': '2755'})
        self.assertEqual(t['notify'], 'Flush the journal to disk')
        h = by_name(load(ROLE / 'handlers/main.yml'), 'Flush the journal to disk')
        self.assertEqual(module(h, 'command'), {'argv': ['journalctl', '--flush']})
        self.assertIs(h['changed_when'], True)

    def test_role_never_sets_targetctl_state(self):
        hits = [t for t in all_tasks() if 'rtslib-fb-targetctl' in str(module(t, 'systemd') or '')]
        self.assertEqual([module(t, 'systemd') for t in hits], [{'name': 'rtslib-fb-targetctl', 'enabled': True}])

    def test_role_order(self):
        names = [t['name'] for t in role_tasks()]
        tasks = role_tasks()
        def first(pred):
            return next(i for i, t in enumerate(tasks) if pred(t))
        programs = first(lambda t: 'homelab_storage' in str(module(t, 'copy') or ''))
        cfg = first(lambda t: 'config.json' in str(module(t, 'template') or ''))
        dropins = first(lambda t: 'service.d' in str(module(t, 'template') or ''))
        nft = first(lambda t: '/etc/nftables.conf' in str(module(t, 'template') or ''))
        self.assertLess(max(programs, cfg), min(dropins, nft))
        self.assertEqual(tasks[-1], {'name': names[-1], 'ansible.builtin.meta': 'flush_handlers'})
        for t in tasks:
            for m in ('copy', 'template'):
                if module(t, m) and str(module(t, m)['dest']).startswith('/usr/local/sbin/'):
                    self.assertEqual(module(t, m)['mode'], '0755')
        self.assertEqual(module(by_name(tasks, 'Install the storage programs'), 'copy')['dest'], '/usr/local/lib/homelab-storage/')

    def test_nftables_order(self):
        rules = input_chain(render('nftables.conf.j2'))
        self.assertRegex(rules[0], r'^type filter hook input priority (filter|0); policy drop;$')
        self.assertEqual(rules[1:4], ['ip saddr @fenced drop', 'ct state established,related accept', 'iif lo accept'])
        self.assertEqual(rules[4], 'tcp dport 22 ip saddr { 10.9.9.0/24, 10.9.8.0/24 } accept')
        self.assertEqual(rules[5], f'iifname "eth1" ip daddr {BRIDGE} ip saddr {{ 10.77.0.21, 10.77.0.22 }} '
                                   'tcp dport { 3260, 9100 } accept')
        self.assertEqual(len(rules), 6)
        self.assertIn('flush ruleset', render('nftables.conf.j2'))

    def test_fenced_set_elements_rendered(self):
        def fenced_set(text):
            return text.split('set fenced', 1)[1].split('}', 1)[0] + '}'
        empty = fenced_set(render('nftables.conf.j2'))
        self.assertIn('type ipv4_addr', empty)
        self.assertNotIn('elements', empty)
        one = render('nftables.conf.j2', storage_vm_fenced_addresses=['10.77.0.22'])
        self.assertIn('elements = { 10.77.0.22 }', one)
        two = render('nftables.conf.j2', storage_vm_fenced_addresses=['10.77.0.21', '10.77.0.22'])
        self.assertIn('elements = { 10.77.0.21, 10.77.0.22 }', two)

    def test_fenced_addresses_from_tags_under_lio_lock(self):
        tasks = role_tasks()
        read = by_name(tasks, 'Read the fence tags under the LIO lock')
        self.assertEqual(argv(read), ['flock', '-w', '120', '/run/lock/storage-lio', '/usr/local/sbin/storage-fencer',
                                      'status'])
        expr = module(by_name(tasks, 'Render fenced workers as bridge addresses'), 'set_fact')['storage_vm_fenced_addresses']
        status = {'stdout': json.dumps({'fenced': {'k3s-worker-02': 'unknown'}, 'holds': [], 'history': {}})}
        got = Templar(variables=defaults(storage_vm_fence_status=status)).template(expr)
        self.assertEqual(list(got), ['10.77.0.22'])

    def test_bridge_iface_from_facts(self):
        tasks = role_tasks()
        expr = module(by_name(tasks, 'Find the bridge interface'), 'set_fact')['storage_vm_bridge_iface']
        facts = {'interfaces': ['lo', 'eth0', 'eth1', 'vmbr0.10'], 'vmbr0_10': {'device': 'vmbr0.10'}, 'lo': {'device': 'lo', 'ipv4': {'address': '127.0.0.1'}},
                 'eth0': {'device': 'eth0', 'ipv4': {'address': '10.9.9.40'}},
                 'eth1': {'device': 'eth1', 'ipv4': {'address': BRIDGE}}}
        self.assertEqual(Templar(variables=defaults(ansible_facts=facts)).template(expr), 'eth1')
        self.assertEqual(Templar(variables=defaults(ansible_facts=facts, storage_vm_bridge_address='10.77.0.99')).template(expr), '')
        check = module(by_name(tasks, 'Require the bridge interface'), 'assert')['that']
        self.assertIn("storage_vm_bridge_iface | length > 0", check)

    def test_dropin_order(self):
        out = render('rtslib-fb-targetctl.service.d-50-homelab.conf.j2')
        execs = [l for l in out.splitlines() if l.startswith('ExecStart')]
        want = ['ExecStartPre=', 'ExecStartPre=/sbin/vgchange --devices /dev/',
                'ExecStartPre=-/usr/local/sbin/storage-reconcile activate --vg regen',
                'ExecStartPre=-/usr/local/sbin/storage-fencer restore-set',
                'ExecStartPost=/usr/local/sbin/storage-fencer restore-set',
                'ExecStartPost=/usr/local/sbin/storage-reconcile boot-check']
        self.assertEqual(len(execs), len(want), execs)
        for line, prefix in zip(execs, want):
            self.assertTrue(line.startswith(prefix) and (prefix != 'ExecStartPre=' or line == prefix), (line, prefix))

    def test_dropin_retained_scoped_only(self):
        out = render('rtslib-fb-targetctl.service.d-50-homelab.conf.j2',
                     storage_vm_pv_devices={'retained': '/dev/disk/by-path/test-retained', 'regen': '/dev/x'})
        self.assertIn('ExecStartPre=/sbin/vgchange --devices /dev/disk/by-path/test-retained -ay retained\n', out)
        self.assertNotIn('||', out)
        self.assertNotIn('/bin/sh', out)
        self.assertEqual(len(re.findall(r'vgchange', out)), 1)

    def test_dropin_device_ordering(self):
        for path, unit in (('/dev/disk/by-path/test-retained', r'dev-disk-by\x2dpath-test\x2dretained.device'),
                           ('/dev/disk/by-path/pci-0000:06:0b.0', DEV_UNIT)):
            out = render('rtslib-fb-targetctl.service.d-50-homelab.conf.j2',
                         storage_vm_pv_devices={'retained': path, 'regen': '/dev/x'})
            self.assertIn(f'Wants={unit}\nAfter={unit}\n', out)
            if shutil.which('systemd-escape'):
                got = subprocess.run(['systemd-escape', '--path', '--suffix=device', path], capture_output=True,
                                     text=True, check=True).stdout.strip()
                self.assertEqual(got, unit)

    def test_dropin_orders_after_network(self):
        # The portal binds to the bridge address; this ordering moved here from the image's 10-lvm.conf.
        out = render('rtslib-fb-targetctl.service.d-50-homelab.conf.j2')
        lines = [line.strip() for line in out.splitlines() if line.strip() and not line.startswith('#')]
        unit = lines[lines.index('[Unit]') + 1:lines.index('[Service]')]
        self.assertIn('Wants=network-online.target', unit)
        self.assertIn('After=network-online.target', unit)

    def test_killmode_process(self):
        self.assertIn('\nKillMode=process\n', render('rtslib-fb-targetctl.service.d-50-homelab.conf.j2'))
        self.assertIn('\nKillMode=process\n', render('storage-fencer.service.j2'))

    def test_nftables_dropin(self):
        out = render('nftables.service.d-50-fences.conf.j2')
        lines = [line.strip() for line in out.splitlines() if line.strip() and not line.startswith('#')]
        self.assertEqual(lines, ['[Service]', 'ExecReload=/usr/local/sbin/storage-fencer restore-set',
                                 'ExecStartPost=-/usr/local/sbin/storage-fencer restore-set'])
        self.assertNotIn('ExecReload=\n', out)  # an empty reset line would drop the vendor nft -f

    def test_dropin_paths(self):
        dests = {module(t, 'template')['src']: module(t, 'template')['dest'] for t in role_tasks() if module(t, 'template')}
        self.assertEqual(dests['nftables.service.d-50-fences.conf.j2'], '/etc/systemd/system/nftables.service.d/50-fences.conf')
        self.assertEqual(dests['rtslib-fb-targetctl.service.d-50-homelab.conf.j2'],
                         '/etc/systemd/system/rtslib-fb-targetctl.service.d/50-homelab.conf')
        nft = next(t for t in role_tasks() if module(t, 'template') and module(t, 'template')['src'] == 'nftables.conf.j2')
        self.assertEqual(module(nft, 'template')['validate'], 'nft -c -f %s')
        self.assertEqual(nft['notify'], 'Reload nftables')

    def test_fencer_unit(self):
        out = render('storage-fencer.service.j2')
        self.assertIn('ExecStart=/usr/local/sbin/storage-fencer run', out)
        self.assertIn('Restart=always', out)
        self.assertIn('After=network-online.target nftables.service rtslib-fb-targetctl.service', out)

    def test_packages_held(self):
        t = by_name(role_tasks(), 'Hold the LIO and LVM packages')
        self.assertEqual(module(t, 'dpkg_selections'), {'name': '{{ item }}', 'selection': 'hold'})
        self.assertEqual(t['loop'], ['python3-rtslib-fb', 'targetcli-fb', 'lvm2'])

    STOCK_OK = {'storage_vm_lvm_event_activation': {'stdout': 'event_activation=0'},
                'storage_vm_lvm_units': {'stdout_lines': ['masked', 'masked']},
                'storage_vm_lvm_selection': {'stdout': 'hold ok installed'},
                'storage_vm_lvm_online': {'matched': 0}}

    def test_assert_stock_lvm_reads(self):
        tasks = role_tasks('assert')
        reads = {t['register']: t for t in tasks if t.get('register', '').startswith('storage_vm_lvm_')}
        self.assertEqual(set(reads), set(self.STOCK_OK))
        for t in reads.values():
            self.assertIs(t.get('check_mode'), False, t)
            self.assertIs(t.get('changed_when'), False, t)
        # --type full: exits 0 and prints the effective value even when the key is unset (old image: rc 5 without it)
        self.assertEqual(argv(reads['storage_vm_lvm_event_activation']),
                         ['/sbin/lvmconfig', '--type', 'full', 'global/event_activation'])
        self.assertEqual(argv(reads['storage_vm_lvm_units']),
                         ['systemctl', 'is-enabled', 'lvm2-monitor.service', 'blk-availability.service'])
        self.assertIs(reads['storage_vm_lvm_units'].get('failed_when'), False)  # masked exits non-zero
        self.assertEqual(argv(reads['storage_vm_lvm_selection']),
                         ['dpkg-query', '-W', '--showformat=${Status}', 'lvm2'])
        find = module(reads['storage_vm_lvm_online'], 'find')
        self.assertEqual(find['paths'], ['/run/lvm/vgs_online', '/run/lvm/pvs_online'])
        self.assertEqual(find['file_type'], 'any')
        self.assertIs(find.get('hidden'), True)
        names = [t.get('name') for t in tasks]
        self.assertGreater(names.index('Refuse stock LVM activation'),
                           max(names.index(t['name']) for t in reads.values()))

    def test_assert_stock_lvm(self):
        that = module(by_name(role_tasks('assert'), 'Refuse stock LVM activation'), 'assert')['that']
        self.assertTrue(holds(that, **self.STOCK_OK))
        for key, bad in (('storage_vm_lvm_event_activation', {'stdout': 'event_activation=1'}),
                         ('storage_vm_lvm_event_activation', {'stdout': ''}),
                         ('storage_vm_lvm_units', {'stdout_lines': ['enabled', 'masked']}),
                         ('storage_vm_lvm_units', {'stdout_lines': ['masked']}),
                         ('storage_vm_lvm_selection', {'stdout': 'install ok installed'}),
                         ('storage_vm_lvm_selection', {'stdout': ''}),
                         ('storage_vm_lvm_online', {'matched': 1})):
            self.assertFalse(holds(that, **{**self.STOCK_OK, key: bad}), (key, bad))

    def test_config_json_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            path.write_text(render('config.json.j2'))
            cfg = config.load(path)
        self.assertEqual(cfg['workers'], WORKERS)
        self.assertNotIn('vgs', cfg)
        self.assertEqual(cfg['initiator_prefix'], 'iqn.2026-11.net.starktastic:')
        self.assertEqual((cfg['portal'], cfg['api_url'], cfg['csi_driver'], cfg['max_removals'], cfg['pause_after']),
                         (f'{BRIDGE}:3260', f"https://{GROUP_ALL['production_vip_address']}:6443", 'org.democratic-csi.node-manual', 2, []))
        self.assertEqual((cfg['not_ready_seconds'], cfg['loop_seconds'], cfg['proof_timeout_seconds'],
                          cfg['quarantine_seconds'], cfg['blind_after_seconds']), (300, 15, 60, 259200, 600))
        with tempfile.TemporaryDirectory() as tmp:  # a one-off -e storage_vm_max_removals=3 arrives as a string
            path = Path(tmp) / 'config.json'
            path.write_text(render('config.json.j2', storage_vm_max_removals='3',
                                   storage_vm_workers=[dict(w, extra='x') for w in WORKERS]))
            cfg = config.load(path)
        self.assertEqual((cfg['max_removals'], cfg['workers']), (3, WORKERS))

    def test_approved_removals_pass_the_brake(self):
        # Two apps PRs that each remove 2 entries land between two reconciles: 4 untagged removals trip the default
        # brake and block every create and grow; the storage.yml input max_removals (approvals file) lets them through.
        from homelab_storage import ledger, plan
        from homelab_storage.inventory import Volume
        gi = 1024 ** 3
        ids = ['gone1', 'gone2', 'gone3', 'gone4', 'keep']
        vgs = {'retained': plan.VgState(ledger.Ledger.from_tags([ledger.tag_created(i) for i in ids]),
                                        {i: plan.LvState(gi) for i in ids}, True, free_bytes=50 * gi),
               'regen': plan.VgState(ledger.Ledger.from_tags([]), {}, True, free_bytes=50 * gi)}
        volumes = [Volume('keep', 'n', 'c', 2 * gi, 'retained', 'new'), Volume('new1', 'n', 'c', gi, 'retained', 'new')]
        cluster = ledger.Cluster.from_tags([], 'retained')

        def decide(**approvals):
            cfg = json.loads(render('config.json.j2', **approvals))
            return plan.decide(volumes, vgs, cluster, cfg['max_removals'])

        d = decide()
        self.assertTrue(d.fail)
        self.assertEqual({a.kind for a in d.actions}, {'export'})  # no grow of keep, no create of new1
        self.assertEqual({r.reason for r in d.refusals}, {'removal-brake'})
        d = decide(storage_vm_max_removals=4)  # the approvals file holds a JSON int
        self.assertFalse(d.fail)
        self.assertEqual({a.kind for a in d.actions} - {'export'}, {'extend', 'mark_pending', 'create_new'})
        self.assertNotIn('-e storage_vm_max_removals', (ROLE / 'defaults/main.yml').read_text())
        self.assertIn('max_removals', (ROLE / 'defaults/main.yml').read_text())

    def test_config_template_pv_devices(self):
        pv = {'retained': '/dev/disk/by-path/a', 'regen': '/dev/disk/by-path/b'}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            path.write_text(render('config.json.j2', storage_vm_pv_devices=pv))
            self.assertEqual(config.load(path)['pv_devices'], pv)

    def test_production_pv_devices_not_lab(self):
        self.assertEqual(load(ROLE / 'defaults/main.yml')['storage_vm_pv_devices'], {})
        for f in (ROLE / 'defaults/main.yml', ROOT / 'group_vars/storage/storage_vm.yml'):
            self.assertNotIn('by-path/pci-', f.read_text(), f)
        cond = next(t for t in role_tasks('assert') if 'storage_vm_pv_devices' in json.dumps(t))
        self.assertFalse(holds(module(cond, 'assert')['that'], storage_vm_pv_devices={}))
        lab_pv = load(ROOT / 'lab/vars.example.yml')['storage_vm_pv_devices']
        self.assertTrue(holds(module(cond, 'assert')['that'], storage_vm_pv_devices=lab_pv))

    def test_assert_pv_devices(self):
        cond = next(t for t in role_tasks('assert') if 'storage_vm_pv_devices' in json.dumps(t))
        that = module(cond, 'assert')['that']
        good = {'retained': '/dev/disk/by-path/a', 'regen': '/dev/disk/by-path/b'}
        self.assertTrue(holds(that, storage_vm_pv_devices=good))
        for bad in ({'retained': '/dev/a'}, {**good, 'x': '/dev/c'}, {**good, 'regen': 'b'},
                    {'retained': '/dev/a', 'regen': '/dev/a'}, {**good, 'regen': 5}):
            self.assertFalse(holds(that, storage_vm_pv_devices=bad), bad)

    def test_worker_iqn_matches_k3s_common(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            path.write_text(render('config.json.j2'))
            prefix = config.load(path)['initiator_prefix']
        tasks = [t for t in load(ROOT / 'roles/k3s_common/tasks/main.yml')
                 if t.get('ansible.builtin.copy', {}).get('dest') == '/etc/iscsi/initiatorname.iscsi']
        self.assertEqual(len(tasks), 1)
        got = Templar(variables={**GROUP_ALL, 'inventory_hostname': 'k3s-worker-01'}).template(
            tasks[0]['ansible.builtin.copy']['content'])
        self.assertEqual(got, 'InitiatorName=' + lio.worker_iqn('k3s-worker-01', prefix) + '\n')
        # The same bytes every node has today: no initiator changes on deploy.
        self.assertEqual(got, 'InitiatorName=iqn.2026-11.net.starktastic:k3s-worker-01\n')

    def test_installed_config_path_is_the_default(self):
        installed = str(config.PATH)
        self.assertEqual(module(by_name(role_tasks(), 'Write the storage config'), 'template')['dest'], installed)
        texts = {n: (ROOT / n).read_text() for n in ('storage.yml', 'storage-expiry.yml')}
        texts['expire-volume.yml'] = (ROLE / 'tasks/expire-volume.yml').read_text()
        texts['targetctl drop-in'] = render('rtslib-fb-targetctl.service.d-50-homelab.conf.j2')
        for name, text in texts.items():
            self.assertNotIn(installed, text, name)
        dry = argv(by_name(role_tasks('dry-run'), 'Dry-run the reconcile against the real ledger'))
        self.assertEqual(dry[dry.index('--config') + 1], '{{ storage_vm_dry_run_dir.path }}/config.json')

    def test_grow_reads_the_reported_iqn(self):
        include = by_name(role_tasks('grow'), 'Grow each attached exported volume')
        self.assertEqual(include['vars']['storage_vm_iqn'], '{{ storage_vm_volume.iqn }}')
        self.assertNotIn('starktastic', (ROLE / 'tasks/grow.yml').read_text())


class StoragePlaybookTests(unittest.TestCase):
    def test_play_order(self):
        ps = plays('storage.yml')
        self.assertEqual([p['hosts'] for p in ps], ['all', 'localhost', 'masters[0]', 'storage', 'storage', 'all_k3s', 'localhost',
                                                'localhost'])
        for p in ps:
            self.assertIs(p.get('any_errors_fatal'), True, p['name'])

    def test_branch_rejects_sha(self):
        that = module(role_tasks('fetch-inventory')[0], 'assert')['that']
        for branch in ('main', 'feat/retained-storage-vm', 'release-1.2'):
            self.assertTrue(holds(that, storage_vm_inventory_branch=branch, groups={'storage': ['s']}), branch)
        for branch in ('1234567', 'deadbeef', 'a' * 40, '0EE876F', ''):
            self.assertFalse(holds(that, storage_vm_inventory_branch=branch, groups={'storage': ['s']}), branch)
        self.assertFalse(holds(that, groups={'storage': ['s', 't']}))

    def test_resolves_branch_tip_and_logs_sha(self):
        tasks = role_tasks('fetch-inventory')
        ls = by_name(tasks, 'Resolve the inventory branch tip')
        self.assertEqual(argv(ls), ['git', 'ls-remote', '--exit-code', '--', '{{ storage_vm_inventory_repo }}',
                                    'refs/heads/{{ storage_vm_inventory_branch }}'])
        expr = module(by_name(tasks, 'Pick the exact branch ref'), 'set_fact')['storage_vm_inventory_sha']
        sha, decoy = 'a' * 40, 'b' * 40
        out = f'{decoy}\trefs/heads/x/refs/heads/main\n{sha}\trefs/heads/main'
        got = Templar(variables=defaults(storage_vm_ls_remote={'stdout_lines': out.splitlines()})).template(expr)
        self.assertEqual(got, sha)
        self.assertIn('is match', str(by_name(tasks, 'Require one 40-hex branch tip')))
        log = module(by_name(tasks, 'Log the inventory commit'), 'debug')['msg']
        self.assertIn('{{ storage_vm_inventory_sha }}', log)
        fetch = argv(by_name(tasks, 'Fetch the inventory commit'))
        self.assertEqual(fetch[-1], '{{ storage_vm_inventory_sha }}')
        self.assertNotIn('storage_vm_inventory_branch', ' '.join(fetch))
        checkout = argv(by_name(tasks, 'Check out only the inventory file'))
        self.assertIn('{{ storage_vm_inventory_sha }}', checkout)
        validate = argv(by_name(tasks, 'Validate the inventory with the controller copy of the role'))
        self.assertEqual(validate[1:3], ['-I', '{{ playbook_dir }}/roles/storage_vm/files/storage-reconcile'])
        self.assertEqual(validate[3:5], ['validate', '--inventory'])
        names = [t['name'] for t in tasks]
        self.assertLess(names.index('Log the inventory commit'), names.index('Fetch the inventory commit'))

    def test_kubeconfig_rewritten_private(self):
        tasks = role_tasks('kubeconfig')
        self.assertTrue(by_name(tasks, 'Read the control-plane kubeconfig without logging credentials')['no_log'])
        rewrite = module(by_name(tasks, 'Address the API at storage_vm_api_url'), 'set_fact')
        self.assertIn('storage_vm_api_url', str(rewrite))
        write = by_name(tasks, 'Write the temporary kubeconfig')
        self.assertEqual((module(write, 'copy')['mode'], write['no_log'], write['delegate_to']), ('0600', True, 'localhost'))

    def test_ledger_dry_run_on_storage_before_lock(self):
        ps = plays('storage.yml')
        dry = next(i for i, p in enumerate(ps) if 'dry-run.yml' in str(p))
        self.assertEqual((ps[dry]['hosts'], ps[dry]['become']), ('storage', True))
        self.assertLess(dry, lock_play_index(ps))
        run = by_name(role_tasks('dry-run'), 'Dry-run the reconcile against the real ledger')
        self.assertEqual(argv(run)[:3], ['python3', '-I', '{{ storage_vm_dry_run_dir.path }}/storage-reconcile'])
        self.assertIn('--dry-run', argv(run))
        self.assertNotIn('failed_when', run)  # a nonzero exit fails the play, and any_errors_fatal ends the run
        self.assertNotIn('maintenance_lock.py', str(role_tasks('dry-run')))

    def test_lock_verify_runs_on_controller(self):
        locks = [t for t in all_tasks() if 'maintenance_lock.py' in ' '.join(argv(t))]
        self.assertEqual(len(locks), 1)  # lock-verify.yml, imported by storage.yml and storage-expiry.yml
        for name in ('storage.yml', 'storage-expiry.yml'):
            self.assertEqual(Path(ROOT / name).read_text().count('tasks_from: lock-verify.yml'), 1, name)
        for t in locks:
            self.assertEqual((t['delegate_to'], t['become'], t['run_once']), ('localhost', False, True))
            self.assertEqual(argv(t)[2:], ['verify', '--root', '{{ storage_vm_maintenance_root }}'])

    def test_role_then_token_then_apply(self):
        play = plays('storage.yml')[4]
        self.assertIn('lock-verify.yml', str(play['pre_tasks']))
        self.assertEqual(play['roles'], [{'role': 'storage_vm'}])
        names = [t['name'] for t in play['tasks']]
        self.assertLess(names.index('Copy the storage-fencer token and CA'), names.index('Reconcile volumes and targets'))
        apply = argv(by_name(play['tasks'], 'Reconcile volumes and targets'))
        self.assertEqual(apply[:2], ['/usr/local/sbin/storage-reconcile', 'apply'])
        self.assertNotIn('--dry-run', apply)

    def test_token_copy_no_log_and_restarts_only_fencer(self):
        tasks = plays('storage.yml')[4]['tasks']
        read = by_name(tasks, 'Read the storage-fencer token Secret')
        k8s = read['kubernetes.core.k8s_info']
        self.assertEqual((k8s['kind'], k8s['namespace'], k8s['name']), ('Secret', 'democratic-csi', 'storage-fencer-token'))
        self.assertEqual((read['no_log'], read['delegate_to'], read['become']), (True, 'localhost', False))
        copy = by_name(tasks, 'Copy the storage-fencer token and CA')
        self.assertTrue(copy['no_log'])
        self.assertEqual(copy['loop'], ['token', 'ca.crt'])
        self.assertEqual(module(copy, 'copy')['dest'], '/etc/storage-fencer/{{ item }}')
        self.assertEqual(module(copy, 'copy')['mode'], '0600')
        self.assertEqual(copy['notify'], 'Restart storage-fencer')
        warn = by_name(tasks, 'Warn that the storage-fencer token Secret is missing')
        self.assertIn('debug', str(list(warn)))
        self.assertEqual((copy['when'], warn['when']), ('storage_vm_token_ready', 'not storage_vm_token_ready'))
        expr = module(by_name(tasks, 'Check that the token Secret is populated'), 'set_fact')['storage_vm_token_ready']
        def ready(secret):
            return Templar(variables={'storage_vm_token_secret': secret}).template(expr)
        self.assertFalse(ready({'resources': []}))
        self.assertTrue(ready({'resources': [{'data': {'token': 'dA==', 'ca.crt': 'Yw=='}}]}))
        self.assertFalse(ready({'resources': [{'data': {'ca.crt': 'Yw=='}}]}))
        self.assertFalse(ready({'resources': [{}]}))

    def test_refused_apply_still_grows_and_cleans_up(self):
        tasks = plays('storage.yml')[4]['tasks']
        apply = by_name(tasks, 'Reconcile volumes and targets')
        self.assertIs(apply['failed_when'], False)
        expr = module(by_name(tasks, 'Record the exported volumes for the growth play'), 'set_fact')['storage_vm_exported']
        report = json.dumps({'actions': [], 'exported': [{'id': 'a', 'size_bytes': 1}]})
        self.assertEqual(list(Templar(variables={'storage_vm_apply': {'rc': 1, 'stdout': report}}).template(expr)),
                         [{'id': 'a', 'size_bytes': 1}])
        self.assertEqual(list(Templar(variables={'storage_vm_apply': {'rc': 1, 'stdout': ''}}).template(expr)), [])
        self.assertIn('ansible.builtin.file', plays('storage.yml')[-2]['tasks'][0])
        fail = plays('storage.yml')[-1]['tasks'][-1]
        self.assertIn('ansible.builtin.fail', fail)
        for rc, failed in ((0, False), (1, True)):
            self.assertIs(holds([fail['when']], **fail['vars'], groups={'storage': ['s']},
                                hostvars={'s': {'storage_vm_apply': {'rc': rc, 'stderr': ''}}}), failed)

    def test_first_boot_check_on_a_fresh_root(self):
        # A fresh root whose stock rtslib-fb-targetctl was already active never ran the drop-in's ExecStartPost: no
        # storage_boot_check_ok until the first reboot, so StorageBootCheckFailed's absent() leg fires (finding n=15).
        # After the reconcile, so a rebuilt root's targets exist when it checks them.
        tasks = plays('storage.yml')[4]['tasks']
        names = [t['name'] for t in tasks]
        find = by_name(tasks, 'Find the boot check result')
        unit = by_name(tasks, 'Read the LIO restore unit state')
        check = by_name(tasks, 'Run the boot check once on a root that never ran it')
        self.assertLess(names.index('Reconcile volumes and targets'), names.index(find['name']))
        self.assertLess(names.index(find['name']), names.index(unit['name']))
        self.assertLess(names.index(unit['name']), names.index(check['name']))
        self.assertEqual(module(find, 'stat')['path'], str(metrics.TEXTFILE_DIR / 'storage-boot-check.prom'))
        self.assertIn("metrics.write('storage-boot-check',", (ROLE / 'files/homelab_storage/reconcile.py').read_text())
        self.assertEqual(argv(unit), ['systemctl', 'is-active', 'rtslib-fb-targetctl'])
        self.assertIs(unit['failed_when'], False)
        self.assertEqual(argv(check), ['/usr/local/sbin/storage-reconcile', 'boot-check'])
        self.assertIs(check['failed_when'], False)  # grow and cleanup still run; the last play fails the run
        for exists, active, runs in ((False, 'active', True), (True, 'active', False), (False, 'inactive', False),
                                     (False, 'failed', False)):
            with self.subTest(exists=exists, active=active):
                state = {'storage_vm_boot_check_file': {'stat': {'exists': exists}},
                         'storage_vm_targetctl_active': {'stdout': active} if not exists else {'skipped': True}}
                self.assertIs(holds(check['when'], **state), runs)
        fail = by_name(plays('storage.yml')[-1]['tasks'], 'Fail if the first boot check failed')
        for result, failed in (({'skipped': True}, False), ({'rc': 0}, False), ({'rc': 1, 'stderr': 'x'}, True)):
            self.assertIs(holds([fail['when']], **fail['vars'], groups={'storage': ['s']},
                                hostvars={'s': {'storage_vm_first_boot_check': result}}), failed)

    def test_growth_play_is_level_triggered(self):
        play = plays('storage.yml')[5]
        self.assertIn('grow.yml', str(play))
        include = by_name(role_tasks('grow'), 'Grow each attached exported volume')
        volumes = module(by_name(role_tasks('grow'), 'Find the exported volumes attached to this node'),
                         'set_fact')['storage_vm_grow_volumes']
        self.assertEqual(include['loop'], '{{ storage_vm_grow_volumes }}')
        self.assertIn('storage_vm_exported', volumes)
        self.assertNotRegex(volumes, r'extend|actions')
        steps = [t['name'] for t in walk(role_tasks('grow-volume'))]
        self.assertEqual(steps, ['List the iSCSI sessions', 'Find the session of this target',
                                 'Refuse more than one session of this target', 'Grow only while logged in here',
                                 'Rescan the session', 'Read the device size', 'Refuse a device smaller than the inventory size',
                                 'Find the mounted filesystem of the device', 'Grow the filesystem online'])
        # A VolumeAttachment outlives the login and the mount (pod restart, drain): skip then, never fail or go offline
        # (lab B5 review, 2026-11-16: resize2fs on an unmounted fs takes the offline path and exits 1).
        sessions = by_name(role_tasks('grow-volume'), 'List the iSCSI sessions')
        self.assertEqual(sessions['failed_when'], 'storage_vm_sessions.rc not in [0, 21]')  # 21: no active sessions
        gate = by_name(role_tasks('grow-volume'), 'Grow only while logged in here')
        self.assertEqual(gate['when'], 'storage_vm_session_lines | length == 1')
        mount = by_name(role_tasks('grow-volume'), 'Find the mounted filesystem of the device')
        self.assertEqual(argv(mount), ['findmnt', '-n', '-o', 'FSTYPE', '--source', '{{ storage_vm_device }}'])
        self.assertEqual(mount['failed_when'], 'storage_vm_mount.rc not in [0, 1]')
        grow_when = by_name(role_tasks('grow-volume'), 'Grow the filesystem online')['when']
        self.assertEqual(grow_when, ["storage_vm_mount.rc == 0", "storage_vm_mount.stdout_lines | unique == ['ext4']"])
        rescan = argv(by_name(role_tasks('grow-volume'), 'Rescan the session'))
        self.assertEqual(rescan, ['iscsiadm', '-m', 'session', '-r', '{{ storage_vm_sid }}', '-R'])
        size = argv(by_name(role_tasks('grow-volume'), 'Read the device size'))
        self.assertEqual(size, ['blockdev', '--getsize64', '{{ storage_vm_device }}'])
        line = f'tcp: [7] {BRIDGE}:3260,1 iqn.2026-11.net.starktastic.storage:paperless (non-flash)'
        variables = defaults(**{**include['vars'], 'storage_vm_portal': f'{BRIDGE}:3260',
                                'storage_vm_volume': {'id': 'paperless', 'size_bytes': 1, 'iqn': lio.iqn('paperless')},
                                'storage_vm_session_lines': [line]})
        templar = Templar(variables=variables)
        self.assertEqual(templar.template(trust_as_template('{{ storage_vm_device }}')),
                         f'/dev/disk/by-path/ip-{BRIDGE}:3260-iscsi-iqn.2026-11.net.starktastic.storage:paperless-lun-0')
        self.assertEqual(str(templar.template(trust_as_template('{{ storage_vm_sid }}'))), '7')
        find = module(by_name(role_tasks('grow-volume'), 'Find the session of this target'), 'set_fact')['storage_vm_session_lines']
        other = f'tcp: [8] {BRIDGE}:3260,1 iqn.2026-11.net.starktastic.storage:paperless-old (non-flash)'
        got = Templar(variables={**variables, 'storage_vm_sessions': {'stdout_lines': [other, line]}}).template(find)
        self.assertEqual(list(got), [line])

    @unittest.skipUnless(shutil.which('ansible-playbook', path=str(Path(sys.executable).parent)) or
                         shutil.which('ansible-playbook'), 'ansible-playbook is not installed')
    def test_unreachable_worker_does_not_fail_the_grow(self):
        # A hung worker keeps its VolumeAttachments: failing the grow play on it kept the lock (D31) through exactly
        # those incidents (finding n=4). The real grow.yml runs, with only its two k8s_info reads replaced.
        exe = shutil.which('ansible-playbook', path=str(Path(sys.executable).parent)) or shutil.which('ansible-playbook')
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        tasks = d / 'roles/storage_vm/tasks'
        tasks.mkdir(parents=True)
        shutil.copy(ROLE / 'tasks/grow-volume.yml', tasks)
        grow = (ROLE / 'tasks/grow.yml').read_text()
        reads = [t['name'] for t in role_tasks('grow') if 'kubernetes.core.k8s_info' in t]
        self.assertEqual(reads, ['List PersistentVolumes', 'List VolumeAttachments'])
        fixture = [t for t in load(ROLE / 'tasks/grow.yml') if t['name'] not in reads]
        probe = by_name(fixture, 'Check that this node answers')
        self.assertEqual((module(probe, 'wait_for_connection'), probe['ignore_errors']), ({'timeout': 30}, True))
        probe['ansible.builtin.wait_for_connection'] = {'timeout': 2}  # the refused port fails at once; keep it quick
        driver = 'org.democratic-csi.node-manual'
        pvs = {'resources': [{'metadata': {'name': f'pv-{i}'}, 'spec': {'csi': {'driver': driver, 'volumeHandle': i}}}
                             for i in ('a', 'b')]}
        vas = {'resources': [{'spec': {'attacher': driver, 'nodeName': n, 'source': {'persistentVolumeName': f'pv-{i}'}}}
                             for n, i in (('hung', 'a'), ('alive', 'b'))]}
        (tasks / 'grow.yml').write_text(json.dumps(
            [{'name': 'Fixture reads', 'ansible.builtin.set_fact': {'storage_vm_pvs': pvs, 'storage_vm_vas': vas}},
             *fixture]))
        self.assertIn('storage_vm_exported', grow)
        (d / 'inventory.json').write_text(json.dumps({'all': {'hosts': {
            'storage-01': {'ansible_connection': 'local'},
            'alive': {'ansible_connection': 'local'},
            'hung': {'ansible_host': '127.0.0.1', 'ansible_port': 1, 'ansible_connection': 'ssh',
                     'ansible_ssh_common_args': '-o ConnectTimeout=3 -o BatchMode=yes'}},
            'children': {'storage': {'hosts': {'storage-01': {}}}, 'masters': {'hosts': {'storage-01': {}}},
                         'all_k3s': {'hosts': {'alive': {}, 'hung': {}}}}},
            'vars': {'ansible_python_interpreter': sys.executable}}))
        storage_play = plays('storage.yml')[5]
        self.assertEqual((storage_play['hosts'], storage_play['any_errors_fatal']), ('all_k3s', True))

        def run(exported, iscsiadm_rc):
            (d / 'bin').mkdir(exist_ok=True)
            fake = d / 'bin/iscsiadm'
            fake.write_text(f'#!/bin/sh\nexit {iscsiadm_rc}\n')  # 21: no active sessions, so nothing to grow
            fake.chmod(0o755)
            (d / 'play.yml').write_text(json.dumps([
                {'name': 'Exported', 'hosts': 'storage', 'gather_facts': False, 'tasks': [
                    {'ansible.builtin.set_fact': {'storage_vm_exported': exported, 'storage_vm_portal': '192.0.2.10:3260'}}]},
                {**{k: v for k, v in storage_play.items() if k != 'become'}, 'tasks': [
                    {'ansible.builtin.import_role': {'name': 'storage_vm', 'tasks_from': 'grow.yml'}}]}]))
            env = dict(os.environ, PATH=f'{d / "bin"}:{os.environ["PATH"]}', ANSIBLE_HOST_KEY_CHECKING='False',
                       ANSIBLE_CONFIG=str(d / 'none.cfg'), ANSIBLE_ROLES_PATH=str(d / 'roles'))
            return subprocess.run([exe, '-i', str(d / 'inventory.json'), '-e', f'storage_vm_csi_driver={driver}',
                                   str(d / 'play.yml')], capture_output=True, text=True, env=env, timeout=120, cwd=d)

        vol = lambda i: {'id': i, 'size_bytes': 1, 'iqn': lio.iqn(i)}
        got = run([vol('a'), vol('b')], 21)
        self.assertEqual(got.returncode, 0, got.stdout[-3000:] + got.stderr[-2000:])
        self.assertIn('hung is unreachable; not grown: a', got.stdout)
        # A real failure on a reachable worker still fails the run.
        got = run([vol('a'), vol('b')], 1)
        self.assertNotEqual(got.returncode, 0, got.stdout[-3000:])
        self.assertIn('alive', got.stdout)

    def test_set_fact_never_reads_a_sibling_key(self):
        # Ansible templates every set_fact argument before it sets any, so a sibling reference is undefined
        # (lab B4 Step 4, 2026-11-16: storage_vm_attached_ids read storage_vm_attached_pvs and failed every run).
        found = []
        for task in all_tasks():
            names = [n for n in ('ansible.builtin.set_fact', 'ansible.legacy.set_fact', 'set_fact') if n in task]
            if not names:
                continue
            args = task[names[0]]
            if not isinstance(args, dict):
                found.append(f"{task.get('name')}: one-line set_fact; use the mapping form")
                continue
            keys = [k for k in args if k != 'cacheable']
            found += [f"{task.get('name')}: {key} reads {other}" for key in keys for other in keys
                      if other != key and re.search(rf'\b{re.escape(other)}\b', str(args[key]))]
        self.assertEqual(found, [])

    def test_attached_ids_only_this_node(self):
        tasks = role_tasks('grow')
        names = module(by_name(tasks, 'Find the PersistentVolumes attached to this node'), 'set_fact')
        ids = module(by_name(tasks, 'Find the volume IDs attached to this node'), 'set_fact')
        driver = 'org.democratic-csi.node-manual'
        vas = {'resources': [
            {'spec': {'attacher': driver, 'nodeName': 'k3s-worker-01', 'source': {'persistentVolumeName': 'pv-a'}}},
            {'spec': {'attacher': driver, 'nodeName': 'k3s-worker-02', 'source': {'persistentVolumeName': 'pv-b'}}},
            {'spec': {'attacher': 'csi.proxmox.sinextra.dev', 'nodeName': 'k3s-worker-01', 'source': {'persistentVolumeName': 'pv-c'}}},
            {'spec': {'attacher': driver, 'nodeName': 'k3s-worker-01', 'source': {'inlineVolumeSpec': {}}}}]}
        pvs = {'resources': [
            {'metadata': {'name': 'pv-a'}, 'spec': {'csi': {'driver': driver, 'volumeHandle': 'a'}}},
            {'metadata': {'name': 'pv-b'}, 'spec': {'csi': {'driver': driver, 'volumeHandle': 'b'}}},
            {'metadata': {'name': 'pv-c'}, 'spec': {'csi': {'driver': 'csi.proxmox.sinextra.dev', 'volumeHandle': 'c'}}},
            {'metadata': {'name': 'pv-nfs'}, 'spec': {'nfs': {}}}]}
        templar = Templar(variables=defaults(inventory_hostname='k3s-worker-01', storage_vm_vas=vas, storage_vm_pvs=pvs))
        self.assertEqual(list(names), ['storage_vm_attached_pvs'])
        pv_names = templar.template(names['storage_vm_attached_pvs'])
        got = Templar(variables=defaults(storage_vm_attached_pvs=pv_names, storage_vm_pvs=pvs)).template(ids['storage_vm_attached_ids'])
        self.assertEqual(list(got), ['a'])


class SharedPlayTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('ansible-playbook', path=str(Path(sys.executable).parent)) or
                         shutil.which('ansible-playbook'), 'ansible-playbook is not installed')
    def test_missing_inventory_file_fails_never_reads_as_empty(self):
        # Before the apps inventory file lands (or after a path rename), the fetch must fail before the lock, never
        # hand the reconcile or the expiry an empty inventory, which reads as "every volume removed" (finding n=17).
        # The real storage-play-fetch-inventory.yml runs against a local apps repo.
        exe = shutil.which('ansible-playbook', path=str(Path(sys.executable).parent)) or shutil.which('ansible-playbook')
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        (d / 'roles').symlink_to(ROOT / 'roles')  # the validate step runs {{ playbook_dir }}/roles/.../storage-reconcile
        shutil.copy(ROOT / 'storage-play-fetch-inventory.yml', d)
        repo = d / 'apps'
        path = load(ROLE / 'defaults/main.yml')['storage_vm_inventory_path']
        git = lambda *a: subprocess.run(['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@t', *a],
                                        check=True, capture_output=True)
        repo.mkdir()
        git('init', '-q', '-b', 'main')
        (repo / 'README.md').write_text('x\n')
        git('add', '-A')
        git('commit', '-qm', 'no inventory yet')
        (d / 'inventory.json').write_text(json.dumps({'all': {
            'hosts': {'localhost': {'ansible_connection': 'local', 'ansible_python_interpreter': sys.executable}},
            'children': {'storage': {'hosts': {'storage-01': {}}}}}}))

        def run():
            return subprocess.run([exe, '-i', str(d / 'inventory.json'), '-e', f'storage_vm_inventory_repo={repo}',
                                   str(d / 'storage-play-fetch-inventory.yml')], capture_output=True, text=True,
                                  env=dict(os.environ, ANSIBLE_CONFIG=str(d / 'none.cfg')), timeout=120, cwd=d)

        got = run()
        self.assertNotEqual(got.returncode, 0, got.stdout[-3000:])
        self.assertIn('Check out only the inventory file', got.stdout)
        self.assertNotIn('Validate the inventory with the controller copy of the role] *\nok', got.stdout)
        (repo / path).parent.mkdir(parents=True)
        (repo / path).write_text('[]\n')
        git('add', '-A')
        git('commit', '-qm', 'empty inventory')
        got = run()
        self.assertEqual(got.returncode, 0, got.stdout[-3000:] + got.stderr[-2000:])

    def test_dry_run_playbook_takes_no_lock(self):
        ps = plays('storage-dry-run.yml')
        self.assertEqual([p['hosts'] for p in ps], ['all', 'localhost', 'storage', 'localhost'])
        self.assertEqual(ps[0]['name'], 'Lab guard')
        self.assertEqual(ps[2], next(p for p in plays('storage.yml') if p['name'] == 'Dry-run the reconcile before the lock'))
        cleanup = ps[3]['tasks']
        self.assertEqual(len(cleanup), 1)
        self.assertEqual(module(cleanup[0], 'file'), {'path': '{{ storage_vm_checkout.path }}', 'state': 'absent'})
        text = (ROOT / 'storage-dry-run.yml').read_text()
        self.assertNotIn('lock-verify', text)
        self.assertNotIn('maintenance_lock', text)

    def test_preflight_checks_k3s_without_the_lock(self):
        ps = plays('storage-preflight.yml')
        # The expiry has no dry run, so this fetch and validate is its only pre-lock check of the inventory: a fetch
        # failure (a GitHub outage, a missing file) ends the run before acquire, not under a kept lock (finding n=17).
        self.assertEqual([p['hosts'] for p in ps], ['all', 'localhost', 'masters[0]', 'localhost', 'localhost'])
        self.assertEqual(ps[1]['name'], 'Fetch and validate the inventory at the branch tip')
        self.assertEqual(ps[4]['tasks'], plays('storage-dry-run.yml')[-1]['tasks'])  # checkout only: no kubeconfig ran
        ps = ps[:1] + ps[2:4]
        self.assertTrue(all(p['any_errors_fatal'] is True for p in ps))
        text = (ROOT / 'storage-preflight.yml').read_text()
        self.assertNotIn('lock-verify', text)
        self.assertNotIn('maintenance_lock', text)
        self.assertEqual([module(t, 'import_role') for t in ps[1]['tasks']],
                         [{'name': 'storage_vm', 'tasks_from': 'k3s-check.yml'}])
        tasks = role_tasks('k3s-check')
        ask = by_name(tasks, 'Ask the API at storage_vm_api_url for readiness')
        slurp = module(by_name(role_tasks('kubeconfig'), 'Read the control-plane kubeconfig without logging credentials'),
                       'slurp')['src']
        args = argv(ask)
        # The kubeconfig and the URL the run's kubeconfig play and k8s_info use, so a skip-free pass means they work.
        self.assertEqual(args[:3], ['k3s', 'kubectl', '--kubeconfig'])
        self.assertEqual(args[3], slurp)
        self.assertEqual(args[args.index('--server') + 1], '{{ storage_vm_api_url }}')
        self.assertEqual(args[-3:], ['get', '--raw', '/readyz'])
        self.assertTrue(any(a.startswith('--request-timeout=') for a in args))
        self.assertIs(ask['failed_when'], False)
        self.assertNotIn('ignore_unreachable', ask)  # an unreachable master goes red, never a green skip
        ready = module(by_name(tasks, 'Record whether k3s answers'), 'set_fact')['storage_vm_k3s_ready']
        for result, want in (({'rc': 0, 'stdout': 'ok'}, True), ({'rc': 0, 'stdout': 'ok\n'}, True),
                             ({'rc': 1, 'stdout': ''}, False), ({'rc': 0, 'stdout': 'not ok'}, False),
                             ({'failed': True, 'msg': 'No such file or directory: k3s'}, False)):
            with self.subTest(result=result):
                got = Templar(variables={'storage_vm_k3s_readyz': result}).template(ready)
                self.assertIs(got, want)
        record = ps[2]['tasks']
        write = by_name(record, 'Write the k3s answer to the step outputs')
        line = module(write, 'lineinfile')['line']
        for answer, want in ((True, 'k3s=true'), (False, 'k3s=false')):
            hv = {'groups': {'masters': ['m']}, 'hostvars': {'m': {'storage_vm_k3s_ready': answer}}}
            self.assertEqual(Templar(variables=hv).template(trust_as_template(line)), want)
        need = module(by_name(record, 'Require the answer of masters[0]'), 'assert')['that']
        self.assertFalse(holds(need, groups={'masters': ['m']}, hostvars={'m': {}}))
        self.assertTrue(holds(need, groups={'masters': ['m']}, hostvars={'m': {'storage_vm_k3s_ready': False}}))

    def test_host_metrics_reads_only_on_pve(self):
        tasks = role_tasks('host-metrics')
        on_pve = [t for t in tasks if t.get('delegate_to') == "{{ groups['proxmox_hosts'][0] }}"]
        self.assertEqual(len(on_pve), 1)
        for t in on_pve:
            self.assertEqual(argv(t)[0], 'df', t['name'])
            self.assertIs(t['changed_when'], False, t['name'])
        self.assertTrue(all(t.get('delegate_to') in (None, 'localhost') for t in tasks if t not in on_pve))

    def test_host_metrics_never_reads_nonce(self):
        self.assertNotIn('nonce', (ROLE / 'tasks/host-metrics.yml').read_text())
        lock = by_name(role_tasks('host-metrics'), 'Read the maintenance lock holder and start time')
        script = argv(lock)[2]
        self.assertEqual(set(re.findall(r"record\['(\w+)'\]", script)), {'operation', 'created_at'})
        with tempfile.TemporaryDirectory() as d:
            record = Path(d) / 'operation.json'
            run = lambda: json.loads(subprocess.run([sys.executable, '-c', script, str(record)], check=True,
                                                    capture_output=True, text=True).stdout)
            self.assertEqual(run(), {'operation': '', 'created': 0})  # the lock is free
            record.write_text(json.dumps({'schema': 1, 'owner': 'o', 'nonce': 'secret', 'operation': 'ansible-deploy',
                                          'stage': 'acquired', 'created_at': '2026-11-23T10:00:00.123456+00:00'}))
            self.assertEqual(run(), {'operation': 'ansible-deploy', 'created': 1795428000.123456})
        self.assertEqual(argv(lock)[3], '{{ storage_vm_maintenance_root }}/operation.json')

    def test_host_metrics_writes_the_alert_series(self):
        write = by_name(role_tasks('host-metrics'), 'Write storage-host.prom')
        args = argv(write)
        for metric in ('storage_dataset_avail_bytes', 'storage_dataset_timestamp_seconds',
                       'storage_maintenance_lock_created_timestamp_seconds'):
            self.assertIn(f"('{metric}',", args[2])
        self.assertNotIn('size', args[2])  # pve's df size is not the quota; the alert floors avail
        self.assertIn("metrics.write('storage-host',", args[2])
        install = by_name(role_tasks(), 'Install the storage programs')
        self.assertIn(f"sys.path.insert(0, '{module(install, 'copy')['dest'].rstrip('/')}')", args[2])
        self.assertEqual(by_name(role_tasks('host-metrics'), 'Read the k3s-block free space on pve')['timeout'], 60)
        df = {'stdout_lines': ['Mounted on Type Avail', '/mnt/pve/k3s-block nfs4 104903946240']}
        lock = {'stdout': '{"operation": "ansible-deploy", "created": 1795428000.5}'}
        templar = Templar(variables=defaults(storage_vm_df=df, storage_vm_lock=lock))
        self.assertEqual([str(templar.template(trust_as_template(a))) for a in args[3:]],  # argv items become strings
                         ['apps/k3s-block', '104903946240', 'ansible-deploy', '1795428000.5'])
        with tempfile.TemporaryDirectory() as d, mock.patch.object(metrics, 'TEXTFILE_DIR', Path(d)), \
                mock.patch.object(sys, 'argv', ['-c', 'apps/k3s-block', '15', '', '0']):
            exec(args[2], {})  # the role's own metrics module: the path insert finds nothing here
            text = (Path(d) / 'storage-host.prom').read_text()
        self.assertRegex(text, r'^storage_dataset_avail_bytes\{dataset="apps/k3s-block"\} 15\n'
                               r'storage_dataset_timestamp_seconds \d+(\.\d+)?\n'  # test_proxmox_backup pins time.time to an int
                               r'storage_maintenance_lock_created_timestamp_seconds\{operation=""\} 0\.0\n$')

    def test_host_metrics_df_must_be_the_nfs_mount(self):
        # An unmounted path reports pve's root filesystem with a fresh timestamp; fail so the staleness leg fires.
        df = by_name(role_tasks('host-metrics'), 'Read the k3s-block free space on pve')
        self.assertEqual(argv(df)[:3], ['df', '-B1', '--output=target,fstype,avail'])
        failed = lambda rc, line: holds([df['failed_when']], storage_vm_df={
            'rc': rc, 'stdout_lines': ['Mounted on Type Avail', line] if line else []})
        self.assertFalse(failed(0, '/mnt/pve/k3s-block nfs4 104903946240'))
        self.assertFalse(failed(0, '/mnt/pve/k3s-block nfs 104903946240'))
        self.assertTrue(failed(0, '/ zfs 90000000000'))  # not mounted: rpool
        self.assertTrue(failed(0, '/mnt/pve/k3s-block zfs 90000000000'))
        self.assertTrue(failed(1, ''))
        # The task timeout registers no rc or stdout_lines: fail cleanly, not with a templating error.
        self.assertTrue(holds([df['failed_when']], storage_vm_df={'failed': True, 'msg': 'timed out'}))

    def test_host_metrics_playbook_takes_no_lock(self):
        ps = plays('storage-host-metrics.yml')
        self.assertEqual([p['hosts'] for p in ps], ['all', 'storage'])
        self.assertEqual(ps[0]['name'], 'Lab guard')
        self.assertEqual(imported(load(ROOT / 'storage-host-metrics.yml')[0]), 'storage-play-lab-guard.yml')
        self.assertEqual(ps[1]['gather_facts'], False)
        self.assertEqual([module(t, 'import_role') for t in ps[1]['tasks']],
                         [{'name': 'storage_vm', 'tasks_from': 'host-metrics.yml'}])
        text = (ROOT / 'storage-host-metrics.yml').read_text()
        self.assertNotIn('lock-verify', text)
        self.assertNotIn('maintenance_lock', text)

    def test_storage_playbooks_share_plays(self):
        # storage-host-metrics.yml reconciles nothing, so it fetches no inventory (test_host_metrics_playbook_takes_no_lock).
        self.assertEqual(TOP_PLAYBOOKS, ['storage-dry-run.yml', 'storage-expiry.yml', 'storage-host-metrics.yml',
                                         'storage-preflight.yml', 'storage.yml'])
        for name in (n for n in TOP_PLAYBOOKS if n != 'storage-host-metrics.yml'):
            raw = load(ROOT / name)
            self.assertEqual(imported(raw[0]), 'storage-play-lab-guard.yml', name)
            self.assertIn('storage-play-fetch-inventory.yml', [imported(p) for p in raw], name)
            for p in raw:
                self.assertNotIn(p.get('name'), ('Lab guard', 'Fetch and validate the inventory at the branch tip'), name)
        for name in ('storage.yml', 'storage-expiry.yml'):
            self.assertIn('storage-play-cleanup.yml', [imported(p) for p in load(ROOT / name)], name)
        self.assertEqual([p['name'] for p in plays('storage-expiry.yml')[:3]],
                         ['Lab guard', 'Verify maintenance ownership', 'Fetch and validate the inventory at the branch tip'])


class ExpiryPlaybookTests(unittest.TestCase):
    def test_expiry_verifies_lock_before_delete(self):
        ps = plays('storage-expiry.yml')
        self.assertEqual([p['name'] for p in ps], ['Lab guard', 'Verify maintenance ownership',
                                                    'Fetch and validate the inventory at the branch tip',
                                                    'Write a private kubeconfig', 'List expiry candidates',
                                                    'Expire removed volumes', 'Remove the temporary files',
                                                    'Fail if a volume was refused or failed'])
        for p in ps:
            self.assertIs(p.get('any_errors_fatal'), True, p['name'])
        self.assertEqual(lock_play_index(ps), 1)
        self.assertEqual((ps[2]['hosts'], ps[2]['tasks'][0]['ansible.builtin.import_role']['tasks_from']),
                         ('localhost', 'fetch-inventory.yml'))
        self.assertIn('expire.yml', str(ps[5]))
        cand = argv(by_name(ps[4]['tasks'], 'List pending-delete volumes past quarantine'))
        self.assertEqual(cand[:2], ['/usr/local/sbin/storage-reconcile', 'expire-candidates'])

    def test_expiry_refuses_before_delete(self):
        tasks = role_tasks('expire-volume')[0]['block']
        names = [t['name'] for t in tasks]
        refusal = names.index('Refuse while a VolumeAttachment, a session or a Pod uses the volume')
        deletes = [i for i, t in enumerate(tasks) if t.get('kubernetes.core.k8s', {}).get('state') == 'absent']
        self.assertEqual(len(deletes), 2)
        self.assertLess(refusal, min(deletes))
        expire = by_name(tasks, 'Expire the volume on the storage VM')
        self.assertLess(max(deletes), names.index(expire['name']))
        self.assertEqual(argv(expire)[:4], ['/usr/local/sbin/storage-reconcile', 'expire', '--id', '{{ storage_vm_candidate.id }}'])
        self.assertEqual((expire['delegate_to'], expire['become']), ("{{ groups['storage'][0] }}", True))
        self.assertEqual([tasks[i]['kubernetes.core.k8s']['kind'] for i in deletes], ['PersistentVolumeClaim', 'PersistentVolume'])

    def test_expiry_pv_lookup_and_refusals(self):
        tasks = role_tasks('expire-volume')
        driver = 'org.democratic-csi.node-manual'
        pvs = {'resources': [
            {'metadata': {'name': 'pv-x'}, 'spec': {'csi': {'driver': driver, 'volumeHandle': 'x'},
                                                    'claimRef': {'namespace': 'media', 'name': 'x-data'}}},
            {'metadata': {'name': 'pv-other'}, 'spec': {'csi': {'driver': 'other', 'volumeHandle': 'x'}}},
            {'metadata': {'name': 'pv-nfs'}, 'spec': {'nfs': {}}}]}
        facts = module(by_name(tasks, 'Select the PVs of this volume'), 'set_fact')
        facts = {**facts, **module(by_name(tasks, 'Select the claims of those PVs'), 'set_fact')}
        cand = {'id': 'x', 'volume_handle': 'x', 'sessions': False}
        t = Templar(variables=defaults(storage_vm_candidate=cand, storage_vm_expiry_pvs_all=pvs))
        mine = t.template(facts['storage_vm_expiry_pvs'])
        self.assertEqual([p['metadata']['name'] for p in mine], ['pv-x'])
        claims = Templar(variables={'storage_vm_expiry_pvs': mine}).template(facts['storage_vm_expiry_claims'])
        self.assertEqual(list(claims), [{'namespace': 'media', 'name': 'x-data'}])
        refuse = by_name(tasks, 'Refuse while a VolumeAttachment, a session or a Pod uses the volume')
        def ok(vas=(), pods=(), sessions=False):
            pod_results = {'results': [{'item': claims[0], 'resources': list(pods)}]}
            return holds(module(refuse, 'assert')['that'], **refuse['vars'], storage_vm_candidate=dict(cand, sessions=sessions),
                         storage_vm_expiry_pvs=mine, storage_vm_expiry_vas={'resources': list(vas)},
                         storage_vm_expiry_pods=pod_results)
        self.assertTrue(ok())
        self.assertFalse(ok(sessions=True))
        self.assertFalse(ok(vas=[{'spec': {'source': {'persistentVolumeName': 'pv-x'}}}]))
        self.assertTrue(ok(vas=[{'spec': {'source': {'persistentVolumeName': 'pv-y'}}}, {'spec': {'source': {}}}]))
        user = {'spec': {'volumes': [{'name': 'd', 'persistentVolumeClaim': {'claimName': 'x-data'}}]}}
        other = {'spec': {'volumes': [{'name': 'c', 'configMap': {}}]}}
        self.assertFalse(ok(pods=[other, user]))
        self.assertTrue(ok(pods=[other, {'spec': {}}]))

    def test_expiry_users_expression(self):
        refuse = by_name(role_tasks('expire-volume'), 'Refuse while a VolumeAttachment, a session or a Pod uses the volume')
        expr = refuse['vars']['storage_vm_expiry_used_claims']
        def mounting(*claims):
            return {'spec': {'volumes': [{'name': c, 'persistentVolumeClaim': {'claimName': c}} for c in claims]}}
        results = {'results': [
            {'item': {'namespace': 'media', 'name': 'a-data'},
             'resources': [mounting('a-data'), {'spec': {}}, mounting('other')]},
            {'item': {'namespace': 'media', 'name': 'b-data'}, 'resources': [{'spec': {}}, mounting('other')]}]}
        got = Templar(variables={'storage_vm_expiry_pods': results}).template(expr)
        self.assertEqual(got.split(), ['a-data'])
        none = {'results': [{'item': {'namespace': 'media', 'name': 'b-data'}, 'resources': [mounting('other')]}]}
        self.assertEqual((Templar(variables={'storage_vm_expiry_pods': none}).template(expr) or '').split(), [])


    def test_expiry_skips_volumes_in_the_inventory(self):
        # A volume re-added to the inventory keeps its pending-delete tag until a green reconcile: never expire it.
        tasks = role_tasks('expire')
        ids = module(by_name(tasks, 'Read the volume ids in the inventory'), 'set_fact')['storage_vm_inventory_ids']
        self.assertIn('storage_vm_inventory_file', ids)
        inv = [{'id': 'back'}, {'id': 'kept'}]
        got = Templar(variables={}).template(trust_as_template(
            ids.replace("lookup('ansible.builtin.file', storage_vm_inventory_file)", repr(json.dumps(inv)))))
        self.assertEqual(list(got), ['back', 'kept'])
        cands = [{'id': 'back'}, {'id': 'gone'}]
        variables = {'groups': {'storage': ['s'], 'masters': ['m']}, 'storage_vm_inventory_ids': ['back', 'kept'],
                     'hostvars': {'s': {'storage_vm_expiry_candidates': cands}}}
        each = by_name(tasks, 'Expire each candidate')
        self.assertEqual([c['id'] for c in Templar(variables=variables).template(each['loop'])], ['gone'])
        warn = by_name(tasks, 'Warn about pending-delete volumes that are back in the inventory')
        self.assertEqual([c['id'] for c in Templar(variables={**variables, **warn['vars']}).template(warn['loop'])],
                         ['back'])

    def test_expiry_refusal_skips_only_that_volume_then_cleans_up(self):
        block = role_tasks('expire-volume')
        self.assertEqual(len(block), 1)
        record = module(block[0]['rescue'][0], 'set_fact')['storage_vm_expiry_refused']
        self.assertEqual(Templar(variables={'storage_vm_candidate': {'id': 'b'}, 'storage_vm_expiry_refused': ['a']})
                         .template(record), ['a', 'b'])
        cleanup = plays('storage-expiry.yml')[-2]['tasks']
        self.assertEqual(cleanup[0]['loop'], ["{{ hostvars[groups['masters'][0]].storage_vm_kubeconfig_path }}",
                                              '{{ storage_vm_checkout.path }}'])
        fail = plays('storage-expiry.yml')[-1]['tasks'][-1]
        self.assertIn('ansible.builtin.fail', fail)
        self.assertTrue(holds([fail['when']], storage_vm_expiry_refused=['a']))
        self.assertFalse(holds([fail['when']]))



class BootUnitTests(unittest.TestCase):
    def test_targetctl_dropin(self):
        pv = {'retained': '/dev/disk/by-id/virtio-retained', 'regen': '/dev/disk/by-id/virtio-regen'}
        unit = r'dev-disk-by\x2did-virtio\x2dretained.device'
        want = [line for line in TARGETCTL_DROPIN.replace(DEV_UNIT, unit).replace(
            '/dev/disk/by-path/pci-0000:06:0b.0', pv['retained']).splitlines() if not line.startswith('#')]
        got = render('rtslib-fb-targetctl.service.d-50-homelab.conf.j2', storage_vm_pv_devices=pv)
        self.assertEqual([line for line in got.splitlines() if not line.startswith('#')], want)
        self.assertIn(f'Wants={unit}', want)
        self.assertIn('ExecStartPre=/sbin/vgchange --devices /dev/disk/by-id/virtio-retained -ay retained', want)

    def test_guest_panics_on_oops(self):
        t = by_name(role_tasks(), 'Reboot 10 s after a kernel oops')
        self.assertEqual(module(t, 'sysctl') or t.get('ansible.posix.sysctl'), {
            'name': '{{ item.key }}', 'value': '{{ item.value }}', 'sysctl_file': '/etc/sysctl.d/90-panic-reboot.conf',
            'sysctl_set': True, 'state': 'present', 'reload': True})
        self.assertEqual(t['loop'], [{'key': 'kernel.panic_on_oops', 'value': '1'}, {'key': 'kernel.panic', 'value': '10'}])
        self.assertNotIn('when', t)


class LabExampleTests(unittest.TestCase):
    def test_examples_hosts_and_groups_only(self):
        inv = load(ROOT / 'lab/inventory.example.yml')
        self.assertEqual(set(inv['all']['children']), {'storage', 'all_k3s'})
        self.assertEqual(set(inv['all']['children']['all_k3s']['children']), {'masters', 'workers'})
        self.assertEqual(inv['all']['vars'], {'lab': True})  # the lab marker; nothing else in the inventory
        self.assertNotIn('vars', str(inv['all']['children']))
        lab_vars = load(ROOT / 'lab/vars.example.yml')
        self.assertNotIn('lab', lab_vars)  # the guard exists to catch a run that forgot this file
        self.assertNotEqual(lab_vars['vip_address'], GROUP_ALL['production_vip_address'])
        self.assertTrue(lab_vars['storage_vm_ssh_sources'])
        tasks = role_tasks('assert')
        that = module(tasks[0], 'assert')['that'] + module(
            by_name(tasks, 'Require distinct worker bridge addresses inside the bridge subnet'), 'assert')['that']
        self.assertTrue(holds(that, **lab_vars))

    def test_examples_match_the_plan(self):
        groups = load(ROOT / 'lab/inventory.example.yml')['all']['children']['all_k3s']['children']
        self.assertEqual((set(groups['masters']['hosts']), set(groups['workers']['hosts'])),
                         ({'lab-server'}, {'lab-server', 'lab-agent'}))
        lab_vars = load(ROOT / 'lab/vars.example.yml')
        self.assertEqual({k: lab_vars[k] for k in ('mgmt_iface', 'flannel_iface', 'proxmox_csi_enabled',
                                                   'k3s_node_retirement_enabled')},
                         {'mgmt_iface': 'eth0', 'flannel_iface': 'eth1', 'proxmox_csi_enabled': False,
                          'k3s_node_retirement_enabled': False})
        self.assertRegex(lab_vars['storage_vm_inventory_repo'], r'^/maintenance/lab-storage/.+[.]git$')
        self.assertEqual(lab_vars['storage_vm_timings']['quarantine_seconds'], 900)
        self.assertEqual(set(lab_vars['storage_vm_timings']), set(load(ROLE / 'defaults/main.yml')['storage_vm_timings']))



if __name__ == '__main__':
    unittest.main()

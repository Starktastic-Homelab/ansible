"""Offline checks for the storage VM role, storage.yml, storage-expiry.yml and the lab guard (ADR 0017)."""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / 'roles/storage_vm'
sys.path.insert(0, str(ROLE / 'files'))
from homelab_storage import config, lio  # noqa: E402

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
    "After=nftables.service\nWants=" + DEV_UNIT + "\nAfter=" + DEV_UNIT + "\n\n[Service]\n"
    "# A failed start or a stop never signals an abandoned (D-state) LVM child.\nKillMode=process\n"
    "ExecStartPre=\n"
    "ExecStartPre=/sbin/vgchange --devices /dev/disk/by-path/pci-0000:06:0b.0 -ay retained\n"
    "ExecStartPre=-/usr/local/sbin/storage-reconcile activate --vg regen --config /etc/homelab-storage/config.json\n"
    "ExecStartPre=-/usr/local/sbin/storage-fencer restore-set\n"
    "ExecStartPost=/usr/local/sbin/storage-fencer restore-set\n"
    "ExecStartPost=/usr/local/sbin/storage-reconcile boot-check --config /etc/homelab-storage/config.json\n")


def load(path):
    return DataLoader().load_from_file(str(path), trusted_as_template=True)


def plays(name):
    return load(ROOT / name)


def role_tasks(name='main'):
    return load(ROLE / 'tasks' / f'{name}.yml')


def defaults(**values):
    variables = load(ROLE / 'defaults/main.yml')
    variables.update({'vip_address': '10.9.9.99', 'argocd_repo_url': 'https://example.invalid/apps.git',
                      'ansible_managed': 'managed', 'storage_vm_workers': WORKERS, 'storage_vm_bridge_address': BRIDGE,
                      'storage_vm_ssh_sources': ['10.9.9.0/24', '10.9.8.0/24'], 'storage_vm_bridge_iface': 'eth1',
                      'storage_vm_fenced_addresses': []})
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
    files = [ROOT / 'storage.yml', ROOT / 'storage-expiry.yml', ROOT / 'tasks/lab-guard.yml',
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

    def guard_passes(self, inventory_file, vip, masters):
        t = self.guard()
        hostvars = {h: {'ansible_host': a} for h, a in masters.items()}
        variables = {'inventory_file': inventory_file, 'vip_address': vip, 'hostvars': hostvars,
                     'groups': {'masters': list(masters), 'all': list(masters)}}
        templar = Templar(variables=variables)
        if not templar.evaluate_conditional(t['when']):
            return None  # skipped
        return all(templar.evaluate_expression(e) for e in module(t, 'assert')['that'])

    def test_lab_guard(self):
        for name in ('k3s.yml', 'storage.yml', 'storage-expiry.yml'):
            first = plays(name)[0]
            self.assertEqual(first['name'], 'Lab guard', name)
            self.assertEqual((first['hosts'], first['gather_facts'], first['any_errors_fatal'], first['tags']),
                             ('all', False, True, ['always']), name)
            self.assertEqual(first['tasks'], [{'name': 'Refuse a lab run aimed at production',
                                               'ansible.builtin.import_tasks': 'tasks/lab-guard.yml', 'run_once': True}])
        lab = '/maintenance/lab-storage/inventory.yml'
        self.assertFalse(self.guard_passes(lab, '10.9.9.99', {'lab-server': '10.9.9.99'}))
        self.assertFalse(self.guard_passes(lab, '10.9.9.180', {'lab-server': '10.9.9.181'}))
        self.assertTrue(self.guard_passes(lab, '10.9.9.181', {'lab-server': '10.9.9.181'}))
        self.assertIsNone(self.guard_passes('/work/ansible/inventory/proxmox.yml', '10.9.9.99', {'m': '10.9.9.11'}))


class RoleTests(unittest.TestCase):
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

    def test_no_restart_of_target_or_nftables(self):
        for t in all_tasks():
            for key in ('ansible.builtin.systemd', 'ansible.builtin.systemd_service', 'ansible.builtin.service'):
                if key in t and t[key].get('name') in ('rtslib-fb-targetctl', 'nftables'):
                    self.assertNotIn(t[key].get('state'), ('restarted', 'stopped'), t['name'])
            text = ' '.join(argv(t)) + str(module(t, 'shell') or '')
            self.assertNotRegex(text, r'systemctl (restart|stop|try-restart)|nft -f|targetctl (clear|restore)', t.get('name'))
        handlers = load(ROLE / 'handlers/main.yml')
        self.assertEqual([h['name'] for h in handlers], ['Reload systemd', 'Reload nftables', 'Restart storage-fencer'])
        self.assertEqual(module(handlers[1], 'systemd'), {'name': 'nftables', 'state': 'reloaded'})
        self.assertEqual(module(handlers[2], 'systemd'), {'name': 'storage-fencer', 'state': 'restarted'})
        restarts = [t for t in all_tasks() if 'restarted' in str(t)]
        self.assertEqual([t['name'] for t in restarts], ['Restart storage-fencer'])

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
                'ExecStartPre=-/usr/local/sbin/storage-reconcile activate --vg regen ',
                'ExecStartPre=-/usr/local/sbin/storage-fencer restore-set',
                'ExecStartPost=/usr/local/sbin/storage-fencer restore-set',
                'ExecStartPost=/usr/local/sbin/storage-reconcile boot-check ']
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
        self.assertEqual((cfg['portal'], cfg['api_url'], cfg['csi_driver'], cfg['max_removals'], cfg['pause_after']),
                         (f'{BRIDGE}:3260', 'https://10.9.9.99:6443', 'org.democratic-csi.node-manual', 2, []))
        self.assertEqual((cfg['not_ready_seconds'], cfg['loop_seconds'], cfg['proof_timeout_seconds'],
                          cfg['quarantine_seconds'], cfg['blind_after_seconds']), (300, 15, 60, 259200, 600))
        with tempfile.TemporaryDirectory() as tmp:  # a one-off -e storage_vm_max_removals=3 arrives as a string
            path = Path(tmp) / 'config.json'
            path.write_text(render('config.json.j2', storage_vm_max_removals='3',
                                   storage_vm_workers=[dict(w, extra='x') for w in WORKERS]))
            cfg = config.load(path)
        self.assertEqual((cfg['max_removals'], cfg['workers']), (3, WORKERS))

    def test_config_template_pv_devices(self):
        pv = {'retained': '/dev/disk/by-path/a', 'regen': '/dev/disk/by-path/b'}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            path.write_text(render('config.json.j2', storage_vm_pv_devices=pv))
            self.assertEqual(config.load(path)['pv_devices'], pv)

    def test_assert_pv_devices(self):
        cond = next(t for t in role_tasks('assert') if 'storage_vm_pv_devices' in json.dumps(t))
        that = module(cond, 'assert')['that']
        good = {'retained': '/dev/disk/by-path/a', 'regen': '/dev/disk/by-path/b'}
        self.assertTrue(holds(that, storage_vm_pv_devices=good))
        for bad in ({'retained': '/dev/a'}, {**good, 'x': '/dev/c'}, {**good, 'regen': 'b'},
                    {'retained': '/dev/a', 'regen': '/dev/a'}, {**good, 'regen': 5}):
            self.assertFalse(holds(that, storage_vm_pv_devices=bad), bad)

    def test_worker_iqn_matches_k3s_common(self):
        self.assertEqual(lio.worker_iqn('k3s-worker-01'), 'iqn.2026-11.net.starktastic:k3s-worker-01')
        tasks = [t for t in load(ROOT / 'roles/k3s_common/tasks/main.yml')
                 if t.get('ansible.builtin.copy', {}).get('dest') == '/etc/iscsi/initiatorname.iscsi']
        self.assertEqual(len(tasks), 1)
        got = Templar(variables={'inventory_hostname': 'k3s-worker-01'}).template(
            tasks[0]['ansible.builtin.copy']['content'])
        self.assertEqual(got, 'InitiatorName=' + lio.worker_iqn('k3s-worker-01') + '\n')


class StoragePlaybookTests(unittest.TestCase):
    def test_play_order(self):
        ps = plays('storage.yml')
        self.assertEqual([p['hosts'] for p in ps], ['all', 'localhost', 'masters[0]', 'storage', 'storage', 'all_k3s', 'localhost'])
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
        cleanup = plays('storage.yml')[-1]['tasks']
        self.assertIn('ansible.builtin.file', cleanup[0])
        fail = cleanup[-1]
        self.assertIn('ansible.builtin.fail', fail)
        for rc, failed in ((0, False), (1, True)):
            self.assertIs(holds([fail['when']], **fail['vars'], groups={'storage': ['s']},
                                hostvars={'s': {'storage_vm_apply': {'rc': rc, 'stderr': ''}}}), failed)

    def test_growth_play_is_level_triggered(self):
        play = plays('storage.yml')[5]
        self.assertIn('grow.yml', str(play))
        include = by_name(role_tasks('grow'), 'Grow each attached exported volume')
        self.assertIn('storage_vm_exported', include['loop'])
        self.assertNotRegex(include['loop'], r'extend|actions')
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
                                'storage_vm_volume': {'id': 'paperless', 'size_bytes': 1}, 'storage_vm_session_lines': [line]})
        templar = Templar(variables=variables)
        self.assertEqual(templar.template(trust_as_template('{{ storage_vm_device }}')),
                         f'/dev/disk/by-path/ip-{BRIDGE}:3260-iscsi-iqn.2026-11.net.starktastic.storage:paperless-lun-0')
        self.assertEqual(str(templar.template(trust_as_template('{{ storage_vm_sid }}'))), '7')
        find = module(by_name(role_tasks('grow-volume'), 'Find the session of this target'), 'set_fact')['storage_vm_session_lines']
        other = f'tcp: [8] {BRIDGE}:3260,1 iqn.2026-11.net.starktastic.storage:paperless-old (non-flash)'
        got = Templar(variables={**variables, 'storage_vm_sessions': {'stdout_lines': [other, line]}}).template(find)
        self.assertEqual(list(got), [line])

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


class ExpiryPlaybookTests(unittest.TestCase):
    def test_expiry_verifies_lock_before_delete(self):
        ps = plays('storage-expiry.yml')
        self.assertEqual([p['name'] for p in ps], ['Lab guard', 'Verify maintenance ownership',
                                                    'Fetch and validate the inventory at the branch tip',
                                                    'Write a private kubeconfig', 'List expiry candidates',
                                                    'Expire removed volumes', 'Remove the temporary files'])
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
        cleanup = plays('storage-expiry.yml')[-1]['tasks']
        self.assertEqual(cleanup[0]['loop'], ["{{ hostvars[groups['masters'][0]].storage_vm_kubeconfig_path }}",
                                              '{{ storage_vm_checkout.path }}'])
        fail = cleanup[-1]
        self.assertIn('ansible.builtin.fail', fail)
        self.assertTrue(holds([fail['when']], storage_vm_expiry_refused=['a']))
        self.assertFalse(holds([fail['when']]))



class BootUnitTests(unittest.TestCase):
    def test_targetctl_dropin(self):
        self.assertEqual(render('rtslib-fb-targetctl.service.d-50-homelab.conf.j2'), TARGETCTL_DROPIN)

    def test_guest_panics_on_oops(self):
        t = by_name(role_tasks(), 'Reboot 10 s after a kernel oops')
        self.assertEqual(module(t, 'sysctl') or t.get('ansible.posix.sysctl'), {
            'name': '{{ item.key }}', 'value': '{{ item.value }}', 'sysctl_file': '/etc/sysctl.d/90-panic-reboot.conf',
            'sysctl_set': True, 'state': 'present', 'reload': True})
        self.assertEqual(t['loop'], [{'key': 'kernel.panic_on_oops', 'value': '1'}, {'key': 'kernel.panic', 'value': '10'}])
        self.assertNotIn('when', t)


class WorkflowTests(unittest.TestCase):
    def test_validate_runs_this_test_and_syntax_checks_playbooks(self):
        text = (ROOT / '.github/workflows/validate.yml').read_text()
        self.assertIn('run: python3 scripts/tests/test_storage_vm_role.py', text)
        syntax = next(line for line in text.splitlines() if '--syntax-check k3s.yml' in line)
        self.assertTrue(syntax.rstrip().endswith('storage.yml storage-expiry.yml 2>&1)'), syntax)


class LabExampleTests(unittest.TestCase):
    def test_examples_hosts_and_groups_only(self):
        inv = load(ROOT / 'lab/inventory.example.yml')
        self.assertEqual(set(inv['all']['children']), {'storage', 'all_k3s'})
        self.assertEqual(set(inv['all']['children']['all_k3s']['children']), {'masters', 'workers'})
        self.assertNotIn('vars', str(inv))
        lab_vars = load(ROOT / 'lab/vars.example.yml')
        self.assertNotEqual(lab_vars['vip_address'], '10.9.9.99')
        self.assertTrue(lab_vars['storage_vm_ssh_sources'])

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

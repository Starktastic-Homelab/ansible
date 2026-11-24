"""Offline checks for the pve timers that dispatch workflows (the storage backstop, D29 reopen)."""
from pathlib import Path
import re
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / 'roles/proxmox_workflow_dispatch'
CONF = '/etc/homelab-dispatch'
HEADER = f'{CONF}/github.header'
SCRIPT = '/usr/local/sbin/homelab-dispatch'
MARK = '# Managed by ansible (proxmox_workflow_dispatch).'
CHANGES = ('ansible.builtin.copy', 'ansible.builtin.template', 'ansible.builtin.file', 'ansible.builtin.systemd',
           'ansible.builtin.set_fact')


def tasks():
    return yaml.safe_load((ROLE / 'tasks/main.yml').read_text())


def template(name):
    return (ROLE / 'templates' / name).read_text()


def module(task):
    return next(k for k in task if k.startswith('ansible.builtin.'))


def index_of(predicate):
    return next(i for i, t in enumerate(tasks()) if predicate(t))


def by_dest(dest):
    return next(t for t in tasks() if t.get(module(t), {}).get('dest') == dest)


class WorkflowDispatchTests(unittest.TestCase):
    def test_defaults_and_group_vars(self):
        self.assertEqual(yaml.safe_load((ROLE / 'defaults/main.yml').read_text()), {
            'proxmox_workflow_dispatch_repo': 'Starktastic-Homelab/ansible',
            'proxmox_workflow_dispatch_api': 'https://api.github.com',
            'proxmox_workflow_dispatch_workflows': {}})
        self.assertEqual(yaml.safe_load((ROOT / 'group_vars/proxmox_hosts/workflow_dispatch.yml').read_text()), {
            'proxmox_workflow_dispatch_workflows': {
                'storage.yml': {'on_calendar': '*-*-* *:05:00 UTC', 'inputs': {'backstop': 'true'}},
                'storage-expiry.yml': {'on_calendar': '*-*-* 10:05:00 UTC', 'inputs': {'backstop': 'true'}}},
            'proxmox_workflow_dispatch_token': '{{ vault_github_dispatch_token }}'})

    def test_asserts_first(self):
        t = tasks()
        first = t[0]
        text = ' '.join(first['ansible.builtin.assert']['that'])
        for needle in (r'^[a-z0-9][a-z0-9._-]*\.ya?ml$', "difference(['backstop'])", '\n',
                       '^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$', '^https://'):
            self.assertIn(needle, text)
        self.assertIn('fail_msg', first['ansible.builtin.assert'])
        self.assertEqual(first['loop'], '{{ proxmox_workflow_dispatch_workflows | dict2items }}')
        token = next(x for x in t if 'ansible.builtin.assert' in x and 'proxmox_workflow_dispatch_token' in
                     ' '.join(x['ansible.builtin.assert']['that']))
        self.assertIs(token['no_log'], True)
        calendar = index_of(lambda x: x.get('ansible.builtin.command', {}).get('argv', [])[:2] ==
                            ['systemd-analyze', 'calendar'])
        self.assertIs(t[calendar]['changed_when'], False)
        first_change = index_of(lambda x: module(x) in CHANGES)
        self.assertLess(t.index(token), first_change)
        self.assertLess(calendar, first_change)

    def test_allowlist_excludes_approvals(self):
        text = ' '.join(tasks()[0]['ansible.builtin.assert']['that'])
        allowlist = re.search(r"difference\((\[[^]]*\])\)", text).group(1)
        self.assertEqual(yaml.safe_load(allowlist), ['backstop'])
        for name in ('held_lock_owner', 'remove_workers', 'max_removals'):
            self.assertNotIn(name, text)

    def test_secret_handling(self):
        header = by_dest(HEADER)
        self.assertIs(header['no_log'], True)
        copy = header['ansible.builtin.copy']
        self.assertEqual((copy['mode'], copy['owner'], copy['group']), ('0600', 'root', 'root'))
        self.assertIn('proxmox_workflow_dispatch_token', copy['content'])
        conf = next(t for t in tasks() if t.get('ansible.builtin.file', {}).get('path') == CONF)
        self.assertEqual((conf['ansible.builtin.file']['state'], conf['ansible.builtin.file']['mode']),
                         ('directory', '0700'))
        for path in (ROLE / 'templates').iterdir():
            self.assertNotIn('token', path.read_text(), path.name)
        self.assertIn(f'-H @{HEADER}', template('homelab-dispatch.sh.j2'))
        # Only the header task and the token assert see the token.
        for t in tasks():
            if 'proxmox_workflow_dispatch_token' in yaml.safe_dump(t):
                self.assertIs(t.get('no_log'), True, t['name'])

    def test_units(self):
        svc, tmr = template('homelab-dispatch@.service.j2'), template('homelab-dispatch.timer.j2')
        self.assertIn('Type=oneshot', svc.splitlines())
        self.assertIn(f'ExecStart={SCRIPT} %i', svc.splitlines())
        for line in ('OnCalendar={{ item.value.on_calendar }}', 'Persistent=false', 'WantedBy=timers.target'):
            self.assertIn(line, tmr.splitlines())
        for text in (svc, tmr):
            self.assertTrue(text.startswith(MARK))
        for path in ROLE.rglob('*'):
            if path.is_file():
                self.assertNotIn('%I', path.read_text(), path.name)  # %I turns '-' into '/'
        self.assertEqual(by_dest('/etc/systemd/system/homelab-dispatch@.service')['ansible.builtin.template']['src'],
                         'homelab-dispatch@.service.j2')
        timer = by_dest('/etc/systemd/system/homelab-dispatch@{{ item.key }}.timer')
        self.assertEqual(timer['loop'], '{{ proxmox_workflow_dispatch_workflows | dict2items }}')

    def test_script(self):
        sh = template('homelab-dispatch.sh.j2')
        self.assertTrue(sh.startswith('#!/bin/bash\n'))
        self.assertIn(MARK, sh.splitlines()[1])
        self.assertIn('set -euo pipefail', sh)
        self.assertIn('curl -fsS --max-time 30', sh)
        self.assertNotIn('--retry', sh)
        self.assertIn(f'--data @{CONF}/"$workflow".json', sh)
        self.assertIn("/actions/workflows/'\"$workflow\"'/dispatches", sh)
        self.assertIn(r'[[ $workflow =~ ^[a-z0-9][a-z0-9._-]*\.ya?ml$ ]]', sh)
        task = by_dest(SCRIPT)['ansible.builtin.template']
        self.assertEqual((task['src'], task['mode']), ('homelab-dispatch.sh.j2', '0755'))

    def test_body_files(self):
        body = by_dest(f'{CONF}/{{{{ item.key }}}}.json')
        self.assertEqual(body['loop'], '{{ proxmox_workflow_dispatch_workflows | dict2items }}')
        content = body['ansible.builtin.copy']['content']
        self.assertIn("'ref': 'main'", content)
        self.assertIn("'inputs': item.value.inputs | default({})", content)
        self.assertIn('to_json', content)

    def test_stale_timers_removed(self):
        t = tasks()
        find = index_of(lambda x: 'ansible.builtin.find' in x)
        self.assertEqual((t[find]['ansible.builtin.find']['paths'], t[find]['ansible.builtin.find']['patterns']),
                         ('/etc/systemd/system', 'homelab-dispatch@*.timer'))
        stale = index_of(lambda x: 'ansible.builtin.set_fact' in x)
        self.assertIn('difference(proxmox_workflow_dispatch_workflows.keys()',
                      t[stale]['ansible.builtin.set_fact']['proxmox_workflow_dispatch_stale'])
        stop = index_of(lambda x: x.get('ansible.builtin.systemd', {}).get('enabled') is False)
        self.assertEqual(t[stop]['ansible.builtin.systemd']['state'], 'stopped')
        removed = [x['ansible.builtin.file']['path'] for x in t
                   if x.get('ansible.builtin.file', {}).get('state') == 'absent']
        self.assertEqual(sorted(removed), [f'{CONF}/{{{{ item }}}}.json',
                                           '/etc/systemd/system/homelab-dispatch@{{ item }}.timer'])
        for x in t:
            if x.get('ansible.builtin.file', {}).get('state') == 'absent' or x is t[stop]:
                self.assertEqual(x['loop'], '{{ proxmox_workflow_dispatch_stale }}')
        self.assertLess(find, stale)
        self.assertLess(stale, stop)
        self.assertLess(stop, len(t) - 1)

    def test_enable_last(self):
        last = tasks()[-1]
        self.assertEqual(last['ansible.builtin.systemd'], {'name': 'homelab-dispatch@{{ item.key }}.timer',
                                                          'enabled': True, 'state': 'started', 'daemon_reload': True})
        self.assertEqual(last['loop'], '{{ proxmox_workflow_dispatch_workflows | dict2items }}')

    def test_registers_use_role_prefix(self):
        for t in tasks():
            if 'register' in t:
                self.assertTrue(t['register'].startswith('proxmox_workflow_dispatch_'), t['register'])

    def test_no_workflow_names_in_role(self):
        text = ''.join(p.read_text() for p in ROLE.rglob('*') if p.is_file())
        self.assertNotIn('storage', text)
        self.assertNotIn('ansible.builtin.shell', text)

    def test_host_only(self):
        plays = yaml.safe_load((ROOT / 'proxmox-workflow-dispatch.yml').read_text())
        self.assertEqual([(p['hosts'], p['become'], p['gather_facts'], [r['role'] for r in p['roles']]) for p in plays],
                         [('proxmox_hosts', True, False, ['proxmox_workflow_dispatch'])])
        self.assertNotIn('proxmox_workflow_dispatch', (ROOT / 'k3s.yml').read_text())

    def test_refuses_token_before_backup_exclude_is_live(self):
        # The exclude only takes effect when proxmox_backup re-renders excludes.txt; until then a daily backup would
        # upload the header. Refuse before writing it while the live excludes file lacks the path.
        t = tasks()
        excludes = '/etc/homelab-backup.d/excludes.txt'
        stat = index_of(lambda x: x.get('ansible.builtin.stat', {}).get('path') == excludes)
        read = index_of(lambda x: x.get('ansible.builtin.slurp', {}).get('src') == excludes)
        guard = index_of(lambda x: 'ansible.builtin.assert' in x and
                         CONF in ' '.join(x['ansible.builtin.assert']['that']))
        self.assertIn('stat.exists', t[read]['when'])
        self.assertIn('stat.exists', t[guard]['when'])
        self.assertIn('b64decode', ' '.join(t[guard]['ansible.builtin.assert']['that']))
        self.assertIn('proxmox-backup.yml', t[guard]['ansible.builtin.assert']['fail_msg'])
        header = t.index(by_dest(HEADER))
        self.assertLess(stat, read)
        self.assertLess(read, guard)
        self.assertLess(guard, header)

    def test_vaulted_files_are_prettier_ignored(self):
        # format.yml rewrites every *.yml on a PR branch; Prettier folding ciphertext lines breaks a vault file.
        import subprocess
        tracked = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT, capture_output=True, check=True).stdout
        vaulted = []
        for name in tracked.decode().split('\0'):
            path = ROOT / name
            if name and path.is_file() and not path.is_symlink():
                with open(path, 'rb') as stream:
                    if stream.read(14) == b'$ANSIBLE_VAULT':  # the header only, never the ciphertext
                        vaulted.append(name)
        self.assertIn('group_vars/proxmox_hosts/vault_workflow_dispatch.yml', vaulted)
        ignored = (ROOT / '.prettierignore').read_text().splitlines()
        for name in vaulted:
            self.assertIn(name, ignored)

    def test_backup_excludes_dispatch(self):
        # The token is restorable from the vault, so it never goes into the off-site snapshots.
        # The defaults use !unsafe, which safe_load cannot read: check the list line in the excludes block.
        text = (ROOT / 'roles/proxmox_backup/defaults/main.yml').read_text()
        block = text.split('proxmox_backup_excludes:\n', 1)[1].split('\n\n', 1)[0]
        self.assertIn(f'  - {CONF}', block.splitlines())

    def test_workflow_and_deploy_ignore(self):
        deploy_ignored = yaml.safe_load((ROOT / '.github/workflows/deploy.yml').read_text())[True]['push']['paths-ignore']
        for path in ('roles/proxmox_workflow_dispatch/**', 'proxmox-workflow-dispatch.yml',
                     '.github/workflows/proxmox-workflow-dispatch.yml', 'scripts/tests/test_proxmox_workflow_dispatch.py',
                     'group_vars/proxmox_hosts/**'):
            self.assertIn(path, deploy_ignored)
        own = yaml.safe_load((ROOT / '.github/workflows/proxmox-workflow-dispatch.yml').read_text())
        self.assertEqual(own[True]['push']['branches'], ['main'])
        self.assertIn('workflow_dispatch', own[True])
        self.assertEqual(sorted(own[True]['push']['paths']), [
            'group_vars/proxmox_hosts/vault_workflow_dispatch.yml', 'group_vars/proxmox_hosts/workflow_dispatch.yml',
            'proxmox-workflow-dispatch.yml', 'roles/proxmox_workflow_dispatch/**'])
        (job,) = own['jobs'].values()
        self.assertEqual(job['runs-on'], 'self-hosted')
        run = ' '.join(s.get('run', '') for s in job['steps'])
        self.assertIn('-i inventory/hosts.yml', run)
        self.assertIn('proxmox-workflow-dispatch.yml', run)
        last = job['steps'][-1]
        self.assertEqual((last['if'], last['run'].strip()), ('always()', 'rm -f .vault_pass'))
        syntax = next(line for line in (ROOT / '.github/workflows/validate.yml').read_text().splitlines()
                      if line.strip().startswith('--syntax-check'))
        self.assertIn('proxmox-workflow-dispatch.yml', syntax.split())


if __name__ == '__main__':
    unittest.main()

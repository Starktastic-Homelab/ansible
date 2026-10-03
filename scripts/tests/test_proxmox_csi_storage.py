"""Offline native-Ansible refusal checks; no host connection or setup execution."""
import json
import shlex
from pathlib import Path
import unittest
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

ROOT = Path(__file__).resolve().parents[2]

def read(path):
    return DataLoader().load_from_file(str(ROOT / path), trusted_as_template=True)

class SetupTests(unittest.TestCase):
    def tasks(self):
        path = ROOT / 'roles/proxmox_csi_storage/tasks/main.yml'
        self.assertTrue(path.is_file(), 'Missing manual shared setup role')
        return read(str(path))

    def task(self, name):
        return next(t for t in self.tasks() if t['name'] == name)

    def allowed(self, name, **values):
        variables = read('roles/proxmox_csi_storage/defaults/main.yml')
        variables.update(values)
        templar = Templar(variables=variables)
        return all(templar.evaluate_expression(expr) for expr in self.task(name)['ansible.builtin.assert']['that'])

    def test_setup_requires_valid_identities_and_unused_integer_owner(self):
        settings=dict(groups={'proxmox_hosts':['pve']}, inventory_hostname='pve', ansible_host='192.0.2.20',
            proxmox_csi_storage_id='k3s-block', proxmox_csi_storage_pool='k3s-csi',
            proxmox_csi_storage_nfs_server='192.0.2.10', proxmox_csi_storage_nfs_export='/mnt/apps/block',
            proxmox_csi_storage_nodes=['pve'], proxmox_csi_storage_owner_id=9999,
            proxmox_csi_storage_secret_destination='/maintenance/private/proxmox-csi.json',
            proxmox_csi_storage_api_url='https://192.0.2.20:8006/api2/json')
        self.assertTrue(self.allowed('Require reviewed shared storage identities', **settings))
        for field,value in [('proxmox_csi_storage_owner_id','9999'),('proxmox_csi_storage_owner_id',9999.5),
                            ('proxmox_csi_storage_id',''),('proxmox_csi_storage_secret_destination','/tmp/token'),
                            ('proxmox_csi_storage_nfs_export','/mnt/a b'),('proxmox_csi_storage_nodes',['other'])]:
            with self.subTest(field=field,value=value):
                self.assertFalse(self.allowed('Require reviewed shared storage identities', **dict(settings, **{field:value})))
        for owner, expected in [(9999,True),(200,False)]:
            self.assertEqual(self.allowed('Refuse an owner ID used by a VM or container',
                proxmox_csi_storage_owner_id=owner, proxmox_csi_storage_vms={'stdout':'[{"vmid":200}]'}),expected)

    def test_existing_storage_cannot_be_repointed(self):
        expected = dict(storage='k3s-block', type='nfs', server='192.0.2.10', export='/mnt/apps/block',
                        content='images', nodes='pve', options='vers=4.2')
        settings = dict(proxmox_csi_storage_id='k3s-block', proxmox_csi_storage_nfs_server='192.0.2.10',
                        proxmox_csi_storage_nfs_export='/mnt/apps/block', proxmox_csi_storage_nodes=['pve'])
        self.assertTrue(self.allowed('Refuse mismatched existing storage', item=expected, **settings))
        for field, value in [('type','dir'), ('server','192.0.2.11'), ('export','/unrelated'),
                             ('content','images,backup'), ('nodes','pve,other'), ('options','soft'), ('disable',1)]:
            with self.subTest(field=field):
                self.assertFalse(self.allowed('Refuse mismatched existing storage', item=dict(expected, **{field:value}), **settings))

    def test_only_exact_runtime_privileges_and_grants(self):
        privileges='Datastore.Allocate Datastore.AllocateSpace Datastore.Audit VM.Audit VM.Config.Disk'
        for extra, expected in [('', True), (' VM.PowerMgmt', False), (' VM.Allocate', False)]:
            self.assertEqual(self.allowed('Refuse a changed runtime role', item={'privs':privileges+extra}), expected)
        settings=dict(proxmox_csi_storage_pool='k3s-csi', proxmox_csi_storage_owner_id=9999, proxmox_csi_storage_id='k3s-block')
        for path, role, propagate, expected in [('/pool/k3s-csi','HomelabCSI',1,True),('/storage/k3s-block','HomelabCSI',0,True),
                                               ('/vms/9999','HomelabCSI',0,True),('/','HomelabCSI',1,False),
                                               ('/vms/201','HomelabCSI',0,False),('/pool/k3s-csi','Administrator',1,False)]:
            self.assertEqual(self.allowed('Refuse broader existing grants', item=dict(path=path,roleid=role,propagate=propagate), **settings), expected)

    def test_token_secret_mismatch_never_regenerates(self):
        for exists, token_exists, symlink, mode, expected in [(False,False,False,'',True),(True,True,False,'0600',True),
                (False,True,False,'',False),(True,False,False,'0600',False),(True,True,True,'0600',False),
                (True,True,False,'0644',False)]:
            with self.subTest(exists=exists, token_exists=token_exists, symlink=symlink, mode=mode):
                self.assertEqual(self.allowed('Refuse implicit token recovery',
                    proxmox_csi_storage_token_exists=token_exists,
                    proxmox_csi_storage_secret_stat={'stat':dict(exists=exists,islnk=symlink,mode=mode,isreg=not symlink)}), expected)

    def test_existing_pool_is_not_adopted_with_another_owner_or_storage(self):
        comment='Retained CSI infrastructure; image owner 9999 is reserved and must never be a VM'
        for data, expected in [({'comment':comment,'members':[]},True),
                               ({'comment':'unrelated','members':[]},False),
                               ({'comment':comment,'members':[{'type':'storage','storage':'vm-pool'}]},False)]:
            self.assertEqual(self.allowed('Refuse an unrelated existing pool',
                proxmox_csi_storage_owner_id=9999, proxmox_csi_storage_pool_detail={'stdout':json.dumps(data)}),expected)

    def test_group_membership_is_requested_and_refused_in_both_roles(self):
        for role, refusal in [('proxmox_csi_storage','Refuse inherited group privileges or a disabled principal'),
                              ('storage_fencing','Reject group grants or a disabled existing principal')]:
            tasks=read('roles/'+role+'/tasks/main.yml')
            with self.subTest(role=role):
                command=next(t['ansible.builtin.command'] for t in tasks
                             if str(t.get('ansible.builtin.command','')).startswith('pveum user list'))
                # PVE's native user-list API defaults full=0, omitting groups.
                self.assertIn('--full', shlex.split(command))
                self.assertEqual(shlex.split(command)[shlex.split(command).index('--full')+1], '1')
                expressions=next(t for t in tasks if t['name']==refusal)['ansible.builtin.assert']['that']
                for groups, allowed in [([],True),(['administrators'],False),('',True),('administrators',False)]:
                    templar=Templar(variables={'item':dict(groups=groups,enable=1,expire=0)})
                    self.assertEqual(all(templar.evaluate_expression(expr) for expr in expressions),allowed)

    def test_setup_is_manual_and_ownership_precedes_writes(self):
        tasks=self.tasks()
        self.assertIn('maintenance_lock.py',str(tasks[0]))
        play=read('proxmox-csi-storage.yml')[0]
        self.assertEqual(play['hosts'],'proxmox_hosts')
        self.assertNotIn('proxmox_csi_storage',str(read('k3s.yml')))
        for task in tasks:
            if task['name'] in ['Create separated runtime token once','Persist token outside the disposable cluster']:
                self.assertIs(task['no_log'],True)
                self.assertIn('not proxmox_csi_storage_token_exists',task['when'])
        write=self.task('Register dedicated NAS NFS storage')
        self.assertLess(tasks.index(self.task('Refuse implicit token recovery')),tasks.index(write))
        self.assertFalse(self.task('Persist token outside the disposable cluster')['ansible.builtin.copy']['force'])

if __name__ == '__main__':unittest.main()

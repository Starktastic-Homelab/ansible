"""Offline evidence checks; no cluster, guest, credentials or host deployment."""
import base64
import copy
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
OLD = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
NEW = '11111111-2222-4333-8444-555555555555'
UID = '99999999-8888-4777-8666-555555555555'


class RetirementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = ROOT / 'filter_plugins/k3s_node_retirement.py'
        if path.exists():
            spec = importlib.util.spec_from_file_location('retirement', path)
            cls.mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.mod)
        else:
            cls.mod = None

    def setUp(self):
        self.assertIsNotNone(self.mod, 'Verified retirement filters are missing')
        self.node = {'metadata': {'name': 'worker', 'uid': UID},
                     'status': {'nodeInfo': {'systemUUID': OLD}}}
        self.expected = {'name': 'worker', 'vmid': 982, 'node': 'pve', 'uuid': NEW}
        self.permissions = {'/vms': {'VM.Audit': 1}, '/pool': {'Pool.Audit': 1},
                            '/access': {'Sys.Audit': 0}}
        self.resources = [{'type': 'qemu', 'vmid': 982, 'node': 'pve', 'name': 'worker', 'status': 'running'}]
        self.configs = [{'item': self.resources[0], 'json': {'data': {'name': 'worker', 'smbios1': 'uuid=' + NEW}}}]
        self.evidence = dict(permissions_before=self.permissions, permissions_after=copy.deepcopy(self.permissions),
                             acls_before=[], acls_after=[], resources_before=self.resources,
                             resources_after=copy.deepcopy(self.resources), configs=self.configs)

    def candidate(self):
        return self.mod.candidate([self.node], 'worker', NEW)

    def verify(self):
        return self.mod.verified(self.candidate(), self.expected, self.evidence)

    def test_native_templates_pass_evidence_and_preserve_the_exact_delete_uid(self):
        from ansible.parsing.dataloader import DataLoader
        from ansible.plugins.loader import filter_loader, init_plugin_loader
        from ansible.template import Templar
        init_plugin_loader()
        filter_loader.add_directory(str(ROOT / 'filter_plugins'))
        tasks = DataLoader().load_from_file(str(ROOT / 'roles/k3s_node_retirement/tasks/main.yml'), trusted_as_template=True)
        block = next(t['block'] for t in tasks if 'block' in t)
        # Render the role's actual expected identity; Ansible tags the | int result.
        expected_template = next(t['vars']['k3s_node_retirement_expected'] for t in tasks if 'block' in t)
        expected = Templar(variables={'inventory_hostname': 'worker', 'proxmox_vmid': '982',
                                     'proxmox_node': 'pve', 'k3s_node_retirement_guest': {
                                         'content': base64.b64encode(NEW.encode()).decode()}}).template(expected_template)
        self.assertIsInstance(expected['vmid'], int)
        variables = {'k3s_node_retirement_candidate': self.candidate(), 'k3s_node_retirement_expected': expected,
                     'k3s_node_retirement_configs': {'results': self.configs}}
        for key in ('permissions_before', 'permissions_after', 'acls_before', 'acls_after', 'resources_before', 'resources_after'):
            variables['k3s_node_retirement_'+key] = {'json': {'data':self.evidence[key]}}
        fact = next(t['ansible.builtin.set_fact'] for t in block if 'ansible.builtin.set_fact' in t)
        variables.update(Templar(variables=variables).template(fact))
        deletion = next(t['kubernetes.core.k8s'] for t in block if 'kubernetes.core.k8s' in t)
        rendered = Templar(variables=variables).template(deletion)
        self.assertEqual(rendered, {'state':'absent', 'api_version':'v1', 'kind':'Node', 'name':'worker',
                                    'delete_options':{'preconditions':{'uid':UID}}})

    def test_noninteger_expected_vmid_refuses(self):
        for vmid in [True, False, '982', 982.0, None]:
            with self.subTest(vmid=vmid), self.assertRaises(ValueError):
                self.mod.verified(self.candidate(), dict(self.expected, vmid=vmid), self.evidence)

    def test_absent_or_same_generation_needs_no_retirement(self):
        self.assertEqual(self.mod.candidate([], 'worker', NEW), {})
        self.node['status']['nodeInfo']['systemUUID'] = NEW.upper()
        self.assertEqual(self.mod.candidate([self.node], 'worker', NEW), {})

    def test_same_name_new_vm_generation_returns_exact_old_uid(self):
        self.assertEqual(self.verify(), {'name': 'worker', 'uid': UID})

    def test_missing_invalid_or_duplicate_node_identity_refuses(self):
        for nodes, guest in [([self.node, self.node], NEW), ([{}], NEW), ([self.node], ''),
                             ([self.node], '00000000-0000-0000-0000-000000000000')]:
            with self.subTest(nodes=nodes, guest=guest), self.assertRaises(ValueError):
                self.mod.candidate(nodes, 'worker', guest)
        self.node['metadata']['name'] = 'another-worker'
        with self.assertRaises(ValueError): self.candidate()

    def test_permission_loss_and_nonpropagating_global_visibility_refuse(self):
        for path, priv in [('/vms', 'VM.Audit'), ('/pool', 'Pool.Audit'), ('/access', 'Sys.Audit')]:
            evidence = copy.deepcopy(self.evidence)
            del evidence['permissions_before'][path][priv]
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.mod.verified(self.candidate(), self.expected, evidence)
        self.permissions['/vms']['VM.Audit'] = 0
        with self.assertRaises(ValueError): self.verify()

    def test_hidden_noaccess_child_and_pool_overrides_refuse(self):
        for path in ['/vms/201', '/pool/hidden']:
            evidence = copy.deepcopy(self.evidence)
            # Proxmox omits all-denied paths from the effective permissions map.
            evidence['acls_before'] = evidence['acls_after'] = [{'path': path}]
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.mod.verified(self.candidate(), self.expected, evidence)

    def test_explicit_vm_audit_and_inherited_pool_audit_are_valid(self):
        for permissions in [self.permissions, self.evidence['permissions_after']]:
            permissions.update({'/vms/201': {'VM.Audit': 0}, '/pool/other': {'Pool.Audit': 1}})
        self.evidence['acls_before'] = self.evidence['acls_after'] = [{'path': '/vms/201'}, {'path': '/pool/other'}]
        self.assertEqual(self.verify()['uid'], UID)

    def test_old_uuid_anywhere_even_stopped_template_refuses(self):
        for status, template in [('running', 0), ('stopped', 0), ('stopped', 1)]:
            evidence = copy.deepcopy(self.evidence)
            old = dict(type='qemu', vmid=777, node='other-host', name='old-copy', status=status, template=template)
            evidence['resources_before'].append(old)
            evidence['resources_after'].append(old)
            evidence['configs'].append({'item': old, 'json': {'data': {'name': 'old-copy', 'smbios1': 'uuid='+OLD.upper()}}})
            with self.subTest(status=status, template=template), self.assertRaises(ValueError):
                self.mod.verified(self.candidate(), self.expected, evidence)

    def test_partial_duplicate_malformed_or_locked_config_refuses(self):
        mutations = [lambda e: e.update(configs=[]), lambda e: e['configs'].append(copy.deepcopy(e['configs'][0])),
                     lambda e: e['configs'][0]['json']['data'].pop('smbios1'),
                     lambda e: e['configs'][0]['json']['data'].update(smbios1='uuid='+NEW+',uuid='+OLD),
                     lambda e: e['configs'][0]['json']['data'].update(lock='migrate')]
        for mutation in mutations:
            evidence=copy.deepcopy(self.evidence); mutation(evidence)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.mod.verified(self.candidate(), self.expected, evidence)

    def test_duplicate_vm_uuid_on_another_id_refuses(self):
        resource = dict(self.resources[0], vmid=983, name='other')
        self.resources.append(resource)
        self.evidence['resources_after'].append(resource)
        self.configs.append({'item': resource, 'json': {'data': {'name':'other', 'smbios1':'uuid='+NEW}}})
        with self.assertRaises(ValueError): self.verify()

    def test_malformed_permission_and_resource_shapes_refuse(self):
        for key, value in [('permissions_before', {'/vms': None}), ('acls_before', [None]),
                           ('resources_before', [None]), ('configs', None)]:
            evidence=dict(self.evidence, **{key:value})
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.mod.verified(self.candidate(), self.expected, evidence)

    def test_wrong_replacement_or_duplicate_uuid_refuses(self):
        for key, value in [('name', 'wrong'), ('vmid', 983), ('node', 'other'), ('uuid', OLD)]:
            expected = dict(self.expected, **{key:value})
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.mod.verified(self.candidate(), expected, self.evidence)

    def test_visibility_or_inventory_drift_refuses_but_metrics_drift_does_not(self):
        for key, value in [('permissions_after', {}), ('acls_after', [{'path':'/vms/999'}]),
                           ('resources_after', []), ('resources_after', [dict(self.resources[0], node='other')])]:
            evidence=dict(self.evidence, **{key:value})
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.mod.verified(self.candidate(), self.expected, evidence)
        self.evidence['resources_after'][0]['cpu'] = 0.1
        self.assertEqual(self.verify()['uid'], UID)


class NativeRoleTests(unittest.TestCase):
    def test_native_deletion_is_uid_conditional_and_before_worker_join(self):
        from ansible.parsing.dataloader import DataLoader
        loader = DataLoader()
        path = ROOT / 'roles/k3s_node_retirement/tasks/main.yml'
        self.assertTrue(path.exists(), 'Native retirement role is missing')
        tasks = loader.load_from_file(str(path))
        block = next(t['block'] for t in tasks if 'block' in t)
        deletion = next(t for t in block if t.get('kubernetes.core.k8s', {}).get('state') == 'absent')
        options = deletion['kubernetes.core.k8s']['delete_options']
        self.assertIn('uid', options['preconditions'])
        self.assertNotIn('force', deletion['kubernetes.core.k8s'])
        self.assertIn('maintenance_lock.py', str(block[block.index(deletion)-1]))
        reads = [t for t in block if 'ansible.builtin.uri' in t]
        self.assertGreaterEqual(len(reads), 7)
        self.assertTrue(all(t.get('no_log') for t in reads))
        defaults = next(t['module_defaults']['ansible.builtin.uri'] for t in tasks if 'module_defaults' in t)
        self.assertEqual(defaults['follow_redirects'], 'none')
        self.assertEqual(defaults['method'], 'GET')
        plays = loader.load_from_file(str(ROOT / 'k3s.yml'))
        retire = next(i for i,p in enumerate(plays) if any(
            isinstance(r,dict) and r.get('role') == 'k3s_node_retirement' for r in p.get('roles', [])))
        join = next(i for i,p in enumerate(plays) if p['name'] == 'Join workers')
        self.assertLess(retire, join)
        self.assertTrue(plays[retire]['any_errors_fatal'])
        self.assertEqual(plays[retire]['serial'], 1)
        settings = loader.load_from_file(str(ROOT / 'roles/k3s_node_retirement/defaults/main.yml'))
        self.assertIs(settings['k3s_node_retirement_enabled'], False)
        self.assertIs(settings['k3s_node_retirement_validate_certs'], True)


if __name__ == '__main__':
    unittest.main()

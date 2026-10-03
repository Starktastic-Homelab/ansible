"""Render recovery tasks offline; never connects to Kubernetes or any guest."""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar
from ansible.plugins.loader import init_plugin_loader

init_plugin_loader()

ROOT = Path(__file__).resolve().parents[2]


def read(path):
    return DataLoader().load_from_file(str(ROOT / path), trusted_as_template=True)


class RecoveryTests(unittest.TestCase):
    def play(self):
        plays = read('k3s.yml')
        matches = [p for p in plays if p['name'] == 'Recover nodes drained by Terraform']
        self.assertEqual(len(matches), 1, 'Missing post-install recovery play')
        return matches[0]

    def templar(self, payload='[]', **overrides):
        with patch.dict(os.environ, {'K3S_DRAIN_RECOVERY_NODES': payload}):
            variables = Templar().template(self.play()['vars'])
        variables.update(groups={'all_k3s': ['master', 'worker', 'held-worker', 'new-worker']})
        variables.update(overrides)
        return Templar(variables=variables)

    def test_recovery_follows_successful_installation_before_bootstrap(self):
        plays = read('k3s.yml')
        recovery = self.play()
        self.assertLess(next(i for i,p in enumerate(plays) if p['name'] == 'Join workers'), plays.index(recovery))
        self.assertLess(plays.index(recovery), next(i for i,p in enumerate(plays) if p['name'] == 'Bootstrap cluster'))
        for play in plays[:plays.index(recovery) + 1]:
            self.assertIs(play.get('any_errors_fatal'), True, play['name'])
        self.assertEqual(recovery['hosts'], 'localhost')

    def test_normal_deploy_and_old_dispatch_have_no_recovery(self):
        block = self.play()['tasks'][1]
        self.assertFalse(self.templar().evaluate_expression(block['when']))
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(Templar().template(self.play()['vars'])['k3s_drain_recovery_nodes'], [])

    def test_only_drained_nodes_still_in_inventory_are_uncordoned(self):
        block = self.play()['tasks'][1]
        templar = self.templar('["worker", "master", "removed-worker"]')
        self.assertTrue(templar.evaluate_expression(block['when']))
        wait, uncordon = block['block']
        self.assertEqual(templar.template(wait['loop']), ['master', 'worker', 'held-worker', 'new-worker'])
        self.assertEqual(set(templar.template(uncordon['loop'])), {'master', 'worker'})
        args = uncordon['kubernetes.core.k8s']
        self.assertEqual(args['state'], 'patched')
        self.assertEqual(args['kind'], 'Node')
        self.assertEqual(args['definition'], {'spec': {'unschedulable': False}})

    def test_wait_requires_registration_and_true_ready_condition(self):
        wait = self.play()['tasks'][1]['block'][0]
        cases = [([], False), ([{'status': {}}], False),
                 ([{'status': {'conditions': [{'type': 'Ready', 'status': 'False'}]}}], False),
                 ([{'status': {'conditions': [{'type': 'Ready', 'status': 'Unknown'}]}}], False),
                 ([{'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}}], True)]
        for resources, expected in cases:
            with self.subTest(resources=resources):
                templar = self.templar('["worker"]', k3s_drain_node={'resources': resources})
                self.assertEqual(templar.evaluate_expression(wait['until']), expected)

    def test_malformed_handoff_refused_before_scheduling_writes(self):
        assertion = self.play()['tasks'][0]['ansible.builtin.assert']['that']
        for payload, valid in [('[]', True), ('["worker"]', True), ('"worker"', False),
                               ('{}', False), ('null', False), ('true', False), ('[7]', False)]:
            with self.subTest(payload=payload):
                templar = self.templar(payload)
                self.assertEqual(all(templar.evaluate_expression(expr) for expr in assertion), valid)

    def test_workflow_only_accepts_payload_from_infrastructure_dispatch(self):
        job = read('.github/workflows/deploy.yml')['jobs']['provision']
        expr = job['env']['K3S_DRAIN_RECOVERY_NODES']
        # Evaluate this small GitHub expression against old and new dispatch payloads.
        for event in ['push', 'workflow_dispatch', 'repository_dispatch']:
            for action in ['', 'infrastructure-changed', 'other']:
                for nodes in [None, [], ['worker']]:
                    condition = str(expr).removeprefix('${{').removesuffix('}}').strip()
                    condition = condition.replace('&&', ' and ').replace('||', ' or ')
                    condition = condition.replace('github.event_name', repr(event)).replace('github.event.action', repr(action))
                    condition = condition.replace('github.event.client_payload.drained_nodes', repr(nodes))
                    result = eval(condition, {'__builtins__': {}, 'toJSON': json.dumps, 'fromJSON': json.loads}, {})
                    expected = nodes or [] if event == 'repository_dispatch' and action == 'infrastructure-changed' else []
                    self.assertEqual(json.loads(result), expected)


if __name__ == '__main__':
    unittest.main()

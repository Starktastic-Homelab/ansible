"""Offline checks for the fencer's pure decisions and its Kubernetes API client."""
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'roles/storage_vm/files'))
from homelab_storage import fence_policy, kube, ledger  # noqa: E402

NOW = datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone.utc)
A, B, C = ('aaaaaaaa-0000-4000-8000-00000000000a', 'bbbbbbbb-0000-4000-8000-00000000000b',
           'cccccccc-0000-4000-8000-00000000000c')
CFG = {'workers': [{'name': 'w1', 'bridge_address': '10.0.0.1'}, {'name': 'w2', 'bridge_address': '10.0.0.2'}],
       'not_ready_seconds': 300, 'csi_driver': 'org.democratic-csi.node-manual'}
OOS = {'key': 'node.kubernetes.io/out-of-service', 'value': 'nodeshutdown', 'effect': 'NoExecute'}


def node(name, ready=True, age=1000, bootid=A, taints=None):
    since = (NOW - timedelta(seconds=age)).strftime('%Y-%m-%dT%H:%M:%SZ')
    spec = {} if taints is None else {'taints': taints}
    return {'metadata': {'name': name, 'resourceVersion': '7'}, 'spec': spec,
            'status': {'nodeInfo': {'bootID': bootid},
                       'conditions': [{'type': 'Ready', 'status': 'True' if ready else 'Unknown', 'lastTransitionTime': since}]}}


def va(node_name, attacher='org.democratic-csi.node-manual'):
    return {'spec': {'attacher': attacher, 'nodeName': node_name}}


def history(data=None):
    return fence_policy.BootHistory(data or {})


def evaluate(nodes, vas=(), tags=(), hist=None, cfg=CFG, now=NOW):
    return fence_policy.evaluate(nodes, list(vas), ledger.Cluster.from_tags(list(tags), 'retained'),
                                 hist or history(), cfg, now)


class FencePolicyTest(unittest.TestCase):
    def test_fences_after_window(self):
        v = evaluate([node('w1'), node('w2', ready=False, age=301)])
        self.assertEqual(v.fence, 'w2')
        self.assertEqual(v.boot_id, A)

    def test_not_before_window(self):
        v = evaluate([node('w1'), node('w2', ready=False, age=299)])
        self.assertIsNone(v.fence)
        self.assertEqual(v.reasons['w2'], 'not-ready-too-short')

    def test_clock_skew_uses_server_time(self):
        # The storage VM's clock runs 10 minutes fast; the API server says w2 went NotReady 100 s ago.
        skewed = NOW.timestamp() + 600
        with mock.patch('time.time', return_value=skewed):
            v = evaluate([node('w1'), node('w2', ready=False, age=100)])
        self.assertIsNone(v.fence)

    def test_needs_other_ready_untainted(self):
        v = evaluate([node('w1', taints=[OOS]), node('w2', ready=False, age=301)])
        self.assertIsNone(v.fence)
        self.assertEqual(v.reasons['w2'], 'no-peer')

    def test_both_not_ready(self):
        v = evaluate([node('w1', ready=False, age=400), node('w2', ready=False, age=301)])
        self.assertIsNone(v.fence)
        self.assertEqual(v.reasons, {'w1': 'no-peer', 'w2': 'no-peer'})

    def test_one_at_a_time(self):
        v = evaluate([node('w1', ready=False, age=400), node('w2', ready=False, age=301)],
                     tags=[ledger.tag_fenced('w1', A)])
        self.assertIsNone(v.fence)
        self.assertEqual(v.reasons['w2'], 'one-at-a-time')
        self.assertEqual(v.continue_fence, ['w1'])

    def test_foreign_va_refused(self):
        v = evaluate([node('w1'), node('w2', ready=False, age=301)],
                     vas=[va('w2'), va('w2', 'csi.proxmox.sinextra.dev')])
        self.assertIsNone(v.fence)
        self.assertEqual(v.reasons['w2'], 'foreign-va')
        # A foreign VA on the peer does not block the fence of w2.
        self.assertEqual(evaluate([node('w1'), node('w2', ready=False, age=301)],
                                  vas=[va('w1', 'csi.proxmox.sinextra.dev')]).fence, 'w2')

    def test_holds_block(self):
        for hold in (ledger.HOLD_FENCE, ledger.HOLD_CREATE):
            with self.subTest(hold=hold):
                v = evaluate([node('w1'), node('w2', ready=False, age=301)], tags=[hold])
                self.assertIsNone(v.fence)
                self.assertEqual(v.reasons['w2'], hold)

    def test_bootid_is_last_ready(self):
        v = evaluate([node('w1'), node('w2', ready=False, age=301, bootid=B)],
                     hist=history({'w2': {'seen': [A, B], 'last_ready': A}}))
        self.assertEqual((v.fence, v.boot_id), ('w2', A))

    def test_bootid_falls_back_to_current(self):
        v = evaluate([node('w1'), node('w2', ready=False, age=301, bootid=B)])
        self.assertEqual(v.boot_id, B)

    def test_unfence_needs_unseen_bootid(self):
        hist = {'w2': {'seen': [A, B], 'last_ready': B}}
        for current in (A, B):
            with self.subTest(bootid=current):
                v = evaluate([node('w1'), node('w2', bootid=current)], tags=[ledger.tag_fenced('w2', A)],
                             hist=history(hist))
                self.assertEqual((v.unfence, v.continue_fence), ([], ['w2']))

    def test_unfence_new_bootid(self):
        v = evaluate([node('w1'), node('w2', bootid=C)], tags=[ledger.tag_fenced('w2', A)],
                     hist=history({'w2': {'seen': [A, B], 'last_ready': B}}))
        self.assertEqual((v.unfence, v.continue_fence), (['w2'], []))

    def test_unfence_waits_for_ready(self):
        v = evaluate([node('w1'), node('w2', ready=False, bootid=C)], tags=[ledger.tag_fenced('w2', A)])
        self.assertEqual((v.unfence, v.continue_fence), ([], ['w2']))

    def test_unknown_bootid_never_auto_unfences(self):
        v = evaluate([node('w1'), node('w2', bootid=C)], tags=[ledger.tag_fenced('w2', 'unknown')])
        self.assertEqual((v.unfence, v.continue_fence), ([], ['w2']))
        self.assertEqual(v.reasons['w2'], 'unknown-bootid')

    def test_unconfigured_node_ignored(self):
        v = evaluate([node('w1'), node('w2'), node('m1', ready=False, age=10000)])
        self.assertIsNone(v.fence)
        self.assertNotIn('m1', v.reasons)

    def test_unconfigured_node_is_no_peer(self):
        v = evaluate([node('m1'), node('w1', ready=False, age=400), node('w2', ready=False, age=301)])
        self.assertIsNone(v.fence)


class BootHistoryTest(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'sub' / 'bootids.json'
            h = fence_policy.BootHistory.load(path)
            self.assertEqual(h.seen('w2'), set())
            h.record('w2', A, ready=False)
            h.record('w2', B, ready=True)
            h.record('w2', A, ready=False)
            h.save(path)
            self.assertEqual(json.loads(path.read_text()), {'w2': {'seen': [A, B], 'last_ready': B}})
            h2 = fence_policy.BootHistory.load(path)
            self.assertEqual((h2.seen('w2'), h2.last_ready('w2')), ({A, B}, B))


class FakeResponse(io.BytesIO):
    def __init__(self, body, date='Mon, 09 Nov 2026 12:00:00 GMT'):
        super().__init__(json.dumps(body).encode())
        self.headers = {'Date': date} if date else {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code):
    return urllib.error.HTTPError('https://api/x', code, 'err', {}, io.BytesIO(b'{}'))


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.token = Path(self.tmp.name) / 'token'
        self.token.write_text('t0\n')
        self.ctx = mock.patch.object(kube.ssl, 'create_default_context')
        self.ssl = self.ctx.start()
        self.addCleanup(self.ctx.stop)
        self.client = kube.Client('https://10.9.9.99:6443', str(self.token), '/ca.crt')
        self.calls = []

    def serve(self, *responses):
        responses = list(responses)

        def urlopen(req, timeout, context):
            self.calls.append((req.get_method(), req.full_url, dict(req.header_items()), req.data, timeout))
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        p = mock.patch.object(kube.urllib.request, 'urlopen', side_effect=urlopen)
        p.start()
        self.addCleanup(p.stop)

    def test_auth_error_vs_unreachable(self):
        for err, cls in ((http_error(401), kube.ApiAuthError), (http_error(403), kube.ApiAuthError),
                         (http_error(500), kube.ApiUnreachable), (urllib.error.URLError('refused'), kube.ApiUnreachable),
                         (TimeoutError(), kube.ApiUnreachable)):
            with self.subTest(err=err):
                self.serve(err)
                with self.assertRaises(cls):
                    self.client.get('/api/v1/nodes')
        self.token.unlink()
        with self.assertRaises(kube.ApiAuthError):
            self.client.get('/api/v1/nodes')

    def test_rereads_token_and_ca(self):
        self.serve(FakeResponse({}), FakeResponse({}))
        self.client.get('/api/v1/nodes')
        self.token.write_text('t1\n')
        self.client.get('/api/v1/nodes')
        self.assertEqual([c[2]['Authorization'] for c in self.calls], ['Bearer t0', 'Bearer t1'])
        self.assertEqual(self.ssl.call_count, 2)
        self.ssl.assert_called_with(cafile='/ca.crt')
        self.assertEqual({c[4] for c in self.calls}, {5})

    def test_date_header_parsed(self):
        self.serve(FakeResponse({'items': []}))
        body, server_time = self.client.get('/api/v1/nodes')
        self.assertEqual(body, {'items': []})
        self.assertEqual(server_time, NOW)
        self.serve(FakeResponse({}, date=None))
        with self.assertRaises(kube.ApiUnreachable):
            self.client.get('/api/v1/nodes')

    def patch_ops(self, call):
        self.assertEqual(call[0], 'PATCH')
        self.assertEqual(call[2]['Content-type'], 'application/json-patch+json')
        return json.loads(call[3])

    def test_patch_taints_node_without_taints_field(self):
        n = node('w2')
        self.assertNotIn('taints', n['spec'])
        self.serve(FakeResponse(n))
        self.client.patch_node_taints('w2', kube.with_out_of_service(n, True), '7')
        ops = self.patch_ops(self.calls[0])
        self.assertEqual(self.calls[0][1], 'https://10.9.9.99:6443/api/v1/nodes/w2')
        self.assertEqual(ops, [{'op': 'test', 'path': '/metadata/resourceVersion', 'value': '7'},
                               {'op': 'add', 'path': '/spec/taints', 'value': [kube.OUT_OF_SERVICE]}])

    def test_patch_preserves_other_taints(self):
        other = {'key': 'example.com/keep', 'effect': 'NoSchedule'}
        n = node('w2', taints=[other])
        self.assertEqual(kube.with_out_of_service(n, True), [other, kube.OUT_OF_SERVICE])
        self.assertEqual(kube.with_out_of_service(node('w2', taints=[other, OOS]), True), [other, OOS])
        self.assertEqual(kube.with_out_of_service(node('w2', taints=[other, OOS]), False), [other])

    def test_taint_replaces_other_effect(self):
        other = {'key': 'example.com/keep', 'effect': 'NoSchedule'}
        for stale in ({**OOS, 'effect': 'NoSchedule'}, {**OOS, 'value': 'x'}):
            n = node('w2', taints=[stale, other, {**OOS, 'effect': 'PreferNoSchedule'}])
            self.assertEqual(kube.with_out_of_service(n, True), [other, OOS])

    def test_taint_kept_when_exact(self):
        taints = [OOS, {'key': 'example.com/keep', 'effect': 'NoSchedule'}]
        self.assertIs(kube.with_out_of_service(node('w2', taints=taints), True), taints)

    def test_taint_kept_with_server_fields(self):
        taints = [{**OOS, 'timeAdded': '2026-11-16T00:00:00Z'}]
        self.assertIs(kube.with_out_of_service(node('w2', taints=taints), True), taints)

    def test_untaint_removes_all_with_key(self):
        other = {'key': 'example.com/keep', 'effect': 'NoSchedule'}
        n = node('w2', taints=[other, {**OOS, 'effect': 'NoSchedule'}, OOS])
        self.assertEqual(kube.with_out_of_service(n, False), [other])

    def test_patch_conflict_retries(self):
        other = {'key': 'example.com/new', 'effect': 'NoSchedule'}
        fresh = node('w2', taints=[other])
        fresh['metadata']['resourceVersion'] = '8'
        self.serve(http_error(409), FakeResponse(fresh), http_error(422), FakeResponse(fresh), FakeResponse(fresh))
        self.client.patch_node_taints('w2', [kube.OUT_OF_SERVICE], '7')
        methods = [c[0] for c in self.calls]
        self.assertEqual(methods, ['PATCH', 'GET', 'PATCH', 'GET', 'PATCH'])
        ops = self.patch_ops(self.calls[-1])
        # The retry is rebuilt on the re-read node: its other taints are kept, our out-of-service intent applied.
        self.assertEqual(ops, [{'op': 'test', 'path': '/metadata/resourceVersion', 'value': '8'},
                               {'op': 'add', 'path': '/spec/taints', 'value': [other, kube.OUT_OF_SERVICE]}])

    def test_patch_conflict_gives_up(self):
        fresh = node('w2')
        self.serve(http_error(409), FakeResponse(fresh), http_error(409), FakeResponse(fresh),
                   http_error(409), FakeResponse(fresh), http_error(409))
        with self.assertRaises(kube.ApiUnreachable):
            self.client.patch_node_taints('w2', [kube.OUT_OF_SERVICE], '7')
        self.assertEqual([c[0] for c in self.calls].count('PATCH'), 4)


if __name__ == '__main__':
    unittest.main()

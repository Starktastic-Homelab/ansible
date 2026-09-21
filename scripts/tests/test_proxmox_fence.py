import copy
import hashlib
import http.server
import ssl
import subprocess
import threading
import time
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from maintenance_lock import acquire
from proxmox_fence import verify_vm, fence, reconcile, validate_acl, PVE as TLSClient

EXPECTED=dict(node='pve',vmid=201,name='worker-a',smbios_uuid='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee')
class PVE:
    token_id="fence@pve!retained"
    def __init__(self,**changes):
        self.uuid=EXPECTED['smbios_uuid'];self.name='worker-a';self.pending=[];self.status='running'
        self.exitstatus='OK';self.lose=False;self.stay_running=False;self.deny=False;self.change_after=False
        self.posted=[];self.reads=[];self.__dict__.update(changes)
    def get(self,path,**query):
        self.reads.append((path,query))
        if self.deny:raise PermissionError('denied')
        if path.endswith('/config'):
            if self.change_after and self.posted:self.uuid='replacement'
            return dict(name=self.name,smbios1='uuid='+self.uuid)
        if path.endswith('/pending'):return self.pending
        if '/tasks/' in path:return dict(status='stopped',exitstatus=self.exitstatus)
        if path.endswith('/status/current'):return dict(status=self.status)
        raise AssertionError(path)
    def post(self,path,data):
        self.posted.append((path,data))
        if not self.stay_running:self.status='stopped'
        if self.lose:raise TimeoutError('lost')
        return 'UPID:pve:1:2:3:qmstop:201:fence@pve!retained:'

class FenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'runner-instance').write_text('fixture')
        self.env=patch.dict(os.environ,{'HOMELAB_RUNNER_INSTANCE':'fixture'});self.env.start();self.addCleanup(self.env.stop)
        self.owner='test/1';self.nonce=acquire(self.root,'fence',self.owner);self.receipt=self.root/'fence.json'
    def call(self,api,expected=EXPECTED):return fence(api,expected,self.root,self.owner,self.nonce,self.receipt)
    def test_current_identity_read_before_single_stop(self):
        api=PVE();result=self.call(api)
        self.assertTrue(result['verified']);self.assertEqual(api.posted,[('/nodes/pve/qemu/201/status/stop',{})])
        self.assertIn(('/nodes/pve/qemu/201/config',{'current':1}),api.reads)
        self.assertEqual(json.loads(self.receipt.read_text())['expected'],EXPECTED)
    def test_wrong_identity_pending_and_denial_never_stop(self):
        for changes in [dict(uuid='other'),dict(name='other'),dict(pending=[dict(key='smbios1',pending='uuid=other')]),dict(deny=True)]:
            with self.subTest(changes=changes):
                api=PVE(**changes)
                with self.assertRaises((ValueError,PermissionError)):self.call(api)
                self.assertFalse(api.posted);self.assertFalse(self.receipt.exists())
    def test_protected_vmids_and_wrong_lock_refused(self):
        for vmid in [100,200,300,999]:
            with self.assertRaises(ValueError):self.call(PVE(),dict(EXPECTED,vmid=vmid))
        self.nonce='wrong'
        with self.assertRaises(PermissionError):self.call(PVE())
    def test_unknown_outcome_never_retries_and_reconcile_is_readonly(self):
        api=PVE(lose=True)
        with self.assertRaises(TimeoutError):self.call(api)
        self.assertFalse(self.receipt.exists())
        with self.assertRaises(FileExistsError):self.call(api)
        result=reconcile(api,EXPECTED,self.root,self.owner,self.nonce,self.receipt)
        self.assertTrue(result['verified']);self.assertEqual(len(api.posted),1)
    def test_task_ok_not_poweroff_proof_or_changed_generation(self):
        for changes in [dict(stay_running=True),dict(change_after=True),dict(exitstatus='ERROR')]:
            self.receipt.with_suffix('.intent.json').unlink(missing_ok=True)
            with self.subTest(changes=changes),self.assertRaises((ValueError,RuntimeError)):
                self.call(PVE(**changes))
            self.assertFalse(self.receipt.exists())
    def test_task_timeout_no_receipt(self):
        api=PVE();old=api.get
        api.get=lambda path,**query: dict(status='running') if '/tasks/' in path else old(path,**query)
        with patch('proxmox_fence.time.monotonic',side_effect=[0,0,301]),patch('proxmox_fence.time.sleep'):
            with self.assertRaises(TimeoutError):self.call(api)
        self.assertFalse(self.receipt.exists());self.assertEqual(len(api.posted),1)
    def test_unknown_status_never_authorizes(self):
        api=PVE(status='unknown',stay_running=True)
        with self.assertRaises(ValueError):self.call(api)
        self.assertFalse(api.posted)
    def test_wrong_token_task_and_missing_lock_refused(self):
        api=PVE();api.token_id='other@pve!token'
        with self.assertRaises(RuntimeError):self.call(api)
        self.assertFalse(self.receipt.exists())
        (self.root/'operation.json').unlink()
        with self.assertRaises(FileNotFoundError):self.call(PVE())

    def test_receipt_not_published_if_durable_write_fails(self):
        api=PVE()
        original=__import__('proxmox_fence')._write
        def fail_receipt(path,record):
            original(path,record)
            if record.get('verified'):raise OSError('disk failed after write')
        with patch('proxmox_fence._write',side_effect=fail_receipt):
            with self.assertRaises(OSError):self.call(api)
        self.assertFalse(self.receipt.exists())

    def test_qualification_accepts_exact_memory_in_proxmox_string_or_integer_form(self):
        expected = dict(EXPECTED, vmid=990, name='owned-fence-test-unit',
                        qualification=dict(approved=True, diskless=True, networkless=True, memory_mib=128))
        for memory in (128, '128', 256, '128,max=256', None):
            with self.subTest(memory=memory):
                api = PVE(name=expected['name'])
                original = api.get
                def get(path, **query):
                    result = original(path, **query)
                    return dict(result, memory=memory) if path.endswith('/config') else result
                api.get = get
                if memory in (128, '128'):
                    self.assertEqual(verify_vm(api, expected)['status'], 'running')
                else:
                    with self.assertRaises(ValueError):
                        verify_vm(api, expected)

    def test_acl_refuses_broader_existing_grants(self):
        allowed=[dict(type=t,ugid=p,path=f'/vms/{vmid}',roleid='HomelabFence',propagate=0)
                 for t,p in [('user','fence@pve'),('token','fence@pve!retained')] for vmid in [201,202]]
        validate_acl(allowed,'fence@pve','retained','HomelabFence')
        for patch_ in [dict(path='/'),dict(propagate=1),dict(roleid='Administrator')]:
            bad=copy.deepcopy(allowed);bad[0].update(patch_)
            with self.assertRaises(ValueError):validate_acl(bad,'fence@pve','retained','HomelabFence')


class FencingTLS(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.root = Path(cls.tmp.name)
        # Match the Proxmox-generated CA: CA constraint, but no keyUsage extension.
        (cls.root / 'ca.cnf').write_text('[req]\ndistinguished_name=dn\nx509_extensions=ca\n[dn]\n[ca]\nbasicConstraints=critical,CA:TRUE\nsubjectKeyIdentifier=hash\n')
        (cls.root / 'leaf.cnf').write_text('basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=IP:127.0.0.1\nauthorityKeyIdentifier=keyid:always\n')
        def openssl(*args):
            subprocess.run(['openssl', *args], cwd=cls.root, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for name in ('ca', 'other'):
            openssl('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                    '-config', 'ca.cnf', '-subj', '/CN=' + name,
                    '-keyout', name + '.key', '-out', name + '.pem')
        openssl('req', '-new', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN=fixture',
                '-keyout', 'leaf.key', '-out', 'leaf.csr')
        for name, days in (('leaf', '1'), ('expired', '0')):
            openssl('x509', '-req', '-in', 'leaf.csr', '-CA', 'ca.pem', '-CAkey', 'ca.key',
                    '-CAcreateserial', '-days', days, '-extfile', 'leaf.cnf', '-out', name + '.pem')
        time.sleep(1.1)  # Move past the zero-day certificate's whole-second expiry.

    def client(self, leaf='leaf', ca='ca', wrong_pin=False):
        received = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def handle(self):
                try:
                    super().handle()
                except (BrokenPipeError, ConnectionResetError):
                    pass  # Expected when a pin mismatch closes TLS before HTTP.
            def do_GET(self):
                received.append(self.headers.get('Authorization'))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"data":{"ok":true}}')
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.root / (leaf + '.pem'), self.root / 'leaf.key')
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01})
        thread.start()
        def close():
            server.shutdown()
            server.server_close()
            thread.join()
        self.addCleanup(close)
        credentials = self.root / 'credentials.json'
        credentials.write_text(json.dumps({'origin': 'https://127.0.0.1:' + str(server.server_port),
                                          'token_id': 'fixture@pve!token', 'token_secret': 'synthetic'}))
        credentials.chmod(0o600)
        digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert((self.root / (leaf + '.pem')).read_text())).hexdigest()
        pin = self.root / 'pin'
        pin.write_text('0' * 64 if wrong_pin else digest)
        return TLSClient(credentials, self.root / (ca + '.pem'), pin), received

    def test_proxmox_ca_without_key_usage_accepts_matching_pinned_leaf(self):
        api, received = self.client()
        self.assertEqual(api.context.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(api.get('/nodes/pve/qemu/201/status/current'), {'ok': True})
        self.assertEqual(received, ['PVEAPIToken=fixture@pve!token=synthetic'])

    def test_wrong_pin_never_sends_credentials(self):
        api, received = self.client(wrong_pin=True)
        with self.assertRaises(RuntimeError):
            api.get('/nodes/pve/qemu/201/status/current')
        self.assertEqual(received, [])

    def test_untrusted_or_expired_leaf_never_sends_credentials(self):
        for kwargs in ({'ca': 'other'}, {'leaf': 'expired'}):
            with self.subTest(**kwargs):
                api, received = self.client(**kwargs)
                with self.assertRaises(RuntimeError):
                    api.get('/nodes/pve/qemu/201/status/current')
                self.assertEqual(received, [])

if __name__=='__main__':unittest.main()

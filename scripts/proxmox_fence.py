#!/usr/bin/env python3
"""Identity-bound, stop-only Proxmox fencing. Unknown results require reconciliation."""
import argparse
from datetime import datetime, timezone
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import stat
import time
from urllib.parse import quote, urlencode, urlsplit
from maintenance_lock import verify, _write, _sync_dir


class PVE:
    def __init__(self, credentials, ca, leaf_pin):
        fd = os.open(credentials, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
                raise ValueError('PVE credentials must be a private owned file')
            data = json.load(stream)
        self.url = urlsplit(data['origin'])
        if self.url.scheme != 'https' or not self.url.hostname or self.url.username or self.url.password or self.url.path not in ('', '/') or self.url.query or self.url.fragment:
            raise ValueError('Expected a credential-free HTTPS origin')
        self.token_id = data['token_id']
        if not re.fullmatch(r'[a-zA-Z0-9_.-]+@pve![a-zA-Z0-9_.-]+', self.token_id):
            raise ValueError('Expected a dedicated PVE-realm separated token')
        self.token_secret = data['token_secret']
        self.context = ssl.create_default_context(cafile=str(ca))
        # Proxmox's generated CA lacks keyUsage. Retain chain/expiry verification
        # and the mandatory pre-auth leaf pin, allowing that legacy CA encoding.
        self.context.verify_flags &= ~ssl.VERIFY_X509_STRICT
        self.context.check_hostname = False
        self.pin = Path(leaf_pin).read_text().strip().lower()
        if not re.fullmatch('[0-9a-f]{64}', self.pin):
            raise ValueError('Invalid independent PVE leaf pin')

    def request(self, method, path, data=None, query=None):
        if not path.startswith('/nodes/'):
            raise ValueError('Fencing API is restricted to node resource paths')
        if method == 'POST' and not re.fullmatch(r'/nodes/[a-zA-Z0-9_.-]+/qemu/\d+/status/stop', path):
            raise ValueError('Only stop is exposed by the fencing client')
        connection = http.client.HTTPSConnection(self.url.hostname, self.url.port or 8006, context=self.context, timeout=25)
        try:
            connection.connect()
            actual = hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()
            if not hmac.compare_digest(actual, self.pin):
                raise ValueError('Certificate mismatch')
            endpoint = '/api2/json' + path + ('?' + urlencode(query) if query else '')
            connection.request(method, endpoint, body=urlencode(data or {}) if method == 'POST' else None,
                               headers={'Authorization': 'PVEAPIToken=' + self.token_id + '=' + self.token_secret,
                                        'Content-Type': 'application/x-www-form-urlencoded'})
            response = connection.getresponse()
            if response.status != 200:
                raise RuntimeError('Denied or unsuccessful API request')
            return json.loads(response.read())['data']
        except TimeoutError:
            raise TimeoutError('PVE request timed out; unknown outcome, do not retry stop') from None
        except Exception:
            raise RuntimeError('PVE request failed; inspect without retrying mutation') from None
        finally:
            connection.close()

    def get(self, path, **query):
        return self.request('GET', path, query=query)

    def post(self, path, data):
        return self.request('POST', path, data=data)


def _base(expected):
    allowed = expected.get('vmid') in (201, 202)
    # Explicit qualification exception cannot reach any production/runner VM.
    test = expected.get('qualification', {})
    if test:
        allowed = (type(expected.get('vmid')) is int and expected['vmid'] >= 900
                   and expected.get('name', '').startswith('owned-fence-test-')
                   and test.get('diskless') is True and test.get('networkless') is True
                   and test.get('memory_mib') == 128 and test.get('approved') is True)
    if not allowed or not re.fullmatch(r'[a-zA-Z0-9_.-]+', expected['node']):
        raise ValueError('VM is not an approved worker or owned qualification target')
    if not re.fullmatch(r'[a-zA-Z0-9_.-]+', expected['name']) or not re.fullmatch(r'[0-9a-f-]{36}', expected['smbios_uuid']):
        raise ValueError('Expected VM name and UUID are required')
    return f"/nodes/{expected['node']}/qemu/{expected['vmid']}"


def verify_vm(api, expected):
    base = _base(expected)
    config = api.get(base + '/config', current=1)
    pending = api.get(base + '/pending')
    parts = dict(x.split('=', 1) for x in config.get('smbios1', '').split(',') if '=' in x)
    if config.get('name') != expected['name'] or parts.get('uuid', '').lower() != expected['smbios_uuid']:
        raise ValueError('VM name or SMBIOS UUID changed')
    if config.get('lock') or any('pending' in x or x.get('delete') for x in pending):
        raise ValueError('VM has a current task lock or pending edits')
    if expected.get('qualification'):
        if config.get('memory') not in (128, '128') or any(re.fullmatch(r'(net|scsi|virtio|sata|ide)\d+', k) for k in config):
            raise ValueError('Qualification VM must be diskless/networkless with 128 MiB')
    status = api.get(base + '/status/current')
    if status.get('status') not in ('running', 'stopped'):
        raise ValueError('VM power state unknown')
    return {'expected': expected, 'status': status['status']}


def _save_receipt(api, expected, root, owner, nonce, receipt):
    verify(root, owner, nonce)
    observed = verify_vm(api, expected)
    if observed['status'] != 'stopped':
        raise RuntimeError('Exact VM is not confirmed stopped; keep writers held')
    result = dict(observed, verified=True, owner=owner,
                  nonce_sha256=hashlib.sha256(nonce.encode()).hexdigest(),
                  observed_at=datetime.now(timezone.utc).isoformat())
    receipt = Path(receipt)
    temporary = receipt.with_name(receipt.name + '.' + secrets.token_hex(12) + '.partial')
    _write(temporary, result)
    os.link(temporary, receipt)  # Atomic publication, never overwrite a prior receipt.
    try:
        _sync_dir(receipt.parent)
    except OSError:
        receipt.unlink()
        raise
    temporary.unlink()
    return result


def fence(api, expected, lock_root, owner, nonce, receipt):
    verify(lock_root, owner, nonce)
    _base(expected)
    intent = Path(receipt).with_suffix('.intent.json')
    if os.path.lexists(intent) or os.path.lexists(receipt):
        raise FileExistsError('Existing fencing intent: reconcile; do not repeat stop')
    before = verify_vm(api, expected)
    _write(intent, {'expected': expected, 'owner': owner, 'nonce_sha256': hashlib.sha256(nonce.encode()).hexdigest()})
    if before['status'] == 'running':
        verify(lock_root, owner, nonce)
        verify_vm(api, expected)
        upid = api.post(_base(expected) + '/status/stop', {})
        fields = upid.split(':')
        if len(fields) != 9 or fields[1] != expected['node'] or fields[5] != 'qmstop' or fields[6] != str(expected['vmid']) or fields[7] != api.token_id:
            raise RuntimeError('Stop task does not belong to the expected token/VM')
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            verify(lock_root, owner, nonce)
            task = api.get(f"/nodes/{expected['node']}/tasks/{quote(upid, safe='')}/status")
            if task.get('status') == 'stopped':
                if task.get('exitstatus') != 'OK':
                    raise RuntimeError('Stop task failed; keep writers held')
                break
            time.sleep(1)
        else:
            raise TimeoutError('Stop task timeout; reconcile, never resubmit')
    return _save_receipt(api, expected, lock_root, owner, nonce, receipt)


def reconcile(api, expected, lock_root, owner, nonce, receipt):
    verify(lock_root, owner, nonce)
    intent = json.loads(Path(receipt).with_suffix('.intent.json').read_text())
    if intent != {'expected': expected, 'owner': owner, 'nonce_sha256': hashlib.sha256(nonce.encode()).hexdigest()}:
        raise ValueError('Fencing intent or maintenance ownership mismatch')
    return _save_receipt(api, expected, lock_root, owner, nonce, receipt)


def validate_acl(acls, user, token, role):
    for entry in acls:
        if entry.get('ugid') in (user, user + '!' + token):
            if entry.get('path') not in ('/vms/201', '/vms/202') or entry.get('roleid') != role or entry.get('propagate') != 0:
                raise ValueError('Existing fencing principal has a broader ACL; manual review required')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['inspect', 'fence', 'reconcile'])
    parser.add_argument('--expected', type=Path, required=True)
    parser.add_argument('--credentials', type=Path, required=True)
    parser.add_argument('--ca', type=Path, required=True)
    parser.add_argument('--leaf-pin', type=Path, required=True)
    parser.add_argument('--receipt', type=Path)
    parser.add_argument('--lock-root', type=Path, default=Path('/maintenance'))
    args = parser.parse_args()
    api = PVE(args.credentials, args.ca, args.leaf_pin)
    expected = json.loads(args.expected.read_text())
    if args.operation == 'inspect':
        result = verify_vm(api, expected)
    else:
        if not args.receipt:
            parser.error('A new durable receipt path is required')
        result = globals()[args.operation](api, expected, args.lock_root,
                 os.environ['MAINTENANCE_OWNER'], os.environ['MAINTENANCE_NONCE'], args.receipt)
    print(json.dumps(result))


if __name__ == '__main__':
    main()

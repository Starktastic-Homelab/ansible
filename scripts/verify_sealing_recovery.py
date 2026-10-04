"""Offline recovery proof using external Vault material, never the live cluster key."""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from cryptography import x509
from cryptography.hazmat.primitives import serialization


def verify(private_key, vault_certificate, apps_certificate, kubeseal):
    key = serialization.load_pem_private_key(private_key, password=None)
    vault = x509.load_pem_x509_certificate(vault_certificate)
    apps = x509.load_pem_x509_certificate(apps_certificate)
    def public(value):
        return value.public_key().public_bytes(serialization.Encoding.DER,
                                               serialization.PublicFormat.SubjectPublicKeyInfo)
    if public(key) != public(vault) or vault.public_bytes(serialization.Encoding.DER) != apps.public_bytes(serialization.Encoding.DER):
        raise ValueError('External key/certificate does not match Apps sealing certificate')
    probe = base64.b64encode(os.urandom(32)).decode()
    secret = {'apiVersion': 'v1', 'kind': 'Secret',
              'metadata': {'name': 'csi-recovery-preflight', 'namespace': 'csi-proxmox'},
              'data': {'probe': probe}}
    with tempfile.TemporaryDirectory(prefix='sealing-recovery-') as directory:
        root = Path(directory)
        # Explicit modes also protect against an unusually permissive runner umask.
        for name, data in [('key.pem', private_key), ('cert.pem', apps_certificate)]:
            with os.fdopen(os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as handle:
                handle.write(data)
        def run(arguments, data):
            result = subprocess.run([kubeseal, *arguments], input=data, capture_output=True, timeout=30)
            if result.returncode:
                raise ValueError('Native kubeseal recovery check failed')
            return result.stdout
        sealed = run(['--cert', str(root / 'cert.pem'), '--scope', 'strict', '--format', 'json'],
                     json.dumps(secret).encode())
        recovered = json.loads(run(['--recovery-unseal', '--recovery-private-key', str(root / 'key.pem'),
                                    '--format', 'json'], sealed))
        if (recovered.get('data') != secret['data'] or recovered.get('metadata', {}).get('name') != secret['metadata']['name']
                or recovered.get('metadata', {}).get('namespace') != secret['metadata']['namespace']):
            raise ValueError('Recovered secret does not match the sealed probe')
    return {'external_key_recovery': True,
            'public_certificate_sha256': hashlib.sha256(apps.public_bytes(serialization.Encoding.DER)).hexdigest()}


if __name__ == '__main__':
    try:
        payload = json.load(sys.stdin)
        result = verify(base64.b64decode(payload['key'], validate=True),
                        base64.b64decode(payload['certificate'], validate=True),
                        Path(sys.argv[1]).read_bytes(), sys.argv[2])
    except Exception:
        # Do not let subprocess errors, malformed JSON or key material reach CI logs.
        print('External sealing-key recovery verification failed', file=sys.stderr)
        sys.exit(1)
    print(json.dumps(result))

"""Offline native kubeseal round trips with synthetic keys only."""
import base64
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parents[2]


def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'offline-preflight')])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
            .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
            .sign(key, hashes.SHA256()))
    return (key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()), cert.public_bytes(serialization.Encoding.PEM))


class RecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = ROOT / 'scripts/verify_sealing_recovery.py'
        cls.mod = None
        if path.exists():
            spec = importlib.util.spec_from_file_location('recovery', path)
            cls.mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.mod)
        cls.key, cls.cert = keypair()
        cls.other_key, cls.other_cert = keypair()

    def test_native_round_trip_requires_external_key_matching_apps_certificate(self):
        self.assertIsNotNone(self.mod, 'External-key recovery verifier is missing')
        binary = os.environ['KUBESEAL_BINARY']
        good = self.mod.verify(self.key, self.cert, self.cert, binary)
        self.assertTrue(good['external_key_recovery'])
        self.assertNotIn(base64.b64encode(self.key).decode(), str(good))
        for key, vault_cert, apps_cert in (
            (self.other_key, self.cert, self.cert),
            (self.key, self.other_cert, self.cert),
            (self.key, self.cert, self.other_cert),
        ):
            with self.subTest(mismatch=True), self.assertRaises(ValueError):
                self.mod.verify(key, vault_cert, apps_cert, binary)


    def test_cli_failure_does_not_print_private_input(self):
        marker = b'private-input-must-not-appear'
        encoded = base64.b64encode(marker).decode()
        with tempfile.TemporaryDirectory() as directory:
            certificate = Path(directory) / 'cert.pem'
            certificate.write_bytes(self.cert)
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/verify_sealing_recovery.py'),
                                     str(certificate), os.environ['KUBESEAL_BINARY']],
                                    input=json.dumps({'key': encoded, 'certificate': encoded}),
                                    capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')
        self.assertEqual(result.stderr, 'External sealing-key recovery verification failed\n')
        self.assertNotIn(encoded, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()

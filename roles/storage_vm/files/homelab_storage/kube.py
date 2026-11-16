"""Minimal Kubernetes API client for the fencer: stdlib only, credentials re-read on every request."""
import json
import ssl
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime
from pathlib import Path

TIMEOUT = 5
CONFLICT_RETRIES = 3
OUT_OF_SERVICE = {'key': 'node.kubernetes.io/out-of-service', 'value': 'nodeshutdown', 'effect': 'NoExecute'}


class ApiError(Exception):
    pass


class ApiAuthError(ApiError):
    """401/403, or no readable token: the fencer is blind until the credential is fixed."""


class ApiUnreachable(ApiError):
    pass


class _Conflict(ApiUnreachable):
    """409/422; only patch_node_taints retries it, everywhere else it is just unreachable."""


def with_out_of_service(node, present):
    """The node's taints with exactly one out-of-service taint (key, value and effect) added or removed; other keys are kept as is."""
    taints = (node.get('spec') or {}).get('taints') or []
    others = [t for t in taints if t.get('key') != OUT_OF_SERVICE['key']]
    if not present:
        return others
    oos = [t for t in taints if t.get('key') == OUT_OF_SERVICE['key']]
    # extra fields (timeAdded) are ignored so a server-filled taint is not re-patched every loop
    keep = len(oos) == 1 and all(oos[0].get(k) == OUT_OF_SERVICE[k] for k in ('value', 'effect'))
    return taints if keep else others + [OUT_OF_SERVICE]


class Client:
    def __init__(self, api, token_file, ca_file):
        self.api = api.rstrip('/')
        self.token_file = token_file
        self.ca_file = ca_file

    def _request(self, method, path, body=None, content_type=None):
        try:
            token = Path(self.token_file).read_text().strip()
        except OSError as e:
            raise ApiAuthError(f'token unreadable: {e.strerror}')
        headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}
        if content_type:
            headers['Content-Type'] = content_type
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.api + path, data=data, headers=headers, method=method)
        try:
            context = ssl.create_default_context(cafile=self.ca_file)
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=context) as resp:
                payload = json.loads(resp.read() or b'{}')
                date = resp.headers.get('Date')
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise ApiAuthError(f'{method} {path}: HTTP {e.code}')
            if e.code in (409, 422):
                raise _Conflict(f'{method} {path}: HTTP {e.code}')
            raise ApiUnreachable(f'{method} {path}: HTTP {e.code}')
        except (OSError, ValueError) as e:  # URLError, timeouts, TLS and CA-file errors, bad JSON
            raise ApiUnreachable(f'{method} {path}: {e}')
        if not date:
            raise ApiUnreachable(f'{method} {path}: no Date header')  # no server time, no safe NotReady age
        try:
            return payload, parsedate_to_datetime(date)
        except (TypeError, ValueError):
            raise ApiUnreachable(f'{method} {path}: bad Date header {date!r}')

    def get(self, path):
        """(body, server_time), with server_time from the response's Date header."""
        return self._request('GET', path)

    def patch_node_taints(self, node, taints, resource_version):
        """Set the node's taints, guarded by resourceVersion.

        On a conflict the node is re-read and the patch rebuilt from its current taints with only our
        out-of-service intent applied, so a taint someone else added in between is kept.
        """
        want_oos = any(t.get('key') == OUT_OF_SERVICE['key'] for t in taints)
        for attempt in range(CONFLICT_RETRIES + 1):
            ops = [{'op': 'test', 'path': '/metadata/resourceVersion', 'value': resource_version},
                   {'op': 'add', 'path': '/spec/taints', 'value': taints}]
            try:
                return self._request('PATCH', f'/api/v1/nodes/{node}', ops, 'application/json-patch+json')[0]
            except _Conflict as e:
                if attempt == CONFLICT_RETRIES:
                    raise ApiUnreachable(f'{e} after {CONFLICT_RETRIES} retries')
            fresh = self.get(f'/api/v1/nodes/{node}')[0]
            resource_version = fresh['metadata']['resourceVersion']
            taints = with_out_of_service(fresh, want_oos)

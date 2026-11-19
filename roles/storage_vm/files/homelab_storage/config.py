"""Load /etc/homelab-storage/config.json; unknown or missing keys are errors, never silent defaults."""
import json
from pathlib import Path

PATH = Path('/etc/homelab-storage/config.json')
KEYS = {'workers': list, 'bridge_address': str, 'portal': str, 'vgs': list, 'api_url': str, 'token_file': str,
        'ca_file': str, 'csi_driver': str, 'max_removals': int, 'not_ready_seconds': int, 'loop_seconds': int,
        'proof_timeout_seconds': int, 'quarantine_seconds': int, 'blind_after_seconds': int, 'pause_after': list, 'pv_devices': dict}


class ConfigError(ValueError):
    pass


def load(path=PATH):
    try:
        data = json.loads(Path(path).read_text())
    except ValueError as e:
        raise ConfigError(f'{path}: invalid JSON: {e}')
    if not isinstance(data, dict) or set(data) != set(KEYS):
        got = set(data) if isinstance(data, dict) else set()
        raise ConfigError(f'{path}: missing {sorted(set(KEYS) - got)}, unknown {sorted(got - set(KEYS))}')
    for k, t in KEYS.items():
        if not isinstance(data[k], t) or isinstance(data[k], bool):
            raise ConfigError(f'{path}: {k} must be {t.__name__}')
    for w in data['workers']:
        if not isinstance(w, dict) or set(w) != {'name', 'bridge_address'} or not all(isinstance(v, str) and v for v in w.values()):
            raise ConfigError(f'{path}: each worker is exactly {{name, bridge_address}}')
    if sorted(data['vgs']) != ['regen', 'retained']:
        raise ConfigError(f'{path}: vgs must be ["retained", "regen"]')
    if data['portal'] != f'{data["bridge_address"]}:3260':
        raise ConfigError(f'{path}: portal must be <bridge_address>:3260')
    pv = data['pv_devices']
    if set(pv) != {'retained', 'regen'} or not all(isinstance(v, str) and v.startswith('/dev/') for v in pv.values()) \
            or pv['retained'] == pv['regen']:
        raise ConfigError(f'{path}: pv_devices must map retained and regen to distinct /dev/ paths')
    if not all(isinstance(p, str) for p in data['pause_after']):
        raise ConfigError(f'{path}: pause_after must list step names')
    return data

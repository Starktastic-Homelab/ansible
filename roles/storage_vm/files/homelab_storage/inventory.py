"""Parse and validate the apps-owned volume inventory (a JSON array, one entry per volume)."""
import datetime
import json
import re
from dataclasses import dataclass

# Same string as apps CI; always match with fullmatch ('$' would accept a trailing newline).
ID_PATTERN = r'^(?!snapshot|pvmove)[a-z0-9][a-z0-9-]{0,62}$'
SIZE_PATTERN = r'^[1-9][0-9]*(Gi|Mi)$'
EXTENT = 4 * 1024 ** 2  # lvcreate rounds up to this; a non-multiple would later look like a shrink
CLASSES = ('retained', 'regenerable')
ORIGINS = ('migrated', 'new')
FIELDS = {'id', 'namespace', 'claim', 'size', 'class', 'origin'}
OPTIONAL = {'copied'}  # ISO date the switch PR sets on a migrated entry; not used here, only validated


class InventoryError(ValueError):
    pass


@dataclass(frozen=True)
class Volume:
    id: str
    namespace: str
    claim: str
    size_bytes: int
    cls: str
    origin: str


def parse_size(s):
    if not isinstance(s, str) or not re.fullmatch(SIZE_PATTERN, s):
        raise InventoryError(f'bad size {s!r}: want <n>Gi or <n>Mi')
    n = int(s[:-2]) * (1024 ** 3 if s.endswith('Gi') else 1024 ** 2)
    if n % EXTENT:
        raise InventoryError(f'size {s} is not a multiple of 4 MiB')
    return n


def check_copied(vid, e):
    if e['origin'] != 'migrated':
        raise InventoryError(f'{vid}: copied is only allowed on origin migrated')
    try:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', e['copied']):
            raise ValueError
        datetime.date.fromisoformat(e['copied'])
    except ValueError:
        raise InventoryError(f'{vid}: bad copied {e["copied"]!r}: want YYYY-MM-DD')


def vg_for(cls):
    if cls not in CLASSES:
        raise InventoryError(f'bad class {cls!r}')
    return 'retained' if cls == 'retained' else 'regen'


def load(text):
    try:
        data = json.loads(text)
    except ValueError as e:
        raise InventoryError(f'invalid JSON: {e}')
    if not isinstance(data, list):
        raise InventoryError('inventory must be a JSON array')
    volumes, seen = [], set()
    for i, e in enumerate(data):
        if not isinstance(e, dict) or not FIELDS <= set(e) <= FIELDS | OPTIONAL:
            raise InventoryError(f'entry {i}: fields must be exactly {sorted(FIELDS)} plus optional {sorted(OPTIONAL)}')
        if not all(isinstance(v, str) for v in e.values()):
            raise InventoryError(f'entry {i}: all fields must be strings')
        vid = e['id']
        if not re.fullmatch(ID_PATTERN, vid):
            raise InventoryError(f'entry {i}: bad id {vid!r}')
        if vid in seen:
            raise InventoryError(f'duplicate id {vid!r}')
        seen.add(vid)
        if not e['namespace'] or not e['claim']:
            raise InventoryError(f'{vid}: namespace and claim are required')
        if e['class'] not in CLASSES:
            raise InventoryError(f'{vid}: bad class {e["class"]!r}')
        if e['origin'] not in ORIGINS:
            raise InventoryError(f'{vid}: bad origin {e["origin"]!r}')
        if 'copied' in e:
            check_copied(vid, e)
        volumes.append(Volume(vid, e['namespace'], e['claim'], parse_size(e['size']), e['class'], e['origin']))
    return volumes

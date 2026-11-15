"""Read the LVM tag ledger (per VG) and the cluster-wide fence tags (VG 'retained' only)."""
import re

HOLD_CREATE = 'hold_create'
HOLD_FENCE = 'hold_fence'
UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
FENCED = re.compile(rf'^fenced:([^:\s]+):({UUID}|unknown)$')
LEDGER = re.compile(r'^ledger:([^:\s]+):(creating|created)$')
TOMBSTONE = re.compile(r'^tombstone:([^:\s]+)$')


def tag_creating(id):
    return f'ledger:{id}:creating'


def tag_created(id):
    return f'ledger:{id}:created'


def tag_tombstone(id):
    return f'tombstone:{id}'


def tag_fenced(node, bootid):
    return f'fenced:{node}:{bootid}'


def tag_pending_delete(unix_seconds):
    return f'pending-delete:{int(unix_seconds)}'


class Ledger:
    """Per-VG ledger; hold_* and fenced: tags are deliberately ignored here."""

    def __init__(self, states, tombstones):
        self._states = states
        self._tombstones = tombstones

    @classmethod
    def from_tags(cls, tags):
        states, tombs = {}, set()
        for t in tags:
            if m := LEDGER.match(t):
                # 'created' is written after 'creating', so it wins if both are present.
                if states.get(m[1]) != 'created':
                    states[m[1]] = m[2]
            elif m := TOMBSTONE.match(t):
                tombs.add(m[1])
        return cls(states, tombs)

    def state(self, id):
        return self._states.get(id)

    def tombstoned(self, id):
        return id in self._tombstones


class Cluster:
    def __init__(self, fenced, holds):
        self._fenced = fenced
        self._holds = holds

    @classmethod
    def from_tags(cls, tags, vg):
        if vg != 'retained':
            raise ValueError(f'cluster tags are only honoured on VG retained, not {vg!r}')
        fenced, holds = {}, set()
        for t in tags:
            if t in (HOLD_CREATE, HOLD_FENCE):
                holds.add(t)
            elif t.startswith('fenced:'):
                m = FENCED.match(t)
                if not m:
                    raise ValueError(f'malformed fence tag {t!r}')
                if m[1] in fenced and fenced[m[1]] != m[2]:
                    raise ValueError(f'conflicting fence tags for {m[1]}')
                fenced[m[1]] = m[2]
        return cls(fenced, holds)

    def fenced(self):
        return dict(self._fenced)

    def holds(self):
        return set(self._holds)

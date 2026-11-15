"""Pure fencer decisions: nodes + VolumeAttachments + cluster tags + bootID history -> who to fence or unfence.

Every age is measured against the API server's clock (its Date header), never the storage VM's.
"""
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .kube import OUT_OF_SERVICE

HISTORY_PATH = Path('/var/lib/storage-fencer/bootids.json')
UNKNOWN = 'unknown'


class BootHistory:
    """{node: {seen: [bootID, ...], last_ready: bootID}}; the executor decides when to record."""

    def __init__(self, data):
        self._data = data

    @classmethod
    def load(cls, path=HISTORY_PATH):
        try:
            return cls(json.loads(Path(path).read_text()))
        except FileNotFoundError:
            return cls({})  # a rebuilt storage VM starts empty; the fence tag still carries the bootID

    def save(self, path=HISTORY_PATH):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + '.tmp')
        tmp.write_text(json.dumps(self._data, indent=1, sort_keys=True))
        os.replace(tmp, path)

    def seen(self, node):
        return set(self._data.get(node, {}).get('seen', []))

    def last_ready(self, node):
        return self._data.get(node, {}).get('last_ready')

    def record(self, node, bootid, ready):
        entry = self._data.setdefault(node, {'seen': [], 'last_ready': None})
        if bootid not in entry['seen']:
            entry['seen'].append(bootid)
        if ready:
            entry['last_ready'] = bootid


@dataclass
class Verdict:
    fence: str = None  # at most one node to fence this loop
    boot_id: str = None  # the bootID to tag the fence with
    unfence: list = field(default_factory=list)
    continue_fence: list = field(default_factory=list)  # tagged nodes that get fence steps 2-6 again
    reasons: dict = field(default_factory=dict)  # node -> why it was not fenced or not unfenced


def _ready(node):
    """(is Ready, lastTransitionTime of the Ready condition) or (False, None) without one."""
    for c in node.get('status', {}).get('conditions', []):
        if c.get('type') == 'Ready':
            return c.get('status') == 'True', datetime.fromisoformat(c['lastTransitionTime'].replace('Z', '+00:00'))
    return False, None


def _boot_id(node):
    return node.get('status', {}).get('nodeInfo', {}).get('bootID') or UNKNOWN


def _out_of_service(node):
    return any(t.get('key') == OUT_OF_SERVICE['key'] for t in node.get('spec', {}).get('taints') or [])


def evaluate(nodes, vas, cluster, history, cfg, server_time):
    workers = {w['name'] for w in cfg['workers']}
    by_name = {n['metadata']['name']: n for n in nodes if n['metadata']['name'] in workers}
    fenced = cluster.fenced()
    v = Verdict()

    for name, tagged in sorted(fenced.items()):
        if name not in workers:
            continue  # restore-set fails loudly on such a tag (R13); nothing here can act on it
        n = by_name.get(name)
        if tagged == UNKNOWN:
            v.continue_fence.append(name)
            v.reasons[name] = 'unknown-bootid'  # only `storage-fencer unfence --confirm-stopped` clears it
        elif n is None:
            v.continue_fence.append(name)
            v.reasons[name] = 'node-missing'
        elif not _ready(n)[0]:
            v.continue_fence.append(name)
            v.reasons[name] = 'not-ready'
        elif _boot_id(n) in history.seen(name) | {tagged, UNKNOWN}:
            v.continue_fence.append(name)
            v.reasons[name] = 'seen-bootid'
        else:
            v.unfence.append(name)

    for name, n in sorted(by_name.items()):
        if name in fenced:
            continue
        ready, since = _ready(n)
        if ready:
            continue
        if since is None:
            v.reasons[name] = 'no-ready-condition'
            continue
        if (server_time - since).total_seconds() < cfg['not_ready_seconds']:
            v.reasons[name] = 'not-ready-too-short'
        elif fenced or v.fence:
            v.reasons[name] = 'one-at-a-time'
        elif not any(_ready(p)[0] and not _out_of_service(p) for p_name, p in by_name.items() if p_name != name):
            v.reasons[name] = 'no-peer'
        elif any(a['spec'].get('nodeName') == name and a['spec'].get('attacher') != cfg['csi_driver'] for a in vas):
            v.reasons[name] = 'foreign-va'
        elif holds := sorted(cluster.holds()):
            v.reasons[name] = holds[0]
        else:
            v.fence = name
            v.boot_id = history.last_ready(name) or _boot_id(n)
    return v

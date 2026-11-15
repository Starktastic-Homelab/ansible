"""LVM reads (vgs/lvs JSON reports) and the few LVM writes the reconcile makes."""
import json
from dataclasses import dataclass

from . import host
from .ledger import Ledger
from .plan import LvState, VgState

VGS = ['vgs', '--reportformat', 'json', '--units', 'b', '--nosuffix', '-o', 'vg_name,vg_tags,vg_size,vg_free']
LVS = ['lvs', '--reportformat', 'json', '--units', 'b', '--nosuffix', '-o', 'vg_name,lv_name,lv_size,lv_tags']


@dataclass(frozen=True)
class Vg(VgState):
    tags: tuple = ()  # raw VG tags; Cluster.from_tags reads them on 'retained'
    size_bytes: int = 0
    free_bytes: int = 0


def _rows(argv, key):
    return json.loads(host.run(argv).stdout)['report'][0][key]


def _tags(s):
    return tuple(t for t in s.split(',') if t)


def vgs(names=('retained', 'regen')):
    """Only the named VGs; a missing one is present=False. Other VGs on the host are never looked at."""
    rows = {r['vg_name']: r for r in _rows(VGS, 'vg') if r['vg_name'] in names}
    lvs = {n: {} for n in rows}
    for r in _rows(LVS, 'lv'):
        if r['vg_name'] in rows:
            lvs[r['vg_name']][r['lv_name']] = LvState(int(r['lv_size']), _tags(r['lv_tags']))
    out = {}
    for n in names:
        if (r := rows.get(n)) is None:
            out[n] = Vg(Ledger.from_tags(()), {}, False)
        else:
            tags = _tags(r['vg_tags'])
            out[n] = Vg(Ledger.from_tags(tags), lvs[n], True, tags, int(r['vg_size']), int(r['vg_free']))
    return out


def create(vg, id, size_bytes):
    host.run(['lvcreate', '-n', id, '-L', f'{size_bytes}b', '-W', 'y', '--yes', '-Z', 'y', vg])


def extend(vg, id, size_bytes):
    host.run(['lvextend', '-L', f'{size_bytes}b', f'{vg}/{id}'])


def _flags(add, delete):
    return [x for t in delete for x in ('--deltag', t)] + [x for t in add for x in ('--addtag', t)]


def vg_tags(vg, add=(), delete=()):
    """One vgchange, so a delete+add is a single metadata commit."""
    host.run(['vgchange', *_flags(add, delete), vg])


def lv_tags(vg, id, add=(), delete=()):
    host.run(['lvchange', *_flags(add, delete), f'{vg}/{id}'])

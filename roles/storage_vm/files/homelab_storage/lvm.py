"""LVM reads (vgs/lvs JSON reports) and the few LVM writes the reconcile makes."""
import json
import subprocess
import time
from dataclasses import dataclass

from . import host
from .ledger import Ledger
from .plan import LvState, VgState

VG_COLS = ['--reportformat', 'json', '--units', 'b', '--nosuffix', '-o', 'vg_name,vg_tags,vg_size,vg_free']
LV_COLS = ['--reportformat', 'json', '--units', 'b', '--nosuffix', '-o', 'vg_name,lv_name,lv_size,lv_tags']
# Per VG read (vgs and lvs share it) and per write. A command past it is left running, never killed (host.run), and
# later commands for that VG fail at once until it ends (ADR 0017 F11).
LVM_TIMEOUT = 30


@dataclass(frozen=True)
class Vg(VgState):
    tags: tuple = ()  # raw VG tags; Cluster.from_tags reads them on 'retained'
    size_bytes: int = 0


def _lvm(cmd, vg, *args, timeout=LVM_TIMEOUT, read=False):
    """Opens only `vg`'s PV (--devices also turns hints off: F12); `args` ends with the VG or vg/lv it names."""
    argv = [cmd, '--devices', host.CONFIG['pv_devices'][vg], *(['--readonly'] if read else []), *args]
    return host.run(argv, timeout=timeout, key=vg)


def _rows(cmd, vg, cols, key, timeout):
    return json.loads(_lvm(cmd, vg, *cols, vg, timeout=timeout, read=True).stdout)['report'][0][key]


def _tags(s):
    return tuple(t for t in s.split(',') if t)


def _absent():
    return Vg(ledger=Ledger.from_tags(()), lvs={}, present=False)


def _read(name, with_lvs):
    deadline = time.monotonic() + LVM_TIMEOUT
    try:
        r = next((r for r in _rows('vgs', name, VG_COLS, 'vg', LVM_TIMEOUT) if r['vg_name'] == name), None)
        if r is None:
            return _absent()
        lvs = {}
        if with_lvs:
            for lr in _rows('lvs', name, LV_COLS, 'lv', max(0.1, deadline - time.monotonic())):
                if lr['vg_name'] == name:
                    lvs[lr['lv_name']] = LvState(int(lr['lv_size']), _tags(lr['lv_tags']))
    except (subprocess.SubprocessError, ValueError, KeyError, IndexError) as e:
        host.log(f'error: VG {name} unreadable on {host.CONFIG["pv_devices"][name]}: {e}')
        return _absent()
    tags = _tags(r['vg_tags'])
    return Vg(ledger=Ledger.from_tags(tags), lvs=lvs, present=True, free_bytes=int(r['vg_free']), tags=tags,
              size_bytes=int(r['vg_size']))


def vgs(names=('retained', 'regen')):
    """Only the named VGs, each read from its own PV alone; one that cannot be read (stall, I/O error, absent PV) is
    present=False and never raises."""
    return {n: _read(n, True) for n in names}


def vg_tags_only(name):
    """The VG's tags and sizes without its LVs: one vgs, for the fencer and restore-set."""
    return _read(name, False)


def create(vg, id, size_bytes):
    _lvm('lvcreate', vg, '-n', id, '-L', f'{size_bytes}b', '-W', 'y', '--yes', '-Z', 'y', vg)


def extend(vg, id, size_bytes):
    _lvm('lvextend', vg, '-L', f'{size_bytes}b', f'{vg}/{id}')


def remove(vg, id):
    _lvm('lvremove', vg, '-y', f'{vg}/{id}')


def activate(vg):
    _lvm('vgchange', vg, '-ay', vg)


def _flags(add, delete):
    return [x for t in delete for x in ('--deltag', t)] + [x for t in add for x in ('--addtag', t)]


def vg_tags(vg, add=(), delete=()):
    """One vgchange, so a delete+add is a single metadata commit."""
    _lvm('vgchange', vg, *_flags(add, delete), vg)


def lv_tags(vg, id, add=(), delete=()):
    _lvm('lvchange', vg, *_flags(add, delete), f'{vg}/{id}')

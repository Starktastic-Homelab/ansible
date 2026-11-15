"""storage-reconcile: make LVs and LIO targets match the apps inventory and the LVM ledger.

validate --inventory FILE                     schema only, no host I/O
apply --inventory FILE --config FILE [--dry-run]
"""
import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

from . import config, host, inventory, lio, lvm, metrics, nft, plan
from .ledger import HOLD_CREATE, Cluster, tag_created, tag_creating

MARKER_TMP = None  # parent of the private marker mount dir; None = the system temp dir
# A dry run (before the maintenance lock) exits 1 only for these; hold_create and per-entry refusals do not block it.
BLOCKING = {'vg-missing', 'removal-brake', 'lv-missing'}
METRICS = 'storage-reconcile'
SUCCESS = 'storage_last_reconcile_success_timestamp_seconds'


def _state(cfg):
    vgs = lvm.vgs(tuple(cfg['vgs']))
    cluster = None
    if vgs['retained'].present:
        try:
            cluster = Cluster.from_tags(vgs['retained'].tags, 'retained')
        except ValueError as e:  # unreadable cluster tags: refuse everything, as for a missing VG
            host.log(f'error: {e}')
    return vgs, cluster


class Executor:
    def __init__(self, cfg, lio_, vgs, fenced):
        self.cfg, self.lio, self.vgs = cfg, lio_, vgs
        self.acls = [lio.worker_iqn(w['name']) for w in cfg['workers'] if w['name'] not in fenced]

    def target(self, a, step=None):
        self.lio.ensure_target(a.id, f'/dev/{a.vg}/{a.id}', self.acls, self.cfg['portal'])
        if step:
            host.checkpoint(step)

    def do(self, a):
        getattr(self, a.kind)(a)

    def export(self, a):
        self.target(a)

    def extend(self, a):
        lvm.extend(a.vg, a.id, a.size_bytes)
        host.checkpoint('grow:lvextend')

    def mark_pending(self, a):
        lvm.lv_tags(a.vg, a.id, add=[f'{plan.PENDING_PREFIX}{int(time.time())}'])

    def clear_pending(self, a):
        tags = [t for t in self.vgs[a.vg].lvs[a.id].tags if t.startswith(plan.PENDING_PREFIX)]
        lvm.lv_tags(a.vg, a.id, delete=tags)

    def create_raw(self, a):
        self._create(a, fs=False)

    def create_new(self, a):
        self._create(a, fs=True)

    def _create(self, a, fs):
        dev = f'/dev/{a.vg}/{a.id}'
        lvm.vg_tags(a.vg, add=[tag_creating(a.id)])
        host.checkpoint('create:ledger-creating')
        lvm.create(a.vg, a.id, a.size_bytes)  # fails if the LV exists
        host.checkpoint('create:lvcreate')
        if fs:
            # blkid -p exits 2 for "nothing found" but also for an unreadable device, so prove a read first.
            host.run(['dd', f'if={dev}', 'of=/dev/null', 'bs=1M', 'count=1', 'iflag=direct'])
            host.checkpoint('create:read')
            rc = host.run(['blkid', '-p', dev], check=False).returncode
            if rc != 2:
                raise RuntimeError(f'blkid -p {dev} exited {rc}: not provably blank, not formatting')
            host.checkpoint('create:blkid')
            host.run(['mkfs.ext4', '-O', 'mmp', '-e', 'remount-ro', '-E', 'mmp_update_interval=5', dev])
            host.checkpoint('create:mkfs')
            self._marker(a.id, dev)
            host.checkpoint('create:marker')
        lvm.vg_tags(a.vg, delete=[tag_creating(a.id)], add=[tag_created(a.id)])
        host.checkpoint('create:ledger-created')
        self.target(a, 'create:target')
        self.lio.save()
        host.checkpoint('create:save')

    def _marker(self, id, dev):
        mnt = tempfile.mkdtemp(prefix='storage-marker-', dir=MARKER_TMP)  # 0700
        try:
            host.run(['mount', '-t', 'ext4', '-o', 'nodev,nosuid,noexec', dev, mnt])
            try:
                with open(os.path.join(mnt, f'.homelab-{id}-data'), 'w') as f:
                    f.write(f'homelab/{id}/data/v1\n')
                    f.flush()
                    os.fsync(f.fileno())
            finally:
                host.run(['umount', mnt])
        finally:
            os.rmdir(mnt)


def _fence_set_errors(cfg, fenced):
    """The reconcile never repairs the set (the fencer and restore-set do); it fails loudly instead."""
    addrs = {w['name']: w['bridge_address'] for w in cfg['workers']}
    errors = []
    for node in sorted(fenced):
        if node not in addrs:
            errors.append(f'fenced node {node} is not a configured worker')
            continue
        try:
            if not nft.is_fenced(addrs[node]):
                errors.append(f'fenced node {node}: {addrs[node]} is missing from the nft set fenced')
        except Exception as e:
            errors.append(f'fenced node {node}: cannot read the nft set fenced: {e}')
    for e in errors:
        host.log(f'error: {e}')
    return errors


def _start_targetctl(lio_):
    """An inactive unit would never run its ExecStop/boot check; start it only when that drops no session."""
    if host.run(['systemctl', 'is-active', 'rtslib-fb-targetctl'], check=False).returncode == 0:
        return []
    try:
        busy = [i for i in sorted(lio_.targets()) if lio_.sessions(i)]
    except lio.InUse as e:
        busy = [str(e)]
    if busy:
        host.log(f'warning: rtslib-fb-targetctl is inactive but sessions exist ({", ".join(busy)}); not starting it')
        return []
    r = host.run(['systemctl', 'start', 'rtslib-fb-targetctl'], check=False)
    return [f'systemctl start rtslib-fb-targetctl exited {r.returncode}'] if r.returncode else []


def _exported(d, vgs, failed, planned):
    out, seen = [], set()
    grow = {a.id: a.size_bytes for a in d.actions if a.kind == 'extend'} if planned else {}
    for a in d.actions:
        if a.kind not in ('export', 'create_new', 'create_raw') or a.id in seen or a.id in failed:
            continue
        seen.add(a.id)
        lv = vgs[a.vg].lvs.get(a.id)
        size = grow.get(a.id) or (lv.size_bytes if lv else a.size_bytes)
        out.append({'id': a.id, 'size_bytes': size})
    return out


def _write_metrics(d, vgs, cluster, lio_, ok):
    samples = []
    for n, v in vgs.items():
        if v.present:
            samples += [('storage_vg_size_bytes', {'vg': n}, v.size_bytes), ('storage_vg_free_bytes', {'vg': n}, v.free_bytes)]
    want = {a.id: a.vg for a in d.actions if a.kind in ('export', 'create_new', 'create_raw')}
    want.update({r.id: n for r in d.refusals if r.reason == 'lv-missing' for n, v in vgs.items() if v.ledger.state(r.id)})
    for id, vg in sorted(want.items()):
        samples.append(('storage_target_missing', {'id': id}, int(lio_.lun0_device(id) != f'/dev/{vg}/{id}')))
    for n, v in vgs.items():
        for id, lv in sorted(v.lvs.items()):
            for t in lv.tags:
                if t.startswith(plan.PENDING_PREFIX):
                    samples.append(('storage_pending_delete', {'id': id, 'since': t[len(plan.PENDING_PREFIX):]}, 1))
    samples.append(('storage_hold_create', {}, int(bool(cluster) and HOLD_CREATE in cluster.holds())))
    # A failed run keeps the last success time, so "too old" alerts keep counting from it.
    samples.append((SUCCESS, {}, int(time.time()) if ok else (metrics.read(METRICS, SUCCESS) or 0)))
    metrics.write(METRICS, samples)


def _report(sha, d, exported, errors):
    return {'sha': sha, 'actions': [asdict(a) for a in d.actions], 'refusals': [asdict(r) for r in d.refusals],
            'exported': exported, 'errors': errors}


def apply(volumes, cfg, sha, dry_run):
    if dry_run:  # reads only: vgs and lvs, no lock, no checkpoint, no metrics, no save
        vgs, cluster = _state(cfg)
        d = plan.decide(volumes, vgs, cluster, cfg['max_removals'])
        print(json.dumps(_report(sha, d, _exported(d, vgs, set(), True), [])))
        return 1 if BLOCKING & {r.reason for r in d.refusals} else 0
    errors, failed = [], set()
    with host.LIO_LOCK:  # every LVM, LIO and nft mutation and the save; released before systemctl start
        vgs, cluster = _state(cfg)
        d = plan.decide(volumes, vgs, cluster, cfg['max_removals'])
        fenced = cluster.fenced() if cluster else {}
        errors += _fence_set_errors(cfg, fenced)
        lio_ = lio.Lio()
        ex = Executor(cfg, lio_, vgs, fenced)
        for a in d.actions:
            if a.id in failed:
                continue
            try:
                ex.do(a)
            except Exception as e:
                host.log(f'error: {a.kind} {a.id}: {e}')
                errors.append(f'{a.kind} {a.id}: {e}')
                failed.add(a.id)
        try:
            lio_.save()
        except Exception as e:
            errors.append(f'save: {e}')
    errors += _start_targetctl(lio_)
    final, _ = _state(cfg)
    ok = not (d.fail or d.refusals or errors)
    _write_metrics(d, final, cluster, lio_, ok)
    for r in d.refusals:
        host.log(f'refused: {r.id}: {r.reason}')
    print(json.dumps(_report(sha, d, _exported(d, final, failed, False), errors)))
    return 0 if ok else 1


def main(argv=None):
    p = argparse.ArgumentParser(prog='storage-reconcile')
    sub = p.add_subparsers(dest='cmd', required=True)
    v = sub.add_parser('validate')
    v.add_argument('--inventory', required=True)
    a = sub.add_parser('apply')
    a.add_argument('--inventory', required=True)
    a.add_argument('--config', required=True)
    a.add_argument('--dry-run', action='store_true')
    args = p.parse_args(argv)
    try:
        raw = Path(args.inventory).read_bytes()
        volumes = inventory.load(raw.decode())
        if args.cmd == 'validate':
            print(f'ok: {len(volumes)} volumes')
            return 0
        cfg = config.load(args.config)
        host.CONFIG = cfg
        return apply(volumes, cfg, hashlib.sha256(raw).hexdigest(), args.dry_run)
    except (inventory.InventoryError, config.ConfigError, OSError, UnicodeDecodeError,
            subprocess.CalledProcessError) as e:
        host.log(f'error: {e}')
        return 1

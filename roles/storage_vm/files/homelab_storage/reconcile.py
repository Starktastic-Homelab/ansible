"""storage-reconcile: make LVs and LIO targets match the apps inventory and the LVM ledger.

validate --inventory FILE                     schema only, no host I/O
apply --inventory FILE [--dry-run]
boot-check                                    strip fenced ACLs, save, verify every ledgered target
expire-candidates                             JSON of pending-delete LVs past quarantine (none under hold_create)
expire --id ID                                delete one such LV's target, data, ledger and write its tombstone
activate --vg VG                              activate one VG (the boot drop-in's regen line)
Every subcommand but validate takes --config FILE, default /etc/homelab-storage/config.json (config.PATH).
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
from .ledger import HOLD_CREATE, Cluster, tag_created, tag_creating, tag_tombstone

MARKER_TMP = None  # parent of the private marker mount dir; None = the system temp dir
# A dry run (before the maintenance lock) exits 1 only for these. hold_create and other per-entry refusals do not block
# it (R6, D6); it logs the hold line instead. no-space blocks (D40): the run stops before the lock and the next retries.
BLOCKING = {'vg-missing', 'removal-brake', 'lv-missing', 'no-space'}
METRICS = 'storage-reconcile'
_dev_exists = os.path.exists  # tests patch it
SUCCESS = 'storage_last_reconcile_success_timestamp_seconds'


def _state(cfg):
    vgs = lvm.vgs()
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
        self.acls = [lio.worker_iqn(w['name'], cfg['initiator_prefix']) for w in cfg['workers'] if w['name'] not in fenced]

    def target(self, a, step=None):
        dev = f'/dev/{a.vg}/{a.id}'
        if not _dev_exists(dev):  # a VG whose boot activation was given up on (ADR 0017 F11)
            lvm.activate(a.vg)
        self.lio.ensure_target(a.id, dev, self.acls, self.cfg['portal'])
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


def _fence_set_errors(cfg, fenced, tags_read=True):
    """The reconcile never repairs the set (the fencer and restore-set do); it fails loudly instead.

    With every tag read, an element with no fence tag is an error too: it would re-cut an unfenced worker."""
    addrs = {w['name']: w['bridge_address'] for w in cfg['workers']}
    errors = []
    if tags_read:
        try:
            stale = nft.elements() - {addrs[n] for n in fenced if n in addrs}
        except Exception as e:
            stale, errors = set(), [f'cannot read the nft set fenced: {e}']
        errors += [f'{a} is in the nft set fenced with no fence tag (storage-fencer restore-set removes it once every fence tag is valid)'
                   for a in sorted(stale)]
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
    try:
        r = host.run(['systemctl', 'start', 'rtslib-fb-targetctl'], check=False, timeout=300)
    except subprocess.TimeoutExpired:
        return ['systemctl start rtslib-fb-targetctl gave no answer in 300 s']
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
        out.append({'id': a.id, 'size_bytes': size, 'iqn': lio.iqn(a.id)})
    return out


def _write_metrics(d, vgs, cluster, lio_, ok):
    samples = [('storage_vg_present', {'vg': n}, int(v.present)) for n, v in vgs.items()]
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


def _hold_line(d, cluster):
    if cluster is not None and HOLD_CREATE in cluster.holds():
        return f"hold_create set, {sum(r.reason == 'hold-create' for r in d.refusals)} creates refused"


def apply(volumes, cfg, sha, dry_run):
    if dry_run:  # reads only: vgs and lvs, no lock, no checkpoint, no metrics, no save
        vgs, cluster = _state(cfg)
        d = plan.decide(volumes, vgs, cluster, cfg['max_removals'])
        print(json.dumps(_report(sha, d, _exported(d, vgs, set(), True), [])))
        if line := _hold_line(d, cluster):
            host.log(line)
        # A VG that cannot be read blocks even with nothing to plan (an empty inventory makes no vg-missing).
        return 1 if BLOCKING & {r.reason for r in d.refusals} or not all(v.present for v in vgs.values()) else 0
    errors, failed = [], set()
    with host.LIO_LOCK:  # every LVM, LIO and nft mutation and the save; released before systemctl start
        vgs, cluster = _state(cfg)
        d = plan.decide(volumes, vgs, cluster, cfg['max_removals'])
        fenced = cluster.fenced() if cluster else {}
        errors += _fence_set_errors(cfg, fenced, tags_read=cluster is not None)
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
    for n, v in vgs.items():
        if v.present and not final[n].present:  # report and metrics keep the first read, the run fails
            errors.append(f'final read: VG {n} unreadable')
            final[n] = v
    ok = not (d.fail or d.refusals or errors)
    _write_metrics(d, final, cluster, lio_, ok)
    if line := _hold_line(d, cluster):
        host.log(line)
    for r in d.refusals:
        host.log(f'refused: {r.id}: {r.reason}')
    print(json.dumps(_report(sha, d, _exported(d, final, failed, False), errors)))
    return 0 if ok else 1


def _fenced_tags(cfg, retained=None):
    """(fenced node -> boot id, errors) from the retained VG tags (read alone unless given); valid fence tags survive
    a bad one."""
    retained = retained if retained is not None else lvm.vg_tags_only('retained')
    if not retained.present:
        return {}, ['cannot read the fence tags from VG retained']
    c = Cluster.parse(retained.tags, 'retained')
    return c.fenced(), c.errors


def restore_set(cfg):
    """Make the nft set match the VG fence tags (reads VG tags only); 0 ok, 1 on any error.

    Adds every tagged address, then removes untagged ones, but only when every tag was read (fail closed). A fence
    tags before it adds the address and an unfence removes the address before the tag, so no crash point leaves a
    needed element untagged; a stale one (a reload of an old nftables.conf) would re-cut an unfenced worker."""
    with host.LIO_LOCK:
        fenced, errors = _fenced_tags(cfg)
        addrs = {w['name']: w['bridge_address'] for w in cfg['workers']}
        want = set()
        # Add every resolvable address first: a stale tag must not leave a known fenced worker admitted.
        for node in sorted(fenced):
            if node not in addrs:
                errors.append(f'fenced node {node} is not a configured worker')
            else:
                nft.ensure_fenced(addrs[node])
                want.add(addrs[node])
        if errors:
            errors.append('kept every element of the nft set fenced: the fence tags are not fully known')
        else:
            for addr in sorted(nft.elements() - want):
                host.log(f'restore-set: {addr} has no fence tag; removing it from the nft set fenced')
                nft.remove_fenced(addr)
    for e in errors:
        host.log(f'error: {e}')
    return 1 if errors else 0


def boot_check(cfg):
    """After targetctl restore: drop fenced workers' ACLs, save, then fail if a ledgered target is not restored."""
    with host.LIO_LOCK:
        vgs = lvm.vgs()
        fenced, errors = _fenced_tags(cfg, vgs['retained'])
        errors = [f'{e}: only the valid fence tags were applied' for e in errors]
        lio_ = lio.Lio()
        for node in sorted(fenced):
            lio_.delete_acl(lio.worker_iqn(node, cfg['initiator_prefix']))
        lio_.save()
        for n, v in vgs.items():
            if not v.present:
                errors.append(f'VG {n} is missing')
                continue
            for id in sorted(v.ledger.created_ids()):
                if lio_.lun0_device(id) != f'/dev/{n}/{id}':
                    errors.append(f'{id}: LUN 0 is not bound to /dev/{n}/{id}')
    for e in errors:
        host.log(f'error: {e}')
    # StorageBootCheckFailed: 0 here, or a timestamp older than the boot (this check never ran), alerts.
    metrics.write('storage-boot-check', [('storage_boot_check_ok', {}, 0 if errors else 1),
                                         ('storage_boot_check_timestamp_seconds', {}, time.time())])
    return 1 if errors else 0


def _pending(vgs, cluster, cfg, lio_):
    """pending-delete LVs past quarantine as {id, vg, since, ...}; nothing at all when the holds cannot be read or hold_create is set."""
    if cluster is None or HOLD_CREATE in cluster.holds() or not all(v.present for v in vgs.values()):
        return []  # nothing expires while a VG cannot be read
    out = []
    for n, v in vgs.items():
        for id, lv in sorted(v.lvs.items()):
            since = [int(t[len(plan.PENDING_PREFIX):]) for t in lv.tags if t.startswith(plan.PENDING_PREFIX)]
            if not since or time.time() - max(since) <= cfg['quarantine_seconds']:  # the newest tag wins
                continue
            try:
                busy = bool(lio_.sessions(id))
            except lio.InUse:
                busy = True
            out.append({'id': id, 'vg': n, 'since': max(since), 'volume_handle': id, 'iqn': lio.iqn(id), 'sessions': busy})
    return out


def expire_candidates(cfg):
    with host.LIO_LOCK:
        vgs, cluster = _state(cfg)
        print(json.dumps(_pending(vgs, cluster, cfg, lio.Lio())))
    return 0


def expire(cfg, id):
    """Re-check age, hold and sessions under the lock, then remove the target, the data and the ledger; any error is 1."""
    try:
        with host.LIO_LOCK:
            vgs, cluster = _state(cfg)
            lio_ = lio.Lio()
            c = next((c for c in _pending(vgs, cluster, cfg, lio_) if c['id'] == id), None)
            if c is None:
                raise RuntimeError(f'{id}: not a pending-delete volume past quarantine, or creation is held')
            if c['sessions']:
                raise RuntimeError(f'{id}: has an iSCSI session, or sessions cannot be read')
            lio_.delete_target(id)
            lio_.save()
            # No -f: it would drop the O_EXCL open that fails on a still-mounted or held LV. host.run gives no tty,
            # so the signature probe only warns.
            host.run(['blkdiscard', f'/dev/{c["vg"]}/{id}'])
            lvm.remove(c['vg'], id)
            lvm.vg_tags(c['vg'], delete=[t for t in vgs[c['vg']].tags if t.startswith(f'ledger:{id}:')],
                        add=[tag_tombstone(id)])
        metrics.drop(METRICS, id)  # the deleted id's series; the next reconcile rewrites the rest
    except Exception as e:
        host.log(f'error: expire {id}: {e}')
        return 1
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog='storage-reconcile')
    sub = p.add_subparsers(dest='cmd', required=True)
    v = sub.add_parser('validate')
    v.add_argument('--inventory', required=True)
    cmds = {n: sub.add_parser(n) for n in ('apply', 'boot-check', 'expire-candidates', 'activate', 'expire')}
    for c in cmds.values():
        c.add_argument('--config', default=str(config.PATH))  # read at call time: tests patch config.PATH
    cmds['apply'].add_argument('--inventory', required=True)
    cmds['apply'].add_argument('--dry-run', action='store_true')
    cmds['activate'].add_argument('--vg', required=True, choices=lvm.VG_NAMES)
    cmds['expire'].add_argument('--id', required=True)
    args = p.parse_args(argv)
    try:
        if args.cmd in ('boot-check', 'expire-candidates', 'expire', 'activate'):
            cfg = config.load(args.config)
            host.CONFIG = cfg
            if args.cmd == 'activate':  # the boot drop-in's regen line: bounded, never killed (ADR 0017 F11)
                lvm.activate(args.vg)
                return 0
            if args.cmd == 'expire':
                return expire(cfg, args.id)
            return boot_check(cfg) if args.cmd == 'boot-check' else expire_candidates(cfg)
        raw = Path(args.inventory).read_bytes()
        volumes = inventory.load(raw.decode())
        if args.cmd == 'validate':
            print(f'ok: {len(volumes)} volumes')
            return 0
        cfg = config.load(args.config)
        host.CONFIG = cfg
        return apply(volumes, cfg, hashlib.sha256(raw).hexdigest(), args.dry_run)
    except (inventory.InventoryError, config.ConfigError, OSError, UnicodeDecodeError,
            subprocess.SubprocessError) as e:
        host.log(f'error: {e}')
        return 1

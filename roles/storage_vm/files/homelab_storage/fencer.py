"""storage-fencer: cut a hung worker off at the storage side, prove it, and only then taint it (ADR 0017).

run                                     the loop, every loop_seconds; SIGTERM is honoured between loops
fence NODE                              manual fence: steps 1-5 with bootID unknown, no taint; prints the proof
unfence NODE [--confirm-stopped]        manual unfence; a bootID-unknown tag needs --confirm-stopped
prove NODE                              read-only proof as JSON; exit 1 if any leg fails
restore-set                             re-add every tagged worker's address to the nft set
status                                  fence tags, holds and bootID history as JSON

Every step is idempotent and the VG tag is the durable record, so a crash in a fence is finished by the next loop.
A fence tag starts unsettled (`fenced:<node>:unknown`) and is settled on the bootID of the first API read after the
storage is proven cut; each loop proves every tagged worker before it reads the API. A crash in an unfence after the
ACL is back is seen as a fence that fell off: the worker is cut again and needs one more reboot.
"""
import argparse
import json
import signal
import subprocess
import threading
import time

from . import config, fence_policy, host, kube, lio, lvm, metrics, nft, reconcile
from .ledger import FENCED, Cluster, tag_fenced

METRICS = 'storage-fencer'
NODES = '/api/v1/nodes'
VAS = '/apis/storage.k8s.io/v1/volumeattachments'
POLL_SECONDS = 1


def established(addr):
    """iSCSI connections from `addr` that are still open on this VM."""
    out = host.run(['ss', '-Htn', 'state', 'established', '( sport = :3260 )', 'dst', addr]).stdout
    return [line for line in out.splitlines() if line.strip()]


def proven(proof):
    return not any(proof['acl_present'].values()) and proof['addr_in_set'] and not proof['established']


def _foreign_va(vas, name, driver):
    return any(a['spec'].get('nodeName') == name and a['spec'].get('attacher') != driver for a in vas)


class Fencer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.addrs = {w['name']: w['bridge_address'] for w in cfg['workers']}
        self.api = kube.Client(cfg['api_url'], cfg['token_file'], cfg['ca_file'])
        self.failures = 0
        self.api_errors = {'auth': 0, 'unreachable': 0}
        self.last_api_success = 0  # 0 until the first success, so StorageFencerBlind sees a series from the start
        self.active = set()
        self.started = False  # until a loop reaches its decision: a restart is a gap this process did not watch

    def worker(self, name):
        if name not in self.addrs:
            raise ValueError(f'{name} is not a configured worker')
        return self.addrs[name]

    def cluster(self):
        retained = lvm.vgs(tuple(self.cfg['vgs']))['retained']
        if not retained.present:
            raise RuntimeError('VG retained is missing: cannot read the fence tags')
        return Cluster.parse(retained.tags, 'retained')  # bad fence tags land in .errors; the valid ones still count

    def proof(self, lio_, name):
        addr, iqn = self.worker(name), lio.worker_iqn(name)
        return {'acl_present': {t: iqn in lio_.acl_iqns(t) for t in sorted(lio_.targets())},
                'addr_in_set': nft.is_fenced(addr), 'established': established(addr)}

    def set_taint(self, name, present):
        n = self.api.get(f'{NODES}/{name}')[0]
        taints = kube.with_out_of_service(n, present)
        if taints != ((n.get('spec') or {}).get('taints') or []):
            self.api.patch_node_taints(name, taints, n['metadata']['resourceVersion'])
            host.log(f'{"tainted" if present else "untainted"} {name}')

    # The step functions run with host.LIO_LOCK held by the caller.

    def fence_steps(self, lio_, name, tagged, taint):
        """Fence steps 1-6 (6 only if `taint`); returns the proof. Steps 5 and 6 run only after a passing proof."""
        addr = self.worker(name)
        if not tagged:
            lvm.vg_tags('retained', add=[tag_fenced(name, fence_policy.UNKNOWN)])
            host.log(f'fence {name}: tagged, bootID not settled yet')
        host.checkpoint('fence:1')
        nft.ensure_fenced(addr)
        host.checkpoint('fence:2')
        for s in lio_.delete_acl(lio.worker_iqn(name)):
            host.log(f'fence {name}: deleted ACL {s["target"]}: {s["info"]}')
        host.checkpoint('fence:3')
        deadline = time.monotonic() + self.cfg['proof_timeout_seconds']
        while not proven(p := self.proof(lio_, name)) and time.monotonic() < deadline:
            time.sleep(POLL_SECONDS)
        if not proven(p):
            host.log(f'error: fence {name}: proof failed, no taint: {json.dumps(p)}')
            return p
        host.log(f'fence {name}: proven')
        host.checkpoint('fence:4')
        lio_.save()
        host.checkpoint('fence:5')
        if taint:
            self.set_taint(name, True)
            host.checkpoint('fence:6')
        return p

    def unfence_steps(self, lio_, name, bootid):
        """Taint first (an API error reopens nothing), then ACLs, address, save, and the tag last."""
        addr = self.worker(name)
        self.set_taint(name, False)
        host.checkpoint('unfence:1')
        lio_.add_acl(lio.worker_iqn(name))
        host.checkpoint('unfence:2')
        nft.remove_fenced(addr)
        host.checkpoint('unfence:3')
        lio_.save()
        host.checkpoint('unfence:4')
        lvm.vg_tags('retained', delete=[tag_fenced(name, bootid)])
        host.log(f'unfenced {name}')
        host.checkpoint('unfence:5')

    def _api_error(self, e):
        self.api_errors['auth' if isinstance(e, kube.ApiAuthError) else 'unreachable'] += 1
        host.log(f'error: API: {e}')

    def _attempt(self, what, fn):
        try:
            if fn() is False:
                self.failures += 1
        except kube.ApiError as e:
            self._api_error(e)
            self.failures += 1
        except Exception as e:
            host.log(f'error: {what}: {e}')
            self.failures += 1

    def loop_once(self):
        try:
            # The API is read after the lock wait (a reconcile or a proof poll can hold it for minutes), so the
            # decision never acts on a reading older than the lock wait.
            with host.LIO_LOCK:  # one-at-a-time and the tags hold for the whole decision
                try:
                    # Proved before the read: any boot that could have mounted had posted its bootID by then.
                    opened = self._opening_proofs(lio.Lio())
                    try:
                        nodes, server_time = self.api.get(NODES)
                        vas = self.api.get(VAS)[0]['items']
                    except kube.ApiError as e:
                        self._api_error(e)  # blind: no lio write, nft write or taint call
                        return
                    self.last_api_success = int(time.time())
                    self._decide_and_act(nodes['items'], vas, server_time, opened)
                except Exception as e:
                    host.log(f'error: loop: {e}')
                    self.failures += 1
        finally:
            self.write_metrics()

    def _opening_proofs(self, lio_):
        """{tagged worker: True (still cut) | False (not cut) | None (the proof failed to run)}."""
        tagged = sorted(n for n in self.cluster().fenced() if n in self.addrs)
        if not self.started:
            return dict.fromkeys(tagged, False)  # nothing saw the gap before this process
        opened = {}
        for name in tagged:
            try:
                opened[name] = proven(self.proof(lio_, name))
            except Exception as e:
                host.log(f'error: fence {name}: opening proof: {e}')
                opened[name] = None
        return opened

    def _settle(self, cluster, by_name, opened):
        """Un-settle a tag whose cut fell off; settle an unknown one on the bootID read after a passing proof."""
        unknown = fence_policy.UNKNOWN
        for name, tagged in sorted(cluster.fenced().items()):
            if name not in self.addrs:
                continue
            b = fence_policy._boot_id(by_name[name]) if name in by_name else unknown
            if tagged != unknown and opened.get(name) is False:
                new, msg = unknown, 'unsettled'
            elif tagged == unknown and opened.get(name) is True and not cluster.errors and b != unknown and \
                    FENCED.match(tag_fenced(name, b)):
                new, msg = b, f'settled on bootID {b}'
            else:
                continue

            def retag():  # one vgchange: never zero tags, never two
                lvm.vg_tags('retained', add=[tag_fenced(name, new)], delete=[tag_fenced(name, tagged)])
                host.log(f'fence {name}: {msg}')
            self._attempt(f'fence {name}', retag)
        return self.cluster()

    def _decide_and_act(self, nodes, vas, server_time, opened):
        by_name = {n['metadata']['name']: n for n in nodes if n['metadata']['name'] in self.addrs}
        cluster = self._settle(self.cluster(), by_name, opened)
        self.started = True
        fenced = cluster.fenced()
        history = fence_policy.BootHistory.load(fence_policy.HISTORY_PATH)
        v = fence_policy.evaluate(nodes, vas, cluster, history, self.cfg, server_time, opened)
        if cluster.errors:
            # Re-apply the valid fences only: a bad tag may stand for a fenced worker, so nothing is relaxed or added.
            for e in cluster.errors:
                host.log(f'error: {e}: no new fence or unfence until the tags are fixed')
            self.failures += 1
            v.continue_fence, v.fence, v.unfence = sorted(v.continue_fence + v.unfence), None, []
        lio_ = lio.Lio()
        for name in v.continue_fence:
            # Steps 2-6 again, holds or not (a hold only blocks new fences); a foreign VA still blocks the taint, which
            # would force-detach a disk this fence proves nothing about (ADR 0017 "It is level-triggered").
            taint = not _foreign_va(vas, name, self.cfg['csi_driver'])
            self._attempt(f'fence {name}', lambda: proven(self.fence_steps(lio_, name, True, taint)))
        if v.fence:
            host.log(f'fence {v.fence}: NotReady past {self.cfg["not_ready_seconds"]} s')
            self._attempt(f'fence {v.fence}', lambda: proven(self.fence_steps(lio_, v.fence, False, True)))
        for name in v.unfence:
            self._attempt(f'unfence {name}', lambda: self.unfence_steps(lio_, name, fenced[name]))
        for name, reason in sorted(v.reasons.items()):
            host.log(f'{name}: {reason}')
        self.active = set(self.cluster().fenced())
        for name, n in sorted(by_name.items()):
            if name not in self.active:  # any Ready state; a fenced worker's history is frozen
                history.record(name, fence_policy._boot_id(n))
        history.save(fence_policy.HISTORY_PATH)

    def write_metrics(self):
        samples = [('storage_fence_active', {'node': w}, int(w in self.active)) for w in sorted(self.addrs)]
        samples += [('storage_fence_failures_total', {}, self.failures),
                    ('storage_fencer_last_api_success_timestamp_seconds', {}, self.last_api_success)]
        samples += [('storage_fencer_api_errors_total', {'code': c}, n) for c, n in sorted(self.api_errors.items())]
        metrics.write(METRICS, samples)

    def run(self):
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())  # a fence in progress finishes first
        while not stop.is_set():
            self.loop_once()
            stop.wait(self.cfg['loop_seconds'])
        return 0

    # Manual commands. They never write the metrics textfile: its counters belong to the running loop.

    def manual_fence(self, name):
        """No API read: the tag is unknown (or an existing one is kept), and a running loop settles it."""
        self.worker(name)
        with host.LIO_LOCK:
            fenced = self.cluster().fenced()
            if name in fenced:
                host.log(f'fence {name}: already tagged with bootID {fenced[name]}; keeping that tag')
            p = self.fence_steps(lio.Lio(), name, name in fenced, taint=False)
        print(json.dumps(p))
        return 0 if proven(p) else 1

    def manual_unfence(self, name, confirm_stopped):
        self.worker(name)
        with host.LIO_LOCK:
            cluster = self.cluster()
            if cluster.errors:
                raise ValueError('; '.join(cluster.errors) + ': fix the fence tags on VG retained before an unfence')
            fenced = cluster.fenced()
            if name not in fenced:
                print(f'{name} is not fenced')
                return 0
            if fenced[name] == fence_policy.UNKNOWN and not confirm_stopped:
                print(f'{name} was fenced with bootID unknown. On the Proxmox host run `qm status <vmid> --verbose` '
                      'and check it says stopped (a paused VM shows running with qmpstatus: paused), then rerun with '
                      '--confirm-stopped.')
                return 1
            self.unfence_steps(lio.Lio(), name, fenced[name])
        return 0

    def prove(self, name):
        p = self.proof(lio.Lio(), name)
        print(json.dumps(p))
        return 0 if proven(p) else 1

    def status(self):
        cluster = self.cluster()
        history = fence_policy.BootHistory.load(fence_policy.HISTORY_PATH)
        print(json.dumps({'fenced': cluster.fenced(), 'holds': sorted(cluster.holds()), 'history': history._data,
                          **({'errors': cluster.errors} if cluster.errors else {})}))
        return 1 if cluster.errors else 0


def main(argv=None):
    p = argparse.ArgumentParser(prog='storage-fencer')
    p.add_argument('--config', default=str(config.PATH))
    sub = p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('run')
    sub.add_parser('fence').add_argument('node')
    u = sub.add_parser('unfence')
    u.add_argument('node')
    u.add_argument('--confirm-stopped', action='store_true')
    sub.add_parser('prove').add_argument('node')
    sub.add_parser('restore-set')
    sub.add_parser('status')
    args = p.parse_args(argv)
    try:
        cfg = config.load(args.config)
        host.CONFIG = cfg
        if args.cmd == 'restore-set':
            return reconcile.restore_set(cfg)
        fz = Fencer(cfg)
        if args.cmd == 'run':
            return fz.run()
        if args.cmd == 'fence':
            return fz.manual_fence(args.node)
        if args.cmd == 'unfence':
            return fz.manual_unfence(args.node, args.confirm_stopped)
        if args.cmd == 'prove':
            return fz.prove(args.node)
        return fz.status()
    except (config.ConfigError, kube.ApiError, OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as e:
        host.log(f'error: {e}')
        return 1

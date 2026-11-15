"""Pure reconcile decisions: inventory + per-VG ledger/LVs + cluster tags -> actions and refusals."""
from dataclasses import dataclass, field

from .ledger import HOLD_CREATE
from .inventory import vg_for

PENDING_PREFIX = 'pending-delete:'


@dataclass(frozen=True)
class LvState:
    size_bytes: int
    tags: tuple = ()


@dataclass(frozen=True)
class VgState:
    ledger: object  # ledger.Ledger
    lvs: dict = field(default_factory=dict)  # id -> LvState
    present: bool = True


@dataclass(frozen=True)
class Action:
    kind: str  # create_new | create_raw | export | extend | mark_pending | clear_pending
    id: str
    vg: str
    size_bytes: int = 0


@dataclass(frozen=True)
class Refusal:
    id: str
    reason: str


@dataclass
class Decision:
    actions: list = field(default_factory=list)
    refusals: list = field(default_factory=list)
    fail: bool = False


def _pending(lv):
    return any(t.startswith(PENDING_PREFIX) for t in lv.tags)


def _entry(v, vg, other, other_name):
    """Return (actions, refusal_reason|None, fail) for one inventory entry."""
    led, lv = vg.ledger, vg.lvs.get(v.id)
    if other.ledger.state(v.id) or other.ledger.tombstoned(v.id) or v.id in other.lvs:
        # A live target in the other VG stays exported; only mutations are refused.
        live = other.ledger.state(v.id) == 'created' and v.id in other.lvs
        return [Action('export', v.id, other_name)] if live else [], 'other-vg', False
    if led.tombstoned(v.id):
        return [], 'tombstoned', False
    state = led.state(v.id)
    if state == 'creating':
        return [], 'crashed-create', False
    if state is None:
        if lv:
            return [], 'orphan', False
        return [Action('create_new' if v.origin == 'new' else 'create_raw', v.id, vg_for(v.cls), v.size_bytes)], None, False
    if lv is None:
        return [], 'lv-missing', True
    if v.size_bytes < lv.size_bytes:
        return [Action('export', v.id, vg_for(v.cls))], 'shrink', False
    name = vg_for(v.cls)
    acts = [Action('export', v.id, name)]
    if _pending(lv):
        acts.append(Action('clear_pending', v.id, name))
    if v.size_bytes > lv.size_bytes:
        acts.append(Action('extend', v.id, name, v.size_bytes))
    return acts, None, False


def decide(volumes, vgs, cluster, removed_ok=2):
    d = Decision()
    if cluster is None or not all(vgs.get(n) and vgs[n].present for n in ('retained', 'regen')):
        d.refusals = [Refusal(v.id, 'vg-missing') for v in volumes]
        d.fail = True
        return d
    hold = HOLD_CREATE in cluster.holds()
    ids = {v.id for v in volumes}
    for v in volumes:
        name = vg_for(v.cls)
        oname = "regen" if name == "retained" else "retained"
        acts, reason, fail = _entry(v, vgs[name], vgs[oname], oname)
        d.fail |= fail
        if reason:
            d.refusals.append(Refusal(v.id, reason))
            d.actions += acts
            continue
        if hold:
            kept = [a for a in acts if a.kind == 'export']
            if not kept and acts[0].kind.startswith('create'):
                d.refusals.append(Refusal(v.id, 'hold-create'))
            acts = kept
        d.actions += acts
    # Removed entries: the target stays exported; the LV gets a pending-delete tag.
    removed = []
    for name, vg in vgs.items():
        for lid in sorted(vg.ledger.created_ids() - ids - vg.lvs.keys()):
            d.refusals.append(Refusal(lid, 'lv-missing'))
            d.fail = True
        for lid, lv in vg.lvs.items():
            if lid in ids:
                continue
            state = vg.ledger.state(lid)
            if state == 'created':
                d.actions.append(Action('export', lid, name))
                if not _pending(lv):
                    removed.append(Action('mark_pending', lid, name))
            else:
                d.refusals.append(Refusal(lid, 'crashed-create' if state == 'creating' else 'orphan'))
    if hold:
        d.fail = True
        return d
    if len(removed) > removed_ok:
        d.actions = [a for a in d.actions if a.kind == 'export']  # refuse mutations, keep live targets exported
        d.refusals += [Refusal(v.id, 'removal-brake') for v in volumes if all(r.id != v.id for r in d.refusals)]
        d.refusals += [Refusal(a.id, 'removal-brake') for a in removed]
        d.fail = True
    else:
        d.actions += removed
    return d

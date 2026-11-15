"""A narrow wrapper over rtslib-fb (imported lazily, so CI runs without it): one iSCSI target per volume."""
import os
import uuid
from pathlib import Path

TARGET_PREFIX = 'iqn.2026-11.net.starktastic.storage:'
WORKER_PREFIX = 'iqn.2026-11.net.starktastic:'
SAVE_FILE = Path('/etc/rtslib-fb-target/saveconfig.json')
CONFIGFS = Path('/sys/kernel/config/target/iscsi')
TPG_ATTRIBUTES = {'authentication': '0', 'generate_node_acls': '0', 'cache_dynamic_acls': '0', 'demo_mode_discovery': '0'}


class InUse(Exception):
    """A session exists, or could not be ruled out."""


def iqn(id):
    return TARGET_PREFIX + id


def worker_iqn(name):
    return WORKER_PREFIX + name  # must equal the IQN k3s_common writes on the node


def wwn(id):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, iqn(id)))  # T10 serial: a rebuilt VM presents the same identity


class Lio:
    def __init__(self):
        import rtslib_fb
        self._rts = rtslib_fb
        self._root = rtslib_fb.RTSRoot()

    def _targets(self):
        return [t for t in self._rts.FabricModule('iscsi').targets if t.wwn.startswith(TARGET_PREFIX)]

    def _target(self, id):
        return next((t for t in self._targets() if t.wwn == iqn(id)), None)

    def _tpg(self, id):
        t = self._target(id)
        return next((g for g in t.tpgs if g.tag == 1), None) if t else None

    def ensure_target(self, id, dev, acl_iqns, portal):
        """Make the target exactly: LUN 0 on `dev`, one portal, and ACLs for `acl_iqns` only."""
        r = self._rts
        so = next((s for s in self._root.storage_objects if s.plugin == 'block' and s.name == id), None)
        if so is None:
            so = r.BlockStorageObject(id, dev=dev, wwn=wwn(id))
        elif so.udev_path != dev:
            raise RuntimeError(f'backstore {id} is bound to {so.udev_path}, not {dev}')
        if so.get_attribute('emulate_tpu') != '1':
            so.set_attribute('emulate_tpu', '1')
        tpg = r.TPG(r.Target(r.FabricModule('iscsi'), iqn(id)), 1)
        for k, v in TPG_ATTRIBUTES.items():
            if tpg.get_attribute(k) != v:
                tpg.set_attribute(k, v)
        lun = next((l for l in tpg.luns if l.lun == 0), None)
        if lun is None:
            lun = r.LUN(tpg, 0, so)
        elif lun.storage_object.name != id:
            raise RuntimeError(f'target {id} LUN 0 is {lun.storage_object.name}')
        ip, port = portal.rsplit(':', 1)
        for p in list(tpg.network_portals):
            if (p.ip_address, p.port) != (ip, int(port)):
                p.delete()
        if not any(True for _ in tpg.network_portals):
            r.NetworkPortal(tpg, ip, int(port))
        want = set(acl_iqns)
        for acl in list(tpg.node_acls):
            if acl.node_wwn not in want:
                acl.delete()  # synchronous: LIO closes that initiator's sessions
        for acl_iqn in sorted(want):
            acl = r.NodeACL(tpg, acl_iqn)
            if not any(m.mapped_lun == 0 for m in acl.mapped_luns):
                r.MappedLUN(acl, 0, lun)
        if not tpg.enable:
            tpg.enable = True

    def delete_target(self, id):
        if t := self._target(id):
            t.delete()
        for so in self._root.storage_objects:
            if so.plugin == 'block' and so.name == id:
                so.delete()

    def delete_acl(self, acl_iqn):
        """Delete `acl_iqn` from every target; returns each ACL's session info, read before the delete."""
        out = []
        for t in self._targets():
            for tpg in t.tpgs:
                for acl in list(tpg.node_acls):
                    if acl.node_wwn == acl_iqn:
                        try:
                            info = Path(acl.path, 'info').read_text()
                        except OSError as e:
                            info = f'unreadable: {e}'
                        out.append({'target': t.wwn, 'info': info.strip()})
                        acl.delete()
        return out

    def acl_iqns(self, id):
        tpg = self._tpg(id)
        return {a.node_wwn for a in tpg.node_acls} if tpg else set()

    def lun0_device(self, id):
        tpg = self._tpg(id)
        lun = next((l for l in tpg.luns if l.lun == 0), None) if tpg else None
        return lun.storage_object.udev_path if lun else None

    def sessions(self, id):
        """Initiators logged in to the target, read from configfs (rtslib's NodeACL.session hides read errors)."""
        try:
            tpgs = [p for p in (CONFIGFS / iqn(id)).iterdir() if p.name.startswith('tpgt_')]
            if not tpgs:
                raise InUse(f'{id}: no TPG in configfs')
            found = []
            for tpg in tpgs:
                for acl in (tpg / 'acls').iterdir():
                    if not (acl / 'info').read_text().startswith('No active iSCSI Session'):
                        found.append(acl.name)
                found += (tpg / 'dynamic_sessions').read_text().split()
            return found
        except OSError as e:
            raise InUse(f'{id}: cannot read sessions: {e}')

    def targets(self):
        return {t.wwn[len(TARGET_PREFIX):] for t in self._targets()}

    def save(self):
        """Write through a temp file and rename, so a crash never leaves a half-written save file."""
        tmp = SAVE_FILE.with_name(SAVE_FILE.name + '.tmp')
        try:
            self._root.save_to_file(str(tmp))
            fd = os.open(tmp, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, SAVE_FILE)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        fd = os.open(SAVE_FILE.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

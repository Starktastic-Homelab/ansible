"""Pure evidence checks for native Ansible worker Node retirement; no I/O."""
import json
import re
from uuid import UUID


def _uuid(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-fA-F-]{36}', value):
        raise ValueError('Missing or malformed generation UUID')
    result = UUID(value)
    if not result.int or str(result) != value.lower():
        raise ValueError('Invalid generation UUID')
    return str(result)


def candidate(nodes, name, guest_uuid):
    """Return the stale generation, never treat missing identity as absence."""
    guest_uuid = _uuid(guest_uuid)
    if not isinstance(nodes, list) or len(nodes) > 1:
        raise ValueError('Expected zero or one Kubernetes Node')
    if not nodes:
        return {}
    node = nodes[0]
    metadata = node.get('metadata', {})
    if metadata.get('name') != name:
        raise ValueError('Kubernetes Node name mismatch')
    uid = _uuid(metadata.get('uid'))
    old_uuid = _uuid(node.get('status', {}).get('nodeInfo', {}).get('systemUUID'))
    return {} if old_uuid == guest_uuid else dict(name=name, uid=uid, uuid=old_uuid)


def complete_visibility(permissions, acls):
    """Check global visibility AND overrides that an effective map can omit."""
    if (not isinstance(permissions, dict) or not isinstance(acls, list)
            or any(not isinstance(k, str) or not isinstance(v, dict) for k, v in permissions.items())):
        raise ValueError('Malformed Proxmox permissions or ACL response')
    if (permissions.get('/vms', {}).get('VM.Audit') != 1
            or permissions.get('/pool', {}).get('Pool.Audit') != 1
            or permissions.get('/access', {}).get('Sys.Audit') not in (0, 1)):
        raise ValueError('Complete inventory requires inherited VM.Audit and Pool.Audit, plus Sys.Audit on /access')
    paths = set(permissions)
    for acl in acls:
        if not isinstance(acl, dict) or not isinstance(acl.get('path'), str):
            raise ValueError('Malformed Proxmox ACL path')
        paths.add(acl['path'])
    for path in paths:
        if path.startswith('/vms/') and permissions.get(path, {}).get('VM.Audit') not in (0, 1):
            raise ValueError('A VM permission override prevents complete inventory')
        if path.startswith('/pool/') and permissions.get(path, {}).get('Pool.Audit') != 1:
            raise ValueError('A pool permission override prevents complete inventory')
    return True


def _resources(resources):
    if not isinstance(resources, list):
        raise ValueError('Malformed VM resource inventory')
    result = {}
    for resource in resources:
        if not isinstance(resource, dict):
            raise ValueError('Malformed VM resource')
        vmid = resource.get('vmid')
        if (not isinstance(vmid, int) or isinstance(vmid, bool) or vmid < 100 or vmid in result
                or resource.get('type') not in ('qemu', 'lxc')
                or not all(isinstance(resource.get(k), str) and resource[k] for k in ('node', 'name'))):
            raise ValueError('Incomplete or duplicate VM resource identity')
        result[vmid] = {k: resource.get(k) for k in ('vmid', 'type', 'node', 'name', 'template')}
    return result


def verified(stale, expected, evidence):
    """Authorize only an exact Node UID after complete, stable read evidence."""
    complete_visibility(evidence['permissions_before'], evidence['acls_before'])
    complete_visibility(evidence['permissions_after'], evidence['acls_after'])
    if evidence['permissions_before'] != evidence['permissions_after']:
        raise ValueError('Proxmox permissions changed during verification')
    if sorted(map(lambda a: json.dumps(a, sort_keys=True), evidence['acls_before'])) != sorted(
            map(lambda a: json.dumps(a, sort_keys=True), evidence['acls_after'])):
        raise ValueError('Proxmox ACLs changed during verification')
    before = _resources(evidence['resources_before'])
    if before != _resources(evidence['resources_after']):
        raise ValueError('VM inventory changed during verification')
    old_uuid, new_uuid = _uuid(stale['uuid']), _uuid(expected['uuid'])
    if stale['name'] != expected['name'] or old_uuid == new_uuid:
        raise ValueError('Replacement generation is not distinct')
    if not isinstance(evidence['configs'], list):
        raise ValueError('Malformed VM configuration inventory')
    configs = {}
    uuids = set()
    for response in evidence['configs']:
        resource = response['item']
        vmid = resource['vmid']
        config = response['json']['data']
        if (vmid in configs or before.get(vmid) != _resources([resource])[vmid]
                or resource['type'] != 'qemu' or config.get('name') != resource['name'] or config.get('lock')):
            raise ValueError('Incomplete, changed or locked VM configuration')
        fields = [s.split('=', 1)[1] for s in config.get('smbios1', '').split(',') if s.startswith('uuid=')]
        if len(fields) != 1:
            raise ValueError('VM config lacks one unambiguous SMBIOS UUID')
        generation = _uuid(fields[0])
        if generation == old_uuid:
            raise ValueError('Old VM generation still exists; refuse Node retirement')
        if generation in uuids:
            raise ValueError('Duplicate VM generation UUID; refuse Node retirement')
        uuids.add(generation)
        configs[vmid] = generation
    if set(configs) != {vmid for vmid, r in before.items() if r['type'] == 'qemu'}:
        raise ValueError('A VM configuration is missing from verification')
    vmid = expected['vmid']
    replacement = before.get(vmid, {})
    if (not isinstance(vmid, int) or isinstance(vmid, bool) or replacement.get('template') not in (None, 0)
            or replacement.get('name') != expected['name'] or replacement.get('node') != expected['node']
            or configs.get(vmid) != new_uuid):
        raise ValueError('Replacement VM does not match the SSH guest and inventory')
    return dict(name=stale['name'], uid=_uuid(stale['uid']))


class FilterModule:
    def filters(self):
        return {'k3s_retirement_candidate': candidate,
                'k3s_retirement_complete_visibility': complete_visibility,
                'k3s_retirement_resources': _resources,
                'k3s_retirement_verified': verified}

#!/usr/bin/env python3
"""Decide whether a storage workflow run may touch anything (ADR 0017), writing ready=true|false to GITHUB_OUTPUT.

A run skips, green, while the storage group is empty or storage_vm_pv_devices is still the placeholder, and a
scheduled run skips inside the backup windows (D29). Dispatch, manual and push runs are never window-skipped.
"""
from datetime import datetime, time, timezone
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'roles/storage_vm/files'))
from homelab_storage.lvm import VG_NAMES  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / 'inventory/proxmox.yml'
GROUP_VARS = ROOT / 'group_vars/storage/storage_vm.yml'
# The windows are local time, so DST moves them in UTC: never a cron hour list.
ZONE = ZoneInfo('Asia/Jerusalem')
SUNDAY = 6
# docs/backup-and-restore.md. backup and maint end when their unit has finished, which a runner container cannot
# see: 06:00 and 07:00 are provisional (D29, unanswered); confirm them from the healthchecks run durations.
# The :25-:50 snapshot windows are never hit by a :05 cron.
WINDOWS = (('dumps', None, time(1, 55), time(2, 15)), ('vzdump', SUNDAY, time(0, 55), time(1, 15)),
           ('backup', None, time(4, 25), time(6, 0)), ('maint', SUNDAY, time(5, 25), time(7, 0)))


def skip_reason(hosts, group_vars):
    if not hosts:
        return 'no host in the storage group'
    if len(hosts) > 1:
        sys.exit('more than one host in the storage group')  # a misconfiguration, never a skip
    pv = group_vars.get('storage_vm_pv_devices')
    if not (isinstance(pv, dict) and set(pv) == set(VG_NAMES)
            and all(isinstance(v, str) and v.startswith('/dev/') for v in pv.values())):
        return 'storage_vm_pv_devices is the placeholder'
    return None


def backup_window(now):
    """The name of the backup window that the aware datetime now falls in, or None."""
    local = now.astimezone(ZONE)
    for name, weekday, start, end in WINDOWS:
        if weekday in (None, local.weekday()) and start <= local.time() < end:
            return name
    return None


def storage_hosts():
    from ansible.inventory.manager import InventoryManager
    from ansible.parsing.dataloader import DataLoader
    groups = InventoryManager(loader=DataLoader(), sources=[str(INVENTORY)]).get_groups_dict()
    # An unparsed source (an API outage) only warns: an empty storage group must not hide it.
    if not groups['all']:
        sys.exit(f'{INVENTORY} parsed no hosts; refusing to skip')
    return groups.get('storage', [])


def main(hosts=None, event=None, now=None):
    event = os.environ.get('GITHUB_EVENT_NAME', '') if event is None else event
    window = backup_window(now or datetime.now(timezone.utc)) if event == 'schedule' else None
    if window:
        reason = f'inside backup window {window}; the next hourly run retries'
    else:
        group_vars = (yaml.safe_load(GROUP_VARS.read_text()) or {}) if GROUP_VARS.exists() else {}
        reason = skip_reason(storage_hosts() if hosts is None else hosts, group_vars)
        reason = reason and reason + '; nothing touched'
    print(f'Skipping: {reason}' if reason else 'Ready: the storage VM is configured')
    with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
        stream.write(f'ready={"false" if reason else "true"}\n')


if __name__ == '__main__':
    main()

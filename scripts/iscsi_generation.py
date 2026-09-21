#!/usr/bin/env python3
"""Validate reviewed worker enrollment. This record never authorizes an app writer."""
import json
import re
import sys


def validate(*, worker, iqns, current, review, existing_iqn, sessions, free_bytes):
    if not worker or not iqns or len(set(iqns.values())) != len(iqns):
        raise ValueError('Only workers with unique reviewed IQNs may enroll')
    if any(not re.fullmatch(r'iqn\.\d{4}-\d{2}\.[a-z0-9.-]+:[a-z0-9:.-]+', iqn) for iqn in iqns.values()):
        raise ValueError('Invalid or empty IQN')
    if (current.get('hostname') not in iqns or current.get('iqn') != iqns[current['hostname']]
            or not all(current.get(k) for k in ('vmid', 'smbios_uuid', 'storage_ip'))):
        raise ValueError('Unexpected node identity')
    if free_bytes < 25 * 1024**3:
        raise ValueError('Worker needs at least 25 GiB free before storage enrollment')
    if sessions < 0 or (sessions and existing_iqn != current['iqn']):
        raise ValueError('Active session prevents changing the initiator identity')
    if review.get('generation') != current or not review.get('reviewed_by'):
        raise ValueError('Current VM generation has no reviewed enrollment')
    old = review.get('previous')
    if old is None:
        if review.get('first_enrollment') is not True:
            raise ValueError('First enrollment requires explicit unused-IQN review')
    elif old != current:
        proof = review.get('retirement', {})
        if (proof.get('generation') != old or proof.get('kind') not in ('fenced', 'destroyed')
                or proof.get('verified') is not True or not proof.get('evidence') or not proof.get('reviewed_by')):
            raise ValueError('Old generation retirement has not been reviewed')
    return current


if __name__ == '__main__':
    print(json.dumps(validate(**json.load(sys.stdin))))

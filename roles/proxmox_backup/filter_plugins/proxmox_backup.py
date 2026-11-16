"""Pure checks for the host-side backup settings (vzdump job, TrueNAS drift); no I/O."""
import json

MISSING = '<missing>'


def _flag_args(options):
    return [arg for key, value in options.items() for arg in ('--' + key, str(value))]


def _job_value(value):
    # pvesh returns prune-backups as {"keep-last": "3"}; the CLI takes keep-last=3. Sort pairs so order never drifts.
    if isinstance(value, dict):
        value = ','.join(f'{k}={v}' for k, v in value.items())
    if isinstance(value, str) and '=' in value:
        return ','.join(sorted(value.split(',')))
    return None if value is None else str(value)


def _covers(other, vmids):
    if str(other.get('all', 0)) == '1' or not other.get('vmid'):
        return True
    return bool(vmids & set(str(other['vmid']).split(',')))


def vzdump_argv(jobs, job):
    """Return the pvesh argv that brings the job in line, or [] when it already matches."""
    live = next((j for j in jobs if j.get('id') == job['id']), None)
    if live is None:
        vmids = set(str(job['options']['vmid']).split(','))
        dupes = [j.get('id') for j in jobs if j.get('type', 'vzdump') == 'vzdump' and _covers(j, vmids)]
        if dupes:
            raise ValueError(f"vzdump job {job['id']} is absent but {dupes} already back up some of "
                             f"{sorted(vmids)}; delete or adopt that job (set proxmox_backup_vzdump_job.id) first")
        return ['pvesh', 'create', '/cluster/backup', '--id', job['id']] + _flag_args(job['options'])
    # enabled is set only on create, so a job disabled by hand in the UI stays disabled.
    drift = {k: v for k, v in job['options'].items() if k != 'enabled' and _job_value(live.get(k)) != _job_value(v)}
    return ['pvesh', 'set', '/cluster/backup/' + job['id']] + _flag_args(drift) if drift else []


def truenas_call(method, entries):
    """midclt argv; queries select only the compared fields (replication rows embed SSH key material)."""
    if method == 'nfs.config':
        argv = ['midclt', 'call', method]
    elif method.endswith('.query'):
        select = sorted({key for entry in entries for key in (*entry['match'], *entry.get('fields', {}))})
        if any(k.split('.')[0] == 'ssh_credentials' and k not in ('ssh_credentials.name', 'ssh_credentials.id')
               for k in select):
            raise ValueError("select only ssh_credentials.name or .id (the rest holds the private key)")
        argv = ['midclt', 'call', method, '[]', json.dumps({'select': select})]
    else:
        raise ValueError(f'{method}: only *.query methods and nfs.config may be called (ansible never writes TrueNAS)')
    # Non-ASCII guest-exec arguments wedge qemu-ga.
    if not all(arg.isascii() for arg in argv):
        raise ValueError(f'{method}: guest-exec arguments must be ASCII')
    return argv


def _get(obj, path):
    for part in path.split('.'):
        if not isinstance(obj, dict) or part not in obj:
            return MISSING
        obj = obj[part]
    return obj


def _same(have, want):
    if want is None:
        return have is None
    if isinstance(want, list):
        if not isinstance(have, list):
            return False
        # periodic_snapshot_tasks comes back as full task objects; compare them by dataset.
        have = [h.get('dataset') if isinstance(h, dict) else h for h in have]
        return sorted(map(str, have)) == sorted(map(str, want))
    return have is not None and str(have) == str(want)


def _guest_output(read, method):
    if read.get('rc') != 0:
        raise ValueError(f"qm guest exec failed (rc {read.get('rc')}): {str(read.get('stderr', '')).strip()} "
                         '(is VM 100 running with its guest agent up?)')
    result = json.loads(read['stdout'])
    if result.get('exitcode') != 0:
        raise ValueError(f"midclt exited {result.get('exitcode', 'without finishing')}: "
                         f"{str(result.get('err-data', '')).strip()}")
    live = json.loads(result['out-data'])
    shape = dict if method == 'nfs.config' else list
    if not isinstance(live, shape):
        raise ValueError(f'expected a JSON {shape.__name__}, got {type(live).__name__}')
    return live


def _compare(method, live, expected):
    problems = []
    if method == 'nfs.config':
        for key, wants in expected.items():
            for want in wants:
                if want not in (live.get(key) or []):
                    problems.append(f'{method}: {key} expected to contain {want}, actual {live.get(key)}')
        return problems
    for entry in expected:
        label = method + ' ' + ' '.join(f'{k}={v}' for k, v in entry['match'].items())
        found = [o for o in live if all(o.get(k) == v for k, v in entry['match'].items())]
        count = entry.get('count', 1)
        if len(found) != count:
            problems.append(f'{label}: expected {count} matching, found {len(found)}')
            continue
        for field, want in entry.get('fields', {}).items():
            # A dict compares only its listed keys; a dotted field reads a nested key.
            for name, sub_want in ({f'{field}.{k}': v for k, v in want.items()} if isinstance(want, dict)
                                   else {field: want}).items():
                actual = _get(found[0], name)
                if not _same(actual, sub_want):
                    problems.append(f'{label}: {name} expected {sub_want}, actual {actual}')
    return problems


def truenas_drift(reads, expected):
    """List every difference between the TrueNAS reads and the expected settings; [] means no drift."""
    problems = []
    for read in reads:
        method = read['item']['key']
        try:
            problems += _compare(method, _guest_output(read, method), expected[method])
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            problems.append(f'{method}: {type(error).__name__}: {error}')
    return problems


class FilterModule:
    def filters(self):
        return {'proxmox_backup_vzdump_argv': vzdump_argv,
                'proxmox_backup_truenas_call': truenas_call,
                'proxmox_backup_truenas_drift': truenas_drift}

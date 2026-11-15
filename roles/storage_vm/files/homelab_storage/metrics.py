"""Atomic node-exporter textfiles."""
import os
import re
from pathlib import Path

TEXTFILE_DIR = Path('/var/lib/prometheus/node-exporter')


def _esc(v):
    return str(v).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')


def write(name, samples):
    """samples: (metric, {label: value}, number). Written to <name>.prom via a rename, never half-read."""
    lines = []
    for metric, labels, value in samples:
        lab = ','.join(f'{k}="{_esc(v)}"' for k, v in labels.items())
        lines.append(f'{metric}{{{lab}}} {value}\n' if lab else f'{metric} {value}\n')
    path = Path(TEXTFILE_DIR) / f'{name}.prom'
    tmp = path.with_name(path.name + '.tmp')  # the collector reads only *.prom
    with open(tmp, 'w') as f:
        f.writelines(lines)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read(name, metric):
    """The value of an unlabelled sample in <name>.prom, or None."""
    try:
        text = (Path(TEXTFILE_DIR) / f'{name}.prom').read_text()
    except OSError:
        return None
    m = re.search(rf'^{re.escape(metric)} (\S+)$', text, re.M)
    return m[1] if m else None

#!/usr/bin/env python3
"""Manual qualification ONLY inside an explicitly marked disposable VM.

Never provisions a VM, enters a container or connects to another host. Not CI.
All checks below run inside the VM after its identity gates pass.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from maintenance_executor import build_manifest
from maintenance_lock import acquire
from maintenance_requests import digest, encoded, inspect_request, publish_request, write_receipt

PRODUCTION_RUNNER = 'cc1aeeb7-4827-466c-9d4b-5dc6c881f193'
FIXTURE = '''import os,pathlib,subprocess,sys,time
root=pathlib.Path(sys.argv[sys.argv.index('--state-root')+1])
mode=(root/'fixture-mode').read_text()
if mode=='fail': sys.exit(2)
if mode=='block':
    child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(180)'])
    (root/'child-pid').write_text(str(child.pid))
    deadline=time.monotonic()+120
    while not (root/'fixture-go').exists() and time.monotonic()<deadline: time.sleep(.05)
    child.terminate();child.wait(timeout=5)
print('read-only qualification fixture')
'''


def gate(expected_machine_id, state_root):
    if not re.fullmatch('[0-9a-f]{32}', expected_machine_id):
        raise ValueError('Explicit disposable machine ID required')
    actual = Path('/etc/machine-id').read_text().strip()
    marker = Path('/etc/homelab-maintenance-disposable')
    if (actual != expected_machine_id or marker.is_symlink() or marker.stat().st_uid != 0 or
            marker.stat().st_mode & 0o022 or marker.read_text().strip() != actual):
        raise ValueError('Missing root-owned disposable-VM marker or identity mismatch')
    if Path('/sys/class/dmi/id/product_uuid').read_text().strip().lower() == PRODUCTION_RUNNER:
        raise ValueError('Production runner is prohibited')
    if subprocess.run(['/usr/bin/systemd-detect-virt', '--container'], capture_output=True).returncode != 1:
        raise ValueError('Containers or ambiguous virtualization probes are prohibited')
    if subprocess.run(['/usr/bin/systemd-detect-virt', '--vm'], capture_output=True).returncode:
        raise ValueError('A VM is required; workstations and containers are prohibited')
    if os.geteuid() == 0:
        raise ValueError('Run as an unprivileged disposable test user')
    root = Path(state_root)
    if (not root.is_absolute() or root.parent != Path('/var/tmp') or
            not re.fullmatch(r'maintenance-qualification-[a-zA-Z0-9-]+', root.name) or root.exists()):
        raise ValueError('A new /var/tmp/maintenance-qualification-* root is required')


def qualify(root):
    root = Path(root); root.mkdir(mode=0o700)
    (root/'requests').mkdir(mode=0o700)
    (root/'runner-instance').write_text('disposable-qualification')
    os.environ['HOMELAB_RUNNER_INSTANCE'] = 'disposable-qualification'
    nonce = acquire(root, 'qualification', 'qualification-owner')
    ownership = (root/'operation.json').read_bytes()
    source = Path(__file__).resolve().parents[1]
    release = root/'release'; (release/'ansible/scripts').mkdir(parents=True)
    (release/'apps/scripts/storage').mkdir(parents=True)
    for name in ('maintenance_executor.py', 'maintenance_requests.py', 'maintenance_lock.py'):
        shutil.copyfile(source/name, release/'ansible/scripts'/name)
    adapter = release/'apps/scripts/storage/supervised_readonly.py'; adapter.write_text(FIXTURE)
    data = build_manifest(release, root, 'disposable-qualification', 'a'*40)
    manifest = release/'manifest.json'; manifest.write_bytes(encoded(data))
    cli = [sys.executable, '-B', str(release/'ansible/scripts/maintenance_executor.py'), '--manifest', str(manifest)]
    unit = Path.home()/'.config/systemd/user/homelab-maintenance@.service'
    # Refuse shadowing any existing system or user installation.
    if unit.exists() or Path('/etc/systemd/user/homelab-maintenance@.service').exists():
        raise ValueError('Existing executor installation; use a fresh disposable VM')
    template = (source.parent/'roles/maintenance_runner/templates/homelab-maintenance@.service.j2').read_text()
    for key, value in [('{{ maintenance_runner_executor_python }}',sys.executable),
                       ('{{ maintenance_runner_executor_manifest | dirname }}',str(release)),
                       ('{{ maintenance_runner_executor_manifest }}',str(manifest))]: template=template.replace(key,value)
    if '{{' in template: raise ValueError('Unrendered template')
    unit.parent.mkdir(parents=True, exist_ok=True)
    with unit.open('x') as stream: stream.write(template)
    identities = []
    def systemctl(*args):
        return subprocess.run(['/usr/bin/systemctl','--user',*args],capture_output=True,text=True,timeout=40,check=True)
    def invoke(command, identity=None, payload=None):
        result=subprocess.run(cli+[command]+([identity] if identity else []),
                              input=json.dumps(payload) if payload else None,capture_output=True,text=True,timeout=35)
        return result
    def new_request(mode):
        (root/'fixture-mode').write_text(mode)
        identity=str(uuid.uuid4());identities.append(identity)
        request=dict(schema=1,request_id=identity,operation='status',service='jellyfin',expected_stage='acquired',
                     apps_commit='a'*40,runtime_id=digest(data))
        return identity,dict(request=request,credentials=dict(owner='qualification-owner',nonce=nonce))
    def wait_for(identity, phase):
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            result=invoke('inspect',identity)
            report=json.loads(result.stdout)
            if report.get('phase')==phase:return report
            time.sleep(.1)
        raise AssertionError('Expected request phase did not arrive: '+phase)
    results=[]
    try:
        systemctl('daemon-reload')
        identity,payload=new_request('block')
        result=invoke('submit',payload=payload);assert result.returncode==0
        wait_for(identity,'running')  # submit client has already exited.
        deadline=time.monotonic()+15
        while not (root/'child-pid').exists() and time.monotonic()<deadline:time.sleep(.1)
        assert (root/'child-pid').exists(), 'Fixture child did not become ready'
        started=(root/'requests'/identity/'started.json').read_bytes()
        assert invoke('submit',payload=payload).returncode==0
        assert (root/'requests'/identity/'started.json').read_bytes()==started
        results.append('client exit and duplicate submission preserve one running attempt')
        other,second=new_request('block');invoke('submit',payload=second)
        deadline=time.monotonic()+10
        while not list((root/'requests'/other).glob('attempt-*.json')) and time.monotonic()<deadline:time.sleep(.1)
        assert list((root/'requests'/other).glob('attempt-*.json'))
        assert not (root/'requests'/other/'started.json').exists()
        systemctl('stop','homelab-maintenance@'+identity+'.service')
        wait_for(identity,'unknown')
        child_pid=int((root/'child-pid').read_text())
        deadline=time.monotonic()+5
        while Path('/proc',str(child_pid)).exists() and time.monotonic()<deadline:time.sleep(.1)
        assert not Path('/proc',str(child_pid)).exists()
        assert invoke('start',identity).returncode!=0
        results.append('concurrency, forced stop, descendant cleanup and no replay')
        (root/'fixture-mode').write_text('success')
        assert invoke('start',other).returncode==0;wait_for(other,'succeeded')
        results.append('explicit start of accepted request')
        # Unlike an orderly service stop, kill the supervisor without cleanup.
        (root/'child-pid').unlink()
        killed,payload=new_request('block');invoke('submit',payload=payload)
        wait_for(killed,'running')
        deadline=time.monotonic()+15
        while not (root/'child-pid').exists() and time.monotonic()<deadline:time.sleep(.1)
        assert (root/'child-pid').exists(), 'Kill-test child did not become ready'
        child_pid=int((root/'child-pid').read_text())
        systemctl('kill','--kill-whom=main','--signal=SIGKILL','homelab-maintenance@'+killed+'.service')
        wait_for(killed,'unknown')
        deadline=time.monotonic()+5
        while Path('/proc',str(child_pid)).exists() and time.monotonic()<deadline:time.sleep(.1)
        assert not Path('/proc',str(child_pid)).exists()
        assert invoke('start',killed).returncode!=0
        assert not (root/'requests'/killed/'terminal.json').exists()
        results.append('supervisor SIGKILL cleans children and cannot replay')
        failed,payload=new_request('fail');invoke('submit',payload=payload);wait_for(failed,'failed')
        results.append('nonzero child produces failed receipt')
        stale,payload=new_request('success');publish_request(root,payload['request'],payload['credentials'])
        write_receipt(root,payload['request'],'started','disposable-qualification',exit_code=None)
        assert invoke('start',stale).returncode!=0;wait_for(stale,'unknown')
        results.append('stale started receipt is not success or replay permission')
        drift,payload=new_request('success');publish_request(root,payload['request'],payload['credentials'])
        adapter.write_text(FIXTURE+'\n# changed\n')
        assert invoke('start',drift).returncode!=0
        assert not (root/'requests'/drift/'started.json').exists()
        adapter.write_text(FIXTURE)
        results.append('runtime drift refuses execution')
        assert (root/'operation.json').read_bytes()==ownership
        (root/'qualification-results.json').write_text(json.dumps(dict(passed=results),indent=2)+'\n')
        print(json.dumps(dict(passed=results,evidence=str(root))))
    finally:
        for identity in identities:
            subprocess.run(['/usr/bin/systemctl','--user','stop','homelab-maintenance@'+identity+'.service'],
                           capture_output=True,timeout=40)
        unit.unlink()
        systemctl('daemon-reload')
    # Preserve synthetic state/results for inspection; no implicit recursive deletion.


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-machine-id',required=True)
    parser.add_argument('--state-root',required=True)
    args=parser.parse_args()
    gate(args.expected_machine_id,args.state_root)
    qualify(args.state_root)


if __name__=='__main__':main()

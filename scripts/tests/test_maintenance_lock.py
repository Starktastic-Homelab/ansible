import json
import multiprocessing as mp
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from maintenance_lock import acquire, verify, release, advance

INSTANCE='cc1aeeb7-4827-466c-9d4b-5dc6c881f193'

def contender(root,start,queue):
    start.wait()
    try: queue.put(('won',acquire(Path(root),'apply',str(os.getpid()))))
    except FileExistsError: queue.put(('lost',''))

class LockTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'maintenance';self.root.mkdir()
        (self.root/'runner-instance').write_text(INSTANCE+'\n')
        self.env=patch.dict(os.environ,{'HOMELAB_RUNNER_INSTANCE':INSTANCE});self.env.start();self.addCleanup(self.env.stop)

    def test_two_processes_exactly_one_winner(self):
        start=mp.Event();queue=mp.Queue()
        procs=[mp.Process(target=contender,args=(str(self.root),start,queue)) for _ in range(2)]
        for p in procs:p.start()
        start.set();results=[queue.get(timeout=10)[0] for _ in procs]
        for p in procs:p.join(10);self.assertEqual(p.exitcode,0)
        self.assertCountEqual(results,['won','lost'])

    def test_wrong_owner_nonce_cannot_release(self):
        nonce=acquire(self.root,'apply','owner')
        for owner,key in [('other',nonce),('owner','wrong')]:
            with self.assertRaises(PermissionError):release(self.root,owner,key)
        verify(self.root,'owner',nonce);release(self.root,'owner',nonce)
        self.assertFalse((self.root/'operation.json').exists())

    def test_missing_mismatched_marker_or_env_refused(self):
        for value in ['', 'other']:
            (self.root/'runner-instance').write_text(value)
            with self.assertRaises(ValueError):acquire(self.root,'apply','owner')
        (self.root/'runner-instance').unlink()
        with self.assertRaises(FileNotFoundError):acquire(self.root,'apply','owner')
        self.assertFalse((self.root/'operation.json').exists())

    def test_root_record_marker_symlinks_refused(self):
        alias=self.root.parent/'alias';alias.symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(ValueError):acquire(alias,'apply','owner')
        for name in ['runner-instance','operation.json','guard']:
            p=self.root/name;p.unlink(missing_ok=True);p.symlink_to(self.root.parent/'missing')
            with self.assertRaises((OSError,ValueError)):acquire(self.root,'apply','owner')
            p.unlink()
            if name=='runner-instance':p.write_text(INSTANCE)

    def test_corrupt_or_interrupted_record_is_never_stolen(self):
        for contents in ['', '{', '{}']:
            (self.root/'operation.json').write_text(contents)
            with self.assertRaises(FileExistsError):acquire(self.root,'apply','owner')
            with self.assertRaises(ValueError):verify(self.root,'owner','nonce')

    def test_killed_owner_and_reboot_preserve_hold(self):
        code='''import sys,time;from pathlib import Path
from maintenance_lock import acquire
print(acquire(Path(sys.argv[1]),'apply','dead-owner'),flush=True)
time.sleep(120)
'''
        env=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        proc=subprocess.Popen([sys.executable,'-c',code,str(self.root)],env=env,stdout=subprocess.PIPE,text=True)
        nonce=proc.stdout.readline().strip();proc.kill();proc.wait();proc.stdout.close()
        self.assertTrue(nonce)
        os.utime(self.root/'operation.json',(1,1))
        with self.assertRaises(FileExistsError):acquire(self.root,'fence','new-owner')
        # Reopening through a fresh interpreter approximates process/OS state loss.
        result=subprocess.run([sys.executable,'-c',"from maintenance_lock import verify;from pathlib import Path;import sys;verify(Path(sys.argv[1]),'dead-owner',sys.argv[2])",str(self.root),nonce],env=env)
        self.assertEqual(result.returncode,0)

    def test_stage_transition_requires_original_owner_and_stage(self):
        nonce=acquire(self.root,'migration','owner')
        with self.assertRaises(ValueError):advance(self.root,'owner',nonce,'copied','released')
        advance(self.root,'owner',nonce,'acquired','held')
        verify(self.root,'owner',nonce,stage='held')
        with self.assertRaises(ValueError):verify(self.root,'owner',nonce,stage='acquired')

    def test_interrupted_write_blocks_next_acquire(self):
        with patch('maintenance_lock.os.fsync',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):acquire(self.root,'apply','owner')
        with self.assertRaises(FileExistsError):acquire(self.root,'apply','owner')

if __name__=='__main__':unittest.main()

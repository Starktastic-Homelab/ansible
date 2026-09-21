import copy
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from iscsi_generation import validate

IQNS={'worker-a':'iqn.2026-09.net.starktastic:k3s-worker-a','worker-b':'iqn.2026-09.net.starktastic:k3s-worker-b'}
CURRENT=dict(hostname='worker-a',vmid=201,smbios_uuid='vm-new',storage_ip='10.9.8.51',iqn=IQNS['worker-a'])
REVIEW=dict(generation=CURRENT,previous=None,first_enrollment=True,reviewed_by='operator')

class InitiatorTests(unittest.TestCase):
    def call(self,**overrides):
        data=dict(worker=True,iqns=IQNS,current=CURRENT,review=REVIEW,existing_iqn=IQNS['worker-a'],sessions=0,free_bytes=30*1024**3)
        data.update(overrides);return validate(**data)
    def test_unchanged_rerun(self):self.assertEqual(self.call()['iqn'],IQNS['worker-a'])
    def test_bad_map(self):
        for names in [{},dict(IQNS,**{'worker-b':IQNS['worker-a']}),dict(IQNS,**{'worker-a':''})]:
            with self.subTest(names=names),self.assertRaises(ValueError):self.call(iqns=names)
    def test_nonworker_and_low_disk(self):
        with self.assertRaises(ValueError):self.call(worker=False)
        with self.assertRaises(ValueError):self.call(free_bytes=24*1024**3)
    def test_live_identity_change_refused(self):
        with self.assertRaises(ValueError):self.call(existing_iqn='other',sessions=1)
        self.call(sessions=1)
    def test_no_review_or_wrong_current_generation_refused(self):
        with self.assertRaises(ValueError):self.call(review={})
        with self.assertRaises(ValueError):self.call(current=dict(CURRENT,smbios_uuid='unknown'))
    def test_new_vm_zero_sessions_does_not_retire_old_writer(self):
        review=dict(REVIEW,previous=dict(CURRENT,smbios_uuid='old'),first_enrollment=False)
        with self.assertRaises(ValueError):self.call(review=review)
        for kind in ['fenced','destroyed']:
            safe=copy.deepcopy(review);safe['retirement']=dict(generation=safe['previous'],kind=kind,verified=True,evidence='private/receipt.json',reviewed_by='operator')
            self.call(review=safe)
            safe['retirement']['generation']=CURRENT
            with self.assertRaises(ValueError):self.call(review=safe)
    def test_unchanged_generation_requires_no_new_retirement(self):
        self.call(review=dict(REVIEW,previous=CURRENT,first_enrollment=False))

if __name__=='__main__':unittest.main()

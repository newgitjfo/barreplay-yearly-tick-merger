import hashlib
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import merge_year as m
T=1767225600000

def block(times,bids=None,asks=None):
    n=len(times)
    return struct.pack('<i',n)+struct.pack('<'+'q'*n,*times)+struct.pack('<'+'d'*n,*(bids or [1.1]*n))+struct.pack('<'+'d'*n,*(asks or [1.2]*n))

class Tests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory(); self.addCleanup(t.cleanup); self.root=Path(t.name)
    def file(self,raw,name='day.BIN'):
        p=self.root/name; p.write_bytes(raw); return p
    def test_exact_format_same_timestamp(self):
        raw=block([T+123,T+123],[1.1234567890123457,-0.0],[1.3,-37.63])
        p=self.file(raw); desc=m.inspect_bin(p,'2026-01-01'); out=io.BytesIO(); merger=m.Merger(out); merger.append(p,desc)
        self.assertEqual(out.getvalue(),raw); self.assertEqual(merger.ticks,2)
    def test_merge_hour_and_day(self):
        raw=block([T])+block([T+3600000])+block([T+86400000])
        p=self.file(raw); out=io.BytesIO(); merger=m.Merger(out); merger.append(p,m.inspect_bin(p))
        self.assertEqual(out.getvalue(),raw); self.assertEqual(merger.blocks,3)
    def test_identical_block_skipped_only_as_whole(self):
        a=block([T,T]); p=self.file(a+a); out=io.BytesIO(); merger=m.Merger(out); merger.append(p,m.inspect_bin(p))
        self.assertEqual(out.getvalue(),a); self.assertEqual(merger.duplicates,1); self.assertEqual(merger.ticks,2)
    def test_conflicting_hour_fails(self):
        p=self.file(block([T])+block([T+1])); merger=m.Merger(io.BytesIO())
        with self.assertRaises(m.MergeError): merger.append(p,m.inspect_bin(p))
    def test_reversed_hours_fail(self):
        p=self.file(block([T+3600000])+block([T])); merger=m.Merger(io.BytesIO())
        with self.assertRaises(m.MergeError): merger.append(p,m.inspect_bin(p))
    def test_cross_hour_rejected(self):
        with self.assertRaises(m.MergeError): m.inspect_bin(self.file(block([T,T+3600000])))
    def test_wrong_daily_name(self):
        with self.assertRaises(m.MergeError): m.inspect_bin(self.file(block([T])),'2026-01-02')
    def test_unsorted_ticks(self):
        with self.assertRaises(m.MergeError): m.inspect_bin(self.file(block([T+2,T+1])))
    def test_bad_counts_empty_truncated(self):
        for raw in [b'',b'\1',struct.pack('<i',0),struct.pack('<i',-1),struct.pack('<i',20000001),block([T])[:-1]]:
            with self.subTest(raw=raw),self.assertRaises(m.MergeError): m.inspect_bin(self.file(raw))
    def test_nonfinite_price(self):
        with self.assertRaises(m.MergeError): m.inspect_bin(self.file(block([T],[float('nan')],[1.2])))
    def test_bad_epoch(self):
        with self.assertRaises(m.MergeError): m.inspect_bin(self.file(block([0])))
    def test_end_to_end_build_mock_download(self):
        payloads={'2026-01-01':block([T,T+1]),'2026-01-02':block([T+86400000])}
        files=[{'path':f'EURUSD/EURUSD_{d}.BIN','day':d,'bytes':len(raw),'source_sha256':None} for d,raw in payloads.items()]
        def dl(url,target,n,sha):
            day=url.rsplit('_',1)[1][:-4]; raw=payloads[day]; target.write_bytes(raw); return hashlib.sha256(raw).hexdigest()
        out=self.root/'output'
        with patch.object(m,'discover',return_value=('a'*40,files)),patch.object(m,'download',side_effect=dl):
            report=m.build('EURUSD',2026,out)
        expected=b''.join(payloads.values()); self.assertEqual((out/'EURUSD_2026.BIN').read_bytes(),expected)
        self.assertEqual(report['ticks'],3); self.assertEqual(report['daily_files'],2)
        with zipfile.ZipFile(out/'EURUSD_2026.zip') as z: self.assertEqual(z.read('EURUSD_2026.BIN'),expected)
    def test_build_failure_leaves_no_output(self):
        files=[{'path':'EURUSD/EURUSD_2026-01-01.BIN','day':'2026-01-01','bytes':28,'source_sha256':None}]
        with patch.object(m,'discover',return_value=('a'*40,files)),patch.object(m,'download',side_effect=m.MergeError('network')):
            with self.assertRaises(m.MergeError): m.build('EURUSD',2026,self.root/'output')
        self.assertFalse((self.root/'output').exists()); self.assertFalse(list(self.root.glob('.yearly_*')))
    def test_no_overwrite(self):
        out=self.root/'output'; out.mkdir()
        with self.assertRaises(m.MergeError): m.build('EURUSD',2026,out)

if __name__=='__main__': unittest.main(verbosity=2)

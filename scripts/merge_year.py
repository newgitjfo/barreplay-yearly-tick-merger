#!/usr/bin/env python3
"""Validate daily BarReplay bin_v1 blocks, merge one symbol/year, ZIP output.
Python 3.10+; standard library only. Source revision is pinned for each run.
"""
import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import sys
import tempfile
import time
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
import zipfile

HOUR_MS = 3600000
MAX_COUNT = 20000000
CHUNK = 1024 * 1024
MAX_ZIP = 2 * 1024**3 - 1
MAX_DAILY = 4 * 1024**3
DATASET = 'Esmaeil9ss/Tickdata'

class MergeError(Exception): pass


def request(url):
    # All URLs are public Hugging Face URLs. No GitHub credentials are used here.
    for attempt in range(4):
        try:
            return urlopen(Request(url, headers={'User-Agent':'BarReplay-Yearly-Merger/1.0'}), timeout=120)
        except Exception:
            if attempt == 3: raise
            time.sleep(2 ** attempt)


def json_url(url):
    with request(url) as response:
        return json.load(response)


def discover(symbol, year, dataset=DATASET):
    base='https://huggingface.co/api/datasets/'+dataset
    revision=json_url(base+'/revision/main')['sha']
    if not re.fullmatch(r'[0-9a-f]{40}',revision): raise MergeError('Invalid source commit SHA.')
    pattern=re.compile(re.escape(symbol)+r'/'+re.escape(symbol)+r'_(\d{4}-\d{2}-\d{2})\.BIN$',re.I)
    url=base+'/tree/'+revision+'/'+quote(symbol,safe='')+'?limit=1000'
    files=[]; pages=set()
    while url:
        if url in pages or len(pages)>100: raise MergeError('Invalid inventory pagination.')
        pages.add(url)
        if not url.startswith(base+'/tree/'+revision+'/'): raise MergeError('Unexpected inventory pagination URL.')
        with request(url) as response:
            items=json.load(response); link=response.headers.get('Link','')
        for item in items:
            m=pattern.fullmatch(item.get('path',''))
            if item.get('type')!='file' or not m: continue
            day=date.fromisoformat(m.group(1))
            if day.year!=year: continue
            n=int(item['size'])
            if n<4 or n>MAX_DAILY: raise MergeError('Invalid or oversized daily source: '+item['path'])
            files.append({'path':item['path'],'day':day.isoformat(),'bytes':n,
                          'source_sha256':item.get('lfs',{}).get('oid')})
        match=re.search(r'<([^>]+)>;\s*rel="next"',link)
        url=match.group(1) if match else None
    files.sort(key=lambda f:f['day'])
    if not files: raise MergeError(f'No canonical daily BIN files found for {symbol} {year}.')
    if len({x['day'] for x in files})!=len(files): raise MergeError('Duplicate source dates in inventory.')
    return revision,files


def download(url, target, expected_bytes, expected_sha=None):
    for attempt in range(3):
        digest=hashlib.sha256(); size=0
        try:
            with request(url) as response, open(target,'wb') as out:
                while True:
                    block=response.read(CHUNK)
                    if not block: break
                    size+=len(block)
                    if size>expected_bytes or size>MAX_DAILY: raise MergeError('Daily download exceeded inventory size.')
                    digest.update(block); out.write(block)
            actual=digest.hexdigest()
            if size!=expected_bytes: raise MergeError('Daily download size mismatch.')
            if expected_sha and actual!=expected_sha: raise MergeError('Source LFS SHA-256 mismatch.')
            return actual
        except Exception:
            Path(target).unlink(missing_ok=True)
            if attempt==2: raise
            time.sleep(2**attempt)


def utc(ms):
    sec,milli=divmod(ms,1000)
    return datetime.fromtimestamp(sec,timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')+f'.{milli:03d}Z'


def read_exact(stream,n):
    raw=stream.read(n)
    if len(raw)!=n: raise MergeError('Truncated BIN block.')
    return raw


def inspect_bin(path, expected_day=None):
    """Return hourly block descriptors, validating every timestamp and Bid/Ask.
    Array order is count:int32; all time:int64; all bid:float64; all ask:float64.
    Equal millisecond timestamps are deliberately preserved.
    """
    result=[]; size=Path(path).stat().st_size
    with open(path,'rb') as f:
        while f.tell()<size:
            offset=f.tell(); raw=read_exact(f,4); n=struct.unpack('<i',raw)[0]
            if not 1<=n<=MAX_COUNT: raise MergeError('Invalid tick count in BIN.')
            length=4+n*24
            if offset+length>size: raise MergeError('Truncated BIN arrays.')
            h=hashlib.sha256(raw); first=last=None
            for channel in range(3):
                remaining=n
                while remaining:
                    take=min(remaining,32768); raw=read_exact(f,take*8); h.update(raw)
                    for (value,) in struct.iter_unpack('<q' if channel==0 else '<d',raw):
                        if channel==0:
                            if not 0<value<4133980800000: raise MergeError('Invalid epoch millisecond timestamp.')
                            if last is not None and value<last: raise MergeError('Unsorted tick timestamps.')
                            if first is None: first=value
                            last=value
                        elif not math.isfinite(value): raise MergeError('Non-finite BIN Bid/Ask.')
                    remaining-=take
            hour=first//HOUR_MS
            if hour!=last//HOUR_MS: raise MergeError('A source block crosses UTC hours; refusing unsafe compaction.')
            if expected_day and (utc(first)[:10]!=expected_day or utc(last)[:10]!=expected_day):
                raise MergeError('Tick day does not match daily filename: '+expected_day)
            result.append({'offset':offset,'length':length,'ticks':n,'hour':hour,
                           'first_ms':first,'last_ms':last,'sha256':h.hexdigest()})
    if not result: raise MergeError('Empty BIN input.')
    return result


class Merger:
    def __init__(self, stream):
        self.stream=stream; self.last=None; self.ticks=0; self.blocks=0; self.duplicates=0; self.first_ms=None; self.last_ms=None
    def append(self,path,descriptors):
        added=0
        with open(path,'rb') as src:
            for block in descriptors:
                if self.last is not None and block['hour']==self.last['hour']:
                    if block['sha256']!=self.last['sha256']:
                        raise MergeError('Conflicting blocks in the same UTC hour; refusing to discard or overwrite ticks.')
                    self.duplicates+=1
                    continue  # Only an identical WHOLE block is skipped, not individual same-time ticks.
                if self.last is not None and block['hour']<self.last['hour']:
                    raise MergeError('Overlapping/reversed source hours.')
                src.seek(block['offset']); remaining=block['length']
                while remaining:
                    raw=read_exact(src,min(CHUNK,remaining)); self.stream.write(raw); remaining-=len(raw)
                self.last=block; self.ticks+=block['ticks']; self.blocks+=1; added+=1
                if self.first_ms is None: self.first_ms=block['first_ms']
                self.last_ms=block['last_ms']
        return added


def file_sha(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        while True:
            block=f.read(CHUNK)
            if not block: break
            h.update(block)
    return h.hexdigest()


def build(symbol,year,output):
    output=Path(output)
    if output.exists(): raise MergeError('Output already exists; choose a new directory.')
    revision,files=discover(symbol,year)
    size=sum(x['bytes'] for x in files)
    print(f'Snapshot: {revision}\nSources: {len(files)} daily files; {size:,} bytes',flush=True)
    output.parent.mkdir(parents=True,exist_ok=True)
    if shutil.disk_usage(output.parent).free<2*size+512*1024**2:
        raise MergeError('Insufficient disk space for yearly BIN and ZIP.')
    staging=Path(tempfile.mkdtemp(prefix='.yearly_',dir=output.parent))
    try:
        name=f'{symbol}_{year}'; binpath=staging/(name+'.BIN')
        report={'symbol':symbol,'year':year,'dataset':DATASET,'source_revision':revision,
                'created_utc':datetime.now(timezone.utc).isoformat(),
                'source_folder':symbol,'inventory_bytes':size,'source_files':[],
                'coverage_note':'Available canonical daily files only; absent days are not automatically errors or proven market closures.'}
        with tempfile.TemporaryDirectory(prefix='daily_bin_') as temp, binpath.open('xb') as out:
            daily=Path(temp)/'day.BIN'; merger=Merger(out)
            for i,item in enumerate(files,1):
                url='https://huggingface.co/datasets/'+DATASET+'/resolve/'+revision+'/'+quote(item['path'],safe='/')
                digest=download(url,daily,item['bytes'],item['source_sha256'])
                blocks=inspect_bin(daily,item['day']); added=merger.append(daily,blocks)
                report['source_files'].append({**item,'sha256':digest,'source_blocks':len(blocks),'added_blocks':added})
                daily.unlink()
                print(f"[{i}/{len(files)}] {item['day']} checked and merged ({sum(b['ticks'] for b in blocks):,} ticks)",flush=True)
            out.flush(); os.fsync(out.fileno())
        # Independently validate the entire finished BIN before releasing anything.
        verified=inspect_bin(binpath)
        if (sum(x['ticks'] for x in verified)!=merger.ticks or len(verified)!=merger.blocks
                or any(b['hour']<=a['hour'] for a,b in zip(verified,verified[1:]))):
            raise MergeError('Merged BIN validation mismatch.')
        if binpath.stat().st_size!=merger.ticks*24+merger.blocks*4: raise MergeError('Merged byte length mismatch.')
        bin_sha=file_sha(binpath); zippath=staging/(name+'.zip')
        with zipfile.ZipFile(zippath,'x',zipfile.ZIP_DEFLATED,compresslevel=6,allowZip64=True) as z:
            z.write(binpath,arcname=binpath.name)
        if zippath.stat().st_size>MAX_ZIP: raise MergeError('ZIP must be under 2 GiB for one GitHub Release asset; refusing to split silently.')
        with zipfile.ZipFile(zippath) as z:
            if z.namelist()!=[binpath.name]: raise MergeError('Unexpected ZIP structure.')
            with z.open(binpath.name) as src:
                h=hashlib.sha256()
                while True:
                    block=src.read(CHUNK)
                    if not block: break
                    h.update(block)
            if h.hexdigest()!=bin_sha: raise MergeError('ZIP content SHA-256 mismatch.')
        present={x['day'] for x in files}; d=date(year,1,1); absent=[]
        while d.year==year:
            if d.isoformat() not in present: absent.append(d.isoformat())
            d+=timedelta(days=1)
        report.update({'daily_files':len(files),'ticks':merger.ticks,'hourly_blocks':merger.blocks,
                       'identical_blocks_skipped':merger.duplicates,'first_tick_utc':utc(merger.first_ms),
                       'last_tick_utc':utc(merger.last_ms),'days_without_source_file':absent,
                       'bin_file':binpath.name,'zip_file':zippath.name,'bin_bytes':binpath.stat().st_size,
                       'zip_bytes':zippath.stat().st_size,'bin_sha256':bin_sha,'zip_sha256':file_sha(zippath)})
        (staging/'merge_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        (staging/'SHA256SUMS.txt').write_text(f"{report['zip_sha256']}  {zippath.name}\n{bin_sha}  {binpath.name}\n")
        staging.rename(output)
        print(f"SUCCESS: {name}.zip | {merger.ticks:,} ticks | {report['zip_bytes']:,} ZIP bytes",flush=True)
        return report
    except BaseException:
        shutil.rmtree(staging,ignore_errors=True)
        raise


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--symbol',default='EURUSD'); p.add_argument('--year',type=int,default=2026)
    p.add_argument('--output',type=Path,default=Path('output')); a=p.parse_args()
    symbol=a.symbol.upper()
    if not re.fullmatch(r'[A-Z0-9][A-Z0-9._-]{0,39}',symbol): p.error('Invalid symbol.')
    if not 1971<=a.year<=2100: p.error('Year must be 1971..2100.')
    try: build(symbol,a.year,a.output)
    except Exception as e:
        print(f'ERROR: {type(e).__name__}: {e}',file=sys.stderr); return 1
    return 0

if __name__=='__main__': raise SystemExit(main())

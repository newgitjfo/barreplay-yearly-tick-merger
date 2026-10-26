#!/usr/bin/env python3
"""Save a small run receipt in the repository so connected clients can verify it."""
import base64
import json
import os
from pathlib import Path
import subprocess
from datetime import datetime,timezone
repo=os.environ['GITHUB_REPOSITORY']; run=os.environ['GITHUB_RUN_ID']
receipt={'status':os.environ.get('JOB_STATUS','unknown'),'run_id':run,
         'run_url':f'https://github.com/{repo}/actions/runs/{run}',
         'commit':os.environ['GITHUB_SHA'],'finished_utc':datetime.now(timezone.utc).isoformat()}
for logname in ('test.log','merge.log','publish.log'):
    log=Path(logname)
    if log.exists():
        # These project logs contain only test results/public source progress, not environment variables.
        receipt[logname.replace('.','_')+'_tail']='\n'.join(log.read_text(errors='replace').splitlines()[-20:])[-10000:]
report=Path('output/merge_report.json')
if report.exists():
    data=json.loads(report.read_text())
    for key in ['symbol','year','daily_files','ticks','hourly_blocks','first_tick_utc','last_tick_utc','bin_bytes','zip_bytes','bin_sha256','zip_sha256','source_revision','zip_file']:
        receipt[key]=data[key]
url=f'repos/{repo}/contents/run-status.json'
r=subprocess.run(['gh','api',url,'--jq','.sha'],text=True,capture_output=True)
payload={'message':f'Record yearly merge run {run}: '+receipt['status'],
         'content':base64.b64encode((json.dumps(receipt,indent=2)+'\n').encode()).decode(),'branch':'main'}
if r.returncode==0: payload['sha']=r.stdout.strip()
subprocess.run(['gh','api',url,'--method','PUT','--input','-'],input=json.dumps(payload),text=True,check=True,stdout=subprocess.DEVNULL)
print('Recorded run result: '+receipt['status'])

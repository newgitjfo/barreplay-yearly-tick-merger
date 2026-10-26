#!/usr/bin/env python3
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def gh(*args,check=True):
    return subprocess.run(['gh',*args],text=True,capture_output=True,check=check)


def main():
    report=json.loads(Path('output/merge_report.json').read_text())
    repo=os.environ['GITHUB_REPOSITORY']; tag=os.environ.get('RELEASE_TAG','yearly-eurusd-2026')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}',tag) or '..' in tag or tag.endswith('.'):
        raise ValueError('Invalid release tag.')
    stem=report['symbol']+'_'+str(report['year']); asset=report['zip_file']
    if not re.fullmatch(r'[A-Z0-9][A-Z0-9._-]*_[0-9]{4}\.zip',asset): raise ValueError('Invalid asset name.')
    dest=gh('release','view',tag,'--repo',repo,'--json','assets,isDraft',check=False)
    created=dest.returncode!=0
    overwrite=os.environ.get('OVERWRITE','false').lower()=='true'
    if not created and not overwrite:
        names={a['name'] for a in json.loads(dest.stdout)['assets']}
        if names & {asset,stem+'_report.json',stem+'_SHA256SUMS.txt'}:
            raise ValueError('Output already exists in this release. Choose a new tag or explicitly enable overwrite.')
    notes=(f"Merged {report['daily_files']} available daily files for {report['symbol']} {report['year']}.\n\n"
           f"UTC tick coverage: {report['first_tick_utc']} to {report['last_tick_utc']}.\n\n"
           f"Ticks: {report['ticks']:,}. This is available coverage, not a claim of a complete calendar year.\n\n"
           f"Hugging Face dataset: {report['dataset']} at {report['source_revision']}.\n\n"
           'BIN blocks and ZIP content verified. See report and SHA256SUMS assets.')
    if created:
        gh('release','create',tag,'--repo',repo,'--target',os.environ['GITHUB_SHA'],'--draft',
           '--title',f"{report['symbol']} {report['year']} — yearly tick data",'--notes',notes)
    folder=Path('output'); (folder/(stem+'_report.json')).write_bytes((folder/'merge_report.json').read_bytes())
    (folder/(stem+'_SHA256SUMS.txt')).write_bytes((folder/'SHA256SUMS.txt').read_bytes())
    args=['release','upload',tag,'--repo',repo,str(folder/asset),str(folder/(stem+'_report.json')),str(folder/(stem+'_SHA256SUMS.txt'))]
    if overwrite: args.append('--clobber')
    gh(*args)
    if created: gh('release','edit',tag,'--repo',repo,'--draft=false')
    url=gh('release','view',tag,'--repo',repo,'--json','url','--jq','.url').stdout.strip()
    print('Published verified ZIP: '+url)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'],'a') as f:
            f.write(f"## Yearly merge completed\n\n[Download Release]({url})\n\n"
                    f"- ZIP: `{asset}`\n- Daily files: {report['daily_files']}\n- Ticks: {report['ticks']:,}\n"
                    f"- UTC coverage: {report['first_tick_utc']} → {report['last_tick_utc']}\n"
                    f"- Source snapshot: `{report['source_revision']}`\n")

if __name__=='__main__':
    try: main()
    except subprocess.CalledProcessError as e:
        print('GitHub publication failed: '+(e.stderr or ''),file=sys.stderr); raise SystemExit(1)
    except Exception as e:
        print(f'ERROR: {e}',file=sys.stderr); raise SystemExit(1)

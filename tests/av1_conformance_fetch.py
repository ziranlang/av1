#!/usr/bin/env python3
"""Fetch the pinned libaom decoder test vectors into an ignored cache.

The vector list is libaom's own test/test_vectors.cc and every file is checked
against test/test-data.sha1 at the pinned revision. Existing verified files
are reused; nothing is fetched twice.
"""
import argparse
import hashlib
from pathlib import Path
import re
import urllib.request

BASE='https://storage.googleapis.com/aom-test-data/'


def sha1(path):
    hasher=hashlib.sha1()
    with path.open('rb') as data:
        for chunk in iter(lambda:data.read(1<<20),b''):hasher.update(chunk)
    return hasher.hexdigest()


def vectors(aom):
    names=re.findall(r'"(av1-[^"]+\.(?:ivf|mkv))"',(aom/'test/test_vectors.cc').read_text())
    if len(names)!=len(set(names)) or not names:raise SystemExit('unexpected libaom test vector list')
    return names


def pins(aom):
    table={}
    for line in (aom/'test/test-data.sha1').read_text().splitlines():
        if line.strip():
            digest,name=line.split(maxsplit=1);table[name.lstrip('*')]=digest
    return table


def main():
    p=argparse.ArgumentParser();p.add_argument('--aom',type=Path,required=True);p.add_argument('--cache',type=Path,required=True)
    p.add_argument('--ivf-only',action='store_true');a=p.parse_args()
    a.cache.mkdir(parents=True,exist_ok=True);table=pins(a.aom);fetched=0;total=0
    names=[n for n in vectors(a.aom) if not a.ivf_only or n.endswith('.ivf')]
    for name in names:
        for file in (name,name+'.md5'):
            if file not in table:raise SystemExit(f'{file} has no pinned sha1')
            path=a.cache/file
            if path.exists() and sha1(path)==table[file]:total+=path.stat().st_size;continue
            partial=path.with_suffix(path.suffix+'.partial')
            with urllib.request.urlopen(BASE+file,timeout=60) as response,partial.open('wb') as out:
                for chunk in iter(lambda:response.read(1<<20),b''):out.write(chunk)
            if sha1(partial)!=table[file]:partial.unlink();raise SystemExit(f'{file} sha1 mismatch')
            partial.replace(path);fetched+=1;total+=path.stat().st_size
    print(f'libaom test vectors: {len(names)} streams verified ({fetched} files fetched, {total} bytes)')


if __name__=='__main__':main()

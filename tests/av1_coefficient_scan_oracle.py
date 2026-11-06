#!/usr/bin/env python3
"""All normative scan tables, checked against pinned native storage and spec.
This is scalar position ordering, not transform legality or frame conformance.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess

from av1_quantization_oracle import PIN, SIZES, digest, literal

SPEC_PIN='5e04f3f75e73a5898d7616c47c52f032144b8f80'
SPEC_SHA='9d9a54994541adbd61e83f9af95aad818ea8e5608d090a9553d2178bbef52998'
HOST=r'''
#include <stdio.h>
#include <stdlib.h>
#include "av1/common/scan.h"
int main(int argc,char **argv) {
  if(argc!=3)return 1;
  int tx=atoi(argv[1]),mode=atoi(argv[2]);
  if(tx<0||tx>=TX_SIZES_ALL||mode<0||mode>2)return 2;
  int w=tx_size_wide[tx],h=tx_size_high[tx],cw=w<32?w:32,ch=h<32?h:32;
  const SCAN_ORDER *order=&av1_scan_orders[tx][mode==1?V_DCT:mode==2?H_DCT:DCT_DCT];
  for(int i=0;i<cw*ch;i++) {
    int pos=order->scan[i];
    if(pos<0||pos>=cw*ch||order->iscan[pos]!=i)return 3;
    printf("%d ",(pos%ch)*cw+pos/ch);
  }
  puts("");return 0;
}
'''


def spec_tables(path):
    tables={};name=None
    with path.open() as f:
        for line in f:
            if name is None:
                match=re.match(r'(Default|Mrow|Mcol)_Scan_(\d+)x(\d+)\[\s*\d+\s*\] = \{',line)
                if not match:continue
                name=(match[1],int(match[2]),int(match[3]));tables[name]=[]
            elif '}' in line:
                name=None
            else:tables[name]+=list(map(int,re.findall(r'\d+',line)))
            if len(tables)==32 and name is None:break
    return tables


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True);p.add_argument('--aom',type=Path,required=True)
    a=p.parse_args();a.root=a.root.resolve();a.out=a.out.resolve();a.out.mkdir(parents=True,exist_ok=True)
    assert subprocess.check_output(['git','-C',str(a.aom),'rev-parse','HEAD'],text=True).strip()==PIN
    subprocess.run(['git','-C',str(a.aom),'diff-index','--quiet','HEAD','--'],check=True)
    spec=a.root/'build/scratch/av1/10.additional.tables.md'
    if not spec.exists():
        from urllib.request import urlopen
        with urlopen(f'https://raw.githubusercontent.com/AOMediaCodec/av1-spec/{SPEC_PIN}/10.additional.tables.md',timeout=30) as source,spec.open('wb') as output:
            while block:=source.read(65536):output.write(block)
    if digest(spec)!=SPEC_SHA:raise SystemExit('Pinned AV1 scan tables digest mismatch')
    tables=spec_tables(spec)
    library=Path(os.environ.get('AV1_AOM_LIBRARY',str(a.root/'build/native/aom/libaom.a')))
    config=Path(os.environ.get('AV1_AOM_CONFIG',str(a.root/'build/native/aom')))
    host=a.out/'oracle.c';host.write_text(HOST);binary=a.out/'oracle'
    subprocess.run(shlex.split(os.environ.get('CC','cc'))+['-std=c11','-O2','-I'+str(a.aom),'-I'+str(config),str(host),str(library),'-lm','-lpthread','-o',str(binary)],check=True)
    product=os.path.relpath(a.root/'src/entropy/coefficient_scan.zi',a.out)
    boundary=os.path.relpath(a.root/'tests/av1_coefficient_scan.zi',a.out)
    lines=[f'#import, file "{product}";',f'#import, file "{boundary}";'];cases=positions=0
    for tx,(w,h) in enumerate(SIZES):
        cw=min(w,32);ch=min(h,32)
        for mode in (0,1,2) if max(w,h)<=16 else (0,):
            expected=tables[('Default' if mode==0 else 'Mrow' if mode==1 else 'Mcol',cw,ch)]
            assert sorted(expected)==list(range(cw*ch))
            actual=list(map(int,subprocess.check_output([str(binary),str(tx),str(mode)],text=True).split()))
            assert actual==expected,(w,h,mode)
            lines += [f'Case{cases} :: () -> s32 {{']+literal('expected','u16',expected)+[
                f'    got := BuildCoefficientScan({w}, {h}, {mode})',
                f'    if got.error.code != DecodeOK || got.count != {cw*ch} || got.coded_width != {cw} || got.coded_height != {ch} {{ return 1 }}',
                '    for i: 0..1023 {',f'        if i < {cw*ch} {{',
                '            if got.positions[i] != expected[i] || got.inverse[expected[i]] != cast(u16)i { return 2 }',
                '        } else { if got.positions[i] != 0 || got.inverse[i] != 0 { return 3 } }',
                '    }; return 0','}']
            cases+=1;positions+=len(expected)
    lines+=['main :: () -> s32 {','    if ScanBoundaries() != 0 { return 1 }']+[
        f'    if Case{i}() != 0 {{ return {i+2} }}' for i in range(cases)]+['    return 0','}']
    module='av1_coefficient_scan_vectors';(a.out/f'{module}.zi').write_text('\n'.join(lines)+'\n')
    (a.out/'oracle.json').write_text(json.dumps(dict(revision=PIN,spec_revision=SPEC_PIN,spec_sha256=SPEC_SHA,
        library_sha256=digest(library),native_host_sha256=digest(host),fixture_sha256=digest(a.out/f'{module}.zi'),
        vectors=cases,positions=positions,compared_cells=cases*2048,transform_sizes=SIZES,
        scope='all normative default/row/column scan positions and inverse maps; caller tx-type/set legality and general entropy/frame integration excluded'),indent=2)+'\n')
    print(f'Pinned coefficient scans:{cases} normative/native vectors,{positions} positions plus full inverse/inactive arrays')


if __name__=='__main__':main()

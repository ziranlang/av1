#!/usr/bin/env python3
"""Pinned native coefficient contexts and independent normative offset tables.
This scalar corpus does not claim general coefficient/frame decoding.
"""
import argparse
import json
import os
from pathlib import Path
import random
import re
import shlex
import subprocess

from av1_quantization_oracle import PIN,SIZES,digest,literal
from av1_coefficient_scan_oracle import SPEC_PIN,SPEC_SHA,spec_tables

PARSING_SHA='6423515792cf1118dbd79c25171b9be628adc839aae811678a83eb6923bbca95'
HOST=r'''
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include "av1/common/scan.h"
#include "av1/common/txb_common.h"
int main(int argc,char **argv) {
  if(argc!=3)return 1;
  int tx=atoi(argv[1]),cls=atoi(argv[2]);
  if(tx<0||tx>=TX_SIZES_ALL||cls<0||cls>2)return 2;
  int w=tx_size_wide[tx],h=tx_size_high[tx],cw=w<32?w:32,ch=h<32?h:32;
  int bhl=0;while((1<<bhl)<ch)bhl++;
  uint8_t levels[2048]={0};int stride=ch+TX_PAD_HOR;
  for(int row=0;row<ch;row++)for(int col=0;col<cw;col++) {
    unsigned value;if(scanf("%u",&value)!=1||value>255)return 3;
    levels[col*stride+row]=value;
  }
  const SCAN_ORDER *scan=&av1_scan_orders[tx][cls==1?H_DCT:cls==2?V_DCT:DCT_DCT];
  for(int i=0;i<cw*ch;i++) {
    int pos=scan->scan[i],compact=(pos%ch)*cw+pos/ch;
    int base=get_lower_levels_ctx_general(0,i,bhl,cw,levels,pos,tx,cls);
    int br=get_br_ctx(levels,pos,bhl,cls);
    int eob=get_lower_levels_ctx_general(1,i,bhl,cw,levels,pos,tx,cls);
    printf("%d %d %d %d\n",compact,base,br,eob);
  }
  return 0;
}
'''


def read_offsets(path):
    values=[];active=False;depth=0
    with path.open() as f:
        for line in f:
            if not active:
                if not line.startswith('Coeff_Base_Ctx_Offset[ TX_SIZES_ALL ]'):continue
                active=True;line=line[line.index('{'):]
            values+=list(map(int,re.findall(r'\d+',line)))
            depth+=line.count('{')-line.count('}')
            if depth==0:break
    assert len(values)==19*25
    return values


def reference(levels,w,h,pos,idx,cls,offsets):
    cw=min(w,32);ch=min(h,32);row,col=divmod(pos,cw)
    def neighbor(dr,dc,limit):
        return min(levels[(row+dr)*cw+col+dc],limit) if row+dr<ch and col+dc<cw else 0
    sig=[(0,1),(1,0)]+([(1,1),(0,2),(2,0)] if cls==0 else [(0,2),(0,3),(0,4)] if cls==1 else [(2,0),(3,0),(4,0)])
    base=min((sum(neighbor(dr,dc,3) for dr,dc in sig)+1)//2,4)
    if cls==0:base=0 if pos==0 else base+offsets[min(row,4)*5+min(col,4)]
    else:base+=(26,31,36)[min(row if cls==2 else col,2)]
    mag=[(0,1),(1,0),(1,1) if cls==0 else (0,2) if cls==1 else (2,0)]
    br=min((sum(neighbor(dr,dc,15) for dr,dc in mag)+1)//2,6)
    if pos:
        near=(row<2 and col<2) if cls==0 else col==0 if cls==1 else row==0
        br+=7 if near else 14
    eob=0 if idx==0 else 1 if idx<=cw*ch//8 else 2 if idx<=cw*ch//4 else 3
    return base,br,eob


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True);p.add_argument('--aom',type=Path,required=True)
    a=p.parse_args();a.root=a.root.resolve();a.out=a.out.resolve();a.out.mkdir(parents=True,exist_ok=True)
    assert subprocess.check_output(['git','-C',str(a.aom),'rev-parse','HEAD'],text=True).strip()==PIN
    subprocess.run(['git','-C',str(a.aom),'diff-index','--quiet','HEAD','--'],check=True)
    parsing=a.root/'build/scratch/av1/parsing.md'
    # The pinned chapter9 parser/CDF definitions are the same primary source
    # used by the existing coefficient corpus. Never substitute a latest file.
    if not parsing.exists():
        from urllib.request import urlopen
        parsing.parent.mkdir(parents=True,exist_ok=True)
        with urlopen(f'https://raw.githubusercontent.com/AOMediaCodec/av1-spec/{SPEC_PIN}/09.parsing.process.md',timeout=30) as source,parsing.open('wb') as output:
            while block:=source.read(65536):output.write(block)
    if digest(parsing)!=PARSING_SHA:raise SystemExit('Pinned AV1 CDF-selection document digest mismatch')
    offsets=read_offsets(parsing)
    spec=a.root/'build/scratch/av1/10.additional.tables.md'
    if not spec.exists():
        from urllib.request import urlopen
        with urlopen(f'https://raw.githubusercontent.com/AOMediaCodec/av1-spec/{SPEC_PIN}/10.additional.tables.md',timeout=30) as source,spec.open('wb') as output:
            while block:=source.read(65536):output.write(block)
    assert digest(spec)==SPEC_SHA
    scans=spec_tables(spec)
    library=Path(os.environ.get('AV1_AOM_LIBRARY',str(a.root/'build/native/aom/libaom.a')))
    config=Path(os.environ.get('AV1_AOM_CONFIG',str(a.root/'build/native/aom')))
    host=a.out/'oracle.c';host.write_text(HOST);binary=a.out/'oracle'
    subprocess.run(shlex.split(os.environ.get('CC','cc'))+['-std=c11','-O2','-I'+str(a.aom),'-I'+str(config),str(host),str(library),'-lm','-lpthread','-o',str(binary)],check=True)
    rng=random.Random(0x41563143);cases=[];positions=0
    for tx,(w,h) in enumerate(SIZES):
        count=min(w,32)*min(h,32)
        for cls in (0,1,2) if max(w,h)<=16 else (0,):
            mode=0 if cls==0 else 2 if cls==1 else 1
            order=scans[('Default' if mode==0 else 'Mrow' if mode==1 else 'Mcol',min(w,32),min(h,32))]
            for pattern in range(3):
                levels=[0 if pattern==0 else 255 if pattern==1 else rng.randrange(256) for _ in range(count)]
                native=subprocess.check_output([str(binary),str(tx),str(cls)],input=' '.join(map(str,levels)),text=True)
                rows=[list(map(int,line.split())) for line in native.splitlines()]
                assert len(rows)==count
                for idx,(pos,base,br,eob) in enumerate(rows):
                    assert pos==order[idx]
                    expected=reference(levels,w,h,pos,idx,cls,offsets[tx*25:tx*25+25])
                    assert (base,br,eob)==expected,(w,h,cls,pattern,pos,(base,br,eob),expected)
                cases.append((w,h,cls,mode,levels,[r[1] for r in rows],[r[2] for r in rows],[r[3] for r in rows]))
                positions+=count
    imports=[f'#import, file "{os.path.relpath(a.root/path,a.out)}";' for path in (
        'src/entropy/coefficient_context.zi','src/entropy/coefficient_scan.zi','tests/av1_coefficient_context.zi')]
    modules=[]
    for shard in range((len(cases)+5)//6):
        module=f'av1_coefficient_context_vectors_{shard}';modules.append(module);lines=imports[:]
        subset=cases[shard*6:shard*6+6]
        for local,(w,h,cls,mode,levels,bases,brs,eobs) in enumerate(subset):
            count=len(levels)
            lines += [f'Case{local} :: () -> s32 {{']+literal('levels','u8',levels)+literal('bases','u8',bases)+literal('brs','u8',brs)+literal('eobs','u8',eobs)+[
                f'    saved: [{count}]u8; for i: 0..{count-1} {{ saved[i] = levels[i] }}',
                f'    scan := BuildCoefficientScan({w}, {h}, {mode})',
                '    if scan.error.code != DecodeOK { return 1 }',f'    for i: 0..{count-1} {{',
                f'        got := GetCoefficientContexts(levels[:], {w}, {h}, cast(s32)scan.positions[i], cast(s32)i, {cls}, false)',
                f'        last := GetCoefficientContexts(levels[:], {w}, {h}, cast(s32)scan.positions[i], cast(s32)i, {cls}, true)',
                '        if got.error.code != DecodeOK || last.error.code != DecodeOK { return 2 }',
                '        if got.base != cast(s32)bases[i] || got.br != cast(s32)brs[i] || last.base != cast(s32)eobs[i] || last.br != got.br { return 3 }',
                f'    }}; for i: 0..{count-1} {{ if levels[i] != saved[i] {{ return 4 }} }}',
                '    return 0','}']
        lines+=['main :: () -> s32 {','    if ContextBoundaries() != 0 { return 1 }']+[
            f'    if Case{i}() != 0 {{ return {i+2} }}' for i in range(len(subset))]+['    return 0','}']
        (a.out/f'{module}.zi').write_text('\n'.join(lines)+'\n')
    (a.out/'modules.txt').write_text('\n'.join(modules)+'\n')
    (a.out/'oracle.json').write_text(json.dumps(dict(revision=PIN,spec_revision=SPEC_PIN,parsing_sha256=digest(parsing),scan_tables_sha256=SPEC_SHA,
        library_sha256=digest(library),native_host_sha256=digest(host),vectors=len(cases),context_positions=positions,
        compared_context_fields=positions*4,shards=len(modules),
        fixtures_sha256={module:digest(a.out/f'{module}.zi') for module in modules},
        scope='scalar base/base-eob/BR context indices; symbol decoding,CDF defaults and general entropy/frame integration incomplete'),indent=2)+'\n')
    print(f'Pinned coefficient contexts:{len(cases)} planes,{positions} positions,{positions*4} context fields')


if __name__=='__main__':main()

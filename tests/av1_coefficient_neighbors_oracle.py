#!/usr/bin/env python3
"""Pinned get_txb_ctx plus the normative split level/DC context algorithm."""
import argparse
import json
import os
from pathlib import Path
import random
import shlex
import subprocess

from av1_quantization_oracle import PIN,SIZES,digest,literal
from av1_coefficient_context_oracle import PARSING_SHA

BLOCKS=[(4,4),(4,8),(8,4),(8,8),(8,16),(16,8),(16,16),(16,32),(32,16),(32,32),
        (32,64),(64,32),(64,64),(64,128),(128,64),(128,128),(4,16),(16,4),(8,32),(32,8),(16,64),(64,16)]
HOST=r'''
#include <stdio.h>
#include <stdlib.h>
#include "av1/common/txb_common.h"
int main(int argc,char **argv) {
  if(argc!=4)return 1;int tx=atoi(argv[1]),block=atoi(argv[2]),plane=atoi(argv[3]);
  if(tx<0||tx>=TX_SIZES_ALL||block<0||block>=BLOCK_SIZES_ALL||plane<0||plane>1)return 2;
  ENTROPY_CONTEXT above[16]={0},left[16]={0};
  for(int i=0;i<16;i++){unsigned value;if(scanf("%u",&value)!=1||value>23)return 3;above[i]=value;}
  for(int i=0;i<16;i++){unsigned value;if(scanf("%u",&value)!=1||value>23)return 3;left[i]=value;}
  TXB_CTX result;get_txb_ctx(block,tx,plane,above,left,&result);
  printf("%d %d\n",result.txb_skip_ctx,result.dc_sign_ctx);return 0;
}
'''


def reference(a,l,ad,ld,w,h,bw,bh,plane):
    dc=sum(-1 if v==1 else 1 if v==2 else 0 for v in ad+ld)
    if plane:
        skip=(10 if bw*bh>w*h else 7)+int(any(a+ad))+int(any(l+ld))
    elif (w,h)==(bw,bh):skip=0
    else:
        high=max(max(a,default=0),max(l,default=0));low=min(max(a,default=0),max(l,default=0))
        skip=1 if high==0 else 2+int(high>3) if low==0 else 4 if high<=3 else 5 if low<=3 else 6
    return [skip,1 if dc<0 else 2 if dc>0 else 0]


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True);p.add_argument('--aom',type=Path,required=True)
    a=p.parse_args();a.root=a.root.resolve();a.out=a.out.resolve();a.out.mkdir(parents=True,exist_ok=True)
    assert subprocess.check_output(['git','-C',str(a.aom),'rev-parse','HEAD'],text=True).strip()==PIN
    subprocess.run(['git','-C',str(a.aom),'diff-index','--quiet','HEAD','--'],check=True)
    parsing=a.root/'build/scratch/av1/parsing.md';assert digest(parsing)==PARSING_SHA
    library=Path(os.environ.get('AV1_AOM_LIBRARY',str(a.root/'build/native/aom/libaom.a')))
    config=Path(os.environ.get('AV1_AOM_CONFIG',str(a.root/'build/native/aom')))
    source=a.out/'oracle.c';source.write_text(HOST);binary=a.out/'oracle'
    subprocess.run(shlex.split(os.environ.get('CC','cc'))+['-std=c11','-O2','-I'+str(a.aom),'-I'+str(config),str(source),str(library),'-lm','-o',str(binary)],check=True)
    rng=random.Random(0x4E454947);cases=[];coverage=set()
    for tx,(w,h) in enumerate(SIZES):
        for b,(bw,bh) in enumerate(BLOCKS):
            if bw<w or bh<h:continue
            for plane in range(2):
                for pattern in range(12):
                    na=w//4 if pattern<9 else rng.randrange(w//4+1)
                    nl=h//4 if pattern<9 else rng.randrange(h//4+1)
                    top=(0,1,4)[pattern%3];left=(0,1,4)[(pattern//3)%3]
                    above=[top if pattern<9 else rng.randrange(64) for _ in range(na)]
                    side=[left if pattern<9 else rng.randrange(64) for _ in range(nl)]
                    # Zero-level/nonzero-DC is meaningful at the20-bit wrap.
                    ad=[pattern%3 if pattern<9 else rng.randrange(3) for _ in range(na)]
                    ld=[(pattern//3)%3 if pattern<9 else rng.randrange(3) for _ in range(nl)]
                    # Native packs min(level,7) with two sign bits. This retains
                    # the normative zero/1..3/>3 classification and DC sum.
                    packed_a=[min(v,7)+(d<<3) for v,d in zip(above,ad)]+[0]*(16-na)
                    packed_l=[min(v,7)+(d<<3) for v,d in zip(side,ld)]+[0]*(16-nl)
                    native=list(map(int,subprocess.check_output([str(binary),str(tx),str(b),str(plane)],
                        input=' '.join(map(str,packed_a+packed_l))+'\n',text=True).split()))
                    expected=reference(above,side,ad,ld,w,h,bw,bh,plane)
                    assert native==expected,(tx,b,plane,pattern,native,expected)
                    coverage.add(tuple(native));cases.append(dict(w=w,h=h,bw=bw,bh=bh,plane=plane,
                        above=above,left=side,ad=ad,ld=ld,expected=expected))
    imports=[f'#import, file "{os.path.relpath(a.root/path,a.out)}";' for path in (
        'src/entropy/coefficient_neighbors.zi','tests/av1_coefficient_neighbors.zi')]
    imports+=['using Codec :: #import "image_codec/api"']
    modules=[]
    for shard in range((len(cases)+191)//192):
        module=f'av1_coefficient_neighbor_vectors_{shard}';modules.append(module);lines=imports[:]
        subset=cases[shard*192:shard*192+192]
        for local,c in enumerate(subset):
            lines += [f'Case{local} :: () -> s32 {{']
            for name,key in (('above','above'),('left','left'),('ad','ad'),('ld','ld')):lines+=literal(name,'u8',c[key] or [0])
            lines += [f'    result := GetTransformCoefficientContexts(above[:{len(c["above"])}], left[:{len(c["left"])}], ad[:{len(c["ad"])}], ld[:{len(c["ld"])}], {c["w"]}, {c["h"]}, {c["bw"]}, {c["bh"]}, {c["plane"]})',
                f'    if result.error.code != DecodeOK || result.skip != {c["expected"][0]} || result.dc != {c["expected"][1]} {{ return 1 }}',
                '    return 0','}']
        lines += ['main :: () -> s32 {','    if CoefficientNeighborBoundaries() != 0 { return 99 }']+[
            f'    if Case{i}() != 0 {{ return 1 }}' for i in range(len(subset))]+['    return 0','}']
        (a.out/f'{module}.zi').write_text('\n'.join(lines)+'\n')
    assert set(skip for skip,dc in coverage)==set(range(13)) and set(dc for skip,dc in coverage)=={0,1,2}
    (a.out/'modules.txt').write_text('\n'.join(modules)+'\n')
    (a.out/'oracle.json').write_text(json.dumps(dict(revision=PIN,library_sha256=digest(library),native_host_sha256=digest(source),
        parsing_sha256=PARSING_SHA,cases=len(cases),compared_context_fields=len(cases)*2,
        transform_sizes=SIZES,residual_block_sizes=BLOCKS,skip_contexts=list(range(13)),dc_contexts=[0,1,2],
        fixtures_sha256={m:digest(a.out/f'{m}.zi') for m in modules},
        scope='native/spec all_zero and DC edge-context selection; tile edge scheduling outside component coverage'),indent=2)+'\n')
    print(f'Pinned coefficient neighbors:{len(cases)} native/spec cases, all13 skip/3DC contexts,19Tx/22block sizes')


if __name__=='__main__':main()

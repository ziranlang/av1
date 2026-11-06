#!/usr/bin/env python3
"""Check scalar lossless inverse transforms against the AV1 integer process
and the actual pinned libaom scalar kernel. This is a kernel unit corpus,
not a claim of full AV1 bitstream conformance.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import shlex
import subprocess

PIN = 'd772e334cc724105040382a977ebb10dfd393293'
HOST = r'''
#include <stdio.h>
#include <stdint.h>
#include "aom_ports/mem.h"
void av1_highbd_iwht4x4_16_add_c(const int32_t*,uint8_t*,int,int);
int main(void) {
  int depth;
  while(scanf("%d",&depth)==1) {
    if(depth!=8&&depth!=10&&depth!=12)return 1;
    int32_t input[16];uint16_t output[28];
    for(int i=0;i<16;i++) {int q;if(scanf("%d",&q)!=1)return 2;input[(i%4)*4+i/4]=q*4;}
    for(int i=0;i<28;i++)output[i]=65535;
    for(int y=0;y<4;y++)for(int x=0;x<4;x++)output[y*7+x]=1u<<(depth-1);
    av1_highbd_iwht4x4_16_add_c(input,CONVERT_TO_BYTEPTR(output),7,depth);
    for(int y=0;y<4;y++) {
      for(int x=0;x<4;x++)printf("%u ",output[y*7+x]);
      for(int x=4;x<7;x++)if(output[y*7+x]!=65535)return 3;
    }
    printf("\n");
  }
  return 0;
}
'''


def wht(vector, shift):
    a, c, d, b = (value >> shift for value in vector)
    a += c
    d -= b
    e = (a-d) >> 1
    b, c = e-b, e-c
    return [a-b, b, c, d+c]


def residual(quant):
    rows = [wht([value*4 for value in quant[i:i+4]], 2) for i in range(0,16,4)]
    result = [0]*16
    for column in range(4):
        for row, value in enumerate(wht([rows[r][column] for r in range(4)], 0)):
            result[row*4+column] = value
    return result


def run(command, **kwargs):
    return subprocess.run(command, check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--aom',type=Path,required=True)
    args=parser.parse_args();args.out.mkdir(parents=True,exist_ok=True)
    if run(['git','-C',str(args.aom),'rev-parse','HEAD'],text=True,capture_output=True).stdout.strip()!=PIN:
        raise SystemExit('pinned libaom revision mismatch')
    run(['git','-C',str(args.aom),'diff','--quiet','HEAD','--'])
    source=args.out/'oracle.c';source.write_text(HOST)
    host=args.out/'oracle'
    library=Path(os.environ.get('AV1_AOM_LIBRARY',str(args.root/'build/native/aom/libaom.a')))
    config=Path(os.environ.get('AV1_AOM_CONFIG',str(args.root/'build/native/aom')))
    run(shlex.split(os.environ.get('CC','cc'))+['-std=c11','-O2','-I'+str(args.aom),
        '-I'+str(config),str(source),str(library),'-lm','-o',str(host)])
    random_source=random.Random(0x415631574854)
    cases=[]
    for depth in (8,10,12):
        quantizers=[[0]*16]
        for position in range(16):
            for value in (-9,-1,1,9):
                quant=[0]*16;quant[position]=value;quantizers.append(quant)
        amplitude=(1<<depth)//16
        quantizers += [[random_source.randrange(-amplitude,amplitude+1) for _ in range(16)] for _ in range(64)]
        for quant in quantizers:
            expected=residual(quant)
            if not all(-(1<<depth)<=value<(1<<depth) for value in expected):raise AssertionError('test range')
            cases.append((depth,quant,expected))
    inputs=''.join(str(depth)+' '+' '.join(map(str,quant))+'\n' for depth,quant,_ in cases)
    actual=run([str(host)],input=inputs,text=True,capture_output=True).stdout.splitlines()
    if len(actual)!=len(cases):raise SystemExit('native case count mismatch')
    for (depth,quant,expected),line in zip(cases,actual):
        midpoint=1<<(depth-1);maximum=(1<<depth)-1
        if list(map(int,line.split()))!=[min(maximum,max(0,value+midpoint)) for value in expected]:
            raise SystemExit('native WHT disagrees with integer specification')
    relative=os.path.relpath(args.root/'src/transform/lossless.zi',args.out)
    bounds=os.path.relpath(args.root/'tests/av1_scalar_kernels.zi',args.out)
    lines=['#import, file "'+relative+'";','#import, file "'+bounds+'";','#program_export','main :: () -> s32 {',
        '    if ScalarKernelBounds() != 0 { return 99 }']
    # Separate procedures keep source expressions and generated VM code bounded.
    for group in range((len(cases)+15)//16):lines.append(f'    if Group{group}() != 0 {{ return {group+1} }}')
    lines+=['    return 0','}']
    for group in range((len(cases)+15)//16):
        lines.append(f'Group{group} :: () -> s32 {{')
        for depth,quant,expected in cases[group*16:group*16+16]:
            lines+=['    {','        quant: [16]s32 = .['+','.join(map(str,quant))+']',
                f'        result := InverseLossless4x4(quant[:], {depth})',
                '        if result.error.code != DecodeOK { return 1 }',
                '        expected: [16]s32 = .['+','.join(map(str,expected))+']',
                '        for i: 0..15 { if result.samples[i] != expected[i] { return 2 } }','    }']
        lines+=['    return 0','}']
    (args.out/'av1_scalar_vectors.zi').write_text('\n'.join(lines)+'\n')
    hasher=hashlib.sha256()
    with library.open('rb') as binary:
        for chunk in iter(lambda:binary.read(1024*1024),b''):hasher.update(chunk)
    (args.out/'oracle.json').write_text(json.dumps(dict(revision=PIN,library_sha256=hasher.hexdigest(),
        cases=len(cases),seed='0x415631574854',depths=[8,10,12],scope='lossless 4x4 scalar inverse WHT'),indent=2)+'\n')
    print(f'Pinned libaom scalar WHT: {len(cases)} signed impulse/random vectors; 8/10/12-bit')


if __name__=='__main__':main()

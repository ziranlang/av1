#!/usr/bin/env python3
"""Scalar quantization corpus: pinned native lookup/matrix data and AV1 7.12.
These coefficient math vectors do not claim lossy encoded-frame conformance.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import shlex
import subprocess

PIN = 'b141d9c45db236335351c8a4b2a14b3ade27c937'
SIZES = [(4,4),(8,8),(16,16),(32,32),(64,64),(4,8),(8,4),(8,16),(16,8),
         (16,32),(32,16),(32,64),(64,32),(4,16),(16,4),(8,32),(32,8),(16,64),(64,16)]
HOST = r'''
#include <stdio.h>
#include <stdlib.h>
#include "av1/common/av1_common_int.h"
#include "av1/common/quant_common.h"
int main(int argc,char **argv) {
  if(argc==1) {
    for(int depth=8;depth<=12;depth+=2) {
      for(int q=0;q<256;q++)printf("%d ",av1_dc_quant_QTX(q,0,depth));
      for(int q=0;q<256;q++)printf("%d ",av1_ac_quant_QTX(q,0,depth));
      puts("");
      for(int q=0;q<256;q++)for(int d=-64;d<64;d++) {
        int dc=q+d,ac=q-1-d;
        if(dc<0)dc=0;if(dc>255)dc=255;if(ac<0)ac=0;if(ac>255)ac=255;
        if(av1_dc_quant_QTX(q,d,depth)!=av1_dc_quant_QTX(dc,0,depth)||
           av1_ac_quant_QTX(q,-1-d,depth)!=av1_ac_quant_QTX(ac,0,depth))return 2;
      }
    }
    return 0;
  }
  if(argc!=4)return 3;
  int tx=atoi(argv[1]),level=atoi(argv[2]),plane=atoi(argv[3]);
  if(tx<0||tx>=TX_SIZES_ALL||level<0||level>=15||plane<0||plane>1)return 4;
  CommonQuantParams params={0};av1_qm_init(&params,3);
  int w=tx_size_wide[tx],h=tx_size_high[tx];
  printf("%d %d ",w,h);
  int cw=w<32?w:32,ch=h<32?h:32;
  const qm_val_t *matrix=params.giqmatrix[level][plane][tx];
  if(!matrix)return 5;
  for(int i=0;i<cw*ch;i++)printf("%u ",(unsigned)matrix[i]);
  puts("");return 0;
}
'''


def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def literal(name,kind,values):
    return [f'    {name}: [{len(values)}]{kind} = .[']+[
        '        '+','.join(map(str,values[i:i+16]))+',' for i in range(0,len(values),16)]+['    ]']


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True);p.add_argument('--aom',type=Path,required=True)
    a=p.parse_args();a.root=a.root.resolve();a.out=a.out.resolve();a.out.mkdir(parents=True,exist_ok=True)
    assert subprocess.check_output(['git','-C',str(a.aom),'rev-parse','HEAD'],text=True).strip()==PIN
    subprocess.run(['git','-C',str(a.aom),'diff-index','--quiet','HEAD','--'],check=True)
    library=Path(os.environ.get('AV1_AOM_LIBRARY',str(a.root/'build/native/aom/libaom.a')))
    config=Path(os.environ.get('AV1_AOM_CONFIG',str(a.root/'build/native/aom')))
    host=a.out/'oracle.c';host.write_text(HOST);binary=a.out/'oracle'
    subprocess.run(shlex.split(os.environ.get('CC','cc'))+['-std=c11','-O2','-I'+str(a.aom),'-I'+str(config),str(host),str(library),'-lm','-lpthread','-o',str(binary)],check=True)
    tables=[list(map(int,line.split())) for line in subprocess.check_output([str(binary)],text=True).splitlines()]
    assert len(tables)==3 and all(len(table)==512 for table in tables)
    relative=os.path.relpath(a.root/'tests/av1_quantization.zi',a.out)
    product=os.path.relpath(a.root/'src/transform/quantization.zi',a.out)
    imports=[f'#import, file "{relative}";',f'#import, file "{product}";']
    lines=imports[:]
    for depth,table in zip((8,10,12),tables):
        lines += [f'Lookup{depth} :: () -> s32 {{']+literal('expected','s32',table)+[
            '    for q: 0..255 { for d: -64..63 {',
            f'        got := GetQuantizationSteps(cast(s32)q, cast(s32)d, cast(s32)(-1 - d), {depth})',
            '        dc := q + d; ac := q - 1 - d',
            '        if dc < 0 { dc = 0 }; if dc > 255 { dc = 255 }',
            '        if ac < 0 { ac = 0 }; if ac > 255 { ac = 255 }',
            '        if got.error.code != DecodeOK || got.dc != expected[dc] || got.ac != expected[256 + ac] { return 1 }',
            '    } }; return 0','}']
    lines += ['main :: () -> s32 {','    if QuantizationBoundaries() != 0 { return 1 }','    if SparseDequantization() != 0 { return 3 }',
              '    if Lookup8() != 0 || Lookup10() != 0 || Lookup12() != 0 { return 2 }; return 0','}']
    modules=['av1_quant_lookup'];(a.out/f'{modules[0]}.zi').write_text('\n'.join(lines)+'\n')
    rng=random.Random(0x41563171);cases=[];masked=clipped=0;matrix_levels=set();matrix_planes=set()
    for depth,table in zip((8,10,12),tables):
        for tx,(w,h) in enumerate(SIZES):
            for use_matrix in (False,True):
                number=len(cases);cw=min(w,32);ch=min(h,32);count=cw*ch
                qindex=(1,4,20,95,128,200,255)[number%7]
                dc_delta=(-64,-1,0,1,63)[number%5];ac_delta=(63,1,0,-1,-64)[number%5]
                dc=table[max(0,min(255,qindex+dc_delta))];ac=table[256+max(0,min(255,qindex+ac_delta))]
                weights=[];level=number%15;plane=(number//2)%2
                if use_matrix:
                    values=list(map(int,subprocess.check_output([str(binary),str(tx),str(level),str(plane)],text=True).split()))
                    assert values[:2]==[w,h];weights=values[2:]
                    assert len(weights)==count and all(1<=x<=255 for x in weights)
                    matrix_levels.add(level);matrix_planes.add(plane)
                quant=[rng.randrange(-1048575,1048576) for _ in range(count)]
                boundaries=[0,1,-1,1048575,-1048575]
                # Place signed levels immediately around a real 24-bit wrap.
                step=(ac*weights[5]+16)//32 if weights else ac
                edge=(1<<24)//step
                boundaries += [max(-1048575,min(1048575,s*(edge+offset))) for s in (1,-1) for offset in (-1,0,1)]
                quant[:len(boundaries)]=boundaries
                denominator=4 if w*h>1024 else 2 if w*h>256 else 1
                limit=1<<(depth+7);expected=[]
                for at,value in enumerate(quant):
                    step=dc if at==0 else ac
                    if weights:step=(step*weights[at]+16)//32
                    product=abs(value)*step
                    masked+=product>0xffffff
                    magnitude=(product%0x1000000)//denominator
                    signed=-magnitude if value<0 else magnitude
                    clipped+=not -limit<=signed<limit
                    expected.append(max(-limit,min(limit-1,signed)))
                cases.append(dict(width=w,height=h,depth=depth,qindex=qindex,dc_delta=dc_delta,ac_delta=ac_delta,
                                  quant=quant,weights=weights,expected=expected))
    for shard in range((len(cases)+5)//6):
        module=f'av1_dequant_vectors_{shard}';modules.append(module);lines=imports[:]
        subset=cases[shard*6:shard*6+6]
        for local,case in enumerate(subset):
            count=len(case['quant']);w=case['width'];h=case['height'];cw=min(w,32);ch=min(h,32)
            lines += [f'Case{local} :: () -> s32 {{']+literal('quant','s32',case['quant'])
            lines += literal('weights','u8',case['weights']) if case['weights'] else [f'    weights: [{count}]u8']
            lines += literal('expected','s32',case['expected'])+[
                f'    saved: [{count}]s32; saved_weights: [{count}]u8',
                f'    for i: 0..{count-1} {{ saved[i] = quant[i]; saved_weights[i] = weights[i] }}',
                f'    got := DequantizeBlock(quant[:], {w}, {h}, {case["qindex"]}, {case["dc_delta"]}, {case["ac_delta"]}, {case["depth"]}, weights[:{count if case["weights"] else 0}])',
                f'    if got.error.code != DecodeOK || got.width != {w} || got.height != {h} {{ return 1 }}',
                '    for i: 0..4095 {',f'        row := i / {w}; column := i % {w}',
                f'        value := ifx row < {ch} && column < {cw} then expected[row * {cw} + column] else 0',
                '        if got.samples[i] != value { return 2 }','    }',
                f'    for i: 0..{count-1} {{ if quant[i] != saved[i] || weights[i] != saved_weights[i] {{ return 3 }} }}',
                '    return 0','}']
        lines += ['main :: () -> s32 {']+[f'    if Case{i}() != 0 {{ return {i+1} }}' for i in range(len(subset))]+['    return 0','}']
        (a.out/f'{module}.zi').write_text('\n'.join(lines)+'\n')
    (a.out/'modules.txt').write_text('\n'.join(modules)+'\n')
    assert matrix_levels==set(range(15)) and matrix_planes=={0,1} and masked and clipped
    receipt=dict(revision=PIN,library_sha256=digest(library),native_host_sha256=digest(host),
        lookup_values=1536,lookup_cases=98304,native_delta_calls=196608,dequant_vectors=len(cases),
        compact_coefficients=sum(len(c['quant']) for c in cases),compared_output_cells=4096*len(cases),
        masked_products=masked,clipped_coefficients=clipped,depths=[8,10,12],transform_sizes=SIZES,
        matrix_levels=sorted(matrix_levels),matrix_planes=sorted(matrix_planes),shards=len(modules),
        fixtures_sha256={name:digest(a.out/f'{name}.zi') for name in modules},
        scope='scalar lookup/matrix data and normative dequantization; caller matrix selection, general entropy and lossy frame reconstruction are outside coverage')
    (a.out/'oracle.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(f'Pinned quantization:1536 lookup values,98304 delta cases;{len(cases)} dequant blocks/all19 sizes,{masked} wraps,{clipped} clips')


if __name__=='__main__':main()

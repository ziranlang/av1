#!/usr/bin/env python3
"""Pinned scalar Wiener vectors with an independent normative round/clip oracle."""
import argparse
import hashlib
import itertools
import json
import os
from pathlib import Path
import random
import shlex
from av1_lossless_oracle import PIN, digest, run
from av1_cdef_oracle import sample, ZIRAN_GENERATOR

HOST = r'''
#include <stdint.h>
#include <stdio.h>
#include "aom_ports/mem.h"
#include "av1/common/convolve.h"
#include "config/av1_rtcd.h"
int main(void) {
  int depth,w,h;
  while(scanf("%d%d%d",&depth,&w,&h)==3) {
    DECLARE_ALIGNED(256,int16_t,fx[16][8]);
    DECLARE_ALIGNED(256,int16_t,fy[16][8]);
    int taps[2][3];
    for(int d=0;d<2;d++)for(int i=0;i<3;i++)if(scanf("%d",&taps[d][i])!=1)return 1;
    for(int phase=0;phase<16;phase++) {
      for(int i=0;i<8;i++)fx[phase][i]=fy[phase][i]=0;
      for(int i=0;i<3;i++) {
        fx[phase][i]=fx[phase][6-i]=taps[0][i];fx[phase][3]-=2*taps[0][i];
        fy[phase][i]=fy[phase][6-i]=taps[1][i];fy[phase][3]-=2*taps[1][i];
      }
    }
    uint16_t input[16*16]={0},output[8*11];
    for(int y=0;y<h+6;y++)for(int x=0;x<w+6;x++) {
      unsigned value;if(scanf("%u",&value)!=1||value>=(1u<<depth))return 2;
      input[y*16+x]=value;
    }
    for(int i=0;i<8*11;i++)output[i]=65535;
    WienerConvolveParams params=get_conv_params_wiener(depth);
    av1_highbd_wiener_convolve_add_src_c(CONVERT_TO_BYTEPTR(input+3*16+3),16,
      CONVERT_TO_BYTEPTR(output),11,fx[0],16,fy[0],16,w,h,&params,depth);
    for(int y=0;y<8;y++)for(int x=0;x<11;x++) {
      if(y<h&&x<w)printf("%u ",output[y*11+x]);
      else if(output[y*11+x]!=65535)return 3;
    }
    printf("\n");
  }
  return 0;
}
'''


def reference(pixels, w, h, horizontal, vertical, depth):
    def expand(coeff):
        return coeff+[128-2*sum(coeff)]+coeff[::-1]
    hf, vf = expand(horizontal), expand(vertical)
    r0 = 5 if depth == 12 else 3
    r1 = 14-r0
    offset = 1 << (depth+7-r0-1)
    limit = (1 << (depth+1+7-r0))-1
    intermediate = []
    horizontal_clips = 0
    for row in pixels:
        values = []
        for x in range(w):
            raw = (sum(hf[t]*row[x+t] for t in range(7))+(1 << (r0-1))) >> r0
            value = max(-offset,min(limit-offset,raw))
            horizontal_clips += value != raw
            values.append(value)
        intermediate.append(values)
    result = []
    final_clips = 0
    for y in range(h):
        for x in range(w):
            raw = (sum(vf[t]*intermediate[y+t][x] for t in range(7))+(1 << (r1-1))) >> r1
            value = max(0,min((1 << depth)-1,raw))
            final_clips += value != raw
            result.append(value)
    return result,horizontal_clips,final_clips


def main():
    parser = argparse.ArgumentParser()
    for name in ('root','out','aom'):
        parser.add_argument('--'+name,type=Path,required=True)
    a = parser.parse_args()
    a.out.mkdir(parents=True,exist_ok=True)
    if run(['git','-C',str(a.aom),'rev-parse','HEAD'],text=True,capture_output=True).stdout.strip() != PIN:
        raise SystemExit('libaom pin mismatch')
    run(['git','-C',str(a.aom),'diff','--quiet','HEAD','--'])
    config = Path(os.environ.get('AV1_AOM_CONFIG',str(a.root/'build/native/aom')))
    library = Path(os.environ.get('AV1_AOM_LIBRARY',str(a.root/'build/native/aom/libaom.a')))
    source = a.out/'oracle.c';source.write_text(HOST);host = a.out/'oracle'
    run(shlex.split(os.environ.get('CC','cc'))+['-std=c11','-O2','-I'+str(a.aom),'-I'+str(config),str(source),str(library),'-lm','-o',str(host)])
    boundaries = list(itertools.product((-5,0,10),(-23,0,8),(-17,0,46)))
    rng = random.Random(0x4156315749454e45)
    cases = [];intermediate_clips = 0;output_clips = 0
    for depth in (8,10,12):
        for shape,(w,h) in enumerate(((1,1),(4,4),(8,8),(3,7))):
            for index in range(48):
                seed = shape*48+index
                if index < 27:
                    horizontal = list(boundaries[index]);vertical = list(boundaries[(index*7)%27])
                else:
                    horizontal = [rng.randint(lo,hi) for lo,hi in ((-5,10),(-23,8),(-17,46))]
                    vertical = [rng.randint(lo,hi) for lo,hi in ((-5,10),(-23,8),(-17,46))]
                if index == 27:
                    horizontal = vertical = [0,0,0]
                if index >= 40:
                    horizontal[0] = vertical[0] = 0  # Chroma 5-tap subset.
                pixels = [[sample(seed,y,x,depth) for x in range(w+6)] for y in range(h+6)]
                expected,ic,oc = reference(pixels,w,h,horizontal,vertical,depth)
                intermediate_clips += ic;output_clips += oc
                cases.append(dict(depth=depth,w=w,h=h,seed=seed,horizontal=horizontal,vertical=vertical,pixels=pixels,expected=expected))
    data = []
    for c in cases:
        params = [c['depth'],c['w'],c['h']]+c['horizontal']+c['vertical']
        data.append(' '.join(map(str,params+[v for row in c['pixels'] for v in row])))
    outputs = run([str(host)],input='\n'.join(data)+'\n',text=True,capture_output=True).stdout.splitlines()
    if len(outputs) != len(cases):
        raise SystemExit('native Wiener vector count mismatch')
    for i,(c,line) in enumerate(zip(cases,outputs)):
        actual = list(map(int,line.split()))
        if actual != c['expected']:
            raise SystemExit(f'native/spec Wiener disagreement at {i}: {actual[:8]} vs {c["expected"][:8]}')
    if not intermediate_clips or not output_clips:
        raise SystemExit('Wiener clipping coverage missing')
    module = os.path.relpath(a.root/'src/filter/wiener.zi',a.out)
    bounds = os.path.relpath(a.root/'tests/av1_wiener.zi',a.out)
    generator = ZIRAN_GENERATOR.split('DirectionInput ::')[0]+'''
WienerInput :: (seed: s32, depth: s32, width: s32, height: s32) -> [196]u16 {
    result: [196]u16
    for i: 0..195 { result[i] = 65535 }
    for y: 0..height+5 { for x: 0..width+5 {
        result[y*14+x] = Sample(seed,cast(s32)y,cast(s32)x,depth)
    } }
    return result
}
'''
    lines = ['#import, file "'+module+'";','#import, file "'+bounds+'";','using Codec :: #import "image_codec/api"',generator,
             '#program_export','main :: () -> s32 {','    if WienerBoundaries() != 0 { return 99 }']
    groups = (len(cases)+15)//16
    for group in range(groups):
        lines.append(f'    if Group{group}() != 0 {{ return {group+1} }}')
    lines += ['    return 0','}']
    for group in range(groups):
        lines.append(f'Group{group} :: () -> s32 {{')
        for c in cases[group*16:(group+1)*16]:
            lines += ['    {',f'        input := WienerInput({c["seed"]}, {c["depth"]}, {c["w"]}, {c["h"]})',
                      '        horizontal: [3]s32 = .['+','.join(map(str,c['horizontal']))+']',
                      '        vertical: [3]s32 = .['+','.join(map(str,c['vertical']))+']',
                      f'        result := FilterWienerBlock(input[:], 14, {c["w"]}, {c["h"]}, horizontal[:], vertical[:], {c["depth"]})',
                      '        expected: ['+str(c['w']*c['h'])+']u16 = .['+','.join(map(str,c['expected']))+']',
                      f'        if result.error.code != DecodeOK || result.width != {c["w"]} || result.height != {c["h"]} {{ return 1 }}',
                      f'        for y: 0..{c["h"]-1} {{ for x: 0..{c["w"]-1} {{ if result.samples[y*8+x] != expected[y*{c["w"]}+x] {{ return 2 }} }} }}',
                      f'        for j: 0..63 {{ if (j/8 >= {c["h"]} || j%8 >= {c["w"]}) && result.samples[j] != 0 {{ return 3 }} }}','    }']
        lines += ['    return 0','}']
    (a.out/'av1_wiener_vectors.zi').write_text('\n'.join(lines)+'\n')
    (a.out/'oracle.json').write_text(json.dumps(dict(revision=PIN,library_sha256=digest(library),host_sha256=hashlib.sha256(HOST.encode()).hexdigest(),
        vectors=len(cases),depths=[8,10,12],coefficient_limits=[[-5,10],[-23,8],[-17,46]],boundary_combinations=27,
        intermediate_clips=intermediate_clips,output_clips=output_clips,block_shapes=[[1,1],[4,4],[8,8],[3,7]],
        scope='standalone scalar Wiener math with caller-resolved 3-pixel halo; no frame/stripe filtering integration'),indent=2)+'\n')
    print(f'Pinned Wiener oracle: {len(cases)} vectors, {intermediate_clips} intermediate + {output_clips} output clips')


if __name__ == '__main__':
    main()

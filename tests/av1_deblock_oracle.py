#!/usr/bin/env python3
"""AV1 edge masks/narrow/wide samples, one line and four-line segments, differential pinned libaom oracle."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import shlex
from av1_lossless_oracle import PIN, digest, run

HOST = r'''
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include "config/aom_dsp_rtcd.h"
typedef void (*filter_fn)(uint16_t *,int,const uint8_t *,const uint8_t *,const uint8_t *,int);
int main(void) {
  int depth,length,blimit,limit,threshold;
  while(scanf("%d%d%d%d%d",&depth,&length,&blimit,&limit,&threshold)==5) {
    uint16_t input[16],horizontal[16*20],vertical[16*20];
    uint8_t b=blimit,l=limit,t=threshold;
    for(int i=0;i<16;i++){unsigned value;if(scanf("%u",&value)!=1||value>=(1u<<depth))return 1;input[i]=value;}
    for(int i=0;i<16*20;i++)horizontal[i]=vertical[i]=65535;
    for(int i=0;i<16;i++)for(int lane=0;lane<4;lane++){
      horizontal[i*20+lane+3]=input[i];vertical[(lane+3)*20+i]=input[i];
    }
    filter_fn hf,vf;
    switch(length){
      case 4:hf=aom_highbd_lpf_horizontal_4_c;vf=aom_highbd_lpf_vertical_4_c;break;
      case 6:hf=aom_highbd_lpf_horizontal_6_c;vf=aom_highbd_lpf_vertical_6_c;break;
      case 8:hf=aom_highbd_lpf_horizontal_8_c;vf=aom_highbd_lpf_vertical_8_c;break;
      case 14:hf=aom_highbd_lpf_horizontal_14_c;vf=aom_highbd_lpf_vertical_14_c;break;
      default:return 2;
    }
    hf(horizontal+8*20+3,20,&b,&l,&t,depth);vf(vertical+3*20+8,20,&b,&l,&t,depth);
    for(int i=0;i<16;i++) {
      for(int lane=0;lane<4;lane++)if(horizontal[i*20+lane+3]!=horizontal[i*20+3]||vertical[(lane+3)*20+i]!=horizontal[i*20+3])return 3;
      printf("%u ",horizontal[i*20+3]);
    }
    for(int y=0;y<16;y++)for(int x=0;x<20;x++) {
      if((x<3||x>=7)&&horizontal[y*20+x]!=65535)return 4;
      if((y<3||y>=7||x>=16)&&vertical[y*20+x]!=65535)return 5;
    }
    printf("\n");
  }
  return 0;
}
'''

# Four different lines per segment, as the frame filter applies them.
LINES_HOST = r'''
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include "config/aom_dsp_rtcd.h"
typedef void (*filter_fn)(uint16_t *,int,const uint8_t *,const uint8_t *,const uint8_t *,int);
int main(void) {
  int depth,length,blimit,limit,threshold;
  while(scanf("%d%d%d%d%d",&depth,&length,&blimit,&limit,&threshold)==5) {
    uint16_t horizontal[16*20],vertical[16*20];
    uint8_t b=blimit,l=limit,t=threshold;
    for(int i=0;i<16*20;i++)horizontal[i]=vertical[i]=65535;
    for(int lane=0;lane<4;lane++)for(int i=0;i<16;i++){
      unsigned value;if(scanf("%u",&value)!=1||value>=(1u<<depth))return 1;
      horizontal[i*20+lane+3]=value;vertical[(lane+3)*20+i]=value;
    }
    filter_fn hf,vf;
    switch(length){
      case 4:hf=aom_highbd_lpf_horizontal_4_c;vf=aom_highbd_lpf_vertical_4_c;break;
      case 6:hf=aom_highbd_lpf_horizontal_6_c;vf=aom_highbd_lpf_vertical_6_c;break;
      case 8:hf=aom_highbd_lpf_horizontal_8_c;vf=aom_highbd_lpf_vertical_8_c;break;
      case 14:hf=aom_highbd_lpf_horizontal_14_c;vf=aom_highbd_lpf_vertical_14_c;break;
      default:return 2;
    }
    hf(horizontal+8*20+3,20,&b,&l,&t,depth);vf(vertical+3*20+8,20,&b,&l,&t,depth);
    for(int lane=0;lane<4;lane++)for(int i=0;i<16;i++) {
      if(vertical[(lane+3)*20+i]!=horizontal[i*20+lane+3])return 3;
      printf("%u ",horizontal[i*20+lane+3]);
    }
    for(int y=0;y<16;y++)for(int x=0;x<20;x++) {
      if((x<3||x>=7)&&horizontal[y*20+x]!=65535)return 4;
      if((y<3||y>=7||x>=16)&&vertical[y*20+x]!=65535)return 5;
    }
    printf("\n");
  }
  return 0;
}
'''


def reference(source,length,blimit,limit,threshold,depth):
    scale = 1 << (depth-8)
    p0,q0,p1,q1 = source[7],source[8],source[6],source[9]
    hev = abs(p1-p0)>threshold*scale or abs(q1-q0)>threshold*scale
    extent = 1 if length == 4 else 2 if length == 6 else 3
    enabled = 2*abs(p0-q0)+abs(p1-q1)//2 <= blimit*scale
    enabled &= all(abs(source[7-i]-source[8-i])<=limit*scale and abs(source[8+i]-source[7+i])<=limit*scale for i in range(1,extent+1))
    flat = length != 4 and all(abs(source[7-i]-p0)<=scale and abs(source[8+i]-q0)<=scale for i in range(1,extent+1))
    wide = length == 14 and flat and all(abs(source[7-i]-p0)<=scale and abs(source[8+i]-q0)<=scale for i in range(4,7))
    output = source[:]
    if enabled:
        if flat:
            n,n2,shift = (6,1,4) if wide else (2,1,3) if length == 6 else (3,0,3)
            for i in range(-n,n):
                total = sum(source[8+max(-(n+1),min(n,i+j))]*(2 if abs(j)<=n2 else 1) for j in range(-n,n+1))
                output[8+i] = (total+(1 << (shift-1))) >> shift
        else:
            middle = 1 << (depth-1)
            def clamp(value):
                return max(-middle,min(middle-1,value))
            ps1,ps0,qs0,qs1 = p1-middle,p0-middle,q0-middle,q1-middle
            f = clamp(ps1-qs1) if hev else 0
            f = clamp(f+3*(qs0-ps0))
            f1,f2 = clamp(f+4)//8,clamp(f+3)//8
            output[8] = clamp(qs0-f1)+middle
            output[7] = clamp(ps0+f2)+middle
            if not hev:
                outer = (f1+1)//2
                output[9] = clamp(qs1-outer)+middle
                output[6] = clamp(ps1+outer)+middle
    return output,[bool(enabled),hev,flat,wide]


def main():
    parser = argparse.ArgumentParser()
    for name in ('root','out','aom'):
        parser.add_argument('--'+name,type=Path,required=True)
    a = parser.parse_args();a.out.mkdir(parents=True,exist_ok=True)
    if run(['git','-C',str(a.aom),'rev-parse','HEAD'],text=True,capture_output=True).stdout.strip() != PIN:
        raise SystemExit('libaom pin mismatch')
    run(['git','-C',str(a.aom),'diff','--quiet','HEAD','--'])
    config = Path(os.environ.get('AV1_AOM_CONFIG',str(a.root/'build/native/aom')))
    library = Path(os.environ.get('AV1_AOM_LIBRARY',str(a.root/'build/native/aom/libaom.a')))
    source = a.out/'oracle.c';source.write_text(HOST);host = a.out/'oracle'
    run(shlex.split(os.environ.get('CC','cc'))+['-std=c11','-O2','-I'+str(a.aom),'-I'+str(config),str(source),str(library),'-lm','-o',str(host)])
    rng = random.Random(0x4156314445424c4b)
    cases = [];counts = dict(enabled=0,disabled=0,flat=0,wide=0,high_variance=0,changed_samples=0)
    for depth in (8,10,12):
        scale = 1 << (depth-8);maximum = (1 << depth)-1
        for length in (4,6,8,14):
            for index in range(256):
                middle = (0,1,2,127,128,253,254,255)[index%8]*scale
                step = ((index//8)%16-7)*scale
                source = [max(0,min(maximum,middle+(step if i>=8 else 0))) for i in range(16)]
                blimit = limit = 255;threshold = index%4
                if index < 128:
                    mode = index//32
                    if mode == 1:
                        for i in (1,2,3,12,13,14):source[i]=max(0,min(maximum,source[i]+8*scale))
                    if mode >= 2:
                        for i in range(16):source[i]=max(0,min(maximum,source[i]+((i*3+index)%7-3)*scale))
                elif index < 192:
                    source = [max(0,min(maximum,128*scale+rng.randrange(-8*scale,8*scale+1))) for _ in range(16)]
                    raw = 2*abs(source[7]-source[8])+abs(source[6]-source[9])//2
                    blimit = max(0,min(255,raw//scale+(index%3)-1))
                    limit = (0,1,2,4,8,16,127,255)[index%8]
                else:
                    source = [rng.randrange(maximum+1) for _ in range(16)]
                    blimit,limit,threshold = [rng.randrange(256) for _ in range(3)]
                expected,flags = reference(source,length,blimit,limit,threshold,depth)
                for name,flag in zip(('enabled','high_variance','flat','wide'),flags):counts[name]+=flag
                counts['disabled']+=not flags[0]
                counts['changed_samples']+=sum(v!=original for v,original in zip(expected,source))
                cases.append(dict(depth=depth,length=length,blimit=blimit,limit=limit,threshold=threshold,source=source,expected=expected,flags=flags))
    data = [' '.join(map(str,[c[k] for k in ('depth','length','blimit','limit','threshold')]+c['source'])) for c in cases]
    outputs = run([str(host)],input='\n'.join(data)+'\n',text=True,capture_output=True).stdout.splitlines()
    if len(outputs) != len(cases):
        raise SystemExit('native deblock vector count mismatch')
    for i,(c,line) in enumerate(zip(cases,outputs)):
        actual = list(map(int,line.split()))
        if actual != c['expected']:
            raise SystemExit(f'native/spec deblock disagreement at {i}: {actual} vs {c["expected"]}')
    if not all(counts.values()):
        raise SystemExit('deblock branch coverage missing')
    # Four-line segments: every third case's parameters over four lines drawn
    # from different cases of the same depth.
    lines_source = a.out/'oracle_lines.c';lines_source.write_text(LINES_HOST);lines_host = a.out/'oracle_lines'
    run(shlex.split(os.environ.get('CC','cc'))+['-std=c11','-O2','-I'+str(a.aom),'-I'+str(config),str(lines_source),str(library),'-lm','-o',str(lines_host)])
    segments = [];mixed = 0
    for c in range(0,len(cases),3):
        base = c//1024*1024;case = cases[c]
        lines = [cases[base+(c-base+d*257)%1024]['source'] for d in range(4)]
        expected = []
        flags = []
        for line in lines:
            output,line_flags = reference(line,case['length'],case['blimit'],case['limit'],case['threshold'],case['depth'])
            expected += output;flags.append(tuple(line_flags))
        mixed += len(set(flags)) > 1
        segments.append(dict(case,lines=lines,expected=expected))
    data = [' '.join(map(str,[c[k] for k in ('depth','length','blimit','limit','threshold')]+sum(c['lines'],[]))) for c in segments]
    outputs = run([str(lines_host)],input='\n'.join(data)+'\n',text=True,capture_output=True).stdout.splitlines()
    if len(outputs) != len(segments):
        raise SystemExit('native four-line deblock count mismatch')
    for i,(c,line) in enumerate(zip(segments,outputs)):
        if list(map(int,line.split())) != c['expected']:
            raise SystemExit(f'native/spec four-line deblock disagreement at {i}')
    if mixed == 0:
        raise SystemExit('four-line deblock segments never mix line decisions')
    module = os.path.relpath(a.root/'src/filter/deblock.zi',a.out)
    bounds = os.path.relpath(a.root/'tests/av1_deblock.zi',a.out)
    lines = ['#import, file "'+module+'";','#import, file "'+bounds+'";','using Codec :: #import "image_codec/api"',
             '#program_export','main :: () -> s32 {','    if DeblockBoundaries() != 0 { return 99 }',
             '    if DeblockLinesBoundaries() != 0 { return 98 }']
    groups = (len(cases)+15)//16
    for group in range(groups):
        lines.append(f'    if Group{group}() != 0 {{ return {group+1} }}')
    line_groups = (len(segments)+15)//16
    for group in range(line_groups):
        lines.append(f'    if Lines{group}() != 0 {{ return {groups+group+1} }}')
    lines += ['    return 0','}']
    for group in range(groups):
        lines.append(f'Group{group} :: () -> s32 {{')
        for c in cases[group*16:(group+1)*16]:
            checks = ' || '.join('result.'+name+' != '+str(flag).lower() for name,flag in zip(('filter_enabled','high_variance','flat','wide'),c['flags']))
            lines += ['    {','        input: [16]u16 = .['+','.join(map(str,c['source']))+']',
                      f'        result := FilterDeblockEdge(input[:], {c["length"]}, {c["blimit"]}, {c["limit"]}, {c["threshold"]}, {c["depth"]})',
                      '        expected: [16]u16 = .['+','.join(map(str,c['expected']))+']',
                      f'        if result.error.code != DecodeOK || {checks} {{ return 1 }}',
                      '        for j: 0..15 { if result.samples[j] != expected[j] { return 2 } }','    }']
        lines += ['    return 0','}']
    for group in range(line_groups):
        lines.append(f'Lines{group} :: () -> s32 {{')
        for c in segments[group*16:(group+1)*16]:
            lines += ['    {','        lines: [64]u16 = .['+','.join(map(str,sum(c['lines'],[])))+']',
                      '        expected: [64]u16 = .['+','.join(map(str,c['expected']))+']',
                      f'        if DeblockLinesCase(lines[:], expected[:], {c["length"]}, {c["blimit"]}, {c["limit"]}, {c["threshold"]}, {c["depth"]}) != 0 {{ return 1 }}',
                      '    }']
        lines += ['    return 0','}']
    (a.out/'av1_deblock_vectors.zi').write_text('\n'.join(lines)+'\n')
    (a.out/'oracle.json').write_text(json.dumps(dict(revision=PIN,library_sha256=digest(library),host_sha256=hashlib.sha256(HOST.encode()).hexdigest(),
        lines_host_sha256=hashlib.sha256(LINES_HOST.encode()).hexdigest(),
        vectors=len(cases),line_segments=len(segments),mixed_decision_segments=mixed,depths=[8,10,12],filter_lengths=[4,6,8,14],native_orientations=['horizontal','vertical'],coverage=counts,
        scope='standalone edge masks/narrow/wide math, one line and four-line segments; no frame boundary/threshold scheduling or encoded-frame conformance'),indent=2)+'\n')
    print(f'Pinned deblock oracle: {len(cases)} vectors, {len(segments)} four-line segments ({mixed} with mixed line decisions), '
          f'horizontal/vertical parity, {counts["changed_samples"]} changed samples')


if __name__ == '__main__':
    main()

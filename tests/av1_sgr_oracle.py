#!/usr/bin/env python3
"""AV1 self-guided restoration vectors; not encoded stream conformance.

Blocks up to 8x8 go through FilterSgrBlock; wide blocks up to 64x8 go
straight to SgrFilterInto, with sentinels around the output, so every
vector tail and the flat-maximum and checkerboard extremes are checked."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
from av1_lossless_oracle import PIN, digest, run
from av1_cdef_oracle import sample, ZIRAN_GENERATOR

HOST = r'''
#include <stdint.h>
#include <stdio.h>
#include "aom_ports/mem.h"
#include "av1/common/restoration.h"
#include "config/av1_rtcd.h"
int main(void) {
  int depth,w,h,set,projection[2];
  static int32_t scratch[SGRPROJ_TMPBUF_SIZE/sizeof(int32_t)];
  while(scanf("%d%d%d%d%d%d",&depth,&w,&h,&set,&projection[0],&projection[1])==6) {
    uint16_t input[14*70]={0},output[8*67];
    if(w<1||w>64||h<1||h>8)return 1;
    for(int y=0;y<h+6;y++)for(int x=0;x<w+6;x++) {
      unsigned value;if(scanf("%u",&value)!=1||value>=(1u<<depth))return 1;
      input[y*70+x]=value;
    }
    for(int i=0;i<8*67;i++)output[i]=65535;
    if(av1_apply_selfguided_restoration_c(CONVERT_TO_BYTEPTR(input+3*70+3),w,h,70,set,
       projection,CONVERT_TO_BYTEPTR(output),67,scratch,depth,1)!=0)return 2;
    for(int y=0;y<8;y++)for(int x=0;x<67;x++) {
      if(y<h&&x<w)printf("%u ",output[y*67+x]);
      else if(output[y*67+x]!=65535)return 3;
    }
    printf("\n");
  }
  return 0;
}
'''

PARAMETERS = ((2,12,1,4),(2,15,1,6),(2,18,1,8),(2,21,1,9),
              (2,24,1,10),(2,29,1,11),(2,36,1,12),(2,45,1,13),
              (2,56,1,14),(2,68,1,15),(0,0,1,5),(0,0,1,8),
              (0,0,1,11),(0,0,1,14),(2,30,0,0),(2,75,0,0))


def reference(pixels,w,h,set,projection0,projection1,depth):
    def rounded(value,shift):
        return (value+(1 << shift)//2) >> shift
    filtered = []
    for pass_index in range(2):
        radius,epsilon = PARAMETERS[set][2*pass_index:2*pass_index+2]
        if not radius:
            filtered.append(None)
            continue
        n = (2*radius+1)**2
        n2e = n*n*epsilon
        scale = ((1 << 20)+n2e//2)//n2e
        reciprocal = ((1 << 12)+n//2)//n
        a_values = {};b_values = {}
        for y in range(-1,h+1):
            for x in range(-1,w+1):
                samples = [pixels[y+3+dy][x+3+dx] for dy in range(-radius,radius+1) for dx in range(-radius,radius+1)]
                b = sum(samples)
                a = rounded(sum(v*v for v in samples),2*(depth-8))
                d = rounded(b,depth-8)
                p = max(0,a*n-d*d)
                z = rounded(p*scale,20)
                a2 = 256 if z >= 255 else 1 if z == 0 else (z*256+z//2)//(z+1)
                a_values[y,x] = a2
                b_values[y,x] = rounded((256-a2)*b*reciprocal,12)
        output = []
        for y in range(h):
            shift = 4 if pass_index == 0 and y%2 else 5
            for x in range(w):
                a = 0;b = 0
                for dy in range(-1,2):
                    for dx in range(-1,2):
                        if pass_index == 0:
                            weight = (6 if dx == 0 else 5) if (y+dy)%2 else 0
                        else:
                            weight = 4 if dx == 0 or dy == 0 else 3
                        a += weight*a_values[y+dy,x+dx]
                        b += weight*b_values[y+dy,x+dx]
                output.append(rounded(a*pixels[y+3][x+3]+b,8+shift-4))
        filtered.append(output)
    output = [];clips = 0;changed = 0
    for y in range(h):
        for x in range(w):
            index = y*w+x
            original = pixels[y+3][x+3]
            u = original*16
            first = filtered[0][index] if filtered[0] is not None else u
            second = filtered[1][index] if filtered[1] is not None else u
            v = projection1*u+projection0*first+(128-projection0-projection1)*second
            raw = rounded(v,11)
            value = max(0,min((1 << depth)-1,raw))
            clips += value != raw;changed += value != original
            output.append(value)
    return output,clips,changed


# Wide blocks: full vectors, every tail length and single rows. Seeds that
# are 8 mod 9 hold the maximum everywhere, where (256 - A) * B * reciprocal
# is largest.
WIDE_SHAPES = ((64,8),(61,8),(34,7),(13,8),(9,3),(64,1))


def wide_sample(seed,y,x,depth):
    return (1 << depth)-1 if seed%9 == 8 else sample(seed,y,x,depth)


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
    cases = [];clips = 0;changed = 0
    projections = ((-96,-32),(-96,95),(31,-32),(31,95),(0,0),(-1,1),(-32,31),(7,8))
    for depth in (8,10,12):
        for shape,(w,h) in enumerate(((1,1),(4,4),(8,8),(3,7))):
            for set in range(16):
                for scenario,(p0,p1) in enumerate(projections):
                    if PARAMETERS[set][0] == 0:
                        p0 = 0
                    if PARAMETERS[set][2] == 0:
                        p1 = 95
                    seed = shape*128+set*8+scenario
                    pixels = [[sample(seed,y,x,depth) for x in range(w+6)] for y in range(h+6)]
                    expected,nclips,nchanged = reference(pixels,w,h,set,p0,p1,depth)
                    clips += nclips;changed += nchanged
                    cases.append(dict(depth=depth,w=w,h=h,set=set,seed=seed,p0=p0,p1=p1,pixels=pixels,expected=expected,wide=False))
        for shape,(w,h) in enumerate(WIDE_SHAPES):
            for set in range(16):
                p0,p1 = projections[(shape*16+set+depth)%8]
                if PARAMETERS[set][0] == 0:
                    p0 = 0
                if PARAMETERS[set][2] == 0:
                    p1 = 95
                seed = 1000+shape*16+set
                pixels = [[wide_sample(seed,y,x,depth) for x in range(w+6)] for y in range(h+6)]
                expected,nclips,nchanged = reference(pixels,w,h,set,p0,p1,depth)
                clips += nclips;changed += nchanged
                cases.append(dict(depth=depth,w=w,h=h,set=set,seed=seed,p0=p0,p1=p1,pixels=pixels,expected=expected,wide=True))
    wide = sum(c['wide'] for c in cases)
    flat = sum(c['wide'] and c['seed']%9 == 8 for c in cases)
    if not flat:
        raise SystemExit('self-guided flat-maximum coverage missing')
    data = []
    for c in cases:
        params = [c[k] for k in ('depth','w','h','set','p0','p1')]
        data.append(' '.join(map(str,params+[v for row in c['pixels'] for v in row])))
    outputs = run([str(host)],input='\n'.join(data)+'\n',text=True,capture_output=True).stdout.splitlines()
    if len(outputs) != len(cases):
        raise SystemExit('native self-guided vector count mismatch')
    for i,(c,line) in enumerate(zip(cases,outputs)):
        actual = list(map(int,line.split()))
        if actual != c['expected']:
            raise SystemExit(f'native/spec self-guided disagreement at {i}: {actual[:8]} vs {c["expected"][:8]}')
    if not changed:
        raise SystemExit('self-guided changed sample coverage missing')
    module = os.path.relpath(a.root/'src/filter/sgr.zi',a.out)
    bounds = os.path.relpath(a.root/'tests/av1_sgr.zi',a.out)
    generator = ZIRAN_GENERATOR.split('DirectionInput ::')[0]+'''
SgrInput :: (seed: s32, depth: s32, width: s32, height: s32) -> [196]u16 {
    result: [196]u16
    for i: 0..195 { result[i] = 65535 }
    for y: 0..height+5 { for x: 0..width+5 {
        result[y*14+x] = Sample(seed,cast(s32)y,cast(s32)x,depth)
    } }
    return result
}
SgrWideInput :: (seed: s32, depth: s32, width: s32, height: s32) -> [980]u16 {
    result: [980]u16
    for i: 0..979 { result[i] = 65535 }
    maximum := cast(u16)((cast(u32)1 << cast(u32)depth) - 1)
    for y: 0..height+5 { for x: 0..width+5 {
        result[y*70+x] = ifx seed%9 == 8 then maximum else Sample(seed,cast(s32)y,cast(s32)x,depth)
    } }
    return result
}
'''
    lines = ['#import, file "'+module+'";','#import, file "'+bounds+'";','using Codec :: #import "image_codec/api"',generator,
             '#program_export','main :: () -> s32 {','    if SgrBoundaries() != 0 { return 99 }']
    groups = (len(cases)+15)//16
    for group in range(groups):
        lines.append(f'    if Group{group}() != 0 {{ return {group+1} }}')
    lines += ['    return 0','}']
    for group in range(groups):
        lines.append(f'Group{group} :: () -> s32 {{')
        for c in cases[group*16:(group+1)*16]:
            if c['wide']:
                lines += ['    {',f'        input := SgrWideInput({c["seed"]}, {c["depth"]}, {c["w"]}, {c["h"]})',
                          '        output: [536]u16',
                          '        for i: 0..535 { output[i] = 65535 }',
                          f'        SgrFilterInto(input[:], 70, {c["w"]}, {c["h"]}, {c["set"]}, {c["p0"]}, {c["p1"]}, {c["depth"]}, output[:], 67)',
                          '        expected: ['+str(c['w']*c['h'])+']u16 = .['+','.join(map(str,c['expected']))+']',
                          f'        for y: 0..{c["h"]-1} {{ for x: 0..{c["w"]-1} {{ if output[y*67+x] != expected[y*{c["w"]}+x] {{ return 4 }} }} }}',
                          f'        for j: 0..535 {{ if (j/67 >= {c["h"]} || j%67 >= {c["w"]}) && output[j] != 65535 {{ return 5 }} }}','    }']
                continue
            lines += ['    {',f'        input := SgrInput({c["seed"]}, {c["depth"]}, {c["w"]}, {c["h"]})',
                      f'        result := FilterSgrBlock(input[:], 14, {c["w"]}, {c["h"]}, {c["set"]}, {c["p0"]}, {c["p1"]}, {c["depth"]})',
                      '        expected: ['+str(c['w']*c['h'])+']u16 = .['+','.join(map(str,c['expected']))+']',
                      f'        if result.error.code != DecodeOK || result.width != {c["w"]} || result.height != {c["h"]} {{ return 1 }}',
                      f'        for y: 0..{c["h"]-1} {{ for x: 0..{c["w"]-1} {{ if result.samples[y*8+x] != expected[y*{c["w"]}+x] {{ return 2 }} }} }}',
                      f'        for j: 0..63 {{ if (j/8 >= {c["h"]} || j%8 >= {c["w"]}) && result.samples[j] != 0 {{ return 3 }} }}','    }']
        lines += ['    return 0','}']
    (a.out/'av1_sgr_vectors.zi').write_text('\n'.join(lines)+'\n')
    (a.out/'oracle.json').write_text(json.dumps(dict(revision=PIN,library_sha256=digest(library),host_sha256=hashlib.sha256(HOST.encode()).hexdigest(),
        vectors=len(cases),depths=[8,10,12],parameter_sets=list(range(16)),projections=projections,changed_samples=changed,output_clips=clips,
        block_shapes=[[1,1],[4,4],[8,8],[3,7]],wide_block_shapes=[list(s) for s in WIDE_SHAPES],wide_vectors=wide,flat_maximum_vectors=flat,
        scope='standalone self-guided restoration with caller-resolved 3-pixel halo; not encoded frame conformance'),indent=2)+'\n')
    print(f'Pinned self-guided oracle: {len(cases)} vectors ({wide} wide, {flat} flat maximum), {changed} changed samples, {clips} output clips')


if __name__ == '__main__':
    main()

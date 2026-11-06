#!/usr/bin/env python3
"""AV1 CDEF scalar differential vectors, not frame conformance streams."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
from av1_lossless_oracle import PIN, digest, run

HOST = r'''
#include <stdint.h>
#include <stdio.h>
#include "av1/common/cdef.h"
#include "config/av1_rtcd.h"
int main(void) {
  int mode,depth,w,h,primary,secondary,damping,direction;
  while(scanf("%d%d%d%d%d%d%d%d",&mode,&depth,&w,&h,&primary,&secondary,&damping,&direction)==8) {
    uint16_t input[CDEF_BSTRIDE*12],output[8*11];
    for(int i=0;i<CDEF_BSTRIDE*12;i++)input[i]=CDEF_VERY_LARGE;
    if(mode==0) {
      for(int y=0;y<8;y++)for(int x=0;x<8;x++) {
        unsigned value;if(scanf("%u",&value)!=1||value>=(1u<<depth))return 1;
        input[y*CDEF_BSTRIDE+x]=value;
      }
      int32_t variance;int dir=cdef_find_dir_c(input,CDEF_BSTRIDE,&variance,depth-8);
      printf("%d %d\n",dir,variance);
    } else {
      for(int y=0;y<h+4;y++)for(int x=0;x<w+4;x++) {
        unsigned value;if(scanf("%u",&value)!=1||value>65535)return 2;
        input[y*CDEF_BSTRIDE+x]=value;
      }
      for(int i=0;i<8*11;i++)output[i]=65535;
      cdef_filter_16_0_c(output,11,input+2*CDEF_BSTRIDE+2,primary,secondary,direction,damping,damping,depth-8,w,h);
      for(int y=0;y<8;y++)for(int x=0;x<11;x++) {
        if(y<h&&x<w)printf("%u ",output[y*11+x]);
        else if(output[y*11+x]!=65535)return 3;
      }
      printf("\n");
    }
  }
  return 0;
}
'''


def sample(seed, y, x, depth):
    maximum = (1 << depth) - 1
    scale = 1 << (depth - 8)
    mode = seed % 8
    if mode == 0:
        return 1 << (depth - 1)
    if mode == 1:
        return maximum if (x + y) % 2 else 0
    if mode == 2:
        return (128 + ((x * 3 + y * 5 + seed) % 31) - 15) * scale
    if mode == 3:
        return 2 * scale
    if mode == 4:
        return (120 + ((x * 11 + y * 7 + seed * 3) % 17)) * scale
    if mode == 5:
        return (seed * 977 + x * 383 + y * 197 + x * y * 29) & maximum
    if mode == 6:
        return maximum if (x + 2 * y + seed) % 5 < 2 else 0
    return maximum if x == 5 and y == 5 else (maximum // 2)


def direction_samples(seed, depth):
    # Stripes perpendicular to each normative direction, plus flat/noisy cases.
    if seed % 16 < 8:
        slopes = ((1, 1), (2, 1), (1, 0), (2, -1), (1, -1), (1, -2), (0, 1), (1, 2))
        dy, dx = slopes[seed % 16]
        return [[((dy * y + dx * x + 16) % 5) * 50 << (depth - 8) for x in range(8)] for y in range(8)]
    return [[sample(seed, y, x, depth) for x in range(8)] for y in range(8)]


def direction_reference(pixels, depth):
    partial = [[0] * 15 for _ in range(8)]
    div = [0, 840, 420, 280, 210, 168, 140, 120, 105]
    for y in range(8):
        for x in range(8):
            value = (pixels[y][x] >> (depth - 8)) - 128
            offsets = (y+x, y+x//2, y, 3+y-x//2, 7+y-x, 3-y//2+x, x, y//2+x)
            for d, offset in enumerate(offsets):
                partial[d][offset] += value
    costs = [0] * 8
    for d in (2, 6):
        costs[d] = sum(v*v for v in partial[d][:8]) * div[8]
    for d in (0, 4):
        costs[d] = sum((partial[d][i]**2+partial[d][14-i]**2)*div[i+1] for i in range(7)) + partial[d][7]**2*div[8]
    for d in (1, 3, 5, 7):
        costs[d] = sum(v*v for v in partial[d][3:8])*div[8]
        costs[d] += sum((partial[d][i]**2+partial[d][10-i]**2)*div[2*i+2] for i in range(3))
    best = max(range(8), key=lambda d: costs[d])
    return [best, (costs[best]-costs[(best+4)%8])//1024]


def filter_reference(pixels, w, h, primary, secondary, damping, direction, depth):
    offsets = (((-1,1),(-2,2)),((0,1),(-1,2)),((0,1),(0,2)),((0,1),(1,2)),
               ((1,1),(2,2)),((1,0),(2,1)),((1,0),(2,0)),((1,0),(2,-1)))
    pri = ((4, 2), (3, 3))[(primary >> (depth-8)) & 1]
    def constrain(difference, strength):
        if not strength:
            return 0
        shift = max(0, damping-(strength.bit_length()-1))
        value = min(abs(difference), max(0, strength-(abs(difference) >> shift)))
        return -value if difference < 0 else value
    result = []
    for y in range(h):
        for x in range(w):
            current = pixels[y+2][x+2]
            neighbors = [current]
            total = 0
            for distance in range(2):
                for d, strength, weight in ((direction,primary,pri[distance]),((direction+2)%8,secondary,2-distance),((direction+6)%8,secondary,2-distance)):
                    dy, dx = offsets[d][distance]
                    for sign in (-1, 1):
                        value = pixels[y+2+sign*dy][x+2+sign*dx]
                        if value == 30000:
                            continue
                        neighbors.append(value)
                        total += weight*constrain(value-current,strength)
            value = current+(8+total-(total<0))//16
            result.append(max(min(neighbors),min(max(neighbors),value)))
    return result


ZIRAN_GENERATOR = '''
Sample :: (seed: s32, y: s32, x: s32, depth: s32) -> u16 {
    maximum := cast(s32)((cast(u32)1 << cast(u32)depth) - 1)
    scale := cast(s32)(cast(u32)1 << cast(u32)(depth - 8))
    mode := seed % 8
    if mode == 0 { return cast(u16)(cast(u32)1 << cast(u32)(depth - 1)) }
    if mode == 1 { return cast(u16)(ifx (x+y)%2 != 0 then maximum else 0) }
    if mode == 2 { return cast(u16)((128+(x*3+y*5+seed)%31-15)*scale) }
    if mode == 3 { return cast(u16)(2*scale) }
    if mode == 4 { return cast(u16)((120+(x*11+y*7+seed*3)%17)*scale) }
    if mode == 5 { return cast(u16)((seed*977+x*383+y*197+x*y*29)&maximum) }
    if mode == 6 { return cast(u16)(ifx (x+2*y+seed)%5 < 2 then maximum else 0) }
    return cast(u16)(ifx x == 5 && y == 5 then maximum else maximum/2)
}
DirectionInput :: (seed: s32, depth: s32) -> [88]u16 {
    result: [88]u16
    for i: 0..87 { result[i] = 65535 }
    slopes: [16]s32 = .[1,1,2,1,1,0,2,-1,1,-1,1,-2,0,1,1,2]
    for y: 0..7 { for x: 0..7 {
        if seed%16 < 8 {
            dy := slopes[(seed%16)*2]; dx := slopes[(seed%16)*2+1]
            value := (dy*cast(s32)y+dx*cast(s32)x+16)%5*50
            result[y*11+x] = cast(u16)(cast(u32)value << cast(u32)(depth-8))
        } else { result[y*11+x] = Sample(seed,cast(s32)y,cast(s32)x,depth) }
    } }
    return result
}
FilterInput :: (seed: s32, depth: s32, width: s32, height: s32, edge_mask: s32) -> [144]u16 {
    result: [144]u16
    for i: 0..143 { result[i] = 65535 }
    for y: 0..height+3 { for x: 0..width+3 {
        missing := ((edge_mask&1)!=0 && y<2) || ((edge_mask&2)!=0 && x<2) ||
            ((edge_mask&4)!=0 && y>=height+2) || ((edge_mask&8)!=0 && x>=width+2)
        result[y*12+x] = ifx missing then CdefMissingSample else Sample(seed,cast(s32)y,cast(s32)x,depth)
    } }
    return result
}
'''


def main():
    parser = argparse.ArgumentParser()
    for name in ('root', 'out', 'aom'):
        parser.add_argument('--'+name, type=Path, required=True)
    a = parser.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    if run(['git','-C',str(a.aom),'rev-parse','HEAD'],text=True,capture_output=True).stdout.strip() != PIN:
        raise SystemExit('libaom pin mismatch')
    run(['git','-C',str(a.aom),'diff','--quiet','HEAD','--'])
    config = Path(os.environ.get('AV1_AOM_CONFIG',str(a.root/'build/native/aom')))
    library = Path(os.environ.get('AV1_AOM_LIBRARY',str(a.root/'build/native/aom/libaom.a')))
    source = a.out/'oracle.c'
    source.write_text(HOST)
    host = a.out/'oracle'
    run(shlex.split(os.environ.get('CC','cc'))+['-std=c11','-O2','-I'+str(a.aom),'-I'+str(config),str(source),str(library),'-lm','-o',str(host)])
    cases = []
    for depth in (8, 10, 12):
        for seed in range(256):
            pixels = direction_samples(seed,depth)
            cases.append(dict(mode=0,depth=depth,seed=seed,pixels=pixels,expected=direction_reference(pixels,depth)))
        for shape, (w,h) in enumerate(((4,4),(4,8),(8,4),(8,8))):
            for direction in range(8):
                for primary in range(16):
                    seed = shape*128+direction*16+primary
                    mask = (seed//8+primary)%16
                    scale = 1 << (depth-8)
                    secondary = (0,1,2,4)[(primary+direction+shape)%4]*scale
                    damping = 2+(seed//3)%5+depth-8
                    pixels = [[30000 if (mask&1 and y<2) or (mask&2 and x<2) or (mask&4 and y>=h+2) or (mask&8 and x>=w+2)
                               else sample(seed,y,x,depth) for x in range(w+4)] for y in range(h+4)]
                    args = dict(w=w,h=h,primary=primary*scale,secondary=secondary,damping=damping,direction=direction)
                    cases.append(dict(mode=1,depth=depth,seed=seed,mask=mask,pixels=pixels,expected=filter_reference(pixels,**args,depth=depth),**args))
        # Luma variance adjustment may produce strengths between scaled syntax
        # values. Exhaust every such strength rather than accepting only multiples.
        scale = 1 << (depth-8)
        for strength in range(1,15*scale):
            if strength % scale == 0:
                continue
            seed = 512+strength
            w,h = ((4,4),(4,8),(8,4),(8,8))[strength%4]
            mask = strength%16
            pixels = [[30000 if (mask&1 and y<2) or (mask&2 and x<2) or (mask&4 and y>=h+2) or (mask&8 and x>=w+2)
                       else sample(seed,y,x,depth) for x in range(w+4)] for y in range(h+4)]
            args = dict(w=w,h=h,primary=strength,secondary=(0,1,2,4)[strength%4]*scale,damping=2+strength%5+depth-8,direction=strength%8)
            cases.append(dict(mode=1,depth=depth,seed=seed,mask=mask,pixels=pixels,expected=filter_reference(pixels,**args,depth=depth),**args))
    data = []
    for c in cases:
        params = [c['mode'],c['depth']]+[c.get(k,0) for k in ('w','h','primary','secondary','damping','direction')]
        data.append(' '.join(map(str,params+[v for row in c['pixels'] for v in row])))
    outputs = run([str(host)],input='\n'.join(data)+'\n',text=True,capture_output=True).stdout.splitlines()
    if len(outputs) != len(cases):
        raise SystemExit('native CDEF vector count mismatch')
    histogram = [0]*8
    changed = 0
    for i, (c,line) in enumerate(zip(cases,outputs)):
        actual = list(map(int,line.split()))
        if actual != c['expected']:
            raise SystemExit(f'native/spec CDEF disagreement at {i}: {actual[:8]} vs {c["expected"][:8]}')
        if c['mode'] == 0:
            histogram[actual[0]] += 1
        else:
            changed += sum(v != c['pixels'][j//c['w']+2][j%c['w']+2] for j,v in enumerate(actual))
    if min(histogram) == 0 or changed == 0:
        raise SystemExit('incomplete CDEF direction/filter coverage')
    module = os.path.relpath(a.root/'src/filter/cdef.zi',a.out)
    bounds = os.path.relpath(a.root/'tests/av1_cdef.zi',a.out)
    lines = ['#import, file "'+module+'";','#import, file "'+bounds+'";','using Codec :: #import "image_codec/api"',ZIRAN_GENERATOR,
             '#program_export','main :: () -> s32 {','    if CdefBoundaries() != 0 { return 99 }']
    groups = (len(cases)+15)//16
    for group in range(groups):
        lines.append(f'    if Group{group}() != 0 {{ return {group+1} }}')
    lines += ['    return 0','}']
    for group in range(groups):
        lines.append(f'Group{group} :: () -> s32 {{')
        for c in cases[group*16:(group+1)*16]:
            lines.append('    {')
            if c['mode'] == 0:
                lines += [f'        input := DirectionInput({c["seed"]}, {c["depth"]})',
                          f'        result := FindCdefDirection(input[:], 11, {c["depth"]})',
                          f'        if result.error.code != DecodeOK || result.direction != {c["expected"][0]} || result.variance != {c["expected"][1]} {{ return 1 }}']
            else:
                lines += [f'        input := FilterInput({c["seed"]}, {c["depth"]}, {c["w"]}, {c["h"]}, {c["mask"]})',
                          f'        result := FilterCdefBlock(input[:], 12, {c["w"]}, {c["h"]}, {c["primary"]}, {c["secondary"]}, {c["damping"]}, {c["direction"]}, {c["depth"]})',
                          '        expected: ['+str(c['w']*c['h'])+']u16 = .['+','.join(map(str,c['expected']))+']',
                          f'        if result.error.code != DecodeOK || result.width != {c["w"]} || result.height != {c["h"]} {{ return 2 }}',
                          f'        for y: 0..{c["h"]-1} {{ for x: 0..{c["w"]-1} {{ if result.samples[y*8+x] != expected[y*{c["w"]}+x] {{ return 3 }} }} }}',
                          f'        for j: 0..63 {{ if (j/8 >= {c["h"]} || j%8 >= {c["w"]}) && result.samples[j] != 0 {{ return 4 }} }}']
            lines.append('    }')
        lines += ['    return 0','}']
    (a.out/'av1_cdef_vectors.zi').write_text('\n'.join(lines)+'\n')
    (a.out/'oracle.json').write_text(json.dumps(dict(revision=PIN,library_sha256=digest(library),host_sha256=hashlib.sha256(HOST.encode()).hexdigest(),
        direction_vectors=sum(c['mode']==0 for c in cases),filter_vectors=sum(c['mode']==1 for c in cases),direction_histogram=histogram,
        changed_samples=changed,depths=[8,10,12],primary_strengths=list(range(16)),secondary_strengths=[0,1,2,4],edge_masks=list(range(16)),
        block_shapes=[[4,4],[4,8],[8,4],[8,8]],scope='standalone scalar direction/filter with explicit missing halo; no frame filtering or stream conformance claim'),indent=2)+'\n')
    print(f'Pinned CDEF oracle: 768 direction + {len(cases)-768} filter vectors, {changed} changed samples, all eight directions')


if __name__ == '__main__':
    main()

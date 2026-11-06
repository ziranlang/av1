#!/usr/bin/env python3
"""Native AV1 film grain PRNG/scaling kernels, no grain/frame conformance claim."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import shlex
from av1_lossless_oracle import PIN, digest, run

HOST=r'''
#include <stdint.h>
#include <stdio.h>
static uint16_t random_register;
'''
MAIN=r'''
int main(void) {
  int mode,index,count;
  while(scanf("%d%d%d",&mode,&index,&count)==3) {
    if(mode==0) {
      random_register=index;
      for(int i=0;i<count;i++) {
        int bits=1+(i+index)%16,value=get_random_number(bits);
        printf("%d %u ",value,random_register);
      }
    } else {
      int points[14][2]={{0}},lut[256]={0};
      if(count<0||count>14)return 1;
      for(int i=0;i<count;i++)if(scanf("%d%d",&points[i][0],&points[i][1])!=2)return 2;
      init_scaling_function(points,count,lut);
      for(int i=0;i<256;i++)printf("%d ",lut[i]);
      for(int depth=8;depth<=12;depth+=2)for(int q=0;q<64;q++) {
        int scale=1<<(depth-8),maximum=(1<<depth)-1;
        int sample=(q*67+index*127+q*q*3)%(maximum+1);
        if(q==0)sample=0;if(q==1)sample=maximum;if(q==2)sample=255*scale-1;
        if(q==3)sample=255*scale;if(q==4)sample=255*scale+scale-1;
        printf("%d ",scale_LUT(lut,sample,depth));
      }
    }
    printf("\n");
  }
  return 0;
}
'''


def main():
    parser=argparse.ArgumentParser()
    for name in ('root','out','aom'):parser.add_argument('--'+name,type=Path,required=True)
    a=parser.parse_args();a.out.mkdir(parents=True,exist_ok=True)
    if run(['git','-C',str(a.aom),'rev-parse','HEAD'],text=True,capture_output=True).stdout.strip()!=PIN:
        raise SystemExit('libaom pin mismatch')
    run(['git','-C',str(a.aom),'diff','--quiet','HEAD','--'])
    native='';functions=('static inline int get_random_number(', 'static void init_scaling_function(', 'static int scale_LUT(')
    for name in functions:
        with (a.aom/'av1/decoder/grain_synthesis.c').open() as stream:
            lines=[];active=False
            for line in stream:
                if name in line:active=True
                if active:
                    lines.append(line)
                    if line=='}\n':break
        if not lines or lines[-1]!='}\n':raise SystemExit('cannot extract pinned grain scaling function')
        native+=''.join(lines)+'\n'
    source=a.out/'oracle.c';source.write_text(HOST+native+MAIN);host=a.out/'oracle'
    run(shlex.split(os.environ.get('CC','cc'))+['-std=c11','-O2',str(source),'-o',str(host)])
    rng=random.Random(0x415631475241494e)
    seeds=[0,1,2,3,32767,32768,65534,65535]+[rng.randrange(65536) for _ in range(56)]
    curves=[]
    for index in range(128):
        count=index%15
        xs=sorted(rng.sample(range(256),count))
        if index==1:xs=[255]
        if index==2:xs=[0,255]
        ys=[rng.randrange(256) for _ in range(count)]
        if index%4==0:ys=[0]*count
        if index%4==1:ys=[255]*count
        if index%4==2:ys=[255 if i%2==0 else 0 for i in range(count)]
        curves.append([v for pair in zip(xs,ys) for v in pair])
    data=[f'0 {seed} 64' for seed in seeds]
    data += [' '.join(map(str,[1,index,len(points)//2]+points)) for index,points in enumerate(curves)]
    outputs=run([str(host)],input='\n'.join(data)+'\n',text=True,capture_output=True).stdout.splitlines()
    if len(outputs)!=len(data):raise SystemExit('native grain primitive vector count mismatch')
    for seed,line in zip(seeds,outputs):
        expected=list(map(int,line.split()));state=seed
        if len(expected)!=128:raise SystemExit('native random output count mismatch')
        for i in range(64):
            bit=(state^(state>>1)^(state>>3)^(state>>12))&1
            state=(state>>1)|(bit<<15);bits=1+(i+seed)%16
            if expected[i*2:i*2+2]!=[(state>>(16-bits))&((1<<bits)-1),state]:raise SystemExit('native/spec random disagreement')
    rel=os.path.relpath(a.root/'src/grain/grain_scaling.zi',a.out)
    bounds=os.path.relpath(a.root/'tests/av1_grain_scaling.zi',a.out)
    lines=['#import, file "'+rel+'";','#import, file "'+bounds+'";','using Codec :: #import "image_codec/api"',
           '#program_export','main :: () -> s32 {','    if GrainScalingBounds() != 0 { return 99 }']
    for i in range(64):lines.append(f'    if Random{i}() != 0 {{ return 1 }}')
    for i in range(128):lines.append(f'    if Curve{i}() != 0 {{ return 2 }}')
    lines+=['    return 0','}']
    for i,(seed,line) in enumerate(zip(seeds,outputs)):
        expected=list(map(int,line.split()))
        lines += [f'Random{i} :: () -> s32 {{',f'    state: GrainRandom; state.register_value = {seed}',
                  '    independent: GrainRandom; independent.register_value = 12345',
                  '    expected: [128]u32 = .['+','.join(map(str,expected))+']',
                  '    for j: 0..63 {',f'        bits := cast(s32)(1+(j+{seed})%16)',
                  '        result := NextGrainRandom(*state, bits)',
                  '        if result.error.code != DecodeOK || cast(u32)result.value != expected[j*2] || state.register_value != expected[j*2+1] || independent.register_value != 12345 { return 1 }',
                  '    }','    return 0','}']
    for index,points in enumerate(curves):
        values=list(map(int,outputs[64+index].split()))
        if len(values)!=448:raise SystemExit('native scaling output count mismatch')
        expected_lut=[0]*256
        pairs=list(zip(points[::2],points[1::2]))
        if pairs:
            for x in range(pairs[0][0]):expected_lut[x]=pairs[0][1]
            for (x0,y0),(x1,y1) in zip(pairs,pairs[1:]):
                dx=x1-x0;delta=(y1-y0)*((65536+dx//2)//dx)
                for x in range(dx):expected_lut[x0+x]=y0+(x*delta+32768)//65536
            for x in range(pairs[-1][0],256):expected_lut[x]=pairs[-1][1]
        if values[:256]!=expected_lut:raise SystemExit('native/spec scaling LUT disagreement')
        lines += [f'Curve{index} :: () -> s32 {{','    points: ['+str(max(1,len(points)))+']s32'+(' = .['+','.join(map(str,points))+']' if points else ''),
                  f'    lut := BuildGrainScaling(points[:{len(points)}])','    if lut.error.code != DecodeOK { return 1 }',
                  '    expected: [256]u16 = .['+','.join(map(str,values[:256]))+']',
                  '    for j: 0..255 { if lut.samples[j] != expected[j] { return 2 } }']
        for i,depth in enumerate((8,10,12)):
            maximum=(1<<depth)-1;scale=1<<(depth-8)
            indices=[(q*67+index*127+q*q*3)%(maximum+1) for q in range(64)]
            indices[:5]=[0,maximum,255*scale-1,255*scale,255*scale+scale-1]
            expected=values[256+i*64:256+(i+1)*64]
            for q,value in zip(indices,expected):
                x=q>>(depth-8);v=expected_lut[x]
                if depth!=8 and x!=255:v+=((expected_lut[x+1]-v)*(q&(scale-1))+scale//2)//scale
                if v!=value:raise SystemExit('native/spec high-depth scaling disagreement')
            lines += ['    {','        indices: [64]s32 = .['+','.join(map(str,indices))+']',
                      '        values: [64]s32 = .['+','.join(map(str,expected))+']',
                      '        for j: 0..63 {',f'            sampled := SampleGrainScaling(lut.samples[:], indices[j], {depth})',
                      '            if sampled.error.code != DecodeOK || sampled.value != values[j] { return 3 }','        }','    }']
        lines+=['    return 0','}']
    (a.out/'av1_grain_scaling_vectors.zi').write_text('\n'.join(lines)+'\n')
    (a.out/'oracle.json').write_text(json.dumps(dict(revision=PIN,native_functions_sha256=hashlib.sha256(native.encode()).hexdigest(),
        random_streams=64,random_steps=4096,random_bit_widths=list(range(1,17)),scaling_curves=128,lut_entries=32768,
        sampled_scaling_values=24576,depths=[8,10,12],scope='standalone state-owned PRNG, scaling LUT and high-depth sampling; no grain synthesis or frame conformance claim'),indent=2)+'\n')
    print('Pinned grain primitives: 4096 random transitions, 128 scaling curves, 24576 depth-specific samples')


if __name__=='__main__':main()

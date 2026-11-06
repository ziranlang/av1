#!/usr/bin/env python3
"""Stream complete pinned Gaussian/AR noise arrays into bounded test groups."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
from av1_quantization_oracle import literal
from av1_lossless_oracle import PIN, run

HOST=r'''
#include <stdio.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include "aom_dsp/aom_dsp_common.h"
#include "aom_dsp/grain_params.h"
typedef struct { uint16_t random_register; } aom_grain_rng_t;
static const int gauss_bits=11;
'''
MAIN=r'''
int main(void) {
  for(int di=0;di<3;di++)for(int config=0;config<4;config++)for(int lag=0;lag<4;lag++) {
    int index=di*16+config*4+lag,depth=8+2*di,mono=config==0,sx=config!=3,sy=config<2;
    int cw=sx?44:82,ch=sy?38:73;
    aom_film_grain_t p={0};p.apply_grain=1;p.bit_depth=depth;p.ar_coeff_lag=lag;
    p.ar_coeff_shift=6+config;p.grain_scale_shift=lag;
    p.random_seed=index==0?0:index==47?65535:(index*997+37)&65535;
    p.num_y_points=(config==1&&lag==0)||(config==2&&lag==0)?0:config==0||config==3?14:2;
    p.chroma_scaling_from_luma=(config==1&&lag==1)||(config==3&&lag==2);
    if(!mono&&!p.chroma_scaling_from_luma&&!(sx&&sy&&!p.num_y_points)) {
      p.num_cb_points=10;p.num_cr_points=config==2&&lag==1?0:10;
    }
    for(int i=0;i<24;i++)p.ar_coeffs_y[i]=((i*83+index*31)&255)-128;
    for(int i=0;i<25;i++){p.ar_coeffs_cb[i]=((i*23+index*47)&255)-128;p.ar_coeffs_cr[i]=((i*67+index*13)&255)-128;}
    int positions[25][3]={{0}},*pred[25],pos=0;
    for(int y=-lag;y<=0;y++)for(int x=-lag;x<=lag;x++) {
      if(y==0&&x==0)break;
      positions[pos][0]=y;positions[pos][1]=x;pred[pos]=positions[pos];pos++;
    }
    positions[pos][2]=1;pred[pos]=positions[pos];
    int ygrain[82*73]={0},cb[82*73]={0},cr[82*73]={0};
    aom_grain_rng_t rng={p.random_seed};
    generate_luma_grain_block(&p,&rng,pred,ygrain,73,82,82,3,3,3,0);
    if(!mono&&!generate_chroma_grain_blocks(&p,&rng,pred,ygrain,cb,cr,82,ch,cw,cw,3,3,3,0,sy,sx))return 1;
    printf("%d %d %d %d %d %d ",index,depth,mono,sx,sy,mono?0:cw);
    printf("%d ",mono?0:ch);
    for(int plane=0;plane<3;plane++)for(int y=0;y<73;y++)for(int x=0;x<82;x++) {
      int value=plane==0?ygrain[y*82+x]:mono||y>=ch||x>=cw?0:(plane==1?cb[y*cw+x]:cr[y*cw+x]);
      printf("%d ",value);
    }
    printf("\n");
  }
  return 0;
}
'''
GENERATOR='''
NoiseParameters :: (index: s32, config: s32, lag: s32) -> FilmGrainParams {
    p: FilmGrainParams; p.apply_grain = true
    p.ar_coeff_lag = lag; p.ar_coeff_shift = 6+config; p.grain_scale_shift = lag
    p.random_seed = cast(u32)((index*997+37)&65535)
    if index == 0 { p.random_seed = 0 }; if index == 47 { p.random_seed = 65535 }
    if (config==1&&lag==0)||(config==2&&lag==0) { p.num_y_points = 0 }
    else { p.num_y_points = ifx config==0||config==3 then 14 else 2 }
    p.chroma_scaling_from_luma = (config==1&&lag==1)||(config==3&&lag==2)
    if config != 0 && !p.chroma_scaling_from_luma && !(config==1&&p.num_y_points==0) {
        p.num_cb_points = 10; p.num_cr_points = ifx config==2&&lag==1 then 0 else 10
    }
    for i: 0..23 { p.ar_coeffs_y[i] = cast(s32)((i*83+index*31)&255)-128 }
    for i: 0..24 {
        p.ar_coeffs_cb[i] = cast(s32)((i*23+index*47)&255)-128
        p.ar_coeffs_cr[i] = cast(s32)((i*67+index*13)&255)-128
    }
    return p
}
'''


def extract(path,name):
    with path.open() as stream:
        lines=[];active=False
        for line in stream:
            if name in line:active=True
            if active:
                lines.append(line)
                if line=='}\n' or (name.startswith('static const') and line.strip().endswith('};')):break
    if not lines:raise SystemExit(f'cannot extract {name}')
    return ''.join(lines)+'\n'


@contextmanager
def oracle_process(host,errors):
    process=subprocess.Popen([str(host)],stdout=subprocess.PIPE,stderr=errors,text=True)
    try:
        yield process
    finally:
        if process.poll() is None:process.kill()
        process.wait();process.stdout.close()


def main():
    parser=argparse.ArgumentParser()
    for name in ('root','out','aom'):parser.add_argument('--'+name,type=Path,required=True)
    a=parser.parse_args();a.out.mkdir(parents=True,exist_ok=True)
    if run(['git','-C',str(a.aom),'rev-parse','HEAD'],text=True,capture_output=True).stdout.strip()!=PIN:
        raise SystemExit('libaom pin mismatch')
    run(['git','-C',str(a.aom),'diff','--quiet','HEAD','--'])
    upstream=a.aom/'av1/decoder/grain_synthesis.c'
    names=('static const int gaussian_sequence[2048]', 'static inline int get_random_number(',
           'static void init_random_generator(', 'static void generate_luma_grain_block(', 'static bool generate_chroma_grain_blocks(')
    native=''.join(extract(upstream,name) for name in names)
    source=a.out/'oracle.c';source.write_text(HOST+native+MAIN);host=a.out/'oracle'
    config=Path(os.environ.get('AV1_AOM_CONFIG',str(a.root/'build/native/aom')))
    run(shlex.split(os.environ.get('CC','cc'))+['-std=c11','-O2','-I'+str(a.aom),'-I'+str(config),str(source),'-o',str(host)])
    module=os.path.relpath(a.root/'src/grain/grain_noise.zi',a.out)
    params=os.path.relpath(a.root/'src/grain/grain_params.zi',a.out)
    bounds=os.path.relpath(a.root/'tests/av1_grain_noise.zi',a.out)
    count=0;native_hash=hashlib.sha256();minimum=0;maximum=0
    with (a.out/'av1_grain_noise_vectors.zi').open('w') as target:
        target.write('#import, file "'+module+'";\n#import, file "'+params+'";\n#import, file "'+bounds+'";\nusing Codec :: #import "image_codec/api"\n'+GENERATOR)
        target.write('#program_export\nmain :: () -> s32 {\n    if GrainNoiseBounds() != 0 { return 99 }\n')
        for index in range(48):target.write(f'    if Case{index}() != 0 {{ return {index+1} }}\n')
        target.write('    return 0\n}\n')
        # One bounded native result at a time; never capture all noise arrays.
        with (a.out/'oracle.log').open('w') as errors,oracle_process(host,errors) as process:
            for line in process.stdout:
                native_hash.update(line.encode())
                values=list(map(int,line.split()))
                if len(values)!=7+3*5986:raise SystemExit('native grain noise result shape mismatch')
                index,depth,mono,sx,sy,cw,ch=values[:7];values=values[7:]
                if index!=count:raise SystemExit('native grain noise case order mismatch')
                minimum=min(minimum,min(values));maximum=max(maximum,max(values))
                target.write(f'Case{index} :: () -> s32 {{\n    parameters := NoiseParameters({index}, {(index//4)%4}, {index%4})\n')
                target.write(f'    result := GenerateGrainNoise(parameters, {str(bool(mono)).lower()}, {sx}, {sy}, {depth})\n')
                target.write(f'    if result.error.code != DecodeOK || result.chroma_width != {cw} || result.chroma_height != {ch} {{ return 1 }}\n')
                for plane,name in enumerate(('y','cb','cr')):
                    target.write(f'    if Check{index}_{plane}(result.{name}[:]) != 0 {{ return {plane+2} }}\n')
                target.write('    return 0\n}\n')
                # Compare all samples, not only a digest.
                for plane,name in enumerate(('y','cb','cr')):
                    samples=values[plane*5986:(plane+1)*5986]
                    target.write(f'Check{index}_{plane} :: (actual: []s16) -> s32 {{\n'+'\n'.join(literal('expected','s16',samples))+'\n')
                    target.write('    for j: 0..5985 { if actual[j] != expected[j] { return 1 } }\n    return 0\n}\n')
                count+=1
            code=process.wait()
            if code:raise SystemExit(f'native grain noise oracle exited {code}; see {a.out / "oracle.log"}')
    if count!=48:raise SystemExit('native grain noise case count mismatch')
    # The portable reader admits 100,000 records per IR table. The complete
    # vector corpus exceeds that bound; each eight-case program remains below
    # it while preserving full source/saved C/Go/VM coverage of every sample.
    full=a.out/'av1_grain_noise_vectors.zi'
    header=[]
    with full.open() as source:
        for line in source:
            if line.startswith('#program_export'):break
            header.append(line.replace('#import, file "','#import, file "../'))
    targets=[]
    try:
        for shard in range(6):
            directory=a.out/f'shard{shard}';directory.mkdir(exist_ok=True)
            target=(directory/'av1_grain_noise_vectors.zi').open('w');targets.append(target)
            target.writelines(header);target.write('#program_export\nmain :: () -> s32 {\n    if GrainNoiseBounds() != 0 { return 99 }\n')
            for index in range(shard*8,(shard+1)*8):target.write(f'    if Case{index}() != 0 {{ return {index+1} }}\n')
            target.write('    return 0\n}\n')
        import re
        current=None
        with full.open() as source:
            for line in source:
                match=re.match(r'(?:Case|Check)(\d+)[^ ]* ::',line)
                if match:current=int(match[1])//8
                if current is not None:targets[current].write(line)
    finally:
        for target in targets:target.close()
    (a.out/'oracle.json').write_text(json.dumps(dict(revision=PIN,native_functions_sha256=hashlib.sha256(native.encode()).hexdigest(),native_output_sha256=native_hash.hexdigest(),
        vectors=count,shards=6,compared_samples=count*3*5986,depths=[8,10,12],configs=['mono','420','422','444'],ar_lags=[0,1,2,3],grain_scale_shifts=[0,1,2,3],ar_shifts=[6,7,8,9],
        minimum=minimum,maximum=maximum,scope='full scalar Gaussian/AR noise arrays including inactive cells; no overlap/application or encoded-frame conformance claim'),indent=2)+'\n')
    print(f'Pinned Gaussian/AR oracle: {count} cases, {count*3*5986} full-array sample comparisons')


if __name__=='__main__':main()

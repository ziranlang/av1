#!/usr/bin/env python3
"""Full bounded grain picture comparison to pinned av1_add_film_grain."""
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
from av1_quantization_oracle import literal
from av1_lossless_oracle import PIN, digest, run
from av1_grain_noise_oracle import GENERATOR as PARAMETERS

HOST=r'''
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include "av1/decoder/grain_synthesis.h"
static unsigned sample(int index,int plane,int x,int y,int depth) {
  unsigned maximum=(1u<<depth)-1;
  if(index%4==0)return plane==0?0:plane==1?maximum:maximum/2;
  if(index%4==2){int kind=(x+y+plane)%3;return kind==0?0:kind==1?maximum:maximum/2;}
  return (x*37+y*71+plane*113+index*97+x*y*3)&maximum;
}
static void parameters(aom_film_grain_t *p,int index,int config,int lag,int depth) {
  memset(p,0,sizeof(*p));p->apply_grain=1;p->bit_depth=depth;p->ar_coeff_lag=lag;
  p->ar_coeff_shift=6+config;p->grain_scale_shift=lag;p->scaling_shift=8+lag;
  p->random_seed=index==0?0:index==47?65535:(index*997+37)&65535;
  p->num_y_points=(config==1&&lag==0)||(config==2&&lag==0)?0:config==0||config==3?14:2;
  p->chroma_scaling_from_luma=(config==1&&lag==1)||(config==3&&lag==2);
  if(config!=0&&!p->chroma_scaling_from_luma&&!(config==1&&!p->num_y_points)) {
    p->num_cb_points=10;p->num_cr_points=config==2&&lag==1?0:10;
  }
  for(int i=0;i<24;i++)p->ar_coeffs_y[i]=((i*83+index*31)&255)-128;
  for(int i=0;i<25;i++){p->ar_coeffs_cb[i]=((i*23+index*47)&255)-128;p->ar_coeffs_cr[i]=((i*67+index*13)&255)-128;}
  int *points[]={&p->scaling_points_y[0][0],&p->scaling_points_cb[0][0],&p->scaling_points_cr[0][0]};
  int counts[]={p->num_y_points,p->num_cb_points,p->num_cr_points};
  for(int plane=0;plane<3;plane++)for(int i=0;i<counts[plane];i++) {
    points[plane][i*2]=counts[plane]==1?128:i*255/(counts[plane]-1);
    points[plane][i*2+1]=(i*37+index*17+plane*13)&255;
  }
  p->cb_mult=(index*19)&255;p->cb_luma_mult=(index*73)&255;p->cb_offset=(index*91)&511;
  p->cr_mult=(index*29)&255;p->cr_luma_mult=(index*59)&255;p->cr_offset=(index*113)&511;
  p->overlap_flag=(index+config)%2;p->clip_to_restricted_range=(lag/2+config)%2;
}
int main(void) {
  // Indices 48-95 repeat the 48 parameter sets on pictures above 64x64:
  // several grain blocks and stripes, odd sides and partial last stripes.
  int widths[]={1,7,33,64,65,129,66,257},heights[]={1,5,35,64,65,35,131,97};
  for(int index=0;index<96;index++) {
    int set=index%48,depth=8+2*(set/16),config=(set/4)%4,scenario=set%4;
    int w=widths[scenario+4*(index/48)],h=heights[scenario+4*(index/48)],mono=config==0,sx=config!=3,sy=config<2;
    int cw=(w+sx)/(1+sx),ch=(h+sy)/(1+sy),evenw=(w+1)&~1,evenh=(h+1)&~1;
    int identity=config==3&&scenario%2;
    aom_img_fmt_t fmt=config==3?AOM_IMG_FMT_I444:config==2?AOM_IMG_FMT_I422:AOM_IMG_FMT_I420;
    if(depth>8)fmt=(aom_img_fmt_t)(fmt|AOM_IMG_FMT_HIGHBITDEPTH);
    aom_image_t src,dst;if(!aom_img_alloc(&src,fmt,evenw,evenh,64)||!aom_img_alloc(&dst,fmt,evenw,evenh,64))return 1;
    src.d_w=w;src.d_h=h;src.bit_depth=depth;src.monochrome=mono;src.mc=identity?AOM_CICP_MC_IDENTITY:AOM_CICP_MC_BT_601;
    for(int plane=0;plane<3;plane++) {
      int psx=plane?sx:0,psy=plane?sy:0,pw=plane?cw:w,ph=plane?ch:h;
      for(int y=0;y<evenh/(1+psy);y++)for(int x=0;x<evenw/(1+psx);x++) {
        unsigned value=sample(index,plane,x<pw?x:pw-1,y<ph?y:ph-1,depth);
        if(depth==8)src.planes[plane][y*src.stride[plane]+x]=value;
        else ((uint16_t*)(src.planes[plane]+y*src.stride[plane]))[x]=value;
      }
    }
    aom_film_grain_t p;parameters(&p,index,config,scenario,depth);
    if(av1_add_film_grain(&p,&src,&dst)!=0)return 2;
    printf("%d %d %d %d %d %d %d %d %d %d ",index,depth,mono,sx,sy,w,h,mono?0:cw,mono?0:ch,identity);
    for(int plane=0;plane<(mono?1:3);plane++) {
      int pw=plane?cw:w,ph=plane?ch:h;
      for(int y=0;y<ph;y++)for(int x=0;x<pw;x++) {
        unsigned original=depth==8?src.planes[plane][y*src.stride[plane]+x]:((uint16_t*)(src.planes[plane]+y*src.stride[plane]))[x];
        if(original!=sample(index,plane,x,y,depth))return 3;
        unsigned value=depth==8?dst.planes[plane][y*dst.stride[plane]+x]:((uint16_t*)(dst.planes[plane]+y*dst.stride[plane]))[x];
        printf("%u ",value);
      }
    }
    printf("\n");aom_img_free(&src);aom_img_free(&dst);
  }
  return 0;
}
'''
# Large pictures compare per-row hashes over little-endian sample bytes, the
# convention of av1_intra_frames_oracle.py.
LARGE='''
FillGrainPlane :: (target: []u16, index: s32, plane: s32, width: s32, height: s32, stride: s32, depth: s32) {
    for j: 0..target.count-1 { target[j] = 65535 }
    for y: 0..height-1 { for x: 0..width-1 { target[y*stride+x] = FrameSourceSample(index,plane,cast(s32)x,cast(s32)y,depth) } }
}
// Row hashes of the picture; every stride or tail sample must keep its fill.
CheckGrainRows :: (actual: []u16, width: s32, height: s32, stride: s32, depth: s32, rows: []s64) -> s32 {
    for y: 0..height-1 {
        hash: s64
        for x: 0..width-1 {
            value := cast(s64)actual[y*stride+x]
            hash = (hash*131 + (value & 255) + 1) % 2147483629
            if depth > 8 { hash = (hash*131 + (value >> 8) + 1) % 2147483629 }
        }
        if hash != rows[y] { return cast(s32)y + 1 }
    }
    for j: 0..actual.count-1 { if (j/stride >= height || j%stride >= width) && actual[j] != 65535 { return 100000 } }
    return 0
}
'''


def source_sample(index,plane,x,y,depth):
    maximum=(1<<depth)-1
    if index%4==0:return 0 if plane==0 else maximum if plane==1 else maximum//2
    if index%4==2:return (0,maximum,maximum//2)[(x+y+plane)%3]
    return (x*37+y*71+plane*113+index*97+x*y*3)&maximum


def row_hashes(samples,width,height,depth):
    rows=[]
    for y in range(height):
        value=0
        for sample in samples[y*width:(y+1)*width]:
            value=(value*131+(sample&255)+1)%2147483629
            if depth>8:value=(value*131+(sample>>8)+1)%2147483629
        rows.append(value)
    return rows


GENERATOR=PARAMETERS+'''
FrameGrainParameters :: (index: s32, config: s32, scenario: s32) -> FilmGrainParams {
    p := NoiseParameters(index, config, scenario)
    p.scaling_shift = 8+scenario; p.overlap_flag = (index+config)%2 != 0
    p.clip_to_restricted_range = (scenario/2+config)%2 != 0
    for plane: 0..2 {
        points := p.scaling_points_y[:]; count := p.num_y_points
        if plane == 1 { points = p.scaling_points_cb[:]; count = p.num_cb_points }
        if plane == 2 { points = p.scaling_points_cr[:]; count = p.num_cr_points }
        for i: 0..count-1 {
            points[i*2] = ifx count == 1 then 128 else cast(s32)i*255/(count-1)
            points[i*2+1] = cast(s32)((i*37+index*17+plane*13)&255)
        }
    }
    p.cb_mult = (index*19)&255; p.cb_luma_mult = (index*73)&255; p.cb_offset = (index*91)&511
    p.cr_mult = (index*29)&255; p.cr_luma_mult = (index*59)&255; p.cr_offset = (index*113)&511
    return p
}
FrameSourceSample :: (index: s32, plane: s32, x: s32, y: s32, depth: s32) -> u16 {
    maximum := cast(s32)((cast(u32)1 << cast(u32)depth)-1)
    if index%4 == 0 {
        if plane == 0 { return 0 }; if plane == 1 { return cast(u16)maximum }; return cast(u16)(maximum/2)
    }
    if index%4 == 2 {
        kind := (x+y+plane)%3
        if kind == 0 { return 0 }; if kind == 1 { return cast(u16)maximum }; return cast(u16)(maximum/2)
    }
    return cast(u16)((x*37+y*71+plane*113+index*97+x*y*3)&maximum)
}
FrameSource :: (index: s32, plane: s32, width: s32, height: s32, depth: s32) -> [4096]u16 {
    source: [4096]u16
    for i: 0..4095 { source[i] = 65535 }
    for y: 0..height-1 { for x: 0..width-1 { source[y*64+x] = FrameSourceSample(index,plane,cast(s32)x,cast(s32)y,depth) } }
    return source
}
'''


def main():
    parser=argparse.ArgumentParser()
    for name in ('root','out','aom'):parser.add_argument('--'+name,type=Path,required=True)
    a=parser.parse_args();a.out.mkdir(parents=True,exist_ok=True)
    if run(['git','-C',str(a.aom),'rev-parse','HEAD'],text=True,capture_output=True).stdout.strip()!=PIN:
        raise SystemExit('libaom pin mismatch')
    run(['git','-C',str(a.aom),'diff','--quiet','HEAD','--'])
    source=a.out/'oracle.c';source.write_text(HOST);host=a.out/'oracle'
    library=Path(os.environ.get('AV1_AOM_LIBRARY',str(a.root/'build/native/aom/libaom.a')))
    config=Path(os.environ.get('AV1_AOM_CONFIG',str(a.root/'build/native/aom')))
    run(shlex.split(os.environ.get('CC','cc'))+['-std=c11','-O2','-I'+str(a.aom),'-I'+str(config),str(source),str(library),'-lm','-lpthread','-o',str(host)])
    count=0;compared=0;changed=0;large=0;native_hash=hashlib.sha256()
    with ExitStack() as files:
        targets=[]
        for shard in range(8):
            directory=a.out/f'shard{shard}';directory.mkdir(exist_ok=True)
            target=files.enter_context((directory/'av1_grain_apply_vectors.zi').open('w'));targets.append(target)
            for path in (a.root/'src/grain/grain_apply.zi',a.root/'src/grain/grain_params.zi',a.root/'tests/av1_grain_apply.zi'):
                target.write('#import, file "'+os.path.relpath(path,directory)+'";\n')
            target.write('using Codec :: #import "image_codec/api"\n'+GENERATOR+LARGE+'\n#program_export\nmain :: () -> s32 {\n    if GrainApplyBounds() != 0 { return 99 }\n')
            for index in [*range(shard*6,(shard+1)*6),*range(48+shard*6,48+(shard+1)*6)]:target.write(f'    if Case{index}() != 0 {{ return {index+1} }}\n')
            target.write('    return 0\n}\n')
        errors=files.enter_context((a.out/'oracle.log').open('w'))
        process=subprocess.Popen([str(host)],stdout=subprocess.PIPE,stderr=errors,text=True)
        try:
            for line in process.stdout:
                native_hash.update(line.encode());values=list(map(int,line.split()))
                index,depth,mono,sx,sy,w,h,cw,ch,identity=values[:10];values=values[10:]
                expected_count=w*h+(0 if mono else 2*cw*ch)
                if index!=count or len(values)!=expected_count:raise SystemExit('native grain application result shape mismatch')
                target=targets[(index%48)//6];config=(index%48//4)%4;scenario=index%4
                if index>=48:
                    # In place on padded Vec planes; row hashes, untouched padding.
                    stride=w+3;cstride=max(1,cw)+3
                    target.write(f'Case{index} :: () -> s32 {{\n    y: Vec(u16); u: Vec(u16); v: Vec(u16); scratch: Vec(s16)\n')
                    target.write(f'    if !VecResizeBounded(y, {stride*h}, {stride*h}) || !VecResizeBounded(scratch, GrainScratchCount({w}), GrainScratchCount({w})) {{ return 1 }}\n')
                    target.write(f'    FillGrainPlane(VecSlice(y, 0, y.count), {index}, 0, {w}, {h}, {stride}, {depth})\n')
                    target.write(f'    picture: GrainPlanes; picture.y = VecSlice(y, 0, y.count); picture.y_stride = {stride}; picture.chroma_stride = {cstride}\n')
                    if not mono:
                        target.write(f'    if !VecResizeBounded(u, {cstride*ch}, {cstride*ch}) || !VecResizeBounded(v, {cstride*ch}, {cstride*ch}) {{ return 1 }}\n')
                        target.write(f'    FillGrainPlane(VecSlice(u, 0, u.count), {index}, 1, {cw}, {ch}, {cstride}, {depth}); FillGrainPlane(VecSlice(v, 0, v.count), {index}, 2, {cw}, {ch}, {cstride}, {depth})\n')
                        target.write('    picture.u = VecSlice(u, 0, u.count); picture.v = VecSlice(v, 0, v.count)\n')
                    target.write(f'    picture.width = {w}; picture.height = {h}; picture.monochrome = {str(bool(mono)).lower()}; picture.subsampling_x = {sx}; picture.subsampling_y = {sy}; picture.bit_depth = {depth}; picture.matrix_identity = {str(bool(identity)).lower()}\n')
                    target.write(f'    error := ApplyFilmGrain(picture, FrameGrainParameters({index},{config},{scenario}), VecSlice(scratch, 0, scratch.count))\n')
                    target.write('    if error.code != DecodeOK { return 2 }\n')
                    offset=0
                    for plane,(name,pw,ph,ps) in enumerate((('y',w,h,stride),('u',cw,ch,cstride),('v',cw,ch,cstride))):
                        if mono and plane:continue
                        samples=values[offset:offset+pw*ph];offset+=pw*ph
                        changed+=sum(value!=source_sample(index,plane,j%pw,j//pw,depth) for j,value in enumerate(samples))
                        target.write(f'    rows{plane}: [{ph}]s64 = .['+','.join(map(str,row_hashes(samples,pw,ph,depth)))+']\n')
                        target.write(f'    if CheckGrainRows(VecSlice({name}, 0, {name}.count), {pw}, {ph}, {ps}, {depth}, rows{plane}[:]) != 0 {{ return {plane+3} }}\n')
                    target.write('    return 0\n}\n')
                    compared+=len(values);count+=1;large+=1
                    continue
                target.write(f'Case{index} :: () -> s32 {{\n    y := FrameSource({index},0,{w},{h},{depth})\n')
                target.write(f'    u := FrameSource({index},1,{max(1,cw)},{max(1,ch)},{depth}); v := FrameSource({index},2,{max(1,cw)},{max(1,ch)},{depth})\n')
                target.write('    scratch: [7128]s16\n')
                target.write('    picture: GrainPlanes; picture.y = y[:]; picture.u = u[:]; picture.v = v[:]; picture.y_stride = 64; picture.chroma_stride = 64\n')
                target.write(f'    picture.width = {w}; picture.height = {h}; picture.monochrome = {str(bool(mono)).lower()}; picture.subsampling_x = {sx}; picture.subsampling_y = {sy}; picture.bit_depth = {depth}; picture.matrix_identity = {str(bool(identity)).lower()}\n')
                target.write(f'    parameters := FrameGrainParameters({index},{config},{scenario})\n    error := ApplyFilmGrain(picture, parameters, scratch[:])\n')
                target.write('    if error.code != DecodeOK { return 1 }\n')
                for plane,(name,pw,ph) in enumerate((('y',w,h),('u',cw,ch),('v',cw,ch))):
                    if mono and plane:continue
                    target.write(f'    if Check{index}_{plane}({name}[:]) != 0 {{ return {plane+2} }}\n')
                # In place: samples outside the picture keep FrameSource's fill.
                target.write(f'    for j: 0..4095 {{ if (j/64 >= {h} || j%64 >= {w}) && y[j] != 65535 {{ return 6 }} }}\n')
                if not mono:target.write(f'    for j: 0..4095 {{ if (j/64 >= {ch} || j%64 >= {cw}) && (u[j] != 65535 || v[j] != 65535) {{ return 7 }} }}\n')
                target.write('    return 0\n}\n')
                offset=0
                for plane,(pw,ph) in enumerate(((w,h),(cw,ch),(cw,ch))):
                    if mono and plane:continue
                    samples=values[offset:offset+pw*ph];offset+=pw*ph
                    changed+=sum(value!=source_sample(index,plane,j%pw,j//pw,depth) for j,value in enumerate(samples))
                    target.write(f'Check{index}_{plane} :: (actual: []u16) -> s32 {{\n'+'\n'.join(literal('expected','u16',samples))+'\n')
                    target.write(f'    for j: 0..{len(samples)-1} {{ if actual[(j/{pw})*64+j%{pw}] != expected[j] {{ return 1 }} }}\n    return 0\n}}\n')
                compared+=len(values);count+=1
            code=process.wait()
            if code:raise SystemExit(f'native grain application exited {code}; see {a.out / "oracle.log"}')
        except BaseException:
            if process.poll() is None:process.kill()
            process.wait();raise
        finally:process.stdout.close()
    if count!=96 or large!=48 or not changed:raise SystemExit('native grain application coverage incomplete')
    (a.out/'oracle.json').write_text(json.dumps(dict(revision=PIN,library_sha256=digest(library),native_host_sha256=hashlib.sha256(HOST.encode()).hexdigest(),native_output_sha256=native_hash.hexdigest(),
        vectors=count,large_vectors=large,shards=8,compared_samples=compared,changed_samples=changed,depths=[8,10,12],configs=['mono','420','422','444'],
        dimensions=[[1,1],[7,5],[33,35],[64,64],[65,65],[129,35],[66,131],[257,97]],comparison='full samples up to 64x64; per-row hashes and untouched stride padding above',
        scope='in-place film-grain application to raw native pictures; not encoded-frame or decoder integration conformance'),indent=2)+'\n')
    print(f'Pinned grain application: {count} raw pictures ({large} above 64x64), {compared} samples, {changed} changed samples')


if __name__=='__main__':main()

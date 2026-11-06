#!/usr/bin/env python3
"""Real pinned encoder grain KEY frames, native metadata and displayed planes."""
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shlex
from av1_chroma_oracle import HOST as DECODER
from av1_frame_oracle import zi_bytes
from av1_grain_params_oracle import FIELDS, BOOLS
from av1_quantization_oracle import literal
from av1_lossless_oracle import PIN, digest, run

# Frames 48-71 exceed 64x64: odd sides, several grain stripes and blocks, all
# formats and depths, lossless and lossy (q40 with the post filters allowed).
LARGE_SIZES=((65,66),(130,97),(96,129),(200,72))


def frame_case(index):
    if index<48:
        scenario=index%4
        return 8+2*(index//16),(index//4)%4,scenario,((1,1),(7,5),(33,35),(64,64))[scenario],1+(index%16),0
    case=index-48
    return (8,10,12)[case%3],case%4,4,LARGE_SIZES[(case+case//4)%4],1+(case%16),case//12

ENCODER=r'''
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include "aom/aom_encoder.h"
#include "aom/aomcx.h"
static int control(aom_codec_ctx_t *c,int id,int value) {
  if(aom_codec_control(c,id,value)){fprintf(stderr,"grain control%d: %s / %s\n",id,aom_codec_error(c),aom_codec_error_detail(c));return 1;}return 0;
}
int main(int argc,char **argv) {
  // lossy=1: q40 textured content with deblocking/CDEF/restoration allowed.
  if(argc!=8)return 1;int w=atoi(argv[2]),h=atoi(argv[3]),depth=atoi(argv[4]),format=atoi(argv[5]),vector=atoi(argv[6]),lossy=atoi(argv[7]);
  aom_codec_enc_cfg_t cfg;if(aom_codec_enc_config_default(aom_codec_av1_cx(),&cfg,AOM_USAGE_GOOD_QUALITY))return 2;
  cfg.g_w=w;cfg.g_h=h;cfg.g_threads=1;cfg.g_lag_in_frames=0;cfg.g_limit=0;cfg.g_timebase.num=1;cfg.g_timebase.den=30;
  cfg.g_bit_depth=(aom_bit_depth_t)depth;cfg.g_input_bit_depth=depth;cfg.monochrome=format==0;
  cfg.g_profile=depth==12||format==2?2:format==3?1:0;cfg.rc_end_usage=AOM_Q;cfg.rc_min_quantizer=cfg.rc_max_quantizer=lossy?40:0;
  aom_codec_ctx_t codec={0};if(aom_codec_enc_init(&codec,aom_codec_av1_cx(),&cfg,depth>8?AOM_CODEC_USE_HIGHBITDEPTH:0))return 3;
  if(control(&codec,AOME_SET_CPUUSED,lossy?0:8)||control(&codec,AV1E_SET_LOSSLESS,!lossy)||control(&codec,AV1E_SET_FORCE_VIDEO_MODE,1)||
     control(&codec,AV1E_SET_ENABLE_CDEF,lossy)||control(&codec,AV1E_SET_ENABLE_RESTORATION,lossy)||control(&codec,AV1E_SET_ENABLE_INTRABC,0)||
     control(&codec,AV1E_SET_ENABLE_PALETTE,0)||control(&codec,AV1E_SET_ENABLE_FILTER_INTRA,0)||
     control(&codec,AV1E_SET_FILM_GRAIN_TEST_VECTOR,vector))return 4;
  aom_img_fmt_t fmt=format==3?AOM_IMG_FMT_I444:format==2?AOM_IMG_FMT_I422:AOM_IMG_FMT_I420;
  if(depth>8)fmt=(aom_img_fmt_t)(fmt|AOM_IMG_FMT_HIGHBITDEPTH);
  aom_image_t image;if(!aom_img_alloc(&image,fmt,w,h,64))return 5;image.bit_depth=depth;
  for(int plane=0;plane<3;plane++) {
    int sx=plane?image.x_chroma_shift:0,sy=plane?image.y_chroma_shift:0;
    int pw=(w+sx)/(1+sx),ph=(h+sy)/(1+sy),maximum=(1<<depth)-1;
    for(int y=0;y<ph;y++)for(int x=0;x<pw;x++) {
      unsigned value=plane==0?(maximum/2+2):plane==1?maximum/4:maximum-3;
      if(lossy)value=(unsigned)((x*37+y*23+plane*91+((x*y)>>3)+(((x>>3)+(y>>3))&1)*maximum/3)%(maximum+1));
      if(depth==8)image.planes[plane][y*image.stride[plane]+x]=value;
      else ((uint16_t*)(image.planes[plane]+y*image.stride[plane]))[x]=value;
    }
  }
  FILE *out=fopen(argv[1],"wb");if(!out)return 6;int frames=0;
  for(int pass=0;pass<2;pass++) {
    if(aom_codec_encode(&codec,pass?NULL:&image,pass,1,0))return 7;
    aom_codec_iter_t it=NULL;const aom_codec_cx_pkt_t *packet;
    while((packet=aom_codec_get_cx_data(&codec,&it)))if(packet->kind==AOM_CODEC_CX_FRAME_PKT) {
      if(fwrite(packet->data.frame.buf,1,packet->data.frame.sz,out)!=packet->data.frame.sz)return 8;frames++;
    }
  }
  if(fclose(out)||frames!=1)return 9;aom_img_free(&image);return aom_codec_destroy(&codec);
}
'''


def main():
    parser=argparse.ArgumentParser()
    for name in ('root','out','aom'):parser.add_argument('--'+name,type=Path,required=True)
    a=parser.parse_args();a.out.mkdir(parents=True,exist_ok=True)
    if run(['git','-C',str(a.aom),'rev-parse','HEAD'],text=True,capture_output=True).stdout.strip()!=PIN:raise SystemExit('libaom pin mismatch')
    run(['git','-C',str(a.aom),'diff','--quiet','HEAD','--'])
    library=Path(os.environ.get('AV1_AOM_LIBRARY',str(a.root/'build/native/aom/libaom.a')))
    config=Path(os.environ.get('AV1_AOM_CONFIG',str(a.root/'build/native/aom')))
    encoder_library=Path(os.environ.get('AV1_AOM_ENCODER_LIBRARY',str(a.root/'build/native/aom/libaom.a')))
    compiler=shlex.split(os.environ.get('CC','cc'))
    source=a.out/'encoder.c';source.write_text(ENCODER);encoder=a.out/'encoder'
    run(compiler+['-std=c11','-O2','-I'+str(a.aom),'-I'+str(encoder_library.parent),str(source),str(encoder_library),'-lm','-lpthread','-o',str(encoder)])
    native=DECODER.replace('static int blocks,cfl,palette_y,palette_uv;',
        'static int blocks,cfl,palette_y,palette_uv,mono,grain_present,deblock,cdef,restoration;static aom_film_grain_t observed;')
    native=native.replace('blocks++;','blocks++;observed=p->common.film_grain_params;mono=p->common.seq_params->monochrome;grain_present=p->common.seq_params->film_grain_params_present;'
        'deblock=p->common.lf.filter_level[0]||p->common.lf.filter_level[1];cdef=p->common.cdef_info.nb_cdef_strengths>1||p->common.cdef_info.cdef_strengths[0]||p->common.cdef_info.cdef_uv_strengths[0];'
        'restoration=p->common.rst_info[0].frame_restoration_type!=RESTORE_NONE||p->common.rst_info[1].frame_restoration_type!=RESTORE_NONE||p->common.rst_info[2].frame_restoration_type!=RESTORE_NONE;')
    # Frames above 64x64 are decoded too; the 64KiB input cap stays.
    native=native.replace('image->d_w>64||image->d_h>64||','')
    native=native.replace('||!image->planes[1]||!image->planes[2]','')
    native=native.replace('for(int plane=0;plane<3;plane++) {','for(int plane=0;plane<3;plane++) {if(mono&&plane){printf("\\n");continue;}')
    fields='printf("%d %d ",mono,grain_present);'+''.join(f'printf("%d ",(int)observed.{expression});' for _,expression in FIELDS)+'printf("\\n%d %d %d\\n",deblock,cdef,restoration);'
    native=native.replace('return aom_codec_destroy(&codec);',fields+'return aom_codec_destroy(&codec);')
    source=a.out/'decoder.c';source.write_text(native);decoder=a.out/'decoder'
    run(compiler+['-std=c11','-O2','-I'+str(a.aom),'-I'+str(config),str(source),str(library),'-Wl,--wrap=av1_read_mode_info','-lm','-lpthread','-o',str(decoder)])
    records=[];compared=0;prefixes=0;allocation_case=None
    with ExitStack() as files:
        targets=[];directories=[]
        for shard in range(12):
            directory=a.out/f'shard{shard}';directory.mkdir(exist_ok=True);directories.append(directory)
            target=files.enter_context((directory/'av1_grain_frames_vectors.zi').open('w'));targets.append(target)
            target.write('#import, file "'+os.path.relpath(a.root/'src/decoder.zi',directory)+'";\nusing Codec :: #import "image_codec/api"\n')
            target.write('#program_export\nmain :: () -> s32 {\n')
            for index in range(shard*6,(shard+1)*6):target.write(f'    if Case{index}() != 0 {{ return {index+1} }}\n')
            target.write('    return 0\n}\nPutBytes :: (target: []u8, offset: s64, source: []u8) { for i: 0..source.count-1 { target[offset+i] = source[i] } }\n')
        for index in range(72):
            depth,format,scenario,(w,h),vector,lossy=frame_case(index)
            path=a.out/f'frame-{index}.obu';command=[str(encoder),str(path),str(w),str(h),str(depth),str(format),str(vector),str(lossy)]
            run(command)
            output=run([str(decoder),str(path)],text=True,capture_output=True).stdout.splitlines()
            if len(output)!=7:raise SystemExit('native grain frame output shape mismatch')
            post=dict(zip(('deblock','cdef','restoration'),map(int,output[6].split())))
            if not lossy and any(post.values()):raise SystemExit(f'lossless grain case{index} signalled a post filter')
            dimensions=list(map(int,output[0].split()));metadata=list(map(int,output[5].split()))
            mono=format==0;sx=int(format!=3);sy=int(format<2)
            if dimensions!=[w,h,depth,sx,sy] or metadata[:2]!=[int(mono),1] or len(metadata)!=len(FIELDS)+2 or metadata[2]!=1:
                raise SystemExit(f'encoder did not expose active grain for case{index}: {dimensions} {metadata[:4]}')
            pixels=[list(map(int,line.split())) for line in output[1:4]]
            cw=(w+sx)//(1+sx);ch=(h+sy)//(1+sy)
            if list(map(len,pixels))!=[w*h,0 if mono else cw*ch,0 if mono else cw*ch]:raise SystemExit('native grain frame plane count mismatch')
            # Encoded packet is bounded by the decoder oracle's64KiB input cap.
            if path.stat().st_size>=65536:raise SystemExit('grain frame input exceeds oracle cap')
            data=list(path.read_bytes());target=targets[index//6];byte_count=1 if depth==8 else 2
            body,loaders=zi_bytes('bytes',data,f'Grain{index}');target.write(f'Case{index} :: () -> s32 {{\n'+body)
            target.write('    options := DefaultOptions(); budget := NewWorkerBudget(options.limits); options.workers = *budget\n')
            target.write('    image := DecodeAv1YUV(BorrowBytes(bytes[:]), *options)\n    if !image.is_ok { return 100+cast(s32)image.error.code }\n')
            target.write(f'    if image.y.width != {w} || image.y.height != {h} || image.y.storage.count != {w*h*byte_count} || image.u.storage.count != {0 if mono else cw*ch*byte_count} || image.v.storage.count != {0 if mono else cw*ch*byte_count} {{ return 1 }}\n')
            target.write('    if budget.active_workers != 0 || budget.working_bytes != 0 || !image.sequence.film_grain_params_present { return 2 }\n')
            for (field,_),value in zip(FIELDS,metadata[2:]):
                expected=str(bool(value)).lower() if field in BOOLS else str(value)
                target.write(f'    if image.frame.grain_parameters.{field} != {expected} {{ return 3 }}\n')
            for plane,(name,samples) in enumerate(zip(('y','u','v'),pixels)):
                if samples:target.write(f'    if Check{index}_{plane}(*image.{name}) != 0 {{ return 4 }}\n')
            if lossy:
                # The frame must actually take the lossy path with a post filter.
                target.write('    if image.frame.coded_lossless { return 10 }\n')
            total=(w*h+(0 if mono else 2*cw*ch))*byte_count
            # Headers validate pixel dimensions only; the exact aggregate
            # source-YUV byte count is the output cap.
            shared_cap=total
            target.write(f'    options.limits.max_output_bytes = {shared_cap-1}\n    limited := DecodeAv1YUV(BorrowBytes(bytes[:]), *options)\n')
            # A one-byte frame leaves a zero cap, which the shared limits reject.
            refused='InvalidLimits' if shared_cap==1 else 'LimitExceeded'
            target.write('    if limited.is_ok || limited.error.code != '+refused+' || limited.y.storage.count != 0 || limited.u.storage.count != 0 || limited.v.storage.count != 0 || budget.active_workers != 0 || budget.working_bytes != 0 { return 5 }\n')
            target.write(f'    options.limits.max_output_bytes = {shared_cap}\n    second := DecodeAv1YUV(BorrowBytes(bytes[:]), *options)\n')
            target.write('    if !second.is_ok { return 6 }\n    original := image.y.storage[0]; image.y.storage[0] = original ^ 1\n    if second.y.storage[0] != original { return 7 }; image.y.storage[0] = original\n')
            if scenario==0:
                target.write(f'    for end: 0..{len(data)-1} {{\n        partial := DecodeAv1YUV(BorrowBytes(bytes[:end]), *options)\n')
                target.write('        if partial.is_ok || partial.error.code == DecodeOK || partial.y.storage.count != 0 || partial.u.storage.count != 0 || partial.v.storage.count != 0 || budget.active_workers != 0 || budget.working_bytes != 0 { return 8 }\n    }\n')
                prefixes+=len(data)
            target.write('    bytes[0] = 0\n')
            for plane,(name,samples) in enumerate(zip(('y','u','v'),pixels)):
                if samples:target.write(f'    if Check{index}_{plane}(*image.{name}) != 0 {{ return 9 }}\n')
            target.write('    return 0\n}\n'+loaders)
            for plane,samples in enumerate(pixels):
                if not samples:continue
                if index>=48:
                    pw=w if plane==0 else cw;rows=[]
                    for y in range(len(samples)//pw):
                        value=0
                        for sample in samples[y*pw:(y+1)*pw]:
                            value=(value*131+(sample&255)+1)%2147483629
                            if byte_count==2:value=(value*131+(sample>>8)+1)%2147483629
                        rows.append(value)
                    # Row hashes over little-endian sample bytes (av1_intra_frames_oracle.py).
                    target.write(f'Check{index}_{plane} :: (actual: *Plane) -> s32 {{\n    rows: [{len(rows)}]s64 = .['+','.join(map(str,rows))+']\n')
                    target.write('    for row: 0..rows.count - 1 {\n        hash: s64; base := row * actual.stride\n')
                    target.write('        for i: 0..cast(s64)actual.width * actual.sample_bytes - 1 { hash = (hash * 131 + cast(s64)actual.storage[base + i] + 1) % 2147483629 }\n')
                    target.write('        if hash != rows[row] { return 1 }\n    }\n    return 0\n}\n')
                    continue
                target.write(f'Check{index}_{plane} :: (actual: *Plane) -> s32 {{\n'+'\n'.join(literal('expected','u16',samples))+'\n')
                target.write(f'    for j: 0..{len(samples)-1} {{ at := j*{byte_count}; value := cast(u16)actual.storage[at]\n')
                if byte_count==2:target.write('        value |= cast(u16)actual.storage[at+1] << 8\n')
                target.write('        if value != expected[j] { return 1 }\n    }\n    return 0\n}\n')
            # A large lossy color frame allocates planes, filter frames and grain scratch.
            if allocation_case is None and not mono and lossy:allocation_case=data
            compared+=sum(map(len,pixels))
            records.append(dict(depth=depth,format=format,width=w,height=h,grain_vector=vector,lossy=bool(lossy),post_filters=post,encoded_bytes=len(data),sha256=digest(path),encoder_command=command))
    large=[r for r in records if r['width']>64 or r['height']>64]
    post_totals={name:sum(r['post_filters'][name] for r in large) for name in ('deblock','cdef','restoration')}
    if len(large)!=24 or not post_totals['deblock'] or not post_totals['cdef']:raise SystemExit('large grain frames never exercise deblocking and CDEF: '+json.dumps(post_totals))
    directory=a.out/'allocation';directory.mkdir(exist_ok=True)
    body,loaders=zi_bytes('bytes',allocation_case,'GrainAllocation')
    (directory/'av1_grain_allocation.zi').write_text('#import, file "'+os.path.relpath(a.root/'src/decoder.zi',directory)+'";\nusing Codec :: #import "image_codec/api"\n'+
        'PutBytes :: (target: []u8, offset: s64, source: []u8) { for i: 0..source.count-1 { target[offset+i] = source[i] } }\n'+
        '// expect_failure=0 decodes unfailed, so the host can count every allocation it then fails.\n#program_export\nGrainAllocationFailure :: (expect_failure: s32) -> s32 {\n'+body+
        '    options := DefaultOptions(); budget := NewWorkerBudget(options.limits); options.workers = *budget\n    image := DecodeAv1YUV(BorrowBytes(bytes[:]), *options)\n'+
        '    if budget.active_workers != 0 || budget.working_bytes != 0 { return 1 }\n    if expect_failure == 0 { return ifx image.is_ok then 0 else 2 }\n'+
        '    if image.is_ok || image.error.code != AllocationFailed || image.y.storage.count != 0 || image.u.storage.count != 0 || image.v.storage.count != 0 { return 3 }\n    return 0\n}\n'+loaders)
    (a.out/'oracle.json').write_text(json.dumps(dict(revision=PIN,library_sha256=digest(library),encoder_library_sha256=digest(encoder_library),native_host_sha256=hashlib.sha256(native.encode()).hexdigest(),
        vectors=len(records),large_vectors=len(large),shards=12,compared_samples=compared,grain_fields=len(FIELDS),truncated_prefixes=prefixes,depths=[8,10,12],configs=['mono','420','422','444'],
        large_post_filter_frames=post_totals,comparison='full samples up to 64x64; per-row hashes above',
        output_cap_contract='exact aggregate source YUV bytes (ValidatePixelDimensions in headers)',
        fixtures=records,scope='real displayed grain KEY frames: lossless up to 64x64, lossless and lossy with post filters up to 200x72/130x97/96x129; inter frames remain unsupported'),indent=2)+'\n')
    print(f'Pinned encoded grain: {len(records)} KEY frames ({len(large)} above 64x64; post filters '+', '.join(f'{k} {v}' for k,v in post_totals.items())+f'), {compared} displayed samples, {prefixes} prefixes, {len(FIELDS)} grain fields')


if __name__=='__main__':main()

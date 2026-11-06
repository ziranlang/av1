#!/usr/bin/env python3
"""Pinned libaom encoder/decoder controls for full KEY header edge branches."""
import argparse
import json
import os
from pathlib import Path
import shlex
from av1_chroma_oracle import HOST as DECODER
from av1_frame_oracle import zi_bytes
from av1_lossless_oracle import PIN, digest, run

ENCODER=r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include "aom/aom_encoder.h"
#include "aom/aomcx.h"
static int control(aom_codec_ctx_t *codec,int id,int value) {
  if(aom_codec_control(codec,id,value)) {
    fprintf(stderr,"encoder control%d: %s / %s\n",id,aom_codec_error(codec),aom_codec_error_detail(codec));return 1;
  }return 0;
}
int main(int argc,char **argv) {
  if(argc!=6)return 1;int maximum=atoi(argv[2]),timing=atoi(argv[3]),depth=atoi(argv[4]),mono=atoi(argv[5]);
  if((maximum!=8&&maximum!=64)||timing<0||timing>2||(depth!=8&&depth!=10&&depth!=12)||(mono!=0&&mono!=1))return 2;
  aom_codec_enc_cfg_t cfg;
  if(aom_codec_enc_config_default(aom_codec_av1_cx(),&cfg,AOM_USAGE_GOOD_QUALITY))return 3;
  cfg.g_w=cfg.g_h=8;cfg.g_forced_max_frame_width=cfg.g_forced_max_frame_height=maximum;
  cfg.g_bit_depth=(aom_bit_depth_t)depth;cfg.g_input_bit_depth=depth;cfg.g_profile=depth==12?2:0;cfg.monochrome=mono;
  cfg.g_error_resilient=timing==2?AOM_ERROR_RESILIENT_DEFAULT:0;
  // A limit of1 suppresses timing in libaom, even in forced video mode.
  // Emit one picture from a video configuration instead.
  cfg.g_threads=1;cfg.g_lag_in_frames=0;cfg.g_limit=0;cfg.g_timebase.num=1;cfg.g_timebase.den=30;
  cfg.rc_end_usage=AOM_Q;cfg.rc_min_quantizer=cfg.rc_max_quantizer=0;
  aom_codec_ctx_t codec={0};if(aom_codec_enc_init(&codec,aom_codec_av1_cx(),&cfg,depth>8?AOM_CODEC_USE_HIGHBITDEPTH:0))return 4;
  if(control(&codec,AOME_SET_CPUUSED,8)||control(&codec,AV1E_SET_LOSSLESS,1)||
     control(&codec,AV1E_SET_FORCE_VIDEO_MODE,1)||control(&codec,AV1E_SET_TIMING_INFO_TYPE,timing)||
     control(&codec,AV1E_SET_ENABLE_CDEF,0)||control(&codec,AV1E_SET_ENABLE_RESTORATION,0)||
     control(&codec,AV1E_SET_ENABLE_INTRABC,0)||control(&codec,AV1E_SET_ENABLE_PALETTE,0)||
     control(&codec,AV1E_SET_ENABLE_FILTER_INTRA,0))return 5;
  aom_image_t image;if(!aom_img_alloc(&image,depth>8?AOM_IMG_FMT_I42016:AOM_IMG_FMT_I420,8,8,1))return 6;image.bit_depth=depth;
  for(int plane=0;plane<3;plane++) {
    int side=plane?4:8,middle=1<<(depth-1),value=plane==0?middle+2:(plane==1?middle/2:(1<<depth)-4);
    for(int y=0;y<side;y++) {
      if(depth==8)memset(image.planes[plane]+y*image.stride[plane],value,side);
      else { uint16_t *row=(uint16_t*)(image.planes[plane]+y*image.stride[plane]);for(int x=0;x<side;x++)row[x]=value; }
    }
  }
  FILE *out=fopen(argv[1],"wb");if(!out)return 7;int frames=0;
  for(int pass=0;pass<2;pass++) {
    if(aom_codec_encode(&codec,pass?NULL:&image,7+pass,1,0))return 8;
    aom_codec_iter_t it=NULL;const aom_codec_cx_pkt_t *pkt;
    while((pkt=aom_codec_get_cx_data(&codec,&it)))if(pkt->kind==AOM_CODEC_CX_FRAME_PKT) {
      if(fwrite(pkt->data.frame.buf,1,pkt->data.frame.sz,out)!=pkt->data.frame.sz)return 9;frames++;
    }
  }
  if(fclose(out)||frames!=1)return 10;aom_img_free(&image);return aom_codec_destroy(&codec);
}
'''


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True);p.add_argument('--aom',type=Path,required=True)
    a=p.parse_args();a.out.mkdir(parents=True,exist_ok=True)
    if run(['git','-C',str(a.aom),'rev-parse','HEAD'],text=True,capture_output=True).stdout.strip()!=PIN:raise SystemExit('libaom pin mismatch')
    run(['git','-C',str(a.aom),'diff','--quiet','HEAD','--'])
    library=Path(os.environ.get('AV1_AOM_LIBRARY',str(a.root/'build/native/aom/libaom.a')))
    config=Path(os.environ.get('AV1_AOM_CONFIG',str(a.root/'build/native/aom')))
    compiler=shlex.split(os.environ.get('CC','cc'))+['-std=c11','-O2','-I'+str(a.aom),'-I'+str(config)]
    encoder_library=Path(os.environ.get('AV1_AOM_ENCODER_LIBRARY',str(a.root/'build/native/aom/libaom.a')))
    encoder_config=encoder_library.parent
    source=a.out/'encoder.c';source.write_text(ENCODER);encoder=a.out/'encoder'
    encoder_compiler=shlex.split(os.environ.get('CC','cc'))+['-std=c11','-O2','-I'+str(a.aom),'-I'+str(encoder_config)]
    run(encoder_compiler+[str(source),str(encoder_library),'-lm','-lpthread','-o',str(encoder)])
    native=DECODER.replace('static int blocks,cfl,palette_y,palette_uv;',
        'static int blocks,cfl,palette_y,palette_uv;static unsigned controls[13];')
    native=native.replace('blocks++;',
        'blocks++;controls[0]=p->common.seq_params->reduced_still_picture_hdr;controls[1]=p->common.seq_params->max_frame_width;controls[2]=p->common.seq_params->max_frame_height;controls[3]=p->common.seq_params->timing_info_present;controls[4]=p->common.seq_params->timing_info.equal_picture_interval;controls[5]=p->common.seq_params->decoder_model_info_present_flag;controls[6]=p->common.frame_presentation_time;controls[7]=p->common.buffer_removal_times[0];controls[8]=p->common.current_frame_id;controls[9]=p->common.current_frame.order_hint;controls[10]=p->common.current_frame.frame_type;controls[11]=p->common.seq_params->monochrome;controls[12]=p->common.seq_params->frame_id_numbers_present_flag;')
    native=native.replace('||!image->planes[1]||!image->planes[2]','')
    native=native.replace('for(int plane=0;plane<3;plane++) {','for(int plane=0;plane<3;plane++) {if(controls[11]&&plane){printf("\\n");continue;}')
    native=native.replace('return aom_codec_destroy(&codec);','for(int i=0;i<13;i++)printf("%u ",controls[i]);printf("\\n");return aom_codec_destroy(&codec);')
    source=a.out/'decoder.c';source.write_text(native);decoder=a.out/'decoder'
    run(compiler+[str(source),str(library),'-Wl,--wrap=av1_read_mode_info','-lm','-o',str(decoder)])
    cases=[]
    for depth,mono,maximum,timing in ((depth,mono,maximum,timing) for depth in (8,10,12) for mono in (0,1) for maximum in (8,64) for timing in range(3)):
            path=a.out/f'depth{depth}-mono{mono}-max{maximum}-timing{timing}.obu'
            command=[str(encoder),str(path),str(maximum),str(timing),str(depth),str(mono)];run(command)
            output=run([str(decoder),str(path)],text=True,capture_output=True).stdout.splitlines()
            if list(map(int,output[0].split()))!=[8,8,depth,1,1]:raise SystemExit('native control frame format mismatch')
            middle=1<<(depth-1)
            for plane,side,value in [(0,8,middle+2),(1,4,middle//2),(2,4,(1<<depth)-4)]:
                if mono and plane:continue
                if list(map(int,output[plane+1].split()))!=[value]*(side*side):raise SystemExit('native control frame pixel mismatch')
            controls=list(map(int,output[5].split()))
            if controls[0]!=0 or controls[1:3]!=[maximum,maximum] or controls[10]!=0:raise SystemExit('encoder ignored full-key dimension control')
            if controls[3]!=int(timing!=0) or controls[5]!=int(timing==2):raise SystemExit('encoder ignored timing model control')
            if controls[11:]!=[mono,int(timing==2)]:raise SystemExit('encoder ignored mono/frame-ID control')
            cases.append(dict(maximum=maximum,timing=timing,depth=depth,mono=mono,controls=controls,data=list(path.read_bytes()),sha256=digest(path),command=command))
    rel=os.path.relpath(a.root/'src/decoder.zi',a.out)
    lines=['#import, file "'+rel+'";','using Codec :: #import "image_codec/api"','#program_export','main :: () -> s32 {']
    lines += [f'    if Case{i}() != 0 {{ return {i+1} }}' for i in range(len(cases))]+['    return 0','}',
        'PutBytes :: (target: []u8, offset: s64, source: []u8) { for i: 0..source.count - 1 { target[offset + i] = source[i] } }']
    helpers=''
    fields=['image.sequence.reduced_still_picture_header','image.sequence.max_frame_width','image.sequence.max_frame_height',
        'image.sequence.timing_info_present','image.sequence.equal_picture_interval','image.sequence.decoder_model_info_present',
        'image.frame.frame_presentation_time','image.frame.buffer_removal_times[0]','image.frame.current_frame_id',
        'image.frame.order_hint','image.frame.frame_type','image.sequence.monochrome','image.sequence.frame_id_numbers_present']
    for i,c in enumerate(cases):
        data,loader=zi_bytes('bytes',c['data'],f'KeyControls{i}');helpers+=loader
        sample=1 if c['depth']==8 else 2;middle=1<<(c['depth']-1);uv_count=0 if c['mono'] else 16*sample
        lines += [f'Case{i} :: () -> s32 {{',data.rstrip(),'    options := DefaultOptions()',
            '    budget := NewWorkerBudget(options.limits); options.workers = *budget',
            '    image := DecodeAv1YUV(BorrowBytes(bytes[:]), *options)','    if !image.is_ok { return 100 + cast(s32)image.error.code }',
            f'    if image.y.width != 8 || image.y.height != 8 || image.y.storage.count != {64*sample} || image.u.storage.count != {uv_count} || image.v.storage.count != {uv_count} {{ return 1 }}',
            '    if budget.active_workers != 0 || budget.working_bytes != 0 { return 4 }',
            '    if !image.frame.show_frame || !image.frame.force_integer_mv || image.frame.primary_ref_frame != 7 || image.frame.refresh_frame_flags != 255 { return 5 }']
        for plane,name,count,value in [(0,'y',64,middle+2),(1,'u',16,middle//2),(2,'v',16,(1<<c['depth'])-4)]:
            if c['mono'] and plane:continue
            lines += [f'    for pixel: 0..{count-1} {{',f'        at := pixel * {sample}; value := cast(u32)image.{name}.storage[at]']
            if sample==2:lines += [f'        value |= cast(u32)image.{name}.storage[at + 1] << 8']
            lines += [f'        if value != {value} {{ return {plane+2} }}','    }']
        for field,(name,value) in enumerate(zip(fields,c['controls'])):
            expected=('true' if value else 'false') if field in (0,3,4,5,11,12) else str(value)
            lines.append(f'    if {name} != {expected} {{ return {10+field} }}')
        lines += [f'    if image.frame.frame_size_override != {"true" if c["maximum"]==64 else "false"} {{ return 6 }}',
            '    for end: 0..bytes.count - 1 {','        truncated := DecodeAv1YUV(BorrowBytes(bytes[:end]), *options)',
            '        if truncated.is_ok || truncated.y.storage.count != 0 || truncated.u.storage.count != 0 || truncated.v.storage.count != 0 || budget.active_workers != 0 || budget.working_bytes != 0 { return 7 }','    }','    return 0','}']
    (a.out/'av1_key_controls_vectors.zi').write_text('\n'.join(lines)+'\n'+helpers)
    (a.out/'oracle.json').write_text(json.dumps(dict(revision=PIN,library_sha256=digest(library),encoder_library_sha256=digest(encoder_library),frames=len(cases),
        cases=cases,scope='shown full-key size overrides and equal/decoder-model timing; reference/inter tools pending'),indent=2)+'\n')
    print(f'Pinned full-key control oracle: {len(cases)} frames, maxima8/64, timing none/equal/decoder-model')


if __name__=='__main__':main()

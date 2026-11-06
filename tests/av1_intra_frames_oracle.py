#!/usr/bin/env python3
"""Pinned libaom intra still frames for the owned scalar intra-frame engine.

Test-only native code: the pinned libaom encoder makes lossy/lossless KEY
stills, with deblocking, CDEF and loop restoration enabled per case; the
pinned decoder supplies per-row output hashes, the tools its own mode parser
saw and the post filters the frame signalled. The owned decoder must
reproduce every output row bit-exactly.
"""
import argparse
import json
import os
from pathlib import Path
import shlex
from av1_frame_oracle import zi_bytes
from av1_lossless_oracle import PIN, digest, run

ENCODER=r'''
#include <math.h>
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include "aom/aom_encoder.h"
#include "aom/aomcx.h"
static int control(aom_codec_ctx_t *codec,int id,int value) {
  if(aom_codec_control(codec,id,value)){fprintf(stderr,"control %d: %s\n",id,aom_codec_error_detail(codec));return 1;}
  return 0;
}
static unsigned sample(int kind,int seed,int plane,int x,int y,int depth) {
  double maximum=(double)((1u<<depth)-1),v;
  uint32_t k=(uint32_t)seed*2654435761u^(uint32_t)x*73856093u^(uint32_t)y*19349663u^(uint32_t)plane*83492791u;
  k^=k>>13;k*=0x5bd1e995u;k^=k>>15;
  if(kind==1) {
    static const int colors[5]={16,235,128,60,200};
    v=colors[((x/12)+(y/9)*7+seed+plane*3)%5]/255.0;
    if((x+y*3+seed)%17<2)v=1.0-v;
  } else if(kind==2) {
    v=0.5+0.3*sin((x*0.04+seed)*(1+plane*0.2))*cos(y*0.03);
  } else {
    v=0.5+0.25*sin((x*0.07+seed)*(1+plane*0.3))+0.15*cos(y*0.11-x*0.03)+((k&255)/255.0-0.5)*0.12;
    if((x/16+y/16+seed)%3==0)v+=0.15;
  }
  if(v<0)v=0;if(v>1)v=1;return (unsigned)(v*maximum+0.5);
}
int main(int argc,char **argv) {
  if(argc!=19)return 1;
  int w=atoi(argv[2]),h=atoi(argv[3]),depth=atoi(argv[4]),format=atoi(argv[5]),q=atoi(argv[6]),sb128=atoi(argv[7]);
  int columns=atoi(argv[8]),rows=atoi(argv[9]),screen=atoi(argv[10]),aq=atoi(argv[11]),deltaq=atoi(argv[12]);
  int lossless=atoi(argv[13]),reduced=atoi(argv[14]),seed=atoi(argv[15]);
  int filters=atoi(argv[16]),speed=atoi(argv[17]),sharpness=atoi(argv[18]);
  aom_codec_enc_cfg_t cfg;
  // libaom honours loop-filter sharpness and perceptual-AI delta q (mode 3)
  // only in all-intra mode, and an ROI segment map (aq 9) only in realtime
  // mode at speed 7 or above.
  int roi=aq==9;if(roi){aq=0;if(speed<7)speed=7;}
  if(aom_codec_enc_config_default(aom_codec_av1_cx(),&cfg,
      roi?AOM_USAGE_REALTIME:sharpness||deltaq==3?AOM_USAGE_ALL_INTRA:AOM_USAGE_GOOD_QUALITY))return 2;
  cfg.g_w=w;cfg.g_h=h;cfg.g_bit_depth=(aom_bit_depth_t)depth;cfg.g_input_bit_depth=depth;
  cfg.g_profile=(depth==12||format==1)?2:(format==2?1:0);cfg.monochrome=format==3;
  cfg.g_threads=1;cfg.g_lag_in_frames=0;cfg.g_limit=1;cfg.kf_max_dist=0;
  cfg.rc_end_usage=AOM_Q;cfg.rc_min_quantizer=cfg.rc_max_quantizer=q;
  // ROI deltas clamp to the quantizer range, so ROI rows widen it around q.
  if(roi){cfg.rc_min_quantizer=q>20?q-20:1;cfg.rc_max_quantizer=q<43?q+20:63;}
  aom_codec_ctx_t codec={0};
  if(aom_codec_enc_init(&codec,aom_codec_av1_cx(),&cfg,depth>8?AOM_CODEC_USE_HIGHBITDEPTH:0))return 3;
  if(control(&codec,AOME_SET_CPUUSED,speed)||control(&codec,AV1E_SET_LOOPFILTER_CONTROL,filters&1)||
     control(&codec,AV1E_SET_ENABLE_CDEF,(filters>>1)&1)||control(&codec,AV1E_SET_ENABLE_RESTORATION,(filters>>2)&1)||
     control(&codec,AOME_SET_SHARPNESS,sharpness)||control(&codec,AV1E_SET_DELTALF_MODE,deltaq&&(filters&1))||
     control(&codec,AV1E_SET_ENABLE_INTRABC,0)||control(&codec,AV1E_SET_ENABLE_QM,0)||
     control(&codec,AV1E_SET_ENABLE_SUPERRES,0)||control(&codec,AV1E_SET_LOSSLESS,lossless)||
     control(&codec,AV1E_SET_SUPERBLOCK_SIZE,sb128?AOM_SUPERBLOCK_SIZE_128X128:AOM_SUPERBLOCK_SIZE_64X64)||
     control(&codec,AV1E_SET_TILE_COLUMNS,columns)||control(&codec,AV1E_SET_TILE_ROWS,rows)||
     control(&codec,AV1E_SET_TUNE_CONTENT,screen==1?AOM_CONTENT_SCREEN:AOM_CONTENT_DEFAULT)||
     control(&codec,AV1E_SET_ENABLE_PALETTE,screen==1)||control(&codec,AV1E_SET_AQ_MODE,aq)||
     control(&codec,AV1E_SET_DELTAQ_MODE,deltaq)||control(&codec,AV1E_SET_REDUCED_TX_TYPE_SET,reduced))return 4;
  if(roi&&control(&codec,AOME_SET_CQ_LEVEL,q))return 12;
  if(roi) {
    aom_roi_map_t map;memset(&map,0,sizeof(map));
    map.enabled=1;map.rows=2*((h+7)>>3);map.cols=2*((w+7)>>3);map.roi_map=malloc(map.rows*map.cols);if(!map.roi_map)return 10;
    for(unsigned r=0;r<map.rows;r++)for(unsigned c=0;c<map.cols;c++)map.roi_map[r*map.cols+c]=(unsigned char)((r/4+c/4*3+seed)%4);
    const int delta[4]={0,-12,10,20};for(int i=0;i<8;i++){map.delta_q[i]=i<4?delta[i]:0;map.ref_frame[i]=-1;}
    if(aom_codec_control(&codec,AOME_SET_ROI_MAP,&map)){fprintf(stderr,"roi: %s\n",aom_codec_error_detail(&codec));return 11;}
    free(map.roi_map);
  }
  aom_img_fmt_t fmt=format==1?AOM_IMG_FMT_I422:(format==2?AOM_IMG_FMT_I444:AOM_IMG_FMT_I420);
  if(depth>8)fmt=(aom_img_fmt_t)(fmt|AOM_IMG_FMT_HIGHBITDEPTH);
  aom_image_t image;if(!aom_img_alloc(&image,fmt,w,h,1))return 5;image.bit_depth=depth;
  for(int plane=0;plane<3;plane++) {
    int sx=plane?(int)image.x_chroma_shift:0,sy=plane?(int)image.y_chroma_shift:0;
    int pw=(w+(1<<sx)-1)>>sx,ph=(h+(1<<sy)-1)>>sy;
    for(int y=0;y<ph;y++)for(int x=0;x<pw;x++) {
      unsigned value=format==3&&plane?(1u<<(depth-1)):sample(screen,seed,plane,x,y,depth);
      if(depth>8)((uint16_t*)(image.planes[plane]+y*image.stride[plane]))[x]=(uint16_t)value;
      else image.planes[plane][y*image.stride[plane]+x]=(unsigned char)value;
    }
  }
  FILE *out=fopen(argv[1],"wb");if(!out)return 6;int frames=0;
  for(int pass=0;pass<2;pass++) {
    if(aom_codec_encode(&codec,pass?NULL:&image,0,1,0))return 7;
    aom_codec_iter_t it=NULL;const aom_codec_cx_pkt_t *pkt;
    while((pkt=aom_codec_get_cx_data(&codec,&it)))if(pkt->kind==AOM_CODEC_CX_FRAME_PKT) {
      if(fwrite(pkt->data.frame.buf,1,pkt->data.frame.sz,out)!=pkt->data.frame.sz)return 8;frames++;
    }
  }
  if(fclose(out)||frames!=1)return 9;aom_img_free(&image);return aom_codec_destroy(&codec);
}
'''

DECODER=r'''
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include "aom/aom_decoder.h"
#include "aom/aomdx.h"
#include "av1/decoder/decoder.h"
#include "av1/decoder/decodemv.h"
static long blocks,cfl,palette_y,palette_uv,filter,skips,segments,angles,large,rect;
static int header[16],seen;static const AV1_COMMON *frame_common;
void __real_av1_read_mode_info(AV1Decoder*,DecoderCodingBlock*,aom_reader*,int,int);
void __wrap_av1_read_mode_info(AV1Decoder *p,DecoderCodingBlock *d,aom_reader *r,int x,int y) {
  __real_av1_read_mode_info(p,d,r,x,y);const MB_MODE_INFO *b=d->xd.mi[0];const AV1_COMMON *cm=&p->common;blocks++;
  cfl+=d->xd.is_chroma_ref&&b->uv_mode==UV_CFL_PRED;palette_y+=b->palette_mode_info.palette_size[0]>0;
  palette_uv+=d->xd.is_chroma_ref&&b->palette_mode_info.palette_size[1]>0;filter+=b->filter_intra_mode_info.use_filter_intra;
  skips+=b->skip_txfm;segments+=b->segment_id!=0;angles+=b->angle_delta[0]!=0;large+=block_size_wide[b->bsize]>=64;
  rect+=block_size_wide[b->bsize]!=block_size_high[b->bsize];
  if(!seen) {
    seen=1;frame_common=cm;header[0]=cm->tiles.cols;header[1]=cm->tiles.rows;header[2]=cm->seq_params->sb_size==BLOCK_128X128;
    header[3]=cm->delta_q_info.delta_q_present_flag;header[4]=cm->delta_q_info.delta_lf_present_flag;header[5]=cm->seg.enabled;
    header[6]=cm->features.coded_lossless;header[7]=cm->quant_params.base_qindex;header[8]=cm->features.tx_mode;
    header[9]=cm->features.reduced_tx_set_used;header[10]=cm->lf.filter_level[0];header[11]=cm->lf.filter_level[1];
    header[12]=cm->cdef_info.cdef_bits?1:cm->cdef_info.cdef_strengths[0]|cm->cdef_info.cdef_uv_strengths[0];
    header[13]=cm->rst_info[0].frame_restoration_type|cm->rst_info[1].frame_restoration_type|cm->rst_info[2].frame_restoration_type;
    header[14]=cm->features.allow_screen_content_tools;header[15]=cm->film_grain_params.apply_grain;
  }
}
int main(int argc,char **argv) {
  if(argc!=2)return 1;FILE *f=fopen(argv[1],"rb");if(!f)return 2;
  static unsigned char data[1<<20];size_t bytes=fread(data,1,sizeof(data),f);fclose(f);if(!bytes||bytes==sizeof(data))return 3;
  aom_codec_ctx_t codec={0};aom_codec_dec_cfg_t cfg={0};cfg.threads=1;cfg.allow_lowbitdepth=1;
  if(aom_codec_dec_init(&codec,aom_codec_av1_dx(),&cfg,0))return 4;
  if(aom_codec_decode(&codec,data,bytes,NULL)){fprintf(stderr,"native decode: %s\n",aom_codec_error(&codec));return 5;}
  aom_codec_iter_t it=NULL;aom_image_t *image=aom_codec_get_frame(&codec,&it);if(!image||!blocks)return 6;
  int mono=image->monochrome;
  printf("%u %u %u %u %u %d\n",image->d_w,image->d_h,image->bit_depth,image->x_chroma_shift,image->y_chroma_shift,mono);
  for(int plane=0;plane<3;plane++) {
    if(mono&&plane){printf("\n");continue;}
    unsigned sx=plane?image->x_chroma_shift:0,sy=plane?image->y_chroma_shift:0;
    unsigned width=(image->d_w+((1u<<sx)-1))>>sx,height=(image->d_h+((1u<<sy)-1))>>sy;
    for(unsigned y=0;y<height;y++) {
      long long hash=0;
      for(unsigned x=0;x<width;x++) {
        unsigned value=image->fmt&AOM_IMG_FMT_HIGHBITDEPTH?((uint16_t*)(image->planes[plane]+y*image->stride[plane]))[x]:image->planes[plane][y*image->stride[plane]+x];
        hash=(hash*131+(value&255)+1)%2147483629LL;
        if(image->bit_depth>8)hash=(hash*131+(value>>8)+1)%2147483629LL;
      }
      printf("%lld ",hash);
    }printf("\n");
  }
  for(int i=0;i<16;i++)printf("%d ",header[i]);printf("\n");
  printf("%ld %ld %ld %ld %ld %ld %ld %ld %ld %ld\n",blocks,cfl,palette_y,palette_uv,filter,skips,segments,angles,large,rect);
  const AV1_COMMON *cm=frame_common;long units[3]={0,0,0};int strengths=0;
  for(int plane=0;plane<(mono?1:3);plane++)if(cm->rst_info[plane].frame_restoration_type!=RESTORE_NONE)
    for(int i=0;i<cm->rst_info[plane].num_rest_units;i++) {
      int type=cm->rst_info[plane].unit_info[i].restoration_type;
      units[type==RESTORE_WIENER?1:type==RESTORE_SGRPROJ?2:0]++;
    }
  for(int i=0;i<cm->cdef_info.nb_cdef_strengths;i++)strengths+=cm->cdef_info.cdef_strengths[i]!=0||cm->cdef_info.cdef_uv_strengths[i]!=0;
  printf("%d %d %d %d %d %d %d %d %d %ld %ld %ld\n",cm->lf.filter_level[0],cm->lf.filter_level[1],cm->lf.filter_level_u,cm->lf.filter_level_v,
    cm->lf.sharpness_level,cm->delta_q_info.delta_lf_present_flag,cm->cdef_info.cdef_bits,cm->cdef_info.cdef_damping,strengths,units[0],units[1],units[2]);
  return aom_codec_destroy(&codec);
}
'''

# name, width, height, depth, format (0 420, 1 422, 2 444, 3 mono), quantizer,
# sb128, tile column/row log2, content (0 noisy, 1 screen, 2 smooth), aq (9 ROI segments), deltaq, lossless, reduced tx
# set, seed, then post filters (1 deblock, 2 CDEF, 4 restoration), encoder
# speed and sharpness (nonzero selects all-intra usage). Filter-free rows use speed 6;
# restoration rows use speed 0, the only speed at which libaom searches loop
# restoration for frames this small.
CASES=[
    ('base420',64,64,8,0,30,0,0,0,0,0,0,0,0,1,0,6,0),
    ('odd420',77,45,8,0,10,0,0,0,0,0,0,0,0,2,0,6,0),
    ('sb128',130,98,8,0,45,1,0,0,0,0,0,0,0,3,0,6,0),
    ('tiles2x2',200,136,8,0,25,0,1,1,0,0,0,0,0,4,0,6,0),
    ('yuv444',96,80,8,2,30,0,0,0,0,0,0,0,0,5,0,6,0),
    ('yuv422',90,70,8,1,30,0,0,0,0,0,0,0,0,6,0,6,0),
    ('mono',88,72,8,3,30,0,0,0,0,0,0,0,0,7,0,6,0),
    ('palette420',120,96,8,0,20,0,0,0,1,0,0,0,0,8,0,6,0),
    ('palette444',120,96,8,2,20,0,0,0,1,0,0,0,0,9,0,6,0),
    ('segments',128,128,8,0,40,0,0,0,0,9,0,0,0,10,0,7,0),
    ('deltaq',128,96,8,0,30,0,0,0,0,0,3,0,0,11,0,6,0),
    ('depth10',72,56,10,0,30,0,0,0,0,0,0,0,0,12,0,6,0),
    ('depth12',72,56,12,2,30,0,0,0,0,0,0,0,0,13,0,6,0),
    ('lossless',66,50,8,0,0,0,0,0,0,0,0,1,0,14,0,6,0),
    ('screen422',100,60,10,1,15,0,0,0,1,0,0,0,0,15,0,6,0),
    ('sb128tiles',160,120,8,0,55,1,1,0,0,0,0,0,0,16,0,6,0),
    ('tiny',33,17,8,0,35,0,0,0,0,0,0,0,0,17,0,6,0),
    ('reduced',96,64,8,0,25,0,0,0,0,0,0,0,1,18,0,6,0),
    ('single8',8,8,8,2,20,0,0,0,0,0,0,0,0,19,0,6,0),
    ('deblock420',96,80,8,0,52,0,0,0,0,0,0,0,0,20,1,6,0),
    ('deblock444sharp',80,64,10,2,56,0,0,0,0,0,0,0,0,21,1,6,3),
    ('deblock422sharp',90,70,8,1,50,0,0,0,0,0,0,0,0,22,1,6,6),
    ('cdef420',128,96,8,0,50,0,0,0,0,0,0,0,0,23,3,4,0),
    ('cdef444',96,80,10,2,54,0,0,0,0,0,0,0,0,24,3,4,0),
    ('cdef422',100,72,8,1,52,0,0,0,0,0,0,0,0,25,3,4,0),
    ('cdefmono',88,72,12,3,56,0,0,0,0,0,0,0,0,26,3,4,0),
    ('restore420',144,100,8,0,35,0,0,0,0,0,0,0,0,41,7,0,0),
    ('restore444',128,96,10,2,25,0,0,0,0,0,0,0,0,41,7,0,0),
    ('restore422',112,80,10,1,15,0,0,0,0,0,0,0,0,41,7,0,0),
    ('restoremono',96,96,12,3,35,0,0,0,0,0,0,0,0,41,7,0,0),
    ('restore12',128,96,12,2,30,0,0,0,0,0,0,0,0,44,7,0,0),
    ('restore12_420',144,100,12,0,30,0,0,0,0,0,0,0,0,44,7,0,0),
    ('restoreonly',144,100,8,0,20,0,0,0,0,0,0,0,0,42,4,0,0),
    ('restoredeblock',128,96,10,2,35,0,0,0,0,0,0,0,0,42,5,0,0),
    ('restoretiles',200,136,8,0,30,0,1,1,0,0,0,0,0,45,7,0,0),
    ('restoredeltaq',128,128,8,0,35,0,0,0,0,0,3,0,0,31,7,0,0),
    ('restoresmooth',128,96,8,0,35,0,0,0,2,0,0,0,0,38,7,0,0),
    ('filters128',200,160,8,0,35,1,1,0,0,0,0,0,0,41,7,0,0),
    ('filtersdeltalf',128,128,8,0,50,0,0,0,0,0,3,0,0,31,7,2,0),
    ('filterssegments',128,128,8,0,52,0,0,0,0,9,0,0,0,32,7,7,0),
    ('filterstiny',33,17,8,0,56,0,0,0,0,0,0,0,0,33,7,1,0),
    ('filters12',72,56,12,0,54,0,0,0,0,0,0,0,0,34,7,1,0),
    ('filters422',112,80,10,1,52,0,0,0,0,0,0,0,0,35,7,1,0),
    ('filtersscreen',120,96,8,0,40,0,0,0,1,0,0,0,0,36,7,2,0),
    ('smooth',96,96,8,0,60,0,0,0,2,0,0,0,0,37,0,6,0),
    ('smoothfilters',128,96,8,0,60,0,0,0,2,0,0,0,0,38,7,2,0),
]
FIELDS=['tile_columns','tile_rows','sb128','delta_q_present','delta_lf_present','segmentation','coded_lossless',
    'base_q_idx','tx_mode','reduced_tx_set','loop_filter_y0','loop_filter_y1','cdef','restoration','screen_content','film_grain']
TOOLS=['blocks','cfl','palette_y','palette_uv','filter_intra','skip','nonzero_segment','angle_delta','wide64','rectangular']
FILTERS=['level_y0','level_y1','level_u','level_v','sharpness','delta_lf','cdef_bits','cdef_damping','cdef_strengths',
    'restore_none_units','restore_wiener_units','restore_sgr_units']


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True);p.add_argument('--aom',type=Path,required=True)
    a=p.parse_args();a.out.mkdir(parents=True,exist_ok=True)
    if run(['git','-C',str(a.aom),'rev-parse','HEAD'],text=True,capture_output=True).stdout.strip()!=PIN:raise SystemExit('libaom pin mismatch')
    run(['git','-C',str(a.aom),'diff','--quiet','HEAD','--'])
    library=Path(os.environ.get('AV1_AOM_LIBRARY',str(a.root/'build/native/aom/libaom.a')))
    config=Path(os.environ.get('AV1_AOM_CONFIG',str(a.root/'build/native/aom')))
    encoder_library=Path(os.environ['AV1_AOM_ENCODER_LIBRARY'])
    cc=shlex.split(os.environ.get('CC','cc'))
    source=a.out/'encoder.c';source.write_text(ENCODER);encoder=a.out/'encoder'
    run(cc+['-std=c11','-O2','-I'+str(a.aom),'-I'+str(encoder_library.parent),str(source),str(encoder_library),'-lm','-lpthread','-o',str(encoder)])
    source=a.out/'decoder.c';source.write_text(DECODER);decoder=a.out/'decoder'
    run(cc+['-std=c11','-O2','-I'+str(a.aom),'-I'+str(config),str(source),str(library),'-Wl,--wrap=av1_read_mode_info','-lm','-o',str(decoder)])
    cases=[]
    for name,w,h,depth,fmt,q,sb128,tc,tr,screen,aq,deltaq,lossless,reduced,seed,filters,speed,sharpness in CASES:
        path=a.out/f'{name}.obu'
        command=[str(encoder),str(path)]+[str(v) for v in (w,h,depth,fmt,q,sb128,tc,tr,screen,aq,deltaq,lossless,reduced,seed,filters,speed,sharpness)]
        run(command)
        lines=run([str(decoder),str(path)],text=True,capture_output=True).stdout.split('\n')
        width,height,bit_depth,sx,sy,mono=map(int,lines[0].split())
        if (width,height,bit_depth,mono)!=(w,h,depth,int(fmt==3)):raise SystemExit(name+': native frame format mismatch')
        rows=[list(map(int,lines[1+plane].split())) for plane in range(3)]
        header=dict(zip(FIELDS,map(int,lines[4].split())));tools=dict(zip(TOOLS,map(int,lines[5].split())))
        post=dict(zip(FILTERS,map(int,lines[6].split())))
        # A row may signal only post filters it requested; the encoder's RD
        # may still choose zero, so exercise is checked over the corpus below.
        deblocked=bool(header['loop_filter_y0'] or header['loop_filter_y1']);restored=bool(header['restoration'])
        if header['film_grain'] or (deblocked and not filters&1) or (post['cdef_strengths'] and not filters&2) or (restored and not filters&4):
            raise SystemExit(name+': signalled an unrequested post filter: '+json.dumps(dict(header=header,post=post)))
        if post['sharpness']!=sharpness:raise SystemExit(name+': sharpness control ignored')
        if header['sb128']!=sb128 or header['coded_lossless']!=lossless or header['reduced_tx_set']!=reduced:
            raise SystemExit(name+': encoder ignored a control: '+json.dumps(header))
        if screen==1 and not header['screen_content']:raise SystemExit(name+': screen-content tools not signalled')
        cases.append(dict(name=name,width=w,height=h,depth=depth,format=fmt,quantizer=q,controls=dict(sb128=sb128,
            tile_columns_log2=tc,tile_rows_log2=tr,screen=screen,aq=aq,deltaq=deltaq,lossless=lossless,reduced_tx_set=reduced,seed=seed),
            filters=dict(deblock=filters&1,cdef=(filters>>1)&1,restoration=(filters>>2)&1,speed=speed,sharpness=sharpness),
            subsampling=[sx,sy],header=header,post_filters=post,tools=tools,rows=rows,data=list(path.read_bytes()),sha256=digest(path),command=command))
    totals={tool:sum(c['tools'][tool] for c in cases) for tool in TOOLS}
    # libaom never signals skip_txfm on intra blocks (encodemb.c), so skip=1
    # is outside this oracle's reach and is reported, not required.
    for tool in ('cfl','palette_y','palette_uv','filter_intra','nonzero_segment','angle_delta','wide64','rectangular'):
        if not totals[tool]:raise SystemExit('fixtures never exercise '+tool)
    if not any(c['header']['delta_q_present'] for c in cases):raise SystemExit('fixtures never signal delta q')
    if not any(c['header']['tile_columns']*c['header']['tile_rows']>1 for c in cases):raise SystemExit('fixtures never use multiple tiles')
    if not any(c['header']['segmentation'] for c in cases):raise SystemExit('fixtures never enable segmentation')
    post_totals={name:sum(c['post_filters'][name] for c in cases) for name in FILTERS}
    for name in ('level_y0','level_y1','level_u','level_v','restore_wiener_units','restore_sgr_units','cdef_strengths'):
        if not post_totals[name]:raise SystemExit('fixtures never exercise '+name)
    if not any(c['post_filters']['sharpness']>4 for c in cases):raise SystemExit('fixtures never use sharpness above 4')
    if not any(c['post_filters']['cdef_bits'] for c in cases):raise SystemExit('fixtures never signal several CDEF strengths')
    if not any(c['header']['restoration'] and not c['post_filters']['cdef_strengths'] for c in cases):raise SystemExit('fixtures never restore without CDEF')
    if not any(c['post_filters']['delta_lf'] and c['filters']['deblock'] for c in cases):raise SystemExit('fixtures never filter with delta lf')
    for fmt in (0,1,2,3):
        if not any(c['format']==fmt and c['post_filters']['cdef_strengths'] for c in cases):raise SystemExit(f'format {fmt} never uses CDEF')
    rel=os.path.relpath(a.root/'src/decoder.zi',a.out)
    lines=['#import, file "'+rel+'";','using Codec :: #import "image_codec/api"','',
        '// Detailed result for the Go harness; C and the VM see only pass/fail.','#program_export','Run :: () -> s32 {']
    lines += [f'    result{i} := Case{i}()\n    if result{i} != 0 {{ return {(i+1)*1000000} + result{i} }}' for i in range(len(cases))]
    lines += ['    return 0','}','#program_export','main :: () -> s32 { return ifx Run() == 0 then 0 else 1 }',
        'PutBytes :: (target: []u8, offset: s64, source: []u8) { for i: 0..source.count - 1 { target[offset + i] = source[i] } }',
        '// Row hash over the owned output bytes (little-endian samples), matching the native host.',
        'CheckPlane :: (plane: *Plane, rows: []s64) -> s32 {',
        '    for row: 0..rows.count - 1 {',
        '        hash: s64; base := row * plane.stride',
        '        for i: 0..cast(s64)plane.width * plane.sample_bytes - 1 { hash = (hash * 131 + cast(s64)plane.storage[base + i] + 1) % 2147483629 }',
        '        if hash != rows[row] { return cast(s32)row + 1 }','    }','    return 0','}']
    helpers=''
    for i,c in enumerate(cases):
        data,loader=zi_bytes('bytes',c['data'],f'IntraFrame{i}');helpers+=loader
        sample=1 if c['depth']==8 else 2;sx,sy=c['subsampling'];mono=c['format']==3
        cw=(c['width']+sx)>>sx;ch=(c['height']+sy)>>sy
        lines += [f'// {c["name"]}: {c["width"]}x{c["height"]} depth {c["depth"]} format {c["format"]} q {c["quantizer"]}',
            f'Case{i} :: () -> s32 {{',data.rstrip(),'    options := DefaultOptions()',
            '    budget := NewWorkerBudget(options.limits); options.workers = *budget',
            '    image := DecodeAv1YUV(BorrowBytes(bytes[:]), *options)',
            '    if !image.is_ok || image.error.code != DecodeOK { return 90000 + cast(s32)image.error.code }',
            f'    if image.y.width != {c["width"]} || image.y.height != {c["height"]} || image.y.sample_bytes != {sample} {{ return 80001 }}',
            f'    if image.u.storage.count != {0 if mono else cw*ch*sample} || image.v.storage.count != {0 if mono else cw*ch*sample} {{ return 80002 }}',
            '    if budget.active_workers != 0 || budget.working_bytes != 0 { return 80003 }']
        for plane,field in enumerate(('y','u','v')):
            if mono and plane:continue
            lines += [f'    rows{plane}: [{len(c["rows"][plane])}]s64 = .['+','.join(map(str,c['rows'][plane]))+']',
                f'    check{plane} := CheckPlane(*image.{field}, rows{plane}[:])',
                f'    if check{plane} != 0 {{ return {(plane+1)*10000} + check{plane} }}']
        n=len(c['data'])
        lines += [f'    ends: [4]s64 = .[0,1,{n//2},{n-1}]','    for e: 0..3 {',
            '        truncated := DecodeAv1YUV(BorrowBytes(bytes[:ends[e]]), *options)',
            '        if truncated.is_ok || truncated.y.storage.count != 0 || budget.active_workers != 0 || budget.working_bytes != 0 { return 80004 }',
            '    }','    return 0','}']
    (a.out/'av1_intra_frames_vectors.zi').write_text('\n'.join(lines)+'\n'+helpers)
    (a.out/'oracle.json').write_text(json.dumps(dict(revision=PIN,library_sha256=digest(library),encoder_library_sha256=digest(encoder_library),
        frames=len(cases),tool_totals=totals,post_filter_totals=post_totals,cases=[{k:v for k,v in c.items() if k not in ('rows','data')} for c in cases],
        scope='shown KEY intra stills, with and without deblocking/CDEF/loop restoration; no superres, QM, intrabc or film grain; skip=1 intra blocks not produced by libaom'),indent=2)+'\n')
    print(f'Pinned intra frames: {len(cases)} stills, tools '+', '.join(f'{k} {v}' for k,v in totals.items()))
    print('Post filters: '+', '.join(f'{k} {v}' for k,v in post_totals.items()))


if __name__=='__main__':main()

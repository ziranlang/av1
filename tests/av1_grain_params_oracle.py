#!/usr/bin/env python3
"""Pinned native film_grain_params writer/reader symbol streams, not frames."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
from av1_lossless_oracle import PIN, digest, run

FIELDS = [('apply_grain','apply_grain'),('random_seed','random_seed'),
          ('num_y_points','num_y_points'),('num_cb_points','num_cb_points'),('num_cr_points','num_cr_points'),
          ('chroma_scaling_from_luma','chroma_scaling_from_luma'),('scaling_shift','scaling_shift'),
          ('ar_coeff_lag','ar_coeff_lag'),('ar_coeff_shift','ar_coeff_shift'),('grain_scale_shift','grain_scale_shift'),
          ('cb_mult','cb_mult'),('cb_luma_mult','cb_luma_mult'),('cb_offset','cb_offset'),
          ('cr_mult','cr_mult'),('cr_luma_mult','cr_luma_mult'),('cr_offset','cr_offset'),
          ('overlap_flag','overlap_flag'),('clip_to_restricted_range','clip_to_restricted_range')]
BOOLS = {'apply_grain','chroma_scaling_from_luma','overlap_flag','clip_to_restricted_range'}
for plane,count in (('y',14),('cb',10),('cr',10)):
    for i in range(count*2):
        FIELDS.append((f'scaling_points_{plane}[{i}]',f'scaling_points_{plane}[{i//2}][{i%2}]'))
for plane,count in (('y',24),('cb',25),('cr',25)):
    for i in range(count):
        FIELDS.append((f'ar_coeffs_{plane}[{i}]',f'ar_coeffs_{plane}[{i}]'))

PREFIX = r'''
// Test-only unmodified pinned libaom functions, upstream BSD2/AOM Patent notices
// retained by the source package and src/LICENSE.libaom, PATENTS.libaom.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "av1/encoder/encoder.h"
#include "aom_dsp/bitreader_buffer.h"
#include "aom_dsp/bitwriter_buffer.h"
'''
MAIN = r'''
int main(void) {
  AV1_COMP *cpi=calloc(1,sizeof(*cpi));RefCntBuffer *frame=calloc(1,sizeof(*frame));
  if(!cpi||!frame)return 1;
  SequenceHeader sequence={0};AV1_COMMON *cm=&cpi->common;
  cm->seq_params=&sequence;cm->cur_frame=frame;cm->current_frame.frame_type=KEY_FRAME;
  sequence.bit_depth=8;
  for(int config=0;config<4;config++)for(int mode=0;mode<8;mode++)for(int lag=0;lag<4;lag++) {
    int index=config*32+mode*4+lag,start=index%8;
    sequence.monochrome=config==0;sequence.subsampling_x=config!=3;sequence.subsampling_y=config<2;
    aom_film_grain_t *p=&frame->film_grain_params;memset(p,0,sizeof(*p));
    p->apply_grain=mode!=0;p->update_parameters=1;p->random_seed=(index*601)&65535;
    int ycounts[]={0,0,1,14,2,0,14,6};p->num_y_points=ycounts[mode];
    p->chroma_scaling_from_luma=!sequence.monochrome&&mode==3;
    if(!sequence.monochrome&&!p->chroma_scaling_from_luma&&!(sequence.subsampling_x&&sequence.subsampling_y&&!p->num_y_points)) {
      p->num_cb_points=mode==4||mode==5?10:mode==6?1:mode==7?5:0;
      p->num_cr_points=mode==6&&config>1?0:p->num_cb_points;
    }
    int *points[]={&p->scaling_points_y[0][0],&p->scaling_points_cb[0][0],&p->scaling_points_cr[0][0]};
    int counts[]={p->num_y_points,p->num_cb_points,p->num_cr_points};
    for(int plane=0;plane<3;plane++)for(int i=0;i<counts[plane];i++) {
      points[plane][i*2]=counts[plane]==1?128:i*255/(counts[plane]-1);
      points[plane][i*2+1]=(i*37+index*17+plane*13)&255;
    }
    p->scaling_shift=8+(mode&3);p->ar_coeff_lag=lag;p->ar_coeff_shift=6+((mode+lag)&3);p->grain_scale_shift=mode&3;
    for(int i=0;i<24;i++)p->ar_coeffs_y[i]=((i*83+index*31)&255)-128;
    for(int i=0;i<25;i++){p->ar_coeffs_cb[i]=((i*23+index*47)&255)-128;p->ar_coeffs_cr[i]=((i*67+index*13)&255)-128;}
    p->cb_mult=(index*19)&255;p->cb_luma_mult=(index*73)&255;p->cb_offset=(index*91)&511;
    p->cr_mult=(index*29)&255;p->cr_luma_mult=(index*59)&255;p->cr_offset=(index*113)&511;
    p->overlap_flag=mode&1;p->clip_to_restricted_range=(mode>>1)&1;
    uint8_t bytes[256]={0};struct aom_write_bit_buffer wb={bytes,start};
    write_film_grain_params(cpi,&wb);
    struct aom_read_bit_buffer rb={bytes,bytes+(wb.bit_offset+7)/8,start,NULL,NULL};
    memset(&cm->film_grain_params,0,sizeof(cm->film_grain_params));
    read_film_grain_params(cm,&rb);
    if(rb.bit_offset!=wb.bit_offset)return 2;
    printf("%d %d %d %d %d %d ",sequence.monochrome,sequence.subsampling_x,sequence.subsampling_y,start,wb.bit_offset,(wb.bit_offset+7)/8);
    for(unsigned i=0;i<(wb.bit_offset+7)/8;i++)printf("%u ",bytes[i]);
    printf("| ");p=&cm->film_grain_params;
    OUTPUT_FIELDS
    printf("\n");
  }
  free(frame);free(cpi);return 0;
}
'''


def main():
    parser = argparse.ArgumentParser()
    for name in ('root','out','aom'):
        parser.add_argument('--'+name,type=Path,required=True)
    a=parser.parse_args();a.out.mkdir(parents=True,exist_ok=True)
    if run(['git','-C',str(a.aom),'rev-parse','HEAD'],text=True,capture_output=True).stdout.strip()!=PIN:
        raise SystemExit('libaom pin mismatch')
    run(['git','-C',str(a.aom),'diff','--quiet','HEAD','--'])
    native=''
    for path,name in (('av1/encoder/bitstream.c','static inline void write_film_grain_params('),('av1/decoder/decodeframe.c','static void read_film_grain_params(')):
        # Only the scoped upstream function, never the full source in output.
        with (a.aom/path).open() as stream:
            lines=[];active=False
            for line in stream:
                if name in line:active=True
                if active:
                    lines.append(line)
                    if line=='}\n':break
        if not lines or lines[-1]!='}\n':raise SystemExit('cannot extract pinned grain function')
        native+=''.join(lines)+'\n'
    output='\n'.join(f'printf("%d ",(int)p->{expression});' for _,expression in FIELDS)
    source=a.out/'oracle.c';source.write_text(PREFIX+native+MAIN.replace('OUTPUT_FIELDS',output));host=a.out/'oracle'
    library=Path(os.environ.get('AV1_AOM_ENCODER_LIBRARY',str(a.root/'build/native/aom/libaom.a')))
    config=library.parent
    run(shlex.split(os.environ.get('CC','cc'))+['-std=c11','-O2','-I'+str(a.aom),'-I'+str(config),str(source),str(library),'-lm','-lpthread','-o',str(host)])
    outputs=run([str(host)],text=True,capture_output=True).stdout.splitlines()
    if len(outputs)!=128:raise SystemExit('native grain parameter stream count mismatch')
    cases=[]
    for line in outputs:
        encoded,expected=line.split('|');values=list(map(int,encoded.split()));fields=list(map(int,expected.split()))
        mono,sx,sy,start,end,count=values[:6];data=values[6:]
        if len(data)!=count or len(fields)!=len(FIELDS):raise SystemExit('native grain parameter shape mismatch')
        cases.append(dict(mono=mono,sx=sx,sy=sy,start=start,end=end,data=data,fields=fields))
    malformed=[]
    def mutate(c,position,width,value,offset,label):
        data=c['data'][:]
        for i in range(width):
            at=position+i;mask=1 << (7-at%8)
            data[at//8]=(data[at//8]&~mask)|(((value>>(width-i-1))&1) << (7-at%8))
        malformed.append(dict(c,data=data,offset=offset,label=label))
    for c in cases:
        p=dict((field,value) for (field,_),value in zip(FIELDS,c['fields']))
        if not p['apply_grain']:continue
        position=c['start']+17
        mutate(c,position,4,15,position//8,'y count15')
        y_count=p['num_y_points']
        if y_count>1:
            at=position+4+16
            mutate(c,at,8,p['scaling_points_y[0]'],at//8,'duplicate Y coordinate')
        position+=4+16*y_count
        if not c['mono']:position+=1
        if c['mono'] or p['chroma_scaling_from_luma'] or (c['sx'] and c['sy'] and not y_count):continue
        cb_position=position;cb_count=p['num_cb_points']
        mutate(c,cb_position,4,11,cb_position//8,'Cb count11')
        if cb_count>1:
            at=cb_position+4+16
            mutate(c,at,8,p['scaling_points_cb[0]'],at//8,'duplicate Cb coordinate')
        cr_position=cb_position+4+16*cb_count;cr_count=p['num_cr_points']
        mutate(c,cr_position,4,11,cr_position//8,'Cr count11')
        if cr_count>1:
            at=cr_position+4+16
            mutate(c,at,8,p['scaling_points_cr[0]'],at//8,'duplicate Cr coordinate')
        if c['sx'] and c['sy'] and cb_count:
            mutate(c,cr_position,4,0,(cr_position+4)//8,'420 chroma pair mismatch')
    module=os.path.relpath(a.root/'src/grain/grain_params.zi',a.out)
    bits_module=os.path.relpath(a.root/'src/syntax/bits.zi',a.out)
    bounds=os.path.relpath(a.root/'tests/av1_grain_params.zi',a.out)
    lines=['#import, file "'+module+'";','#import, file "'+bits_module+'";','#import, file "'+bounds+'";','using Codec :: #import "image_codec/api"',
           '#program_export','main :: () -> s32 {','    if GrainParameterBounds() != 0 { return 99 }']
    for i in range(len(cases)):lines.append(f'    if Case{i}() != 0 {{ return {i+1} }}')
    for i in range((len(malformed)+15)//16):lines.append(f'    if Malformed{i}() != 0 {{ return 129 }}')
    lines+=['    return 0','}']
    prefixes=0
    for i,c in enumerate(cases):
        mono=str(bool(c['mono'])).lower();count=len(c['data'])
        lines += [f'Case{i} :: () -> s32 {{','    data: ['+str(count)+']u8 = .['+','.join(map(str,c['data']))+']',
                  f'    bits := NewBits(BorrowBytes(data[:]), 0, {count}); unused ReadBits(*bits, {c["start"]})',
                  f'    parsed := ReadStillFilmGrain(*bits, true, {mono}, {c["sx"]}, {c["sy"]})',
                  f'    if parsed.error.code != DecodeOK || bits.byte*8+bits.bit != {c["end"]} {{ return 1 }}']
        for (field,_),value in zip(FIELDS,c['fields']):
            expected=str(bool(value)).lower() if field in BOOLS else str(value)
            lines.append(f'    if parsed.parameters.{field} != {expected} {{ return 2 }}')
        lines += [f'    for end: 0..{count-1} {{','        short := NewBits(BorrowBytes(data[:]), 0, end)',
                  f'        unused ReadBits(*short, {c["start"]})',
                  f'        truncated := ReadStillFilmGrain(*short, true, {mono}, {c["sx"]}, {c["sy"]})',
                  '        if truncated.error.code != TruncatedInput || truncated.error.offset != end || !GrainParametersEmpty(truncated.parameters) { return 3 }',
                  '    }','    return 0','}']
        prefixes+=count
    for group in range((len(malformed)+15)//16):
        lines.append(f'Malformed{group} :: () -> s32 {{')
        for c in malformed[group*16:(group+1)*16]:
            mono=str(bool(c['mono'])).lower();count=len(c['data'])
            lines+=['    {','        data: ['+str(count)+']u8 = .['+','.join(map(str,c['data']))+']',
                    f'        bits := NewBits(BorrowBytes(data[:]), 0, {count}); unused ReadBits(*bits, {c["start"]})',
                    f'        parsed := ReadStillFilmGrain(*bits, true, {mono}, {c["sx"]}, {c["sy"]})',
                    f'        if parsed.error.code != MalformedInput || parsed.error.offset != {c["offset"]} || !GrainParametersEmpty(parsed.parameters) {{ return 1 }}','    }']
        lines+=['    return 0','}']
    (a.out/'av1_grain_params_vectors.zi').write_text('\n'.join(lines)+'\n')
    (a.out/'oracle.json').write_text(json.dumps(dict(revision=PIN,library_sha256=digest(library),native_functions_sha256=hashlib.sha256(native.encode()).hexdigest(),
        vectors=len(cases),fields=len(FIELDS),truncated_prefixes=prefixes,malformed_cases=len(malformed),malformed_kinds=sorted({c['label'] for c in malformed}),start_bits=list(range(8)),ar_lags=list(range(4)),
        configs=['mono','420','422','444'],scope='non-inter film_grain_params syntax symbol streams from pinned native writer/reader; no encoded-frame or grain synthesis claim'),indent=2)+'\n')
    print(f'Pinned film grain parameters: {len(cases)} native syntax streams, {len(FIELDS)} fields, {prefixes} prefixes')


if __name__=='__main__':main()

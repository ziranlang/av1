#!/usr/bin/env python3
"""Pinned native transform-set selection and unmodified transform-type writer."""
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess

from av1_quantization_oracle import PIN,SIZES,digest,literal
from av1_coefficient_context_oracle import PARSING_SHA

HOST=r'''
#include <stdio.h>
#include <stdlib.h>
#include "av1/common/av1_common_int.h"
#include "av1/common/entropymode.h"
#include "av1/common/txb_common.h"
#include "av1/encoder/bitstream.h"
#include "aom_dsp/bitwriter.h"
static void row(int n,const aom_cdf_prob *cdf) {
  for(int i=0;i<n;i++)printf("%u ",32768u-cdf[i]);printf("%u ",(unsigned)cdf[n]);
}
static void pool(FRAME_CONTEXT *f) {
  for(int s=0;s<2;s++)for(int m=0;m<13;m++)row(7,f->intra_ext_tx_cdf[1][s][m]);
  for(int s=0;s<3;s++)for(int m=0;m<13;m++)row(5,f->intra_ext_tx_cdf[2][s][m]);
  for(int s=0;s<2;s++)row(16,f->inter_ext_tx_cdf[1][s]);
  row(12,f->inter_ext_tx_cdf[2][2]);
  for(int s=0;s<4;s++)row(2,f->inter_ext_tx_cdf[3][s]);puts("");
}
int main(int argc,char **argv) {
  FRAME_CONTEXT fc={0};av1_init_mode_probs(&fc);
  if(argc==1){pool(&fc);return 0;}
  if(argc!=8)return 1;
  int tx=atoi(argv[1]),inter=atoi(argv[2]),reduced=atoi(argv[3]),q=atoi(argv[4]);
  int mode=atoi(argv[5]),filter=atoi(argv[6]),disable=atoi(argv[7]);
  if(tx<0||tx>=TX_SIZES_ALL||mode<0||mode>=13||filter<-1||filter>4||(inter&&filter!=-1))return 2;
  AV1_COMMON cm={0};MACROBLOCKD xd={0};MB_MODE_INFO mi={0},*ptr=&mi;
  cm.fc=&fc;cm.quant_params.base_qindex=q;cm.features.reduced_tx_set_used=reduced;
  xd.tile_ctx=&fc;xd.mi=&ptr;mi.mode=mode;mi.ref_frame[0]=inter?LAST_FRAME:INTRA_FRAME;mi.ref_frame[1]=NONE_FRAME;
  if(filter>=0){mi.filter_intra_mode_info.use_filter_intra=1;mi.filter_intra_mode_info.filter_intra_mode=filter;}
  int set=get_ext_tx_set(tx,inter,reduced),square=txsize_sqr_map[tx],type_set=av1_get_ext_tx_set_type(tx,inter,reduced);
  int n=av1_num_ext_tx_set[type_set];printf("%d %d %d\n",set,square,n);
  unsigned char bytes[4096];aom_writer w={0};w.allow_update_cdf=!disable;aom_start_encode(&w,bytes);
  int expected[64];for(int i=0;i<64;i++) {
    int value=(i*7+mode)%16;
    while(!av1_ext_tx_used[type_set][value])value=(value+1)%16;
    if(!q||!set)value=DCT_DCT;expected[i]=value;
    av1_write_tx_type(&cm,&xd,value,tx,&w);
  }
  if(aom_stop_encode(&w)<0)return 3;
  printf("%u",w.pos);for(unsigned i=0;i<w.pos;i++)printf(" %u",bytes[i]);puts("");
  for(int i=0;i<64;i++)printf("%d ",expected[i]);puts("");
  for(int i=0;i<64;i++)printf("%d ",tx_type_to_class[expected[i]]);puts("");pool(&fc);
  // Chroma's native resolver covers every supplied mode/type, without entropy.
  uint8_t types[256]={0};xd.tx_type_map=types;xd.tx_type_map_stride=16;
  for(int lossless=0;lossless<2;lossless++) {
    xd.lossless[0]=lossless;
    for(int value=0;value<(inter?16:14);value++) {
      types[0]=value;mi.uv_mode=value;
      printf("%d ",av1_get_tx_type(&xd,PLANE_TYPE_UV,0,0,tx,reduced));
    }
  }
  puts("");for(int value=0;value<16;value++)printf("%d ",av1_ext_tx_used[type_set][value]);puts("");return 0;
}
'''


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True);p.add_argument('--aom',type=Path,required=True)
    a=p.parse_args();a.root=a.root.resolve();a.out=a.out.resolve();a.out.mkdir(parents=True,exist_ok=True)
    assert subprocess.check_output(['git','-C',str(a.aom),'rev-parse','HEAD'],text=True).strip()==PIN
    subprocess.run(['git','-C',str(a.aom),'diff-index','--quiet','HEAD','--'],check=True)
    assert digest(a.root/'build/scratch/av1/parsing.md')==PARSING_SHA
    library=Path(os.environ.get('AV1_AOM_ENCODER_LIBRARY',str(a.root/'build/native/aom/libaom.a')))
    source=a.out/'oracle.c';source.write_text(HOST);binary=a.out/'oracle'
    subprocess.run(shlex.split(os.environ.get('CC','cc'))+['-std=c11','-O2','-I'+str(a.aom),'-I'+str(library.parent),str(source),str(library),'-lm','-lpthread','-o',str(binary)],check=True)
    defaults=list(map(int,subprocess.check_output([str(binary)],text=True).split()));assert len(defaults)==501
    (a.out/'defaults.json').write_text(json.dumps(defaults)+'\n')
    cases=[]
    for tx,(w,h) in enumerate(SIZES):
        for inter in range(2):
            for reduced in range(2):
                for mode in range(13 if not inter else 1):
                    filters=[-1]+list(range(5)) if not inter and mode==0 else [-1]
                    for filter_mode in filters:
                        disable=(tx+mode+filter_mode+reduced)%5==0
                        args=[tx,inter,reduced,121,mode,filter_mode,int(disable)]
                        raw=subprocess.check_output([str(binary),*map(str,args)],text=True).splitlines()
                        set_id,square,n=map(int,raw[0].split());encoded=list(map(int,raw[1].split()));expected=list(map(int,raw[2].split()))
                        classes=list(map(int,raw[3].split()));final=list(map(int,raw[4].split()));chroma=list(map(int,raw[5].split()))
                        membership=list(map(int,raw[6].split()))
                        assert len(final)==501 and encoded[0]==len(encoded)-1 and len(expected)==64
                        changes=[i for i in range(501) if final[i]!=defaults[i]]
                        if disable:assert not changes
                        cases.append(dict(w=w,h=h,inter=bool(inter),reduced=bool(reduced),q=121,mode=mode,filter=filter_mode,
                            disable=disable,set=set_id,square=square,n=n,data=encoded[1:],expected=expected,changes=changes,
                            values=[final[i] for i in changes],chroma=chroma,classes=classes,membership=membership))
    imports=[f'#import, file "{os.path.relpath(a.root/path,a.out)}";' for path in (
        'src/transform/transform_type.zi','src/entropy/range.zi','tests/av1_transform_type.zi')]
    modules=[]
    for shard in range((len(cases)+47)//48):
        module=f'av1_transform_type_vectors_{shard}';modules.append(module);lines=imports[:]
        subset=cases[shard*48:shard*48+48]
        for local,c in enumerate(subset):
            changed=c['changes'];values=c['values'];inter=str(c['inter']).lower();reduced=str(c['reduced']).lower()
            lines += [f'Case{local} :: () -> s32 {{']+literal('bytes','u8',c['data'])+literal('expected','s32',c['expected'])
            lines += literal('classes','s32',c['classes'])+literal('membership','u8',c['membership'])
            lines += literal('changed','s32',changed or [0])+literal('values','u16',values or [0])+literal('chroma','s32',c['chroma'])+[
                '    state := NewTransformTypeCDFs(); saved: [501]u16',
                '    for i: 0..500 { saved[i] = state.values[i] }',
                f'    decoder := NewRange(BorrowBytes(bytes[:]), 0, {len(c["data"])}, {str(c["disable"]).lower()})',
                f'    selection := SelectTransformType({c["w"]}, {c["h"]}, {inter}, {reduced}, {c["mode"]})',
                f'    if selection.error.code != DecodeOK || selection.set != {c["set"]} {{ return 1 }}',
                '    for i: 0..63 {',
                f'        value := ReadTransformType(*decoder, *state, {c["w"]}, {c["h"]}, {inter}, {reduced}, {c["q"]}, {c["mode"]}, {c["filter"]})',
                '        if value.error.code != DecodeOK || value.value != expected[i] || value.tx_class != classes[i] { return 2 }','    }',
                f'    for value: 0..15 {{ if TransformTypeInSet(selection, {inter}, cast(s32)value) != (membership[value] != 0) {{ return 6 }} }}',
                '    if FinishRange(*decoder).code != DecodeOK { return 3 }','    at: s64','    for i: 0..500 {',
                '        value := saved[i]',f'        if at < {len(changed)} {{ if changed[at] == cast(s32)i {{ value = values[at]; at += 1 }} }}',
                '        if state.values[i] != value { return 4 }','    }',
                f'    for lossless: 0..1 {{ for mode: 0..{15 if c["inter"] else 13} {{',
                f'        value := ResolveChromaTransformType({c["w"]}, {c["h"]}, {inter}, {reduced}, lossless == 1, cast(s32)mode)',
                f'        if value.error.code != DecodeOK || value.value != chroma[lossless * {16 if c["inter"] else 14} + mode] {{ return 5 }}',
                '    } }','    return 0','}']
        lines += ['main :: () -> s32 {','    if TransformTypeBoundaries() != 0 { return 99 }']+[
            f'    if Case{i}() != 0 {{ return {i+1} }}' for i in range(len(subset))]+['    return 0','}']
        (a.out/f'{module}.zi').write_text('\n'.join(lines)+'\n')
    (a.out/'modules.txt').write_text('\n'.join(modules)+'\n')
    (a.out/'oracle.json').write_text(json.dumps(dict(revision=PIN,encoder_library_sha256=digest(library),native_host_sha256=digest(source),
        parsing_sha256=PARSING_SHA,defaults=501,cases=len(cases),symbols=len(cases)*64,compared_probability_cells=len(cases)*501,
        transform_sizes=SIZES,intra_modes=13,filter_intra_modes=5,updates_disabled=sum(c['disable'] for c in cases),
        defaults_sha256=digest(a.out/'defaults.json'),fixtures_sha256={m:digest(a.out/f'{m}.zi') for m in modules},
        scope='actual native transform-type syntax and chroma resolution; frame transform scheduling/reconstruction outside component coverage'),indent=2)+'\n')
    print(f'Pinned transform types:{len(cases)} native writer streams,501 default cells, all19 sizes/5 transform sets')


if __name__=='__main__':main()

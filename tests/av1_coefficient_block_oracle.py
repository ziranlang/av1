#!/usr/bin/env python3
"""Actual pinned av1_write_coeffs_txb streams, not invented test syntax."""
import argparse
import json
import os
from pathlib import Path
import random
import shlex
import subprocess

from av1_quantization_oracle import PIN,SIZES,digest,literal

HOST=r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "config/aom_dsp_rtcd.h"
#include "config/av1_rtcd.h"
#include "av1/common/av1_common_int.h"
#include "av1/common/entropy.h"
#include "av1/common/scan.h"
#include "av1/common/txb_common.h"
#include "av1/encoder/encodetxb.h"
#include "aom_dsp/bitwriter.h"
static void row(int n,const aom_cdf_prob *cdf) {
  for(int i=0;i<n;i++)printf("%u ",32768u-cdf[i]);printf("%u ",(unsigned)cdf[n]);
}
static void pool(FRAME_CONTEXT *f) {
  for(int s=0;s<5;s++)for(int c=0;c<13;c++)row(2,f->txb_skip_cdf[s][c]);
  for(int p=0;p<2;p++)for(int c=0;c<3;c++)row(2,f->dc_sign_cdf[p][c]);
  for(int s=0;s<5;s++)for(int p=0;p<2;p++)for(int c=0;c<42;c++)row(4,f->coeff_base_cdf[s][p][c]);
  for(int s=0;s<4;s++)for(int p=0;p<2;p++)for(int c=0;c<21;c++)row(4,f->coeff_br_cdf[s][p][c]);
  for(int s=0;s<5;s++)for(int p=0;p<2;p++)for(int c=0;c<4;c++)row(3,f->coeff_base_eob_cdf[s][p][c]);
  for(int s=0;s<5;s++)for(int p=0;p<2;p++)for(int c=0;c<9;c++)row(2,f->eob_extra_cdf[s][p][c]);
  for(int p=0;p<2;p++)for(int c=0;c<2;c++)row(5,f->eob_flag_cdf16[p][c]);
  for(int p=0;p<2;p++)for(int c=0;c<2;c++)row(6,f->eob_flag_cdf32[p][c]);
  for(int p=0;p<2;p++)for(int c=0;c<2;c++)row(7,f->eob_flag_cdf64[p][c]);
  for(int p=0;p<2;p++)for(int c=0;c<2;c++)row(8,f->eob_flag_cdf128[p][c]);
  for(int p=0;p<2;p++)for(int c=0;c<2;c++)row(9,f->eob_flag_cdf256[p][c]);
  for(int p=0;p<2;p++)for(int c=0;c<2;c++)row(10,f->eob_flag_cdf512[p][c]);
  for(int p=0;p<2;p++)for(int c=0;c<2;c++)row(11,f->eob_flag_cdf1024[p][c]);puts("");
}
int main(int argc,char **argv) {
  if(argc!=9)return 1;
  int tx=atoi(argv[1]),cls=atoi(argv[2]),q=atoi(argv[3]),p=atoi(argv[4]);
  int skip=atoi(argv[5]),dc=atoi(argv[6]),eob=atoi(argv[7]),disable=atoi(argv[8]);
  if(tx<0||tx>=TX_SIZES_ALL||cls<0||cls>2||q<0||q>255||p<0||p>1||skip<0||skip>12||dc<0||dc>2)return 2;
  int w=tx_size_wide[tx],h=tx_size_high[tx],cw=w<32?w:32,ch=h<32?h:32;
  if(eob<0||eob>cw*ch||(!p&&cls)||(cls&&(w>16||h>16)))return 3;
  AV1_COMMON *cm=calloc(1,sizeof(*cm));MACROBLOCK *x=calloc(1,sizeof(*x));
  FRAME_CONTEXT *fc=calloc(1,sizeof(*fc));MB_MODE_INFO mi={0},*mi_ptr=&mi;
  MB_MODE_INFO_EXT_FRAME ext={0};CB_COEFF_BUFFER buffer={0};
  tran_low_t coeff[1024]={0};uint16_t end=eob;uint8_t context=skip|(dc<<DC_SIGN_CTX_SHIFT);
  uint8_t types[256]={0};unsigned char output[65536];
  if(!cm||!x||!fc)return 4;
  cm->fc=fc;cm->quant_params.base_qindex=q;cm->seg.enabled=1;
  av1_default_coef_probs(cm);pool(fc);
  mi.bsize=BLOCK_64X64;mi.ref_frame[0]=LAST_FRAME;mi.ref_frame[1]=NONE_FRAME;
  x->e_mbd.mi=&mi_ptr;x->e_mbd.tile_ctx=fc;x->e_mbd.tx_type_map=types;x->e_mbd.tx_type_map_stride=16;
  types[0]=cls==1?H_DCT:cls==2?V_DCT:DCT_DCT;
  if(av1_get_tx_type(&x->e_mbd,p?PLANE_TYPE_UV:PLANE_TYPE_Y,0,0,tx,0)!=types[0])return 5;
  x->mbmi_ext_frame=&ext;x->cb_coef_buff=&buffer;
  buffer.tcoeff[p]=coeff;buffer.eobs[p]=&end;buffer.entropy_ctx[p]=&context;
  for(int r=0;r<ch;r++)for(int c=0;c<cw;c++)if(scanf("%d",&coeff[c*ch+r])!=1)return 6;
  const int16_t *scan=av1_scan_orders[tx][types[0]].scan;
  for(int i=eob;i<cw*ch;i++)if(coeff[scan[i]])return 7;
  if(eob&&!coeff[scan[eob-1]])return 8;
  aom_dsp_rtcd();av1_rtcd();aom_writer writer={0};writer.allow_update_cdf=!disable;
  aom_start_encode(&writer,output,sizeof output);av1_write_coeffs_txb(cm,x,&writer,0,0,p,0,tx);
  if(aom_stop_encode(&writer)<0)return 9;
  printf("%u",writer.pos);for(unsigned i=0;i<writer.pos;i++)printf(" %u",output[i]);puts("");
  pool(fc);free(fc);free(x);free(cm);return 0;
}
'''


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True);p.add_argument('--aom',type=Path,required=True)
    a=p.parse_args();a.root=a.root.resolve();a.out=a.out.resolve();a.out.mkdir(parents=True,exist_ok=True)
    assert subprocess.check_output(['git','-C',str(a.aom),'rev-parse','HEAD'],text=True).strip()==PIN
    subprocess.run(['git','-C',str(a.aom),'diff-index','--quiet','HEAD','--'],check=True)
    library=Path(os.environ.get('AV1_AOM_ENCODER_LIBRARY',str(a.root/'build/native/aom/libaom.a')))
    source=a.out/'oracle.c';source.write_text(HOST);binary=a.out/'oracle'
    subprocess.run(shlex.split(os.environ.get('CC','cc'))+['-std=c11','-O2','-I'+str(a.aom),'-I'+str(library.parent),str(source),str(library),'-lm','-lpthread','-o',str(binary)],check=True)
    # Native scan tables are transposed to the normative row-major layout.
    from av1_coefficient_scan_oracle import HOST as SCAN_HOST
    scan_source=a.out/'scan.c';scan_source.write_text(SCAN_HOST);scan_bin=a.out/'scan'
    subprocess.run(shlex.split(os.environ.get('CC','cc'))+['-std=c11','-O2','-I'+str(a.aom),'-I'+str(library.parent),str(scan_source),str(library),'-lm','-lpthread','-o',str(scan_bin)],check=True)
    configs=[(tx,0,0) for tx in range(19)]+[(tx,cls,1) for tx,(w,h) in enumerate(SIZES) for cls in range(3) if cls==0 or max(w,h)<=16]
    rng=random.Random(0x434F4546);cases=[]
    for config,(tx,cls,plane) in enumerate(configs):
        w,h=SIZES[tx];cw=min(w,32);ch=min(h,32);count=cw*ch
        mode=2 if cls==1 else 1 if cls==2 else 0
        scan_data=subprocess.check_output([str(scan_bin),str(tx),str(mode)],text=True).splitlines()
        scan=list(map(int,scan_data[0].split()))
        assert len(scan)==count and sorted(scan)==list(range(count))
        for variant in range(4):
            q=(0,21,61,121)[(config+variant)%4]
            eob=(0,1,rng.randrange(2,count),count)[variant]
            skip=(config+variant)%7 if not plane else 7+(config+variant)%6;dc=(config+variant)%3
            quant=[0]*count
            for i in range(eob):
                magnitude=rng.choice([0,0,1,2,3,4,14,15,16,127,255,1023,1048575,1048589])
                if i==eob-1 and magnitude==0:magnitude=1
                quant[scan[i]]=magnitude*(-1 if rng.randrange(2) else 1)
            disable=(config+variant)%5==0
            raw=subprocess.check_output([str(binary),*map(str,(tx,cls,q,plane,skip,dc,eob,int(disable)))],input=' '.join(map(str,quant))+'\n',text=True).splitlines()
            before=list(map(int,raw[0].split()));encoded=list(map(int,raw[1].split()));after=list(map(int,raw[2].split()))
            assert len(before)==len(after)==3835 and encoded[0]==len(encoded)-1
            changes=[i for i in range(3835) if before[i]!=after[i]]
            if disable:assert not changes
            expected=[(-1 if value<0 else 1)*(abs(value)&1048575) for value in quant]
            cases.append(dict(w=w,h=h,cls=cls,plane=plane,q=q,skip=skip,dc=dc,eob=eob,disable=disable,
                data=encoded[1:],expected=expected,changes=changes,values=[after[i] for i in changes],
                level=min(63,sum(abs(v) for v in expected)),dc_category=0 if not quant[0] else 1 if quant[0]<0 else 2))
    (a.out/'cases.json').write_text(json.dumps(cases,separators=(',',':'))+'\n')
    imports=[f'#import, file "{os.path.relpath(a.root/path,a.out)}";' for path in (
        'src/entropy/coefficient_block.zi','src/entropy/coefficient_cdfs.zi','src/entropy/range.zi','tests/av1_coefficient_block.zi')]
    modules=[]
    for shard in range((len(cases)+7)//8):
        module=f'av1_coefficient_block_vectors_{shard}';modules.append(module);lines=imports[:]
        subset=cases[shard*8:shard*8+8]
        for local,case in enumerate(subset):
            changes=case['changes'];values=case['values'];count=len(case['expected']);data=case['data']
            lines += [f'Case{local} :: () -> s32 {{']+literal('bytes','u8',data)+literal('expected','s32',case['expected'])
            lines += literal('changed','s32',changes or [0])+literal('values','u16',values or [0])+[
                f'    cdfs := NewCoefficientCDFs({case["q"]}); saved: [3835]u16',
                '    for i: 0..3834 { saved[i] = cdfs.values[i] }',
                f'    decoder := NewRange(BorrowBytes(bytes[:]), 0, {len(data)}, {str(case["disable"]).lower()})',
                f'    skip := ReadCoefficientSkip(*decoder, *cdfs, {case["w"]}, {case["h"]}, {case["plane"]}, {case["skip"]})',
                f'    if skip.error.code != DecodeOK || skip.value != {int(case["eob"]==0)} {{ return 1 }}',
                '    block: CoefficientBlock']
            if case['eob']:
                lines += [f'    block = ReadNonzeroCoefficients(*decoder, *cdfs, {case["w"]}, {case["h"]}, {case["cls"]}, {case["plane"]}, {case["dc"]})',
                    f'    if block.coded_width != {min(case["w"],32)} || block.coded_height != {min(case["h"],32)} {{ return 2 }}']
            lines += [f'    if block.error.code != DecodeOK || block.eob != {case["eob"]} || block.level != {case["level"]} || block.dc_category != {case["dc_category"]} {{ return 3 }}',
                '    for i: 0..1023 {',f'        value := ifx i < {count} then expected[i] else 0',
                '        if block.quant[i] != value { return 4 }','    }',
                '    if FinishRange(*decoder).code != DecodeOK { return 5 }',
                '    at: s64','    for i: 0..3834 {','        value := saved[i]',
                f'        if at < {len(changes)} {{ if changed[at] == cast(s32)i {{ value = values[at]; at += 1 }} }}',
                '        if cdfs.values[i] != value { return 6 }','    }','    return 0','}']
        lines += ['main :: () -> s32 {','    if CoefficientBlockBoundaries() != 0 { return 90 }']+[
            f'    if Case{i}() != 0 {{ return {i+1} }}' for i in range(len(subset))]+['    return 0','}']
        (a.out/f'{module}.zi').write_text('\n'.join(lines)+'\n')
    (a.out/'modules.txt').write_text('\n'.join(modules)+'\n')
    (a.out/'oracle.json').write_text(json.dumps(dict(revision=PIN,encoder_library_sha256=digest(library),native_host_sha256=digest(source),
        cases=len(cases),nonzero_blocks=sum(c['eob']!=0 for c in cases),compared_coefficient_cells=1024*len(cases),
        compared_probability_cells=3835*len(cases),updates_disabled=sum(c['disable'] for c in cases),
        transform_sizes=SIZES,classes=[0,1,2],planes=[0,1],quantizer_categories=4,
        cases_sha256=digest(a.out/'cases.json'),fixtures_sha256={m:digest(a.out/f'{m}.zi') for m in modules},
        scope='actual pinned native coefficient syntax streams; transform-type syntax and lossy frame reconstruction outside component coverage'),indent=2)+'\n')
    print(f'Pinned coefficient blocks:{len(cases)} actual native writer streams, all19 sizes/3 classes/4Q categories')


if __name__=='__main__':main()

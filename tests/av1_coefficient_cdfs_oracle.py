#!/usr/bin/env python3
"""Pinned native coefficient-CDF rows; product tables are never written here."""
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess

from av1_quantization_oracle import PIN,digest

HOST=r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "av1/common/av1_common_int.h"
#include "av1/common/entropy.h"
#include "av1/common/entropymode.h"
#include "aom_dsp/entenc.h"
#include "aom_dsp/prob.h"
static int wanted=-1,cursor,error;
static void row(int kind,int size,int plane,int context,int n,const aom_cdf_prob *cdf) {
  if(wanted>=0) {
    if(cursor++!=wanted)return;
    aom_cdf_prob active[17];memcpy(active,cdf,(n+1)*sizeof(*cdf));
    od_ec_enc encoder;od_ec_enc_init(&encoder,4096);unsigned symbols[64];
    for(int i=0;i<64;i++) {
      int symbol=(i*7+cursor)%n;
      for(int attempts=0;attempts<n;attempts++) {
        int high=symbol==0?32768:active[symbol-1];
        if(high>active[symbol])break;
        symbol=(symbol+1)%n;
      }
      symbols[i]=symbol;od_ec_encode_cdf_q15(&encoder,symbol,active,n);update_cdf(active,symbol,n);
    }
    uint32_t bytes;unsigned char *data=od_ec_enc_done(&encoder,&bytes);
    if(!data||!bytes||bytes>4096){error=3;od_ec_enc_clear(&encoder);return;}
    printf("%u",bytes);for(unsigned i=0;i<bytes;i++)printf(" %u",data[i]);puts("");
    for(int i=0;i<64;i++)printf("%u ",symbols[i]);puts("");
    for(int i=0;i<n;i++)printf("%u ",32768u-active[i]);printf("%u\n",(unsigned)active[n]);
    od_ec_enc_clear(&encoder);return;
  }
  printf("%d %d %d %d %d ",kind,size,plane,context,n);
  for(int i=0;i<n;i++)printf("%u ",32768u-cdf[i]);
  printf("%u\n",(unsigned)cdf[n]);
}
int main(int argc,char **argv) {
  if(argc!=2&&argc!=3)return 1;int q=atoi(argv[1]);if(q<0||q>255)return 2;
  if(argc==3){wanted=atoi(argv[2]);if(wanted<0||wanted>=817)return 2;}
  AV1_COMMON cm={0};FRAME_CONTEXT fc={0};cm.fc=&fc;cm.quant_params.base_qindex=q;
  av1_default_coef_probs(&cm);
  for(int s=0;s<5;s++)for(int c=0;c<13;c++)row(0,s,0,c,2,fc.txb_skip_cdf[s][c]);
  for(int p=0;p<2;p++)for(int c=0;c<3;c++)row(1,0,p,c,2,fc.dc_sign_cdf[p][c]);
  for(int s=0;s<5;s++)for(int p=0;p<2;p++)for(int c=0;c<42;c++)row(2,s,p,c,4,fc.coeff_base_cdf[s][p][c]);
  /* BR uses min(txSzCtx,TX_32X32); the unused TX64 rows are omitted. */
  for(int s=0;s<4;s++)for(int p=0;p<2;p++)for(int c=0;c<21;c++)row(3,s,p,c,4,fc.coeff_br_cdf[s][p][c]);
  for(int s=0;s<5;s++)for(int p=0;p<2;p++)for(int c=0;c<4;c++)row(4,s,p,c,3,fc.coeff_base_eob_cdf[s][p][c]);
  for(int s=0;s<5;s++)for(int p=0;p<2;p++)for(int c=0;c<9;c++)row(5,s,p,c,2,fc.eob_extra_cdf[s][p][c]);
  for(int p=0;p<2;p++)for(int c=0;c<2;c++)row(6,0,p,c,5,fc.eob_flag_cdf16[p][c]);
  for(int p=0;p<2;p++)for(int c=0;c<2;c++)row(6,1,p,c,6,fc.eob_flag_cdf32[p][c]);
  for(int p=0;p<2;p++)for(int c=0;c<2;c++)row(6,2,p,c,7,fc.eob_flag_cdf64[p][c]);
  for(int p=0;p<2;p++)for(int c=0;c<2;c++)row(6,3,p,c,8,fc.eob_flag_cdf128[p][c]);
  for(int p=0;p<2;p++)for(int c=0;c<2;c++)row(6,4,p,c,9,fc.eob_flag_cdf256[p][c]);
  for(int p=0;p<2;p++)for(int c=0;c<2;c++)row(6,5,p,c,10,fc.eob_flag_cdf512[p][c]);
  for(int p=0;p<2;p++)for(int c=0;c<2;c++)row(6,6,p,c,11,fc.eob_flag_cdf1024[p][c]);
  return error;
}
'''


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True);p.add_argument('--aom',type=Path,required=True)
    a=p.parse_args();a.root=a.root.resolve();a.out=a.out.resolve();a.out.mkdir(parents=True,exist_ok=True)
    assert subprocess.check_output(['git','-C',str(a.aom),'rev-parse','HEAD'],text=True).strip()==PIN
    subprocess.run(['git','-C',str(a.aom),'diff-index','--quiet','HEAD','--'],check=True)
    library=Path(os.environ.get('AV1_AOM_LIBRARY',str(a.root/'build/native/aom/libaom.a')))
    config=Path(os.environ.get('AV1_AOM_CONFIG',str(a.root/'build/native/aom')))
    host=a.out/'oracle.c';host.write_text(HOST);binary=a.out/'oracle'
    subprocess.run(shlex.split(os.environ.get('CC','cc'))+['-std=c11','-O2','-I'+str(a.aom),'-I'+str(config),str(host),str(a.aom/'aom_dsp/entenc.c'),str(library),'-lm','-lpthread','-o',str(binary)],check=True)
    profiles=[];native_rows=[]
    for q in (0,21,61,121):
        rows=[];pool=[]
        for line in subprocess.check_output([str(binary),str(q)],text=True).splitlines():
            data=list(map(int,line.split()));kind,size,plane,context,n=data[:5];values=data[5:]
            assert len(values)==n+1 and values[-1]==0 and values[-2]==32768
            assert values[:-1]==sorted(values[:-1]) and all(0<=v<=32768 for v in values[:-1])
            rows.append([kind,size,plane,context,n,values]);pool+=values
        assert len(rows)==817 and len(pool)==3835
        native_rows.append(rows);profiles.append(pool)
    # Quantizer-category boundary behavior is verified by the actual initializer.
    for q,category in ((20,0),(60,1),(120,2),(255,3)):
        pool=[]
        for line in subprocess.check_output([str(binary),str(q)],text=True).splitlines():pool+=list(map(int,line.split()))[5:]
        assert pool==profiles[category]
    (a.out/'profiles.json').write_text(json.dumps(dict(profiles=profiles,rows=native_rows),separators=(',',':'))+'\n')
    imports=[f'#import, file "{os.path.relpath(a.root/path,a.out)}";' for path in (
        'src/entropy/coefficient_cdfs.zi','src/entropy/coefficient_cdf_symbols.zi','src/entropy/range.zi','tests/av1_coefficient_cdfs.zi')]
    modules=[];stream_count=0
    for category,rows in enumerate(native_rows):
        module=f'av1_coefficient_cdf_vectors_{category}';modules.append(module);lines=imports[:]
        for group in range((len(rows)+31)//32):
            lines += [f'Rows{group} :: (state: *CoefficientCDFs) -> s32 {{']
            for kind,size,plane,context,n,values in rows[group*32:group*32+32]:
                lines += ['    {',f'        expected: [{n+1}]u16 = .['+','.join(map(str,values))+']',
                    f'        address := SelectCoefficientCDF(state, {kind}, {size}, {plane}, {context})',
                    f'        if address.error.code != DecodeOK || address.count != {n+1} {{ return 1 }}',
                    f'        for i: 0..{n} {{ if state.values[address.offset + i] != expected[i] {{ return 2 }} }}','    }']
            lines += ['    return 0','}']
        low=(0,21,61,121)[category];high=(20,60,120,255)[category]
        selected=[next(i for i,r in enumerate(rows) if r[:4]==meta) for meta in (
            [0,4,0,12],[1,0,1,2],[2,4,1,30],[3,3,1,20],[4,4,1,3],[5,4,1,8])]
        selected += [next(i for i,r in enumerate(rows) if r[:4]==[6,area,1,1]) for area in range(7)]
        for stream,row_index in enumerate(selected):
            kind,size,plane,context,n,_=rows[row_index]
            native=subprocess.check_output([str(binary),str(low),str(row_index)],text=True).splitlines()
            encoded=list(map(int,native[0].split()));assert encoded[0]==len(encoded)-1
            data=encoded[1:];symbols=list(map(int,native[1].split()));final=list(map(int,native[2].split()))
            assert len(symbols)==64 and len(final)==n+1 and final[-1]==32
            size=4 if kind==3 else size
            lines += [f'Stream{stream} :: () -> s32 {{',
                f'    input: [{len(data)}]u8 = .['+','.join(map(str,data))+']',
                '    expected: [64]s32 = .['+','.join(map(str,symbols))+']',
                f'    final: [{n+1}]u16 = .['+','.join(map(str,final))+']',
                f'    state := NewCoefficientCDFs({low}); saved: [3835]u16',
                '    for i: 0..3834 { saved[i] = state.values[i] }',
                f'    decoder := NewRange(BorrowBytes(input[:]), 0, {len(data)})',
                '    for i: 0..63 {',
                f'        symbol := ReadCoefficientSymbol(*decoder, *state, {kind}, {size}, {plane}, {context})',
                '        if symbol.error.code != DecodeOK || symbol.value != expected[i] { return 1 }','    }',
                '    if FinishRange(*decoder).code != DecodeOK { return 6 }',
                f'    address := SelectCoefficientCDF(*state, {kind}, {size}, {plane}, {context})',
                f'    if address.error.code != DecodeOK || address.count != {n+1} {{ return 2 }}',
                f'    fresh := NewCoefficientCDFs({low})','    for i: 0..3834 {',
                '        if fresh.values[i] != saved[i] { return 3 }',
                '        if i >= cast(s64)address.offset && i < cast(s64)(address.offset + address.count) {',
                '            if state.values[i] != final[i - cast(s64)address.offset] { return 4 }',
                '        } else { if state.values[i] != saved[i] { return 5 } }',
                '    }; return 0','}']
            stream_count+=1
        lines += ['main :: () -> s32 {','    if CDFBoundaries() != 0 { return 1 }',
            f'    quantizers: [2]s32 = .[{low},{high}]','    for i: 0..1 {',
            '        state := NewCoefficientCDFs(quantizers[i])','        if state.error.code != DecodeOK { return 2 }']+[
            f'        if Rows{group}(*state) != 0 {{ return {group+3} }}' for group in range((len(rows)+31)//32)]+[
            '    }']+[f'    if Stream{stream}() != 0 {{ return {stream+40} }}' for stream in range(len(selected))]+[
            '    return 0','}']
        (a.out/f'{module}.zi').write_text('\n'.join(lines)+'\n')
    (a.out/'modules.txt').write_text('\n'.join(modules)+'\n')
    data_modules=[]
    for category,values in enumerate(profiles):
        module=f'av1_coefficient_cdf_data_vectors_{category}';data_modules.append(module)
        imports=[f'#import, file "{os.path.relpath(a.root/path,a.out)}";' for path in (
            'src/entropy/coefficient_cdfs.zi','tests/av1_coefficient_cdf_data.zi')]
        lines=imports[:]
        for group in range((len(values)+511)//512):
            part=values[group*512:group*512+512]
            lines += [f'Data{group} :: (state: *CoefficientCDFs) -> s32 {{',
                f'    expected: [{len(part)}]u16 = .[']+[
                '        '+','.join(map(str,part[i:i+16]))+',' for i in range(0,len(part),16)]+[
                '    ]',f'    for i: 0..{len(part)-1} {{ if state.values[{group*512} + i] != expected[i] {{ return 1 }} }}',
                '    return 0','}']
        low=(0,21,61,121)[category];high=(20,60,120,255)[category]
        lines += ['main :: () -> s32 {','    if CDFDataBoundaries() != 0 { return 1 }',
            f'    quantizers: [2]s32 = .[{low},{high}]','    for i: 0..1 {',
            '        state := NewCoefficientCDFs(quantizers[i])','        if state.error.code != DecodeOK { return 2 }']+[
            f'        if Data{group}(*state) != 0 {{ return {group+3} }}' for group in range((len(values)+511)//512)]+[
            '    }; return 0','}']
        (a.out/f'{module}.zi').write_text('\n'.join(lines)+'\n')
    (a.out/'data-modules.txt').write_text('\n'.join(data_modules)+'\n')
    (a.out/'oracle.json').write_text(json.dumps(dict(revision=PIN,library_sha256=digest(library),native_host_sha256=digest(host),
        profiles=4,rows_per_profile=817,cells_per_profile=3835,quantizer_boundaries=[0,20,21,60,61,120,121,255],
        profiles_sha256=digest(a.out/'profiles.json'),valid_selector_cases=817*8,compared_probability_cells=3835*8,
        native_symbol_streams=stream_count,decoded_symbols=stream_count*64,
        fixtures_sha256={module:digest(a.out/f'{module}.zi') for module in modules},
        scope='native probability-data preparation; product pass is recorded separately after all target forms and ownership tests'),indent=2)+'\n')
    print('Pinned CDF preparation:4 categories,817 rows/3835 cells each; eight quantizer boundaries agree; product tests pending')


if __name__=='__main__':main()

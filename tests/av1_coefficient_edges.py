#!/usr/bin/env python3
"""Encode genuine AV1 lossless coefficient boundary syntax with the pinned
upstream arithmetic encoder and compare whole-frame decodes with libaom.
These are specification boundary tests, not a general conformance corpus.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess

HOST = r'''
#include <stdio.h>
#include <stdint.h>
#include "aom_dsp/entenc.h"
#include "aom_dsp/prob.h"
int main(void) {
 unsigned count;
 while(scanf("%u",&count)==1) {
   if(count>32)return 1;uint16_t cdf[32][17];unsigned n[32];
   for(unsigned i=0;i<count;i++) {
     if(scanf("%u",n+i)!=1||n[i]<2||n[i]>16)return 2;
     for(unsigned j=0;j<n[i];j++) {unsigned x;if(scanf("%u",&x)!=1||x>32768)return 3;cdf[i][j]=32768-x;}
     cdf[i][n[i]]=0;
   }
   unsigned operations;if(scanf("%u",&operations)!=1||operations>4096)return 4;
   od_ec_enc encoder;od_ec_enc_init(&encoder,4096);
   for(unsigned i=0;i<operations;i++) {
     unsigned table,symbol,adapt;
     if(scanf("%u%u%u",&table,&symbol,&adapt)!=3||table>=count||symbol>=n[table])return 5;
     od_ec_encode_cdf_q15(&encoder,symbol,cdf[table],n[table]);
     if(adapt)update_cdf(cdf[table],symbol,n[table]);
   }
   uint32_t size;unsigned char *data=od_ec_enc_done(&encoder,&size);
   if(!data||!size||size>4096)return 6;
   printf("%u",size);for(unsigned i=0;i<size;i++)printf(" %u",data[i]);printf("\n");
   od_ec_enc_clear(&encoder);
 }
 return 0;
}
'''


def run(command, **kwargs):return subprocess.run(command,check=True,**kwargs)


def inverse_wht4(values,shift):
    # AV1 7.13.2.10 inverse Walsh-Hadamard butterfly.
    a=values[0]>>shift;c=values[1]>>shift;d=values[2]>>shift;b=values[3]>>shift
    a+=c;d-=b;e=(a-d)>>1;b=e-b;c=e-c;a-=b;d+=c
    return [a,b,c,d]


def lossless_dc_conforms(level,sign,depth=8):
    """Whether a DC-only lossless 4x4 residual meets the specification's
    1+BitDepth-bit Residual range; libaom clips nonconforming values instead."""
    value=(level&1048575)*4
    if sign:value=-value
    limit=1<<(7+depth);value=max(-limit,min(limit-1,value))
    rows=[inverse_wht4(row,2) for row in [[value,0,0,0]]+[[0]*4]*3]
    columns=[inverse_wht4([rows[r][c] for r in range(4)],0) for c in range(4)]
    return all(-(1<<depth)<=v<(1<<depth) for column in columns for v in column)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--aom',type=Path,required=True)
    args=parser.parse_args()
    pin='d772e334cc724105040382a977ebb10dfd393293'
    if run(['git','-C',str(args.aom),'rev-parse','HEAD'],text=True,capture_output=True).stdout.strip()!=pin:
        raise SystemExit('libaom pin mismatch')
    run(['git','-C',str(args.aom),'diff','--quiet','HEAD','--'])
    args.out.mkdir(parents=True,exist_ok=True)
    source=args.out/'edge_encoder.c';source.write_text(HOST)
    host=args.out/'edge_encoder'
    config=Path(os.environ.get('AV1_AOM_CONFIG',str(args.root/'build/native/aom')))
    library=Path(os.environ.get('AV1_AOM_LIBRARY',str(args.root/'build/native/aom/libaom.a')))
    run(shlex.split(os.environ.get('CC','cc'))+['-std=c11','-O2','-I'+str(args.aom),'-I'+str(config),
        str(source),str(args.aom/'aom_dsp/entenc.c'),str(library),'-lm','-o',str(host)])
    # Encode with the pinned upstream default probabilities. The product CDF
    # constants are checked against the same source by their own stages.
    upstream=(args.aom/'av1/common/token_cdfs.h').read_text()
    definitions=[('skip',7,2,'av1_default_txb_skip_cdfs'),('dc',3,2,'av1_default_dc_sign_cdfs'),
        ('eob',1,5,'av1_default_eob_multi16_cdfs'),('base_eob',4,3,'av1_default_coeff_base_eob_multi_cdfs'),
        ('br',21,4,'av1_default_coeff_lps_multi_cdfs')]
    tables={}
    for field,count,size,name in definitions:
        text=upstream.split(name,1)[1].split('= {',1)[1]
        rows=[]
        for match in re.finditer(r'AOM_CDF'+str(size)+r'\(([^)]+)\)',text):
            rows.append([eval(value.strip(),{'__builtins__':{}},{}) for value in match[1].split(',')]+[32768])
            if len(rows)==count:break
        if len(rows)!=count:raise SystemExit('missing upstream constants: '+field)
        tables[field]=rows
    selected=[tables['skip'][1],tables['eob'][0],tables['base_eob'][0],tables['br'][0],tables['dc'][0],
        [16384,32768],[19132,25510,30392,32768],[31671,32768],
        [15588,17027,19338,20218,20682,21110,21825,23244,24189,28165,29093,30466,32768],
        tables['skip'][3]]
    def encode(operations):
        text=str(len(selected))+'\n'+''.join(str(len(row))+' '+' '.join(map(str,row))+'\n' for row in selected)
        text+=str(len(operations))+'\n'+''.join(f'{table} {symbol} {adapt}\n' for table,symbol,adapt in operations)
        output=list(map(int,run([str(host)],input=text,text=True,capture_output=True).stdout.split()))
        if output[0]!=len(output)-1:raise SystemExit('encoder output mismatch')
        return bytes(output[1:])
    def operations(level,sign,invalid=False):
        # all_zero=0, EOB=1, coeff_base_eob=3, four BR extensions=3,
        # DC sign, then the normative Golomb suffix for level >= 15.
        result=[(0,0,1),(1,0,1),(2,2,1)]+[(3,3,1)]*4+[(4,sign,1)]
        binary=bin(level-14)[2:]
        bits=('0'*20+'1') if invalid else '0'*(len(binary)-1)+binary
        return result+[(5,int(bit),0) for bit in bits]
    # Whole lossless 8x8 monochrome stills: one DC coefficient at each Golomb
    # boundary, then three all-zero blocks. Every stream is decoded by the
    # pinned native libaom host; the owned decoder must match its plane, or
    # reject where libaom rejects.
    prefix=bytes([18,0,10,5,24,8,191,33,80,50])
    cases=[]
    streams=[(level,sign,False) for level in (15,16,31,255,65535,524287,1048575,1048576,1048589) for sign in (0,1)]
    streams.append((15,0,True))
    for level,sign,invalid in streams:
        # The three remaining 4x4 blocks are all-zero. libaom's luma skip
        # context is skip_contexts[min(above,4)][min(left,4)] over masked
        # cumulative levels: right and lower neighbors of a nonzero first block
        # use context 3, the last block context 1. A wrapped level masks to 0.
        rest=[(0,1,1)]*3 if level&1048575==0 else [(9,1,1),(9,1,1),(0,1,1)]
        encoded=encode([(6,0,1),(7,0,1),(8,0,1)]+operations(level,sign,invalid)+rest)
        if len(encoded)+2>=128:raise SystemExit('fixture needs wider length')
        # The verified reduced-still frame header is TWO bytes (16,0).
        name=f'golomb-{level}-{sign}'+('-invalid' if invalid else '')+'.obu'
        obu=args.out/name;obu.write_bytes(prefix+bytes([len(encoded)+2,16,0])+encoded)
        native=subprocess.run([str(args.out/'oracle'),str(obu)],text=True,capture_output=True)
        plane=list(map(int,native.stdout.splitlines()[0].split()))[1:] if native.returncode==0 else None
        if invalid and plane is not None:raise SystemExit('native decoder accepted an invalid Golomb length')
        if not invalid and plane is None:raise SystemExit('native decoder rejected '+name+': '+native.stderr.strip()[:300])
        if level==1048576 and plane!=[128]*64:raise SystemExit('native wrapping plane mismatch')
        conforming=not invalid and lossless_dc_conforms(level,sign)
        cases.append(dict(name=name,level=level,sign=sign,invalid=invalid,native_ok=plane is not None,
            conforming=conforming,data=list(obu.read_bytes()),plane=plane))
    lines=['#import, file "'+os.path.relpath(args.root/'src/decoder.zi',args.out)+'";',
        'using Codec :: #import "image_codec/api"','#program_export','main :: () -> s32 {']
    for index,case in enumerate(cases):
        data=case['data']
        lines+=['    {','        // '+case['name'],'        bytes: ['+str(len(data))+']u8 = .['+','.join(map(str,data))+']',
            '        options := DefaultOptions()',
            '        budget := NewWorkerBudget(options.limits); options.workers = *budget',
            '        image := DecodeAv1YUV(BorrowBytes(bytes[:]), *options)']
        if not case['conforming']:
            # Invalid Golomb lengths and out-of-range residuals are
            # nonconforming; the owned decoder rejects without planes.
            lines+=[f'        if image.is_ok || image.error.code != MalformedInput || image.y.storage.count != 0 {{ return {100+index} }}']
        else:
            lines+=[f'        if !image.is_ok || image.y.storage.count != 64 {{ return {200+index} }}',
                '        expected: [64]u8 = .['+','.join(map(str,case['plane']))+']',
                f'        for i: 0..63 {{ if image.y.storage[i] != expected[i] {{ return {300+index} }} }}']
        lines+=[f'        if budget.active_workers != 0 || budget.working_bytes != 0 {{ return {400+index} }}','    }']
    lines+=['    return 0','}']
    (args.out/'av1_coefficient_edges.zi').write_text('\n'.join(lines)+'\n')
    matched=sum(case['conforming'] for case in cases)
    (args.out/'edge_oracle.json').write_text(json.dumps(dict(revision=pin,whole_frames=len(cases),native_plane_matches=matched,
        nonconforming_rejections=len(cases)-matched,
        cases=[dict(name=c['name'],native_ok=c['native_ok'],conforming=c['conforming']) for c in cases],
        scope='coefficient Golomb boundary whole frames against native libaom; full conformance pending'),indent=2)+'\n')
    print(f'Coefficient boundaries: {len(cases)} whole lossless frames, {matched} native plane matches, {len(cases)-matched} nonconforming rejections')


if __name__=='__main__':main()

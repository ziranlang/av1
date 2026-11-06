#!/usr/bin/env python3
"""Lossy inverse-transform corpus: pinned libaom 2D inverse kernels, AV1 7.13.

Coefficients come from libaom's forward transform of bounded residual patterns,
then a dequantization-like rounding to multiples of a step, clamped to the
(BitDepth+8)-bit Dequant range. Every vector is replayed by an independent
Python transcription of the specification with its conformance range checks;
only conforming vectors become libaom parity cases. A separate small class of
nonconforming extreme inputs checks deterministic s64 semantics on every target
against the Python reference alone. These are kernel vectors, not frame
conformance.
"""
import argparse
import json
import os
from pathlib import Path
import random
import shlex
import subprocess

from av1_quantization_oracle import PIN,SIZES,digest,literal

HOST = r'''
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
typedef void (*Inverse)(const int32_t *,uint16_t *,int,int,int);
typedef void (*Forward)(const int16_t *,int32_t *,int,int,int);
#define PAIR(w,h) void av1_inv_txfm2d_add_##w##x##h##_c(const int32_t *,uint16_t *,int,int,int); \
  void av1_fwd_txfm2d_##w##x##h##_c(const int16_t *,int32_t *,int,int,int);
PAIR(4,4) PAIR(8,8) PAIR(16,16) PAIR(32,32) PAIR(64,64) PAIR(4,8) PAIR(8,4) PAIR(8,16) PAIR(16,8)
PAIR(16,32) PAIR(32,16) PAIR(32,64) PAIR(64,32) PAIR(4,16) PAIR(16,4) PAIR(8,32) PAIR(32,8) PAIR(16,64) PAIR(64,16)
#define ENTRY(w,h) {w,h,av1_inv_txfm2d_add_##w##x##h##_c,av1_fwd_txfm2d_##w##x##h##_c}
static const struct { int w,h; Inverse inverse; Forward forward; } sizes[19]={
  ENTRY(4,4),ENTRY(8,8),ENTRY(16,16),ENTRY(32,32),ENTRY(64,64),ENTRY(4,8),ENTRY(8,4),ENTRY(8,16),ENTRY(16,8),
  ENTRY(16,32),ENTRY(32,16),ENTRY(32,64),ENTRY(64,32),ENTRY(4,16),ENTRY(16,4),ENTRY(8,32),ENTRY(32,8),ENTRY(16,64),ENTRY(64,16)};
static int next(long *value){return scanf("%ld",value)==1;}
void av1_rtcd(void);void aom_dsp_rtcd(void);
int main(void) {
  av1_rtcd();aom_dsp_rtcd();
  static int16_t residual[4096]; static int32_t coefficients[4096]; static uint16_t pixels[4096];
  long mode;
  while(next(&mode)) {
    long tx,type,bd,v; if(!next(&tx)||!next(&type)||!next(&bd)||tx<0||tx>18)return 2;
    int w=sizes[tx].w,h=sizes[tx].h,cw=w<32?w:32,ch=h<32?h:32;
    if(mode==0) {
      for(int i=0;i<w*h;i++){if(!next(&v))return 3;residual[i]=(int16_t)v;}
      memset(coefficients,0,sizeof(coefficients));
      sizes[tx].forward(residual,coefficients,w,(int)type,(int)bd);
      for(int i=0;i<cw*ch;i++)printf("%d ",coefficients[i]);
    } else {
      memset(coefficients,0,sizeof(coefficients));
      for(int i=0;i<cw*ch;i++){if(!next(&v))return 4;coefficients[i]=(int32_t)v;}
      for(int i=0;i<w*h;i++){if(!next(&v))return 5;pixels[i]=(uint16_t)v;}
      sizes[tx].inverse(coefficients,pixels,w,(int)type,(int)bd);
      for(int i=0;i<w*h;i++)printf("%u ",(unsigned)pixels[i]);
    }
    puts("");fflush(stdout);
  }
  return 0;
}
'''

ROW_KIND=[0,0,1,1,0,1,1,1,1,2,2,0,2,1,2,1]
COLUMN_KIND=[0,1,0,1,1,0,1,1,1,2,0,2,1,2,1,2]
FLIP_ROWS={4,6,8,14}
FLIP_COLUMNS={5,6,7,15}
ROW_SHIFT={(4,4):0,(8,8):1,(16,16):2,(32,32):2,(64,64):2,(4,8):0,(8,4):0,(8,16):1,(16,8):1,(16,32):1,
           (32,16):1,(32,64):1,(64,32):1,(4,16):1,(16,4):1,(8,32):2,(32,8):2,(16,64):2,(64,16):2}
COS128=[4096,4095,4091,4085,4076,4065,4052,4036,4017,3996,3973,3948,3920,3889,3857,3822,
        3784,3745,3703,3659,3612,3564,3513,3461,3406,3349,3290,3229,3166,3102,3035,2967,
        2896,2824,2751,2675,2598,2520,2440,2359,2276,2191,2106,2019,1931,1842,1751,1660,
        1567,1474,1380,1285,1189,1092,995,897,799,700,601,501,401,301,201,101,0]


class Nonconforming(Exception):
    pass


def round2(x,n):return x if n==0 else (x+(1<<(n-1)))>>n


def fits(x,bits):return -(1<<(bits-1))<=x<(1<<(bits-1))


def cos128(angle):
    a=angle&255
    if a<=64:return COS128[a]
    if a<=128:return -COS128[128-a]
    if a<=192:return -COS128[a-128]
    return COS128[256-a]


def brev(bits,x):return sum(((x>>i)&1)<<(bits-1-i) for i in range(bits))


class Spec:
    """Literal 7.13.2 transcription; strict raises on a conformance violation."""
    def __init__(self,strict):self.strict=strict
    def check(self,x,bits):
        if self.strict and not fits(x,bits):raise Nonconforming()
    def B(self,t,a,b,angle,flip,r):
        x=round2(t[a]*cos128(angle)-t[b]*cos128(angle-64),12)
        y=round2(t[a]*cos128(angle-64)+t[b]*cos128(angle),12)
        self.check(x,r);self.check(y,r)
        t[a],t[b]=(y,x) if flip else (x,y)
    def H(self,t,a,b,flip,r):
        if flip:a,b=b,a
        x,y=t[a],t[b];lo=-(1<<(r-1));hi=(1<<(r-1))-1
        t[a]=max(lo,min(hi,x+y));t[b]=max(lo,min(hi,x-y))
    def dct(self,t,n,r):
        c=t[:];t[:]=[c[brev(n,i)] for i in range(1<<n)];B=self.B;H=self.H
        if n==6:
            for i in range(16):B(t,32+i,63-i,63-4*brev(4,i),0,r)
        if n>=5:
            for i in range(8):B(t,16+i,31-i,6+(brev(3,7-i)<<3),0,r)
        if n==6:
            for i in range(16):H(t,32+i*2,33+i*2,i&1,r)
        if n>=4:
            for i in range(4):B(t,8+i,15-i,12+(brev(2,3-i)<<4),0,r)
        if n>=5:
            for i in range(8):H(t,16+2*i,17+2*i,i&1,r)
        if n==6:
            for i in range(4):
                for j in range(2):B(t,62-i*4-j,33+i*4+j,60-16*brev(2,i)+64*j,1,r)
        if n>=3:
            for i in range(2):B(t,4+i,7-i,56-32*i,0,r)
        if n>=4:
            for i in range(4):H(t,8+2*i,9+2*i,i&1,r)
        if n>=5:
            for i in range(2):
                for j in range(2):B(t,30-4*i-j,17+4*i+j,24+(j<<6)+((1-i)<<5),1,r)
        if n==6:
            for i in range(8):
                for j in range(2):H(t,32+i*4+j,35+i*4-j,i&1,r)
        for i in range(2):B(t,2*i,2*i+1,32+16*i,1-i,r)
        if n>=3:
            for i in range(2):H(t,4+2*i,5+2*i,i,r)
        if n>=4:
            for i in range(2):B(t,14-i,9+i,48+64*i,1,r)
        if n>=5:
            for i in range(4):
                for j in range(2):H(t,16+4*i+j,19+4*i-j,i&1,r)
        if n==6:
            for i in range(2):
                for j in range(4):B(t,61-i*8-j,34+i*8+j,56-i*32+(j>>1)*64,1,r)
        for i in range(2):H(t,i,3-i,0,r)
        if n>=3:B(t,6,5,32,1,r)
        if n>=4:
            for i in range(2):
                for j in range(2):H(t,8+4*i+j,11+4*i-j,i,r)
        if n>=5:
            for i in range(4):B(t,29-i,18+i,48+(i>>1)*64,1,r)
        if n==6:
            for i in range(4):
                for j in range(4):H(t,32+8*i+j,39+8*i-j,i&1,r)
        if n>=3:
            for i in range(4):H(t,i,7-i,0,r)
        if n>=4:
            for i in range(2):B(t,13-i,10+i,32,1,r)
        if n>=5:
            for i in range(2):
                for j in range(4):H(t,16+i*8+j,23+i*8-j,i,r)
        if n==6:
            for i in range(8):B(t,59-i,36+i,48 if i<4 else 112,1,r)
        if n>=4:
            for i in range(8):H(t,i,15-i,0,r)
        if n>=5:
            for i in range(4):B(t,27-i,20+i,32,1,r)
        if n==6:
            for i in range(8):H(t,32+i,47-i,0,r);H(t,48+i,63-i,1,r)
        if n>=5:
            for i in range(16):H(t,i,31-i,0,r)
        if n==6:
            for i in range(8):B(t,55-i,40+i,32,1,r)
        if n==6:
            for i in range(32):H(t,i,63-i,0,r)
    def adst4(self,t,r):
        s=[1321*t[0],2482*t[0],3344*t[1],3803*t[2],1321*t[2],2482*t[3],3803*t[3]]
        a7=t[0]-t[2];b7=a7+t[3];self.check(a7,r+1);self.check(b7,r)
        s[0]+=s[3];s[1]-=s[4];s[3]=s[2];s[2]=3344*b7;s[0]+=s[5];s[1]-=s[6]
        x=[s[0]+s[3],s[1]+s[3],s[2],s[0]+s[1]];x[3]-=s[3]
        for v in s+x:self.check(v,r+12)
        t[:]=[round2(v,12) for v in x]
    def adst_in(self,t,n):
        c=t[:];n0=1<<n;t[:]=[c[i-1 if i&1 else n0-i-1] for i in range(n0)]
    def adst_out(self,t,n):
        c=t[:];out=[]
        for i in range(1<<n):
            a=(i>>3)&1;b=((i>>2)&1)^((i>>3)&1);cc=((i>>1)&1)^((i>>2)&1);d=(i&1)^((i>>1)&1)
            idx=((d<<3)|(cc<<2)|(b<<1)|a)>>(4-n);out.append(-c[idx] if i&1 else c[idx])
        t[:]=out
    def adst8(self,t,r):
        B=self.B;H=self.H;self.adst_in(t,3)
        for i in range(4):B(t,2*i,2*i+1,60-16*i,1,r)
        for i in range(4):H(t,i,4+i,0,r)
        for i in range(2):B(t,4+3*i,5+i,48-32*i,1,r)
        for i in range(2):
            for j in range(2):H(t,4*j+i,2+4*j+i,0,r)
        for i in range(2):B(t,2+4*i,3+4*i,32,1,r)
        self.adst_out(t,3)
    def adst16(self,t,r):
        B=self.B;H=self.H;self.adst_in(t,4)
        for i in range(8):B(t,2*i,2*i+1,62-8*i,1,r)
        for i in range(8):H(t,i,8+i,0,r)
        for i in range(2):B(t,8+2*i,9+2*i,56-32*i,1,r);B(t,13+2*i,12+2*i,8+32*i,1,r)
        for i in range(4):
            for j in range(2):H(t,8*j+i,4+8*j+i,0,r)
        for i in range(2):
            for j in range(2):B(t,4+8*j+3*i,5+8*j+i,48-32*i,1,r)
        for i in range(2):
            for j in range(4):H(t,4*j+i,2+4*j+i,0,r)
        for i in range(4):B(t,2+4*i,3+4*i,32,1,r)
        self.adst_out(t,4)
    def identity(self,t,n):
        t[:]=[round2(v*5793,12) if n==2 else v*2 if n==3 else round2(v*11586,12) if n==4 else v*4 for v in t]
    def one(self,t,kind,n,r):
        if kind==0:self.dct(t,n,r)
        elif kind==1:(self.adst4(t,r) if n==2 else self.adst8(t,r) if n==3 else self.adst16(t,r))
        else:self.identity(t,n)
    def inverse(self,dequant,w,h,tx_type,bd):
        log2w=w.bit_length()-1;log2h=h.bit_length()-1;rect=abs(log2w-log2h)==1
        rr=bd+8;cr=max(bd+6,16);lo=-(1<<(cr-1));hi=(1<<(cr-1))-1;shift=ROW_SHIFT[(w,h)]
        res=[[0]*w for _ in range(h)]
        for i in range(h):
            t=[dequant[i*w+j] if i<32 and j<32 else 0 for j in range(w)]
            if rect:t=[round2(v*2896,12) for v in t]
            self.one(t,ROW_KIND[tx_type],log2w,rr)
            res[i]=[max(lo,min(hi,round2(v,shift))) for v in t]
        for j in range(w):
            t=[res[i][j] for i in range(h)];self.one(t,COLUMN_KIND[tx_type],log2h,cr)
            for i in range(h):res[i][j]=round2(t[i],4)
        return [v for row in res for v in row]


def legal_types(w,h):
    if w==64 or h==64:return [0]
    ok=lambda kind,side:kind==0 or (kind==1 and side<=16) or (kind==2 and side<=32)
    return [t for t in range(16) if ok(ROW_KIND[t],w) and ok(COLUMN_KIND[t],h)]


def prediction(w,h,bd,seed):
    base=1<<(bd-1);scale=1<<(bd-8)
    return [base+(((i*7+j*13+seed)%64)-32)*scale for i in range(h) for j in range(w)]


def pattern(rng,w,h,bd,kind):
    peak=(1<<bd)-1
    if kind==0:return [rng.randint(-peak,peak) for _ in range(w*h)]
    if kind==1:
        gx=rng.uniform(-1,1);gy=rng.uniform(-1,1)
        return [max(-peak,min(peak,int(peak*0.5*(gx*j/w+gy*i/h)))) for i in range(h) for j in range(w)]
    if kind==2:
        edge=rng.randrange(1,w);hi=rng.randint(-peak,peak);lo=rng.randint(-peak,peak)
        return [hi if j<edge else lo for i in range(h) for j in range(w)]
    if kind==3:return [peak if (i+j)%2 else -peak for i in range(h) for j in range(w)]
    return [rng.choice((peak,-peak)) for _ in range(w*h)]


def quantize(rng,coefficients,bd,cw,ch):
    """Round to a dequantizer step; optionally keep only a low-frequency triangle."""
    step=rng.choice((1,2,5,17,64,301,1024))*(1<<(bd-8))
    cutoff=rng.choice((0,0,1,2,max(2,(cw+ch)//4)))
    limit=1<<(bd+7);out=[]
    for at,value in enumerate(coefficients):
        row,column=divmod(at,cw)
        if cutoff and row+column>=cutoff:out.append(0);continue
        q=(abs(value)+step//2)//step*step
        out.append(max(-limit,min(limit-1,-q if value<0 else q)))
    return out


class Host:
    def __init__(self,binary):
        self.process=subprocess.Popen([str(binary)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True,bufsize=1)
    def ask(self,values):
        self.process.stdin.write(' '.join(map(str,values))+'\n');self.process.stdin.flush()
        return list(map(int,self.process.stdout.readline().split()))
    def close(self):
        self.process.stdin.close()
        if self.process.wait()!=0:raise SystemExit('Transform oracle host failed')


def column_major(values,cw,ch):return [values[r*cw+c] for c in range(cw) for r in range(ch)]
def row_major(values,cw,ch):return [values[c*ch+r] for r in range(ch) for c in range(cw)]


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True);p.add_argument('--aom',type=Path,required=True)
    a=p.parse_args();a.root=a.root.resolve();a.out=a.out.resolve();a.out.mkdir(parents=True,exist_ok=True)
    assert subprocess.check_output(['git','-C',str(a.aom),'rev-parse','HEAD'],text=True).strip()==PIN
    subprocess.run(['git','-C',str(a.aom),'diff-index','--quiet','HEAD','--'],check=True)
    # Forward kernels need the pinned encoder-enabled build used by the coefficient-writer oracles.
    library=Path(os.environ.get('AV1_AOM_ENCODER_LIBRARY',str(a.root/'build/native/aom/libaom.a')))
    config=library.parent
    host=a.out/'oracle.c';host.write_text(HOST);binary=a.out/'oracle'
    subprocess.run(shlex.split(os.environ.get('CC','cc'))+['-std=c11','-O2','-I'+str(a.aom),'-I'+str(config),
        str(host),str(library),'-lm','-lpthread','-o',str(binary)],check=True)
    native=Host(binary);strict=Spec(True);loose=Spec(False)
    rng=random.Random(0x41563174);cases=[];dropped=0;saturated=0;types_seen=set()
    for depth in (8,10,12):
        for tx,(w,h) in enumerate(SIZES):
            cw=min(w,32);ch=min(h,32)
            for tx_type in legal_types(w,h):
                variants=2 if w*h<=64 else 1
                for variant in range(variants):
                    kind=(len(cases)+variant)%5
                    residual=pattern(rng,w,h,depth,kind)
                    forward=native.ask([0,tx,tx_type,depth]+residual)
                    assert len(forward)==cw*ch
                    compact=quantize(rng,row_major(forward,cw,ch),depth,cw,ch)
                    dequant=[compact[i*cw+j] if i<ch and j<cw else 0 for i in range(h) for j in range(w)]
                    try:spec=strict.inverse(dequant,w,h,tx_type,depth)
                    except Nonconforming:dropped+=1;continue
                    seed=len(cases)%61
                    pred=prediction(w,h,depth,seed)
                    pixels=native.ask([1,tx,tx_type,depth]+column_major(compact,cw,ch)+pred)
                    peak=(1<<depth)-1;expected=pred[:]
                    for i in range(h):
                        for j in range(w):
                            yy=h-1-i if tx_type in FLIP_ROWS else i;xx=w-1-j if tx_type in FLIP_COLUMNS else j
                            value=pred[yy*w+xx]+spec[i*w+j];saturated+=not 0<=value<=peak
                            expected[yy*w+xx]=max(0,min(peak,value))
                    if pixels!=expected:raise SystemExit(f'spec/libaom mismatch {w}x{h} type {tx_type} depth {depth}')
                    types_seen.add((w,h,tx_type))
                    cases.append(dict(width=w,height=h,tx_type=tx_type,depth=depth,seed=seed,compact=compact,
                        residual=spec,pixels=pixels,native=True))
    native.close()
    # Nonconforming extremes: deterministic s64 semantics, Python reference only.
    extreme=0
    for depth in (8,12):
        for tx,(w,h) in enumerate(SIZES):
            if w*h>256:continue
            cw=min(w,32);ch=min(h,32);limit=1<<(depth+7)
            for tx_type in legal_types(w,h)[:3]:
                compact=[rng.choice((limit-1,-limit,limit-1,0)) for _ in range(cw*ch)]
                dequant=[compact[i*cw+j] for i in range(h) for j in range(w)]
                try:strict.inverse(dequant,w,h,tx_type,depth);continue
                except Nonconforming:pass
                residual=loose.inverse(dequant,w,h,tx_type,depth)
                seed=len(cases)%61;pred=prediction(w,h,depth,seed);peak=(1<<depth)-1;pixels=pred[:]
                for i in range(h):
                    for j in range(w):
                        yy=h-1-i if tx_type in FLIP_ROWS else i;xx=w-1-j if tx_type in FLIP_COLUMNS else j
                        pixels[yy*w+xx]=max(0,min(peak,pred[yy*w+xx]+residual[i*w+j]))
                cases.append(dict(width=w,height=h,tx_type=tx_type,depth=depth,seed=seed,compact=compact,
                    residual=residual,pixels=pixels,native=False));extreme+=1
    product=os.path.relpath(a.root/'src/transform/inverse_transform.zi',a.out)
    boundary=os.path.relpath(a.root/'tests/av1_inverse_transform.zi',a.out)
    imports=[f'#import, file "{product}";',f'#import, file "{boundary}";']
    modules=[];budget=0;shard=[];shards=[]
    for case in cases:
        weight=case['width']*case['height']
        if shard and budget+weight>12288:shards.append(shard);shard=[];budget=0
        shard.append(case);budget+=weight
    if shard:shards.append(shard)
    for number,subset in enumerate(shards):
        module=f'av1_inverse_transform_vectors_{number}';modules.append(module);lines=imports[:]
        for local,case in enumerate(subset):
            w=case['width'];h=case['height'];cw=min(w,32);ch=min(h,32);d=case['depth'];count=w*h
            lines += [f'Case{local} :: () -> s32 {{']+literal('compact','s32',case['compact'])
            lines += literal('residual_expected','s32',case['residual'])+literal('pixels_expected','u16',case['pixels'])
            lines += [f'    dequant: [{count}]s32; residual: [{count}]s32',
                f'    for i: 0..{ch-1} {{ for j: 0..{cw-1} {{ dequant[i * {w} + j] = compact[i * {cw} + j] }} }}',
                f'    if InverseTransform2D(dequant[:], residual[:], {w}, {h}, {case["tx_type"]}, {d}).code != DecodeOK {{ return 1 }}',
                f'    for i: 0..{count-1} {{ if residual[i] != residual_expected[i] {{ return 2 }} }}',
                f'    samples := TransformPrediction({w}, {h}, {d}, {case["seed"]})',
                f'    if AddResidual(samples[:{count}], {w}, residual[:], {w}, {h}, {case["tx_type"]}, {d}).code != DecodeOK {{ return 3 }}',
                f'    for i: 0..{count-1} {{ if samples[i] != pixels_expected[i] {{ return 4 }} }}',
                f'    if InverseTransform2D(dequant[:], dequant[:], {w}, {h}, {case["tx_type"]}, {d}).code != DecodeOK {{ return 5 }}',
                f'    for i: 0..{count-1} {{ if dequant[i] != residual_expected[i] {{ return 6 }} }}',
                '    return 0','}']
        lines += ['main :: () -> s32 {','    if InverseTransformBoundaries() != 0 { return 99 }']
        lines += [f'    if Case{i}() != 0 {{ return {i+1} }}' for i in range(len(subset))]+['    return 0','}']
        (a.out/f'{module}.zi').write_text('\n'.join(lines)+'\n')
    (a.out/'modules.txt').write_text('\n'.join(modules)+'\n')
    native_cases=sum(case['native'] for case in cases)
    receipt=dict(libaom=PIN,host_sha256=digest(host),library_sha256=digest(library),seed='0x41563174',
        native_cases=native_cases,size_type_pairs=len(types_seen),nonconforming_dropped=dropped,
        saturated_pixels=saturated,reference_only_extremes=extreme,shards=len(modules),
        inputs='libaom forward transform of bounded residual patterns, step-rounded and clamped to Dequant range',
        scope='scalar 2D inverse transforms and flipped Clip1 reconstruction; not frame conformance')
    (a.out/'oracle.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(f'Pinned inverse transforms:{native_cases} libaom cases over {len(types_seen)} size/type pairs, '
          f'{extreme} reference-only extremes, {dropped} nonconforming dropped')


if __name__=='__main__':main()

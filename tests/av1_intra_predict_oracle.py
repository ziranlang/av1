#!/usr/bin/env python3
"""Generic intra prediction corpus against pinned libaom reconintra.c.

The host compiles libaom's own av1/common/reconintra.c translation unit so the
static high-bitdepth builders (the code av1_predict_intra_block runs after its
availability bookkeeping) are reachable. Neighbor samples come from a
deterministic per-position generator implemented identically in the host and
in tests/av1_intra_predict.zi; libaom's predicted block is compared through two
modular hashes so every case stays a short table row. These are kernel vectors,
not frame conformance.
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
#include "av1/common/reconintra.c"
#include "av1/common/cfl.c"
#include <stdio.h>
void av1_rtcd(void);void aom_dsp_rtcd(void);
enum { STRIDE = 144 };
static uint16_t sample(int64_t seed,int64_t style,int64_t depth,int64_t row,int64_t column){
  int64_t maximum=((int64_t)1<<depth)-1;
  int64_t k=(seed*40009+row*263+column*7919+17)%2147483648LL;
  k=(k*1103515245+12345)%2147483648LL;k=(k*1103515245+12345)%2147483648LL;
  int64_t noise=k>>4;
  if(style==0)return (uint16_t)(noise%(maximum+1));
  if(style==1){int64_t scale=(int64_t)1<<(depth-8);
    int64_t v=((int64_t)1<<(depth-1))+((row*3+column*5)%64-32)*scale+noise%(4*scale)-2*scale;
    if(v<0)v=0;if(v>maximum)v=maximum;return (uint16_t)v;}
  return (uint16_t)(noise%2==0?0:maximum);
}
static int next(long *value){return scanf("%ld",value)==1;}
int main(void){
  aom_dsp_rtcd();av1_rtcd();av1_init_intra_predictors();
  static uint16_t plane[STRIDE*STRIDE];static uint16_t out[4096];
  long f[18],kind;
  static uint16_t luma[64*64];static CFL_CTX cfl;
  for(;;){
    if(!next(&kind))return 0;
    if(kind==1){
      for(int i=0;i<10;i++)if(!next(&f[i]))return 2;
      int w=(int)f[0],h=(int)f[1],sx=(int)f[2],sy=(int)f[3],lw=(int)f[4],lh=(int)f[5],alpha=(int)f[6],bd=(int)f[7];
      int64_t seed=f[8],style=f[9];
      int tx=-1;for(int t=0;t<TX_SIZES_ALL;t++)if(tx_size_wide[t]==w&&tx_size_high[t]==h)tx=t;
      if(tx<0||lw%4||lh%4)return 3;
      for(int r=0;r<lh;r++)for(int c=0;c<lw;c++)luma[r*64+c]=sample(seed,style,bd,r,c);
      for(int i=0;i<h;i++)for(int j=0;j<w;j++)out[i*w+j]=sample(seed+1,style,bd,0,0); // flat DC: libaom SIMD broadcasts dst[0]
      memset(&cfl,0,sizeof(cfl));cfl.subsampling_x=sx;cfl.subsampling_y=sy;
      for(int r=0;r<lh/4;r++)for(int c=0;c<lw/4;c++)
        cfl_store(&cfl,CONVERT_TO_BYTEPTR(luma+r*4*64+c*4),64,r,c,TX_4X4,1);
      cfl_pad(&cfl,w,h);
      cfl_get_subtract_average_fn((TX_SIZE)tx)(cfl.recon_buf_q3,cfl.ac_buf_q3);
      cfl_get_predict_hbd_fn((TX_SIZE)tx)(cfl.ac_buf_q3,out,w,alpha,bd);
      long long h1=0,h2=0;
      for(int i=0;i<w*h;i++){h1=(h1*31+out[i]+1)%2147483647LL;h2=(h2*1000003+out[i]*7LL+3)%2147483629LL;}
      printf("%lld %lld\n",h1,h2);fflush(stdout);continue;
    }
    for(int i=0;i<18;i++)if(!next(&f[i]))return 2;
    int w=(int)f[0],h=(int)f[1],x=(int)f[2],y=(int)f[3],max_x=(int)f[4],max_y=(int)f[5];
    int ha=(int)f[6],hl=(int)f[7],har=(int)f[8],hbl=(int)f[9],mode=(int)f[10],delta=(int)f[11];
    int fmode=(int)f[12],edge=(int)f[13],ftype=(int)f[14],bd=(int)f[15];
    int64_t seed=f[16],style=f[17];
    memset(plane,0,sizeof(plane));
    if(y>0)for(int c=x>0?x-1:x;c<=max_x;c++)plane[(y-1)*STRIDE+c]=sample(seed,style,bd,y-1,c);
    if(x>0)for(int r=y>0?y-1:y;r<=max_y;r++)plane[r*STRIDE+x-1]=sample(seed,style,bd,r,x-1);
    int tx=-1;for(int t=0;t<TX_SIZES_ALL;t++)if(tx_size_wide[t]==w&&tx_size_high[t]==h)tx=t;
    if(tx<0)return 3;
    const int n_top=ha?AOMMIN(w,max_x-x+1):0,n_left=hl?AOMMIN(h,max_y-y+1):0;
    const int xr=max_x+1-(x+w),yd=max_y+1-(y+h);
    const uint16_t *ref=plane+y*STRIDE+x;
    memset(out,0,sizeof(out));
    if(fmode>=0||av1_is_directional_mode((PREDICTION_MODE)mode)){
      int angle=0,tr=-1,bl=-1;
      if(fmode<0){angle=mode_to_angle_map[mode]+delta*ANGLE_STEP;
        if(angle<90)tr=har?AOMMIN(w,xr):0;
        if(angle>180)bl=hbl?AOMMIN(h,yd):0;}
      highbd_build_directional_and_filter_intra_predictors(CONVERT_TO_BYTEPTR(ref),STRIDE,CONVERT_TO_BYTEPTR(out),w,
        (PREDICTION_MODE)mode,angle,fmode<0?FILTER_INTRA_MODES:(FILTER_INTRA_MODE)fmode,(TX_SIZE)tx,!edge,
        n_top,tr,n_left,bl,ftype,bd);
    } else {
      highbd_build_non_directional_intra_predictors(CONVERT_TO_BYTEPTR(ref),STRIDE,CONVERT_TO_BYTEPTR(out),w,
        (PREDICTION_MODE)mode,(TX_SIZE)tx,n_top,n_left,bd);
    }
    long long h1=0,h2=0;
    for(int i=0;i<w*h;i++){h1=(h1*31+out[i]+1)%2147483647LL;h2=(h2*1000003+out[i]*7LL+3)%2147483629LL;}
    printf("%lld %lld\n",h1,h2);fflush(stdout);
  }
}
'''

ANGLES=[90,180,45,135,113,157,203,67]
FIELDS=20
CFL_FIELDS=12
CHUNK=40


def strength(w,h,filter_type,delta):
    d=abs(delta);s=w+h
    if filter_type==0:
        if s<=8:return 1 if d>=56 else 0
        if s<=16:return 1 if d>=40 else 0
        if s<=24:return 3 if d>=32 else 2 if d>=16 else 1 if d>=8 else 0
        if s<=32:return 3 if d>=32 else 2 if d>=4 else 1
        return 3
    if s<=8:return 2 if d>=64 else 1 if d>=40 else 0
    if s<=16:return 2 if d>=48 else 1 if d>=20 else 0
    if s<=24:return 3 if d>=4 else 0
    return 3


def upsample(w,h,filter_type,delta):
    d=abs(delta)
    if d<=0 or d>=40:return False
    return w+h<=(16 if filter_type==0 else 8)


def make_case(rng,w,h,mode,delta,filter_mode,avail=None):
    if avail is None:
        if rng.random()<0.45:
            ha=hl=1;har=int(rng.random()<0.6);hbl=int(rng.random()<0.6)
        else:
            ha=int(rng.random()<0.7);hl=int(rng.random()<0.7)
            har=int(ha and rng.random()<0.5);hbl=int(hl and rng.random()<0.5)
    else:ha,hl,har,hbl=avail
    x=rng.randint(1,16) if hl else rng.choice((0,0,rng.randint(1,16)))
    y=rng.randint(1,16) if ha else rng.choice((0,0,rng.randint(1,16)))
    def limit(origin,size,right):
        if right:return min(143,rng.choice((143,origin+size+rng.randint(0,size-1))))
        r=rng.random()
        if r<0.5:return 143
        if r<0.8:return origin+rng.randint(0,size-1)
        return min(143,origin+size-1+rng.randint(0,size))
    max_x=limit(x,w,har);max_y=limit(y,h,hbl)
    return [w,h,x,y,max_x,max_y,ha,hl,har,hbl,mode,delta,filter_mode,int(rng.random()<0.75),rng.randint(0,1),
        rng.choice((8,10,12)),rng.randrange(1<<20),rng.choice((0,0,1,2))]


class Host:
    def __init__(self,binary):
        self.process=subprocess.Popen([str(binary)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True,bufsize=1)
    def ask(self,values):
        self.process.stdin.write(' '.join(map(str,values))+'\n');self.process.stdin.flush()
        return list(map(int,self.process.stdout.readline().split()))
    def close(self):
        self.process.stdin.close()
        if self.process.wait()!=0:raise SystemExit('Intra oracle host failed')


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True);p.add_argument('--aom',type=Path,required=True)
    a=p.parse_args();a.root=a.root.resolve();a.out=a.out.resolve();a.out.mkdir(parents=True,exist_ok=True)
    assert subprocess.check_output(['git','-C',str(a.aom),'rev-parse','HEAD'],text=True).strip()==PIN
    subprocess.run(['git','-C',str(a.aom),'diff-index','--quiet','HEAD','--'],check=True)
    library=Path(os.environ.get('AV1_AOM_LIBRARY',str(a.root/'build/native/aom/libaom.a')))
    config=Path(os.environ.get('AV1_AOM_CONFIG',str(a.root/'build/native/aom')))
    host=a.out/'oracle.c';host.write_text(HOST);binary=a.out/'oracle'
    subprocess.run(shlex.split(os.environ.get('CC','cc'))+['-std=gnu11','-O2','-I'+str(a.aom),'-I'+str(config),
        str(host),str(library),'-lm','-lpthread','-o',str(binary)],check=True)
    rng=random.Random(0x49505231);rows=[]
    combos=[(m,0) for m in (0,9,10,11,12)]+[(m,d) for m in range(1,9) for d in range(-3,4)]
    for w,h in SIZES:
        for mode,delta in combos:
            for _ in range(3):rows.append(make_case(rng,w,h,mode,delta,-1))
        if w<=32 and h<=32:
            for filter_mode in range(5):
                for _ in range(4):rows.append(make_case(rng,w,h,0,0,filter_mode))
        availabilities=[(ha,hl,har,hbl) for ha in (0,1) for hl in (0,1) for har in range(ha+1) for hbl in range(hl+1)]
        for avail in availabilities:
            for mode,delta,filter_mode in ((0,0,-1),(3,-2,-1),(4,1,-1),(7,3,-1),(12,0,-1),(8,-3,-1),(6,2,-1)):
                rows.append(make_case(rng,w,h,mode,delta,filter_mode,avail))
            if w<=32 and h<=32:rows.append(make_case(rng,w,h,0,0,2,avail))
    native=Host(binary);stats=dict(cases=len(rows),upsampled_above=0,upsampled_left=0,corner_filtered=0,
        partial_width=0,partial_height=0,above_right=0,below_left=0,no_neighbors=0,filter_intra=0,
        strengths=[0,0,0,0],depths={8:0,10:0,12:0})
    angles=set()
    for row in rows:
        w,h,x,y,max_x,max_y,ha,hl,har,hbl,mode,delta,filter_mode,edge,filter_type,depth=row[:16]
        hashes=native.ask([0]+row)
        if len(hashes)!=2:raise SystemExit('Intra oracle host returned no hashes')
        row += hashes
        stats['depths'][depth]+=1
        stats['partial_width']+=ha and max_x-x+1<w;stats['partial_height']+=hl and max_y-y+1<h
        stats['above_right']+=har;stats['below_left']+=hbl;stats['no_neighbors']+=not ha and not hl
        if filter_mode>=0:stats['filter_intra']+=1;continue
        if 1<=mode<=8:
            angle=ANGLES[mode-1]+3*delta;angles.add((w,h,angle))
            if edge and angle not in (90,180):
                stats['corner_filtered']+=90<angle<180 and w+h>=24
                if ha:stats['strengths'][strength(w,h,filter_type,angle-90)]+=1
                if hl:stats['strengths'][strength(w,h,filter_type,angle-180)]+=1
            if edge:
                stats['upsampled_above']+=upsample(w,h,filter_type,angle-90)
                stats['upsampled_left']+=upsample(w,h,filter_type,angle-180)
    cfl_rows=[]
    for w,h in SIZES:
        if w>32 or h>32:continue
        for sx,sy in ((0,0),(1,0),(1,1)):
            full_w=w<<sx;full_h=h<<sy
            for variant in range(6):
                lw=full_w if variant<3 else 4*rng.randint(1,max(1,full_w//4))
                lh=full_h if variant in (0,1,3) else 4*rng.randint(1,max(1,full_h//4))
                lw=max(lw,4);lh=max(lh,4)
                alpha=(-16,16,0)[variant] if variant<3 else rng.choice([a for a in range(-16,17) if a])
                row=[w,h,sx,sy,lw,lh,alpha,rng.choice((8,10,12)),rng.randrange(1<<20),rng.choice((0,0,1,2))]
                hashes=native.ask([1]+row)
                if len(hashes)!=2:raise SystemExit('CfL oracle host returned no hashes')
                cfl_rows.append(row+hashes)
                stats['cfl_partial_luma']=stats.get('cfl_partial_luma',0)+(lw<full_w or lh<full_h)
    stats['cfl_cases']=len(cfl_rows)
    native.close()
    stats['size_angle_pairs']=len(angles)
    product=os.path.relpath(a.root/'src/predict/intra_predict.zi',a.out)
    boundary=os.path.relpath(a.root/'tests/av1_intra_predict.zi',a.out)
    imports=[f'#import, file "{product}";',f'#import, file "{boundary}";']
    chunks=[rows[i:i+CHUNK] for i in range(0,len(rows),CHUNK)]
    per_shard=25;modules=[]
    for number in range(0,len(chunks),per_shard):
        shard=number//per_shard;module=f'av1_intra_predict_vectors_{shard}';modules.append(module);lines=imports[:]
        subset=chunks[number:number+per_shard]
        for local,chunk in enumerate(subset):
            flat=[value for row in chunk for value in row]
            lines += [f'IntraChunk{local} :: (plane: []u16, out: []u16) -> s32 {{']+literal('cases','s32',flat)
            lines += [f'    for k: 0..{len(chunk)-1} {{',
                f'        result := RunIntraCase(cases[k * {FIELDS}:k * {FIELDS} + {FIELDS}], plane, out)',
                '        if result != 0 { return result }','    }','    return 0','}']
        if shard==0:
            flat=[value for row in cfl_rows for value in row]
            for start in range(0,len(flat),CFL_FIELDS*80):
                part=flat[start:start+CFL_FIELDS*80];number_rows=len(part)//CFL_FIELDS
                lines += [f'CflChunk{start} :: (luma: []u16, chroma: []u16) -> s32 {{']+literal('cases','s32',part)
                lines += [f'    for k: 0..{number_rows-1} {{',
                    f'        result := RunCflCase(cases[k * {CFL_FIELDS}:k * {CFL_FIELDS} + {CFL_FIELDS}], luma, chroma)',
                    '        if result != 0 { return result }','    }','    return 0','}']
        lines += ['main :: () -> s32 {']
        if shard==0:
            lines += ['    if IntraPredictBoundaries() != 0 { return 251 }','    if IntraIndexSweep() != 0 { return 252 }',
                '    if CflBoundaries() != 0 { return 253 }','    luma: [4096]u16; chroma: [1056]u16']
            for start in range(0,len(cfl_rows)*CFL_FIELDS,CFL_FIELDS*80):
                lines += [f'    if CflChunk{start}(luma[:], chroma[:]) != 0 {{ return 254 }}']
        lines += [f'    plane: [{144*144}]u16; out: [4160]u16']
        for local in range(len(subset)):
            lines += [f'    result{local} := IntraChunk{local}(plane[:], out[:])',
                f'    if result{local} != 0 {{ return {10*local} + result{local} }}']
        lines += ['    return 0','}']
        (a.out/f'{module}.zi').write_text('\n'.join(lines)+'\n')
    (a.out/'modules.txt').write_text('\n'.join(modules)+'\n')
    stats['depths']={str(k):v for k,v in stats['depths'].items()}
    receipt=dict(libaom=PIN,host_sha256=digest(host),library_sha256=digest(library),seed='0x49505231',
        shards=len(modules),chunk_cases=CHUNK,**stats,
        inputs='deterministic per-position neighbor samples (random, gradient, extreme styles) at depths 8/10/12',
        scope='AV1 7.11.2 edge construction, DC/V/H/directional/smooth/Paeth/recursive filter prediction and 7.11.5 CfL; not frame conformance')
    (a.out/'oracle.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(f'Pinned intra prediction: {len(rows)} libaom cases, {len(angles)} size/angle pairs, '
          f'{stats["upsampled_above"]+stats["upsampled_left"]} upsampled edges, {len(modules)} shards')


if __name__=='__main__':main()

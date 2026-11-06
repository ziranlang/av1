#!/usr/bin/env python3
"""Build a real AV1 reduced-still fixture and trace the pinned symbol oracle.
Only test code invokes FFmpeg/libaom. Product decoding cannot use this host.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess

PIN='b141d9c45db236335351c8a4b2a14b3ade27c937'
REFERENCE={
    8:bytes([18,0,10,5,24,8,191,33,80,50,3,16,0,48]),
    10:bytes([18,0,10,5,24,8,191,35,80,50,3,16,0,48]),
    12:bytes([18,0,10,5,88,8,191,35,168,50,3,16,0,48])}

HOST = r'''
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include "aom/aom_decoder.h"
#include "aom/aomdx.h"
#include "aom_dsp/entdec.h"
int __real_od_ec_decode_cdf_q15(od_ec_dec*,const uint16_t*,int);
int __wrap_od_ec_decode_cdf_q15(od_ec_dec *d,const uint16_t *cdf,int n) {
  int symbol=__real_od_ec_decode_cdf_q15(d,cdf,n);
  printf("cdf %d",n);
  for(int i=0;i<n;i++)printf(" %u",32768-cdf[i]);
  printf(" symbol %d\n",symbol);
  return symbol;
}
int main(int argc,char **argv) {
  if(argc!=3)return 1;unsigned depth=(unsigned)atoi(argv[2]); FILE *f=fopen(argv[1],"rb");if(!f)return 2;
  unsigned char data[4096];size_t size=fread(data,1,sizeof(data),f);fclose(f);
  if(!size||size==sizeof(data))return 3;
  aom_codec_ctx_t codec={0};aom_codec_dec_cfg_t cfg={0};cfg.threads=1;cfg.allow_lowbitdepth=1;
  if(aom_codec_dec_init(&codec,aom_codec_av1_dx(),&cfg,0))return 4;
  if(aom_codec_decode(&codec,data,size,NULL))return 5;
  aom_codec_iter_t iter=NULL;aom_image_t *image=aom_codec_get_frame(&codec,&iter);
  if(!image||image->d_w!=8||image->d_h!=8||image->bit_depth!=depth)return 6;
  for(int y=0;y<8;y++)for(int x=0;x<8;x++){
    unsigned sample=image->fmt&AOM_IMG_FMT_HIGHBITDEPTH?((uint16_t*)(image->planes[0]+y*image->stride[0]))[x]:image->planes[0][y*image->stride[0]+x];
    if(sample!=(1u<<(depth-1)))return 7;
  }
  return aom_codec_destroy(&codec);
}
'''


def run(args, **kw):return subprocess.run(args,check=True,**kw)


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--root',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--aom',type=Path,required=True)
    p.add_argument('--depth',type=int,choices=[8,10,12],default=8)
    a=p.parse_args();a.out.mkdir(parents=True,exist_ok=True)
    revision=run(['git','-C',str(a.aom),'rev-parse','HEAD'],text=True,capture_output=True).stdout.strip()
    if revision!=PIN:raise SystemExit('libaom source revision mismatch')
    run(['git','-C',str(a.aom),'diff','--quiet','HEAD','--'])
    raw=a.out/'flat.gray'
    raw.write_bytes((1<<(a.depth-1)).to_bytes(1 if a.depth==8 else 2,'little')*64)
    obu=a.out/'flat.obu'
    pixel_format='gray' if a.depth==8 else f'gray{a.depth}le'
    command=['ffmpeg','-hide_banner','-loglevel','error','-f','rawvideo','-pixel_format',pixel_format,
        '-video_size','8x8','-framerate','1','-i',str(raw),'-frames:v','1','-c:v','libaom-av1',
        '-threads','1','-row-mt','0','-cpu-used','8','-crf','0','-b:v','0','-still-picture','1',
        '-aom-params','enable-intrabc=0:enable-palette=0:enable-filter-intra=0:enable-restoration=0:enable-cdef=0',
        '-f','obu','-y',str(obu)]
    run(command,stdout=subprocess.DEVNULL)
    source=a.out/'trace.c';source.write_text(HOST)
    host=a.out/'trace'
    library=Path(os.environ.get('AV1_AOM_LIBRARY',str(a.root/'build/native/aom/libaom.a')))
    config=Path(os.environ.get('AV1_AOM_CONFIG',str(a.root/'build/native/aom')))
    run(shlex.split(os.environ.get('CC','cc'))+['-std=c11','-O2','-I'+str(a.aom),
        '-I'+str(config),str(source),str(library),
        '-Wl,--wrap=od_ec_decode_cdf_q15','-lm','-o',str(host)])
    reference=a.out/'reference.obu';reference.write_bytes(REFERENCE[a.depth])
    result=run([str(host),str(reference),str(a.depth)],text=True,capture_output=True)
    symbols=[int(line.split()[-1]) for line in result.stdout.splitlines() if line.startswith('cdf ')]
    if symbols!=[0,0,0,1,1,1,1]:raise SystemExit('pinned zero-residual symbol trace changed')
    if obu.read_bytes()!=REFERENCE[a.depth]:run([str(host),str(obu),str(a.depth)],text=True,capture_output=True)
    (a.out/'symbols.txt').write_text(result.stdout)
    data=obu.read_bytes()
    print('real flat still:',a.depth,'bit,',len(data),'bytes; pinned libaom midpoint plane verified')
    version=run(['ffmpeg','-version'],text=True,capture_output=True).stdout.splitlines()[0]
    digest=hashlib.sha256()
    with library.open('rb') as binary:
        for chunk in iter(lambda:binary.read(1024*1024),b''):digest.update(chunk)
    (a.out/'fixture.json').write_text(json.dumps({'encoder':version,'command':command,
        'encoded_sha256':hashlib.sha256(data).hexdigest(),'reference_sha256':hashlib.sha256(REFERENCE[a.depth]).hexdigest(),
        'oracle_revision':PIN,'oracle_library_sha256':digest.hexdigest(),
        'scope':'one real monochrome reduced-still primitive fixture'},indent=2)+'\n')


if __name__=='__main__':main()

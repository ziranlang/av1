#!/usr/bin/env python3
"""Baseline decode timing of the authored decoder against pinned libaom and
dav1d, one thread, one shown KEY frame. Each picks its SIMD at run time.

Every engine decodes the same in-memory temporal unit in one process; a row
counts only when all three produce the same sample hash. Each iteration is a
one-shot still decode: decoder setup, decode and release, as AVIF uses it.
The numbers describe this machine and corpus only.
"""
import argparse
import datetime
import json
import os
import platform
from pathlib import Path
import shlex
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from av1_intra_frames_oracle import ENCODER
from av1_lossless_oracle import PIN

# name: w h depth format q sb128 tile_columns_log2 tile_rows_log2 screen aq
# deltaq lossless reduced seed filters speed sharpness (av1_intra_frames_oracle)
CORPUS = {
    '1080p-8bit-420-q20': (1920, 1080, 8, 0, 20, 0, 0, 0, 0, 0, 0, 0, 0, 11, 7, 6, 0),
    '1080p-8bit-420-q40': (1920, 1080, 8, 0, 40, 0, 0, 0, 0, 0, 0, 0, 0, 12, 7, 6, 0),
    '1080p-10bit-420-q30': (1920, 1080, 10, 0, 30, 0, 0, 0, 0, 0, 0, 0, 0, 13, 7, 6, 0),
    '720p-8bit-444-q30': (1280, 720, 8, 2, 30, 0, 0, 0, 0, 0, 0, 0, 0, 14, 7, 6, 0),
    '720p-8bit-420-screen-q30': (1280, 720, 8, 0, 30, 0, 0, 0, 1, 0, 0, 0, 0, 15, 7, 6, 0),
    '352x288-8bit-420-q30': (352, 288, 8, 0, 30, 0, 0, 0, 0, 0, 0, 0, 0, 16, 7, 6, 0),
}

HOST = r'''
#define _POSIX_C_SOURCE 200809L
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include "aom/aom_decoder.h"
#include "aom/aomdx.h"
#include "dav1d/dav1d.h"
#include "av1_benchmark.h"

static long allocations;
void *__real_malloc(size_t);void *__real_calloc(size_t,size_t);void *__real_realloc(void*,size_t);
int __real_posix_memalign(void**,size_t,size_t);
void *__wrap_malloc(size_t n){allocations++;return __real_malloc(n);}
void *__wrap_calloc(size_t c,size_t n){allocations++;return __real_calloc(c,n);}
void *__wrap_realloc(void *p,size_t n){allocations++;return __real_realloc(p,n);}
int __wrap_posix_memalign(void **p,size_t a,size_t n){allocations++;return __real_posix_memalign(p,a,n);}

static int64_t now(void){struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return (int64_t)t.tv_sec*1000000000+t.tv_nsec;}
static uint64_t fnv(uint64_t h,const uint8_t *row,size_t bytes){for(size_t i=0;i<bytes;i++)h=(h^row[i])*1099511628211ull;return h;}
// Visible samples in their sample size: bytes at 8 bits, little-endian u16 above.
static uint64_t plane_hash(uint64_t h,const uint8_t *data,ptrdiff_t stride,int w,int h_rows,int wide,int depth){
  uint8_t row[2*8192];
  for(int y=0;y<h_rows;y++){
    const uint8_t *src=data+y*stride;
    if(!wide){h=fnv(h,src,(size_t)w);continue;}
    const uint16_t *s=(const uint16_t*)src;
    if(depth==8){for(int x=0;x<w;x++)row[x]=(uint8_t)s[x];h=fnv(h,row,(size_t)w);}
    else{for(int x=0;x<w;x++){row[2*x]=(uint8_t)(s[x]&255);row[2*x+1]=(uint8_t)(s[x]>>8);}h=fnv(h,row,(size_t)w*2);}
  }
  return h;
}
static int authored(const uint8_t *data,size_t size,uint64_t *hash){
  Slice s={(void*)data,(int64_t)size};return Av1BenchDecode(s,hash);
}
static int libaom(const uint8_t *data,size_t size,uint64_t *hash){
  aom_codec_ctx_t codec;aom_codec_dec_cfg_t cfg={0};cfg.threads=1;cfg.allow_lowbitdepth=1;
  if(aom_codec_dec_init(&codec,aom_codec_av1_dx(),&cfg,0))return 101;
  if(aom_codec_decode(&codec,data,size,NULL)){aom_codec_destroy(&codec);return 102;}
  aom_codec_iter_t it=NULL;aom_image_t *img=aom_codec_get_frame(&codec,&it),*next;
  if(!img){aom_codec_destroy(&codec);return 103;}
  while((next=aom_codec_get_frame(&codec,&it)))img=next;
  int wide=(img->fmt&AOM_IMG_FMT_HIGHBITDEPTH)!=0,depth=(int)img->bit_depth;
  uint64_t h=14695981039346656037ull;
  for(int p=0;p<(img->monochrome?1:3);p++){
    int sx=p?(int)img->x_chroma_shift:0,sy=p?(int)img->y_chroma_shift:0;
    h=plane_hash(h,img->planes[p],img->stride[p],((int)img->d_w+(1<<sx)-1)>>sx,((int)img->d_h+(1<<sy)-1)>>sy,wide,depth);
  }
  *hash=h;return aom_codec_destroy(&codec)?104:0;
}
static void no_free(const uint8_t *buf,void *cookie){(void)buf;(void)cookie;}
static int dav1d(const uint8_t *data,size_t size,uint64_t *hash){
  Dav1dSettings s;dav1d_default_settings(&s);s.n_threads=1;s.max_frame_delay=1;
  Dav1dContext *c=NULL;if(dav1d_open(&c,&s))return 201;
  Dav1dData d={0};if(dav1d_data_wrap(&d,data,size,no_free,NULL)){dav1d_close(&c);return 202;}
  Dav1dPicture pic={0};int r;
  do{r=dav1d_send_data(c,&d);if(r&&r!=DAV1D_ERR(EAGAIN)){dav1d_close(&c);return 203;}
     int g=dav1d_get_picture(c,&pic);if(g==0)break;if(g!=DAV1D_ERR(EAGAIN)){dav1d_close(&c);return 204;}
  }while(d.sz>0);
  if(!pic.data[0]){int g=dav1d_get_picture(c,&pic);if(g){dav1d_close(&c);return 205;}}
  if(d.sz)dav1d_data_unref(&d);
  int depth=pic.p.bpc,wide=depth>8,mono=pic.p.layout==DAV1D_PIXEL_LAYOUT_I400;
  int sx=pic.p.layout==DAV1D_PIXEL_LAYOUT_I420||pic.p.layout==DAV1D_PIXEL_LAYOUT_I422,sy=pic.p.layout==DAV1D_PIXEL_LAYOUT_I420;
  uint64_t h=14695981039346656037ull;
  h=plane_hash(h,pic.data[0],pic.stride[0],pic.p.w,pic.p.h,wide,depth);
  if(!mono)for(int p=1;p<3;p++)h=plane_hash(h,pic.data[p],pic.stride[1],(pic.p.w+sx)>>sx,(pic.p.h+sy)>>sy,wide,depth);
  *hash=h;dav1d_picture_unref(&pic);dav1d_close(&c);return 0;
}
static int cmp(const void *a,const void *b){int64_t x=*(const int64_t*)a,y=*(const int64_t*)b;return x<y?-1:x>y;}
int main(int argc,char **argv){
  if(argc!=5)return 1;
  FILE *f=fopen(argv[1],"rb");if(!f)return 2;
  fseek(f,0,SEEK_END);long size=ftell(f);fseek(f,0,SEEK_SET);
  uint8_t *data=malloc((size_t)size);if(!data||fread(data,1,(size_t)size,f)!=(size_t)size)return 3;fclose(f);
  int minimum=atoi(argv[3]);int64_t budget=(int64_t)atoll(argv[4])*1000000;
  int (*engines[3])(const uint8_t*,size_t,uint64_t*)={authored,libaom,dav1d};
  const char *names[3]={"authored","libaom","dav1d"};
  for(int e=0;e<3;e++){
    if(strcmp(argv[2],"all")&&strcmp(argv[2],names[e]))continue;
    uint64_t hash=0;int code=engines[e](data,(size_t)size,&hash);
    if(code){printf("{\"engine\":\"%s\",\"error\":%d}\n",names[e],code);continue;}
    long before=allocations;uint64_t again=0;code=engines[e](data,(size_t)size,&again);
    long per_decode=allocations-before;
    if(code||again!=hash)return 4;
    int64_t samples[100000];int n=0;int64_t start=now();
    while(n<100000&&(n<minimum||now()-start<budget)){
      int64_t t0=now();engines[e](data,(size_t)size,&again);samples[n++]=now()-t0;
      if(again!=hash)return 5;
    }
    qsort(samples,(size_t)n,sizeof samples[0],cmp);
    double mean=0;for(int i=0;i<n;i++)mean+=(double)samples[i]/n;
    printf("{\"engine\":\"%s\",\"hash\":\"%016llx\",\"iterations\":%d,\"min_ns\":%lld,\"median_ns\":%lld,\"mean_ns\":%.0f,"
        "\"allocation_calls_per_decode\":%ld}\n",names[e],(unsigned long long)hash,n,(long long)samples[0],(long long)samples[n/2],mean,per_decode);
  }
  return 0;
}
'''


def run(args, **kw):
    return subprocess.run(args, check=True, **kw)


def main():
    p = argparse.ArgumentParser()
    for name in ('root', 'out', 'aom', 'authored', 'toolchain'):
        p.add_argument('--' + name, type=Path, required=True)
    p.add_argument('--dav1d-root', type=Path, required=True)
    p.add_argument('--dav1d-source', type=Path, required=True)
    p.add_argument('--cpu', default='')
    p.add_argument('--minimum', type=int, default=5)
    p.add_argument('--budget-ms', type=int, default=1500)
    a = p.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    if run(['git', '-C', str(a.aom), 'rev-parse', 'HEAD'], text=True, capture_output=True).stdout.strip() != PIN:
        raise SystemExit('libaom pin mismatch')
    library = a.root / 'build/native/aom/libaom.a'
    config = a.root / 'build/native/aom'
    dav1d = a.dav1d_root / 'lib/libdav1d.a'
    cc = shlex.split(os.environ.get('CC', 'cc'))
    cflags = shlex.split(os.environ.get('AV1_BENCH_CFLAGS', '-O3'))
    encoder = a.out / 'encoder'
    (a.out / 'encoder.c').write_text(ENCODER)
    run(cc + ['-std=c11', '-O2', '-I' + str(a.aom), '-I' + str(config), str(a.out / 'encoder.c'), str(library),
        '-lm', '-lpthread', '-o', str(encoder)])
    (a.out / 'host.c').write_text(HOST)
    host = a.out / 'bench'
    wrap = ['-Wl,--wrap=malloc', '-Wl,--wrap=calloc', '-Wl,--wrap=realloc', '-Wl,--wrap=posix_memalign']
    run(cc + ['-std=c11'] + cflags + ['-I' + str(a.aom), '-I' + str(config), '-I' + str(a.dav1d_root / 'include'),
        '-I' + str(a.toolchain / 'include'), '-I' + str(a.authored), str(a.out / 'host.c')] +
        [str(path) for path in sorted(a.authored.glob('*.c'))] + [str(library), str(dav1d)] + wrap +
        ['-lm', '-lpthread', '-o', str(host)])
    taskset = ['taskset', '-c', a.cpu] if a.cpu else []
    rows = {}
    for name, params in CORPUS.items():
        stream = a.out / f'{name}.obu'
        run([str(encoder), str(stream)] + [str(v) for v in params])
        output = run(taskset + [str(host), str(stream), 'all', str(a.minimum), str(a.budget_ms)],
            text=True, capture_output=True).stdout
        engines = {row['engine']: row for row in map(json.loads, output.splitlines())}
        hashes = {row.get('hash') for row in engines.values()}
        if len(engines) != 3 or None in hashes or len(hashes) != 1:
            raise SystemExit(f'{name}: engines disagree or failed: {engines}')
        reference = engines['authored']['median_ns']
        for row in engines.values():
            row['median_vs_authored'] = round(row['median_ns'] / reference, 4)
        rows[name] = dict(bytes=stream.stat().st_size, params=params, engines=engines)
        print(f"{name}: authored {engines['authored']['median_ns'] / 1e6:.2f} ms, "
            f"libaom {engines['libaom']['median_ns'] / 1e6:.2f} ms, dav1d {engines['dav1d']['median_ns'] / 1e6:.2f} ms (median, bit-exact)")
    version = lambda command: run(command, text=True, capture_output=True).stdout.splitlines()[0]
    receipt = dict(
        command='build-queue -- sh tests/av1_benchmark.sh', exit_code=0,
        recorded_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        machine=dict(cpu=next((line.split(':', 1)[1].strip() for line in open('/proc/cpuinfo') if line.startswith('model name')), ''),
            kernel=platform.release(), pinned_cpu=a.cpu or None, compiler=version(cc + ['--version'])),
        authored=dict(form='generated C', cflags=' '.join(cflags), threads=1,
            simd='std/simd vectors, SSE2 to AVX2 chosen at run time'),
        libaom=dict(revision=PIN, threads=1, simd='runtime CPU detection (AVX2 and below on x86-64)'),
        dav1d=dict(revision=run(['git', '-C', str(a.dav1d_source), 'rev-parse', 'HEAD'], text=True,
            capture_output=True).stdout.strip(), threads=1, max_frame_delay=1, simd='runtime CPU detection'),
        method='one process per stream; each iteration is a one-shot decode (setup, decode, release) of one shown KEY '
            'temporal unit from memory; warm caches; min/median/mean over at least --minimum iterations and --budget-ms; '
            'allocation calls counted through malloc/calloc/realloc/posix_memalign wrappers for one decode',
        bit_exact=True, corpus=rows,
        limits=['single machine and single thread', 'synthetic libaom-encoded KEY frames only', 'warm cache',
            'no energy, RSS or cold-start measurement', 'authored SIMD covers CDEF, inverse transforms, deblocking and self-guided restoration; '
            'libaom and dav1d use their hand-written assembly'])
    (a.out / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')


if __name__ == '__main__':
    main()

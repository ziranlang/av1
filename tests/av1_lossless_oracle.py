#!/usr/bin/env python3
"""Real lossless AV1 fixtures, decoded only in a pinned test-only libaom host.

This generates ordinary AV1 syntax, never a made-up coefficient bitstream.
Product decoding has no reference-decoder/native-code dependency.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess

PIN = 'd772e334cc724105040382a977ebb10dfd393293'
HOST = r'''
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include "aom/aom_decoder.h"
#include "aom/aomdx.h"
#include "aom_dsp/entdec.h"
int __real_od_ec_decode_cdf_q15(od_ec_dec*,const uint16_t*,int);
static int symbols[4096], count;
int __wrap_od_ec_decode_cdf_q15(od_ec_dec *d,const uint16_t *cdf,int n) {
  int value=__real_od_ec_decode_cdf_q15(d,cdf,n);
  if(count<4096) symbols[count++]=value;
  return value;
}
int main(int argc,char **argv) {
  if(argc!=2)return 1; FILE *f=fopen(argv[1],"rb");if(!f)return 2;
  unsigned char data[4096];size_t size=fread(data,1,sizeof(data),f);fclose(f);
  if(!size||size==sizeof(data))return 3;
  aom_codec_ctx_t codec={0};aom_codec_dec_cfg_t cfg={0};cfg.threads=1;cfg.allow_lowbitdepth=1;
  if(aom_codec_dec_init(&codec,aom_codec_av1_dx(),&cfg,0))return 4;
  if(aom_codec_decode(&codec,data,size,NULL)) {
    const char *detail=aom_codec_error_detail(&codec);
    fprintf(stderr,"libaom decode: %s; %s; symbols",aom_codec_error(&codec),detail?detail:"");
    for(int i=0;i<count;i++)fprintf(stderr," %d",symbols[i]);fprintf(stderr,"\n");return 5;
  }
  aom_codec_iter_t iter=NULL;aom_image_t *image=aom_codec_get_frame(&codec,&iter);
  if(!image||image->d_w!=8||image->d_h!=8)return 6;
  printf("%u",image->bit_depth);
  for(int y=0;y<8;y++)for(int x=0;x<8;x++) {
    unsigned value=image->fmt&AOM_IMG_FMT_HIGHBITDEPTH?((uint16_t*)(image->planes[0]+y*image->stride[0]))[x]:image->planes[0][y*image->stride[0]+x];
    printf(" %u",value);
  }
  printf("\n"); for(int i=0;i<count;i++)printf("%d ",symbols[i]);printf("\n");
  return aom_codec_destroy(&codec);
}
'''


def run(command, **kwargs):
    return subprocess.run(command, check=True, **kwargs)


def digest(path):
    hasher = hashlib.sha256()
    with path.open('rb') as binary:
        for chunk in iter(lambda: binary.read(1024 * 1024), b''):
            hasher.update(chunk)
    return hasher.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--aom', type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    revision = run(['git', '-C', str(args.aom), 'rev-parse', 'HEAD'], text=True, capture_output=True).stdout.strip()
    if revision != PIN:
        raise SystemExit('pinned libaom revision mismatch')
    run(['git', '-C', str(args.aom), 'diff', '--quiet', 'HEAD', '--'])
    source = args.out / 'oracle.c'
    source.write_text(HOST)
    library = Path(os.environ.get('AV1_AOM_LIBRARY', str(args.root / 'build/native/aom/libaom.a')))
    config = Path(os.environ.get('AV1_AOM_CONFIG', str(args.root / 'build/native/aom')))
    host = args.out / 'oracle'
    run(shlex.split(os.environ.get('CC', 'cc')) + ['-std=c11', '-O2', '-I'+str(args.aom), '-I'+str(config),
        str(source), str(library), '-Wl,--wrap=od_ec_decode_cdf_q15', '-lm', '-o', str(host)])
    cases = []
    for depth in (8, 10, 12):
        limit = 1 << depth
        patterns = [('constant-'+str(value), [value]*64) for value in
            (0, 1, limit//4, limit//2-1, limit//2, limit//2+1, limit//2+2, limit-2, limit-1)]
        patterns += [('ramp', [(limit//2 + x + 3*y) % limit for y in range(8) for x in range(8)]),
            ('checker', [(limit//2 + ((x+y) % 2)*2-1) for y in range(8) for x in range(8)]),
            ('impulse', [limit//2 + (i==0) for i in range(64)])]
        for name, samples in patterns:
            label = f'{depth}-{name}'
            raw = args.out / (label+'.gray')
            raw.write_bytes(b''.join(value.to_bytes(1 if depth==8 else 2, 'little') for value in samples))
            obu = args.out / (label+'.obu')
            command = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'rawvideo',
                '-pixel_format', 'gray' if depth==8 else f'gray{depth}le', '-video_size', '8x8',
                '-framerate', '1', '-i', str(raw), '-frames:v', '1', '-c:v', 'libaom-av1',
                '-threads', '1', '-row-mt', '0', '-cpu-used', '8', '-crf', '0', '-b:v', '0', '-still-picture', '1',
                '-aom-params', 'enable-intrabc=0:enable-palette=0:enable-filter-intra=0:enable-restoration=0:enable-cdef=0',
                '-f', 'obu', '-y', str(obu)]
            run(command, stdout=subprocess.DEVNULL)
            output = run([str(host), str(obu)], text=True, capture_output=True).stdout.splitlines()
            plane = list(map(int, output[0].split()))
            if plane != [depth]+samples:
                raise SystemExit('lossless native plane mismatch: '+label)
            symbols = list(map(int, output[1].split()))
            cases.append(dict(name=label, depth=depth, data=list(obu.read_bytes()), samples=samples,
                symbols=symbols, command=command, encoded_sha256=digest(obu)))
    relative = os.path.relpath(args.root/'src/decoder.zi', args.out)
    lines = ['#import, file "'+relative+'";', '#program_export', 'main :: () -> s32 {',
        '    options := DefaultOptions()', '    budget := NewWorkerBudget(options.limits); options.workers = *budget']
    for i, case in enumerate(cases):
        data = case['data']
        lines += ['    {', '        // '+case['name'], '        data: ['+str(len(data))+']u8 = .['+','.join(map(str,data))+']',
            '        result := DecodeAv1YUV(BorrowBytes(data[:]), *options)']
        lines += ['        if !result.is_ok { return '+str(100+i)+' }',
            '        expected: [64]u16 = .['+','.join(map(str,case['samples']))+']',
            '        for pixel: 0..63 {', '            value := cast(u32)result.y.storage[pixel * result.y.sample_bytes]',
            '            if result.y.sample_bytes == 2 { value |= cast(u32)result.y.storage[pixel * 2 + 1] << 8 }',
            '            if value != expected[pixel] { return '+str(200+i)+' }', '        }',
            f'        for end: 0..{len(data)-1} {{',
            '            short := DecodeAv1YUV(BorrowBytes(data[:end]), *options)',
            '            if short.is_ok || short.error.code == DecodeOK || short.y.storage.count != 0 { return 401 }',
            '            if budget.active_workers != 0 || budget.working_bytes != 0 { return 402 }', '        }']
        lines += ['        if budget.active_workers != 0 || budget.working_bytes != 0 { return 400 }', '    }']
    lines += ['    return 0', '}']
    (args.out/'av1_lossless_vectors.zi').write_text('\n'.join(lines)+'\n')
    (args.out/'oracle.json').write_text(json.dumps(dict(revision=PIN,library_sha256=digest(library),
        encoder=run(['ffmpeg','-version'],text=True,capture_output=True).stdout.splitlines()[0],cases=cases),indent=2)+'\n')
    print(f'Pinned libaom: {len(cases)} real lossless planes decoded with every truncated prefix rejected')


if __name__ == '__main__':
    main()

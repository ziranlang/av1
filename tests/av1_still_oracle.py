#!/usr/bin/env python3
"""Real larger lossless stills: native plane and coding-block oracle.
Only this test host calls upstream; the authored decoder remains pure Ziran.
"""
import argparse
import json
import os
from pathlib import Path
import shlex
from av1_lossless_oracle import PIN, digest, run
from av1_frame_oracle import zi_bytes

HOST = r'''
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include "aom/aom_decoder.h"
#include "aom/aomdx.h"
#include "av1/decoder/decoder.h"
#include "av1/decoder/decodemv.h"
#include "av1/common/common_data.h"
static int blocks,bad_mode,rectangles;
void __real_av1_read_mode_info(AV1Decoder*,DecoderCodingBlock*,aom_reader*,int,int);
void __wrap_av1_read_mode_info(AV1Decoder *p,DecoderCodingBlock *d,aom_reader *r,int x,int y) {
  __real_av1_read_mode_info(p,d,r,x,y);
  const MB_MODE_INFO *block=d->xd.mi[0];blocks++;
  int mode=block->mode;
  int supported=mode>=DC_PRED&&mode<=PAETH_PRED;
  bad_mode+=!supported;
  rectangles+=block_size_wide[block->bsize]!=block_size_high[block->bsize];
}
int main(int argc,char **argv) {
  if(argc!=2)return 1;FILE *f=fopen(argv[1],"rb");if(!f)return 2;
  unsigned char data[65536];size_t bytes=fread(data,1,sizeof(data),f);fclose(f);
  if(!bytes||bytes==sizeof(data))return 3;
  aom_codec_ctx_t codec={0};aom_codec_dec_cfg_t cfg={0};cfg.threads=1;cfg.allow_lowbitdepth=1;
  if(aom_codec_dec_init(&codec,aom_codec_av1_dx(),&cfg,0))return 4;
  if(aom_codec_decode(&codec,data,bytes,NULL))return 5;
  aom_codec_iter_t it=NULL;aom_image_t *image=aom_codec_get_frame(&codec,&it);
  if(!image||image->d_w!=image->d_h||image->d_w>64||!blocks)return 6;
  printf("%u %u",image->d_w,image->bit_depth);
  for(unsigned y=0;y<image->d_h;y++)for(unsigned x=0;x<image->d_w;x++) {
    unsigned value=image->fmt&AOM_IMG_FMT_HIGHBITDEPTH?((uint16_t*)(image->planes[0]+y*image->stride[0]))[x]:image->planes[0][y*image->stride[0]+x];
    printf(" %u",value);
  }
  printf("\n%d %d %d\n",blocks,bad_mode,rectangles);
  return aom_codec_destroy(&codec);
}
'''


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,required=True);parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--aom',type=Path,required=True);args=parser.parse_args();args.out.mkdir(parents=True,exist_ok=True)
    if run(['git','-C',str(args.aom),'rev-parse','HEAD'],text=True,capture_output=True).stdout.strip()!=PIN:
        raise SystemExit('pinned libaom revision mismatch')
    run(['git','-C',str(args.aom),'diff','--quiet','HEAD','--'])
    source=args.out/'oracle.c';source.write_text(HOST);host=args.out/'oracle'
    library=Path(os.environ.get('AV1_AOM_LIBRARY',str(args.root/'build/native/aom/libaom.a')))
    config=Path(os.environ.get('AV1_AOM_CONFIG',str(args.root/'build/native/aom')))
    run(shlex.split(os.environ.get('CC','cc'))+['-std=c11','-O2','-I'+str(args.aom),'-I'+str(config),
        str(source),str(library),'-Wl,--wrap=av1_read_mode_info','-lm','-o',str(host)])
    cases=[]
    for depth in (8,10,12):
        for side in (16,32,64):
            maximum=(1<<depth)-1;middle=1<<(depth-1)
            patterns=[('constant',0,[middle+2]*(side*side)),
                ('checker',1,[middle+((x+y)%2)*2-1 for y in range(side) for x in range(side)]),
                ('quadrants',2,[(x//(side//2)+2*(y//(side//2)))*(maximum//3) for y in range(side) for x in range(side)]),
                ('noise',3,[((x*37+y*73+x*y*11)^123)&maximum for y in range(side) for x in range(side)])]
            for policy in (0,1,2):
                forced = policy != 0
                for name,pattern,samples in patterns:
                    label=f'{side}-{depth}-{name}-'+('split-dc' if policy==1 else ('intra-dc' if policy==2 else 'default'))
                    raw=args.out/(label+'.gray');raw.write_bytes(b''.join(v.to_bytes(1 if depth==8 else 2,'little') for v in samples))
                    obu=args.out/(label+'.obu')
                    params='enable-intrabc=0:enable-palette=0:enable-filter-intra=0:enable-restoration=0:enable-cdef=0'
                    if forced:params+=':enable-directional-intra=0:enable-smooth-intra=0:enable-paeth-intra=0'
                    if policy==1:params+=':min-partition-size=8:max-partition-size=8'
                    command=['ffmpeg','-hide_banner','-loglevel','error','-f','rawvideo','-pixel_format',
                        'gray' if depth==8 else f'gray{depth}le','-video_size',f'{side}x{side}','-framerate','1',
                        '-i',str(raw),'-frames:v','1','-c:v','libaom-av1','-threads','1','-row-mt','0',
                        '-cpu-used','8','-crf','0','-b:v','0','-still-picture','1','-aom-params',params,'-f','obu','-y',str(obu)]
                    run(command)
                    output=run([str(host),str(obu)],text=True,capture_output=True).stdout.splitlines()
                    if list(map(int,output[0].split()))!=[side,depth]+samples:raise SystemExit('native lossless plane mismatch: '+label)
                    blocks,modes,rectangles=map(int,output[1].split())
                    supported=modes==0
                    if forced and not supported:raise SystemExit('encoder forced DC contract failed: '+label)
                    cases.append(dict(name=label,side=side,depth=depth,pattern=pattern,supported=supported,
                        blocks=blocks,unsupported_modes=modes,rectangles=rectangles,command=command,
                        encoded_sha256=digest(obu),data=list(obu.read_bytes())))
    relative=os.path.relpath(args.root/'src/decoder.zi',args.out)
    lines=['#import, file "'+relative+'";','#program_export','main :: () -> s32 {']
    for i in range(len(cases)):lines.append(f'    if Case{i}() != 0 {{ return {i+1} }}')
    lines+=['    return 0','}',
        'PutBytes :: (target: []u8, offset: s64, source: []u8) { for i: 0..source.count - 1 { target[offset + i] = source[i] } }',
        'Expected :: (kind: s32, x: s32, y: s32, side: s32, depth: s32) -> u32 {',
        '    middle := cast(u32)1 << cast(u32)(depth - 1)',
        '    maximum := (cast(u32)1 << cast(u32)depth) - 1',
        '    if kind == 0 { return middle + 2 }',
        '    if kind == 1 { return middle + cast(u32)((x + y) % 2) * 2 - 1 }',
        '    if kind == 2 { return cast(u32)(x / (side / 2) + 2 * (y / (side / 2))) * (maximum / 3) }',
        '    return (cast(u32)(x * 37 + y * 73 + x * y * 11) ^ 123) & maximum','}']
    helpers=''
    for i,case in enumerate(cases):
        data,loader=zi_bytes('bytes',case['data'],f'Still{i}');helpers+=loader
        lines += [f'Case{i} :: () -> s32 {{','    // '+case['name'],data.rstrip(),
            '    options := DefaultOptions()',
            '    budget := NewWorkerBudget(options.limits); options.workers = *budget',
            '    result := DecodeAv1YUV(BorrowBytes(bytes[:]), *options)']
        if case['supported']:
            side=case['side'];sample=1 if case['depth']==8 else 2
            lines += [f'    if !result.is_ok || result.y.width != {side} || result.y.height != {side} || result.y.storage.count != {side*side*sample} || result.y.storage.capacity != {side*side*sample} {{ return 1 }}',
                f'    for y: 0..{side-1} {{',f'        for x: 0..{side-1} {{',
                '            at := y * result.y.stride + x * result.y.sample_bytes',
                '            value := cast(u32)result.y.storage[at]',
                '            if result.y.sample_bytes == 2 { value |= cast(u32)result.y.storage[at + 1] << 8 }',
                f'            if value != Expected({case["pattern"]}, cast(s32)x, cast(s32)y, {side}, {case["depth"]}) {{ return 2 }}','        }','    }']
        else:lines += ['    if result.is_ok || result.error.code != UnsupportedCapability { return 3 }']
        lines += ['    if budget.active_workers != 0 || budget.working_bytes != 0 { return 4 }','    return 0','}']
    (args.out/'av1_still_vectors.zi').write_text('\n'.join(lines)+'\n'+helpers)
    (args.out/'oracle.json').write_text(json.dumps(dict(revision=PIN,library_sha256=digest(library),
        encoder=run(['ffmpeg','-version'],text=True,capture_output=True).stdout.splitlines()[0],cases=cases),indent=2)+'\n')
    supported=[c for c in cases if c['supported']]
    print(f'Pinned larger still oracle: {len(cases)} real frames; {len(supported)} intra partition planes, {len(cases)-len(supported)} unsupported modes/shapes; {sum(c["blocks"] for c in supported)} coding blocks')


if __name__=='__main__':main()

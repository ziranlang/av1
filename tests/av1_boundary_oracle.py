#!/usr/bin/env python3
"""Native whole-frame oracle for actual and padded AV1 frame boundaries."""
import argparse
import json
import os
from pathlib import Path
import shlex
from av1_lossless_oracle import PIN, digest, run
from av1_still_oracle import HOST
from av1_frame_oracle import zi_bytes


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True);p.add_argument('--aom',type=Path,required=True)
    a=p.parse_args();a.out.mkdir(parents=True,exist_ok=True)
    if run(['git','-C',str(a.aom),'rev-parse','HEAD'],text=True,capture_output=True).stdout.strip()!=PIN:raise SystemExit('libaom pin mismatch')
    run(['git','-C',str(a.aom),'diff','--quiet','HEAD','--'])
    source=a.out/'oracle.c'
    source.write_text(HOST.replace('image->d_w!=image->d_h||image->d_w>64','image->d_w>64||image->d_h>64').replace(
        'printf("%u %u",image->d_w,image->bit_depth);','printf("%u %u %u",image->d_w,image->d_h,image->bit_depth);'))
    host=a.out/'oracle';library=Path(os.environ.get('AV1_AOM_LIBRARY',str(a.root/'build/native/aom/libaom.a')))
    config=Path(os.environ.get('AV1_AOM_CONFIG',str(a.root/'build/native/aom')))
    run(shlex.split(os.environ.get('CC','cc'))+['-std=c11','-O2','-I'+str(a.aom),'-I'+str(config),str(source),
        str(library),'-Wl,--wrap=av1_read_mode_info','-lm','-o',str(host)])
    cases=[]
    dimensions=[(1,1),(3,5),(7,8),(8,7),(9,9),(10,23),(23,10),(24,40),(40,24),(63,64),(64,63),(33,17),(17,33)]
    for depth in (8,10,12):
        maximum=(1<<depth)-1;middle=1<<(depth-1)
        for width,height in dimensions:
            for pattern in (0,1):
                samples=[middle+2 if pattern==0 else ((x*37+y*73+x*y*11)^123)&maximum for y in range(height) for x in range(width)]
                label=f'{width}x{height}-{depth}-{pattern}'
                raw=a.out/(label+'.gray');raw.write_bytes(b''.join(v.to_bytes(1 if depth==8 else 2,'little') for v in samples))
                obu=a.out/(label+'.obu')
                params='enable-intrabc=0:enable-palette=0:enable-filter-intra=0:enable-restoration=0:enable-cdef=0:enable-directional-intra=0:enable-smooth-intra=0:enable-paeth-intra=0'
                command=['ffmpeg','-hide_banner','-loglevel','error','-f','rawvideo','-pixel_format','gray' if depth==8 else f'gray{depth}le',
                    '-video_size',f'{width}x{height}','-framerate','1','-i',str(raw),'-frames:v','1','-c:v','libaom-av1',
                    '-threads','1','-row-mt','0','-cpu-used','8','-crf','0','-b:v','0','-still-picture','1','-aom-params',params,'-f','obu','-y',str(obu)]
                run(command)
                output=run([str(host),str(obu)],text=True,capture_output=True).stdout.splitlines()
                if list(map(int,output[0].split()))!=[width,height,depth]+samples:raise SystemExit('native boundary plane mismatch: '+label)
                blocks,modes,rectangles=map(int,output[1].split())
                if modes:raise SystemExit('forced intra encoder selected unsupported mode: '+label)
                cases.append(dict(name=label,width=width,height=height,depth=depth,pattern=pattern,
                    blocks=blocks,rectangles=rectangles,command=command,encoded_sha256=digest(obu),data=list(obu.read_bytes())))
    rel=os.path.relpath(a.root/'src/decoder.zi',a.out)
    lines=['#import, file "'+rel+'";','#program_export','main :: () -> s32 {']
    for i in range(len(cases)):lines.append(f'    if Case{i}() != 0 {{ return {i+1} }}')
    lines+=['    return 0','}',
        'PutBytes :: (target: []u8, offset: s64, source: []u8) { for i: 0..source.count - 1 { target[offset + i] = source[i] } }']
    helpers=''
    for i,c in enumerate(cases):
        data,loader=zi_bytes('bytes',c['data'],f'Boundary{i}');helpers+=loader
        sample=1 if c['depth']==8 else 2;size=c['width']*c['height']*sample
        lines+=[f'Case{i} :: () -> s32 {{',data.rstrip(),'    options := DefaultOptions()',
            '    budget := NewWorkerBudget(options.limits); options.workers = *budget',
            '    image := DecodeAv1YUV(BorrowBytes(bytes[:]), *options)',
            f'    if !image.is_ok || image.y.width != {c["width"]} || image.y.height != {c["height"]} || image.y.storage.count != {size} || image.y.storage.capacity != {size} {{ return 1 }}',
            f'    for y: 0..{c["height"]-1} {{',f'        for x: 0..{c["width"]-1} {{',
            '            at := y * image.y.stride + x * image.y.sample_bytes',
            '            value := cast(u32)image.y.storage[at]',
            '            if image.y.sample_bytes == 2 { value |= cast(u32)image.y.storage[at + 1] << 8 }']
        if c['pattern']==0:lines.append(f'            if value != {(1<<(c["depth"]-1))+2} {{ return 2 }}')
        else:lines.append(f'            if value != ((cast(u32)(x * 37 + y * 73 + x * y * 11) ^ 123) & {(1<<c["depth"])-1}) {{ return 2 }}')
        lines+=['        }','    }','    if budget.active_workers != 0 || budget.working_bytes != 0 { return 3 }']
        if size>1:
            lines += [f'    options.limits.max_output_bytes = {size-1}',
                '    bounded := DecodeAv1YUV(BorrowBytes(bytes[:]), *options)',
                '    if bounded.is_ok || bounded.error.code != LimitExceeded || bounded.y.storage.count != 0 || budget.active_workers != 0 || budget.working_bytes != 0 { return 4 }']
        lines+=['    return 0','}']
    (a.out/'av1_boundary_vectors.zi').write_text('\n'.join(lines)+'\n'+helpers)
    (a.out/'oracle.json').write_text(json.dumps(dict(revision=PIN,library_sha256=digest(library),dimensions=dimensions,
        frames=len(cases),blocks=sum(c['blocks'] for c in cases),rectangles=sum(c['rectangles'] for c in cases),
        encoder=run(['ffmpeg','-version'],text=True,capture_output=True).stdout.splitlines()[0],cases=cases),indent=2)+'\n')
    print(f'Pinned boundary oracle: {len(cases)} real tiny/odd/rectangular frames, 1..64 pixels,8/10/12-bit')


if __name__=='__main__':main()

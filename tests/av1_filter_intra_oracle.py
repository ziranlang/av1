#!/usr/bin/env python3
"""Genuine native-decoded AV1 filter-intra frames with pinned constant provenance."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
from av1_coefficient_edges import HOST as ENCODER
from av1_lossless_oracle import PIN, digest, run
from av1_still_oracle import HOST as DECODER
from av1_frame_oracle import zi_bytes

def cdfs(source, name, count, alphabet):
    text = source.split(name, 1)[1].split('= {', 1)[1]
    rows = []
    for match in re.finditer(r'AOM_CDF'+str(alphabet)+r'\(([^)]+)\)', text):
        rows.append([int(x.strip()) for x in match[1].split(',')]+[32768])
        if len(rows) == count:
            return rows
    raise SystemExit('missing pinned CDF: '+name)


def rectangles(part):
    if part == 0:return [(0,0,16,16)]
    if part == 1:return [(0,0,16,8),(8,0,16,8)]
    if part == 2:return [(0,0,8,16),(0,8,8,16)]
    if part == 3:return [(0,0,8,8),(0,8,8,8),(8,0,8,8),(8,8,8,8)]
    if part == 4:return [(0,0,8,8),(0,8,8,8),(8,0,16,8)]
    if part == 5:return [(0,0,16,8),(8,0,8,8),(8,8,8,8)]
    if part == 6:return [(0,0,8,8),(8,0,8,8),(0,8,8,16)]
    if part == 7:return [(0,0,8,16),(0,8,8,8),(8,8,8,8)]
    if part == 8:return [(row,0,16,4) for row in (0,4,8,12)]
    return [(0,column,4,16) for column in (0,4,8,12)]


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True);p.add_argument('--aom',type=Path,required=True)
    a=p.parse_args();a.out.mkdir(parents=True,exist_ok=True)
    if run(['git','-C',str(a.aom),'rev-parse','HEAD'],text=True,capture_output=True).stdout.strip()!=PIN:raise SystemExit('libaom pin mismatch')
    run(['git','-C',str(a.aom),'diff','--quiet','HEAD','--'])
    library=Path(os.environ.get('AV1_AOM_LIBRARY',str(a.root/'build/native/aom/libaom.a')))
    config=Path(os.environ.get('AV1_AOM_CONFIG',str(a.root/'build/native/aom')))
    compiler=shlex.split(os.environ.get('CC','cc'))+['-std=c11','-O2','-I'+str(a.aom),'-I'+str(config)]
    upstream=(a.aom/'av1/common/reconintra.c').read_text()
    source=a.out/'decoder.c'
    source.write_text(DECODER.replace('static int blocks,bad_mode,rectangles;', 'static int blocks,bad_mode,rectangles,filters;')
        .replace('int mode=block->mode;', 'int mode=block->mode;filters+=block->filter_intra_mode_info.use_filter_intra;')
        .replace('printf("\\n%d %d %d\\n",blocks,bad_mode,rectangles);',
                 'printf("\\n%d %d %d %d\\n",blocks,bad_mode,rectangles,filters);'))
    decoder=a.out/'decoder';run(compiler+[str(source),str(library),'-Wl,--wrap=av1_read_mode_info','-lm','-o',str(decoder)])
    source=a.out/'encoder.c';source.write_text(ENCODER);encoder=a.out/'encoder'
    run(compiler+[str(source),str(a.aom/'aom_dsp/entenc.c'),str(library),'-lm','-o',str(encoder)])
    entropy=(a.aom/'av1/common/entropymode.c').read_text()
    enabled=cdfs(entropy,'default_filter_intra_cdfs',22,2)
    modes=cdfs(entropy,'default_filter_intra_mode_cdf',1,5)[0]
    product=(a.root/'src/predict/filter_intra.zi').read_text()
    kernels=(a.root/'src/predict/intra_predict.zi').read_text()
    values=re.search(r'probabilities: \[22\]u16 = \.\[([^]]+)\]',product)
    if not values or list(map(int,values[1].split(',')))!=[row[0] for row in enabled]:raise SystemExit('filter enable CDF provenance mismatch')
    values=re.search(r'modes: \[6\]u16 = \.\[([^]]+)\]',product)
    if not values or list(map(int,values[1].split(',')))!=modes+[0]:raise SystemExit('filter mode CDF provenance mismatch')
    tap_text=upstream.split('av1_filter_intra_taps[FILTER_INTRA_MODES][8][8]) = {',1)[1].split('\n};',1)[0]
    native_taps=list(map(int,re.findall(r'-?\d+',tap_text)))
    values=re.search(r'taps: \[280\]s8 = \.\[([^]]+)\]',kernels,re.S)
    if len(native_taps)!=320 or not values or list(map(int,values[1].split(',')))!=[v for i,v in enumerate(native_taps) if i%8!=7]:
        raise SystemExit('filter tap provenance mismatch')
    shapes={(16,16):6,(16,8):5,(8,16):4,(8,8):3,(16,4):17,(4,16):16}
    base=cdfs(entropy,'default_partition_cdf',4,4)+cdfs(entropy,'default_partition_cdf',12,10)
    # Partition contexts0..3 use4 symbols, contexts4..15 use10.
    tables=base+[[31671,32768],[16515,32768],[4576,32768],
        [15588,17027,19338,20218,20682,21110,21825,23244,24189,28165,29093,30466,32768]]
    flag_table={shape:len(tables)+i for i,shape in enumerate(shapes)}
    tables += [enabled[shapes[shape]] for shape in shapes];mode_table=len(tables);tables.append(modes)
    def encode(operations):
        text=str(len(tables))+'\n'+''.join(str(len(row))+' '+' '.join(map(str,row))+'\n' for row in tables)
        text+=str(len(operations))+'\n'+''.join(f'{table} {symbol} 1\n' for table,symbol in operations)
        encoded=list(map(int,run([str(encoder)],input=text,text=True,capture_output=True).stdout.split()))
        if encoded[0]!=len(encoded)-1:raise SystemExit('entropy encoder length mismatch')
        return bytes(encoded[1:])
    cases=[];commands=[]
    for depth in (8,10,12):
        raw=a.out/f'{depth}.gray';raw.write_bytes(((1<<(depth-1))+2).to_bytes(1 if depth==8 else 2,'little')*256)
        obu=a.out/f'{depth}.obu'
        command=['ffmpeg','-hide_banner','-loglevel','error','-f','rawvideo','-pixel_format','gray' if depth==8 else f'gray{depth}le',
            '-video_size','16x16','-framerate','1','-i',str(raw),'-frames:v','1','-c:v','libaom-av1','-threads','1','-row-mt','0',
            '-cpu-used','8','-crf','0','-b:v','0','-still-picture','1','-aom-params',
            'enable-intrabc=0:enable-palette=0:enable-filter-intra=1:enable-restoration=0:enable-cdef=0','-f','obu','-y',str(obu)]
        run(command);commands.append(command);template=obu.read_bytes();at=0
        while at<len(template):
            start=at;header=template[at];at+=1
            if header&4:at+=1
            length=shift=0
            while True:
                byte=template[at];at+=1;length|=(byte&127)<<shift;shift+=7
                if not byte&128:break
            if header>>3&15==6:break
            at+=length
        if template[at:at+2]!=bytes([16,0]):raise SystemExit('native frame header template changed')
        for mode in range(-1,5):
            for part in range(10):
                operations=[(4,part)];skips=[[0]*4 for _ in range(4)];blocks=rectangles(part)
                for row,column,width,height in blocks:
                    if part==3:operations.append((0,0))
                    r=row//4;c=column//4
                    context=(skips[r-1][c] if r else 0)+(skips[r][c-1] if c else 0)
                    operations += [(16+context,1),(19,0),(flag_table[width,height],int(mode>=0))]
                    if mode>=0:operations.append((mode_table,mode))
                    for y in range(r,r+height//4):
                        for x in range(c,c+width//4):skips[y][x]=1
                tile=encode(operations)
                if len(tile)+2>=128:raise SystemExit('frame too large for one-byte test size')
                data=template[:start]+bytes([header,len(tile)+2])+template[at:at+2]+tile
                path=a.out/f'frame-{depth}-{mode}-{part}.obu';path.write_bytes(data)
                output=run([str(decoder),str(path)],text=True,capture_output=True).stdout.splitlines()
                values=list(map(int,output[0].split()));block_count,bad,rects,filters=map(int,output[1].split())
                if values[:2]!=[16,depth] or len(values)!=258 or block_count!=len(blocks) or bad or filters!=(len(blocks) if mode>=0 else 0):
                    raise SystemExit('native whole-frame filter syntax mismatch: '+path.name)
                cases.append(dict(depth=depth,mode=mode,part=part,data=list(data),samples=values[2:],filters=filters))
    encoded_cases=[]
    for depth in (8,10,12):
        for side in (16,32,64):
            for pattern in (0,1):
                maximum=(1<<depth)-1;middle=1<<(depth-1)
                samples=[(middle+((x+y)%2)*2-1) if pattern==0 else ((x*37+y*73+x*y*11)^123)&maximum
                    for y in range(side) for x in range(side)]
                raw=a.out/f'encoded-{depth}-{side}-{pattern}.gray'
                raw.write_bytes(b''.join(v.to_bytes(1 if depth==8 else 2,'little') for v in samples))
                obu=raw.with_suffix('.obu')
                command=['ffmpeg','-hide_banner','-loglevel','error','-f','rawvideo','-pixel_format','gray' if depth==8 else f'gray{depth}le',
                    '-video_size',f'{side}x{side}','-framerate','1','-i',str(raw),'-frames:v','1','-c:v','libaom-av1','-threads','1','-row-mt','0',
                    '-cpu-used','8','-crf','0','-b:v','0','-still-picture','1','-aom-params',
                    'enable-intrabc=0:enable-palette=0:enable-filter-intra=1:enable-restoration=0:enable-cdef=0:enable-directional-intra=0:enable-smooth-intra=0:enable-paeth-intra=0',
                    '-f','obu','-y',str(obu)]
                run(command);commands.append(command)
                output=run([str(decoder),str(obu)],text=True,capture_output=True).stdout.splitlines()
                if list(map(int,output[0].split()))!=[side,depth]+samples:raise SystemExit('native encoder lossless plane mismatch')
                blocks,bad,rects,filters=map(int,output[1].split())
                if bad:raise SystemExit('encoder selected an unsupported mode despite forced DC')
                encoded_cases.append(dict(depth=depth,side=side,pattern=pattern,data=list(obu.read_bytes()),filters=filters,blocks=blocks,
                    encoded_sha256=digest(obu)))
    rel=os.path.relpath(a.root/'src/decoder.zi',a.out);bounds=os.path.relpath(a.root/'tests/av1_filter_intra.zi',a.out)
    lines=['#import, file "'+rel+'";','#import, file "'+bounds+'";',
        'using Codec :: #import "image_codec/api"','#program_export','main :: () -> s32 {',
        '    if FilterIntraBoundaries() != 0 { return 99 }']
    for i in range(len(cases)):lines.append(f'    if Frame{i}() != 0 {{ return {i+1} }}')
    for i in range(len(encoded_cases)):lines.append(f'    if Encoded{i}() != 0 {{ return {i+1} }}')
    lines+=['    return 0','}']
    for i,c in enumerate(cases):
        data,_=zi_bytes('bytes',c['data'],f'Filter{i}')
        lines+=[f'Frame{i} :: () -> s32 {{',data.rstrip(),'    options := DefaultOptions()',
            '    budget := NewWorkerBudget(options.limits); options.workers = *budget',
            '    image := DecodeAv1YUV(BorrowBytes(bytes[:]), *options)',
            '    if !image.is_ok || !image.sequence.enable_filter_intra { return 1 }',
            '    expected: [256]u16 = .['+','.join(map(str,c['samples']))+']',
            '    for pixel: 0..255 {','        value := cast(u32)image.y.storage[pixel * image.y.sample_bytes]',
            '        if image.y.sample_bytes == 2 { value |= cast(u32)image.y.storage[pixel * 2 + 1] << 8 }',
            '        if value != expected[pixel] { return 2 }','    }',
            '    if budget.active_workers != 0 || budget.working_bytes != 0 { return 3 }',
            '    for end: 0..bytes.count - 1 {',
            '        truncated := DecodeAv1YUV(BorrowBytes(bytes[:end]), *options)',
            '        if truncated.is_ok || truncated.y.storage.count != 0 || budget.active_workers != 0 || budget.working_bytes != 0 { return 4 }','    }',
            '    return 0','}']
    helpers=''
    lines+=['PutBytes :: (target: []u8, offset: s64, source: []u8) { for i: 0..source.count - 1 { target[offset + i] = source[i] } }']
    for i,c in enumerate(encoded_cases):
        data,loader=zi_bytes('bytes',c['data'],f'FilterEncoded{i}');helpers+=loader
        side=c['side'];sample=1 if c['depth']==8 else 2;maximum=(1<<c['depth'])-1;middle=1<<(c['depth']-1)
        lines+=[f'Encoded{i} :: () -> s32 {{',data.rstrip(),'    options := DefaultOptions()',
            '    budget := NewWorkerBudget(options.limits); options.workers = *budget',
            '    image := DecodeAv1YUV(BorrowBytes(bytes[:]), *options)',
            f'    if !image.is_ok || !image.sequence.enable_filter_intra || image.y.storage.count != {side*side*sample} || image.y.storage.capacity != {side*side*sample} {{ return 1 }}',
            f'    for y: 0..{side-1} {{',f'        for x: 0..{side-1} {{',
            '            at := y * image.y.stride + x * image.y.sample_bytes',
            '            value := cast(u32)image.y.storage[at]',
            '            if image.y.sample_bytes == 2 { value |= cast(u32)image.y.storage[at + 1] << 8 }']
        expected=f'{middle} + cast(u32)((x + y) % 2) * 2 - 1' if c['pattern']==0 else f'(cast(u32)(x * 37 + y * 73 + x * y * 11) ^ 123) & {maximum}'
        lines += [f'            if value != ({expected}) {{ return 2 }}','        }','    }',
            '    if budget.active_workers != 0 || budget.working_bytes != 0 { return 3 }','    return 0','}']
    (a.out/'av1_filter_vectors.zi').write_text('\n'.join(lines)+'\n'+helpers)
    receipt=dict(revision=PIN,library_sha256=digest(library),native_whole_frames=len(cases),active_filter_blocks=sum(c['filters'] for c in cases),
        encoded_frames=len(encoded_cases),encoded_active_filter_blocks=sum(c['filters'] for c in encoded_cases),encoded_cases=encoded_cases,
        modes=list(range(5)),depths=[8,10,12],partition_types=list(range(10)),commands=commands,
        scope='filter-intra syntax and recursive prediction in native-decoded whole frames; kernels are covered by the intra prediction stage')
    (a.out/'oracle.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(f'Pinned filter-intra oracle: {len(cases)} native whole frames, {len(encoded_cases)} encoder frames ({receipt["encoded_active_filter_blocks"]} active filter blocks), all 5 filters at 8/10/12-bit')


if __name__=='__main__':main()

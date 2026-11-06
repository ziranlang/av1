#!/usr/bin/env python3
"""Pinned palette contexts and actual native-decoded standard palette frames."""
import argparse
import itertools
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from av1_coefficient_edges import HOST as ENCODER
from av1_filter_intra_oracle import cdfs, rectangles
from av1_frame_oracle import zi_bytes
from av1_lossless_oracle import PIN, digest, run
from av1_still_oracle import HOST as DECODER

CONTEXT = r'''
#include <stdio.h>
#include <stdint.h>
#include "av1/common/entropymode.h"
int main(void) {
  unsigned n,r,c,values[4];
  while(scanf("%u%u%u%u%u%u%u",&n,&r,&c,values,values+1,values+2,values+3)==7) {
    if(n<2||n>8||r>1||c>1||(!r&&!c))return 1;
    uint8_t map[4],order[8];
    for(int i=0;i<4;i++){if(values[i]>=n)return 2;map[i]=values[i];}
    int context=av1_get_palette_color_index_context(map,2,r,c,n,order,NULL);
    printf("%d",context);for(int i=0;i<8;i++)printf(" %u",order[i]);printf("\n");
  }return 0;
}
'''


def context(indices, stride, row, column, n):
    scores=[0]*8;order=list(range(8))
    if column:scores[indices[row*stride+column-1]]+=2
    if row and column:scores[indices[(row-1)*stride+column-1]]+=1
    if row:scores[indices[(row-1)*stride+column]]+=2
    for i in range(3):
        maximum=i
        for j in range(i+1,n):
            if scores[j]>scores[maximum]:maximum=j
        score=scores.pop(maximum);color=order.pop(maximum);scores.insert(i,score);order.insert(i,color)
    value=[-1,-1,0,-1,-1,4,3,2,1][scores[0]+2*scores[1]+2*scores[2]]
    if value<0:raise SystemExit('invalid independent palette context')
    return value,order


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True);p.add_argument('--aom',type=Path,required=True)
    a=p.parse_args();a.out.mkdir(parents=True,exist_ok=True)
    if run(['git','-C',str(a.aom),'rev-parse','HEAD'],text=True,capture_output=True).stdout.strip()!=PIN:raise SystemExit('libaom pin mismatch')
    run(['git','-C',str(a.aom),'diff','--quiet','HEAD','--'])
    library=Path(os.environ.get('AV1_AOM_LIBRARY',str(a.root/'build/native/aom/libaom.a')))
    config=Path(os.environ.get('AV1_AOM_CONFIG',str(a.root/'build/native/aom')))
    compiler=shlex.split(os.environ.get('CC','cc'))+['-std=c11','-O2','-I'+str(a.aom),'-I'+str(config)]
    source=a.out/'context.c';source.write_text(CONTEXT);host=a.out/'context'
    run(compiler+[str(source),str(library),'-lm','-o',str(host)])
    contexts=[]
    for n in range(2,9):
        for diagonal,above,left in itertools.product(range(n),repeat=3):contexts.append((n,1,1,[diagonal,above,left,0]))
        for color in range(n):contexts.extend([(n,0,1,[color,0,0,0]),(n,1,0,[color,0,0,0])])
    text=''.join(' '.join(map(str,[n,r,c]+data))+'\n' for n,r,c,data in contexts)
    native_contexts=run([str(host)],input=text,text=True,capture_output=True).stdout.splitlines()
    if len(native_contexts)!=len(contexts):raise SystemExit('native palette context count mismatch')
    for case,line in zip(contexts,native_contexts):
        n,r,c,data=case;ctx,order=context(data,2,r,c,n)
        if list(map(int,line.split()))!=[ctx]+order:raise SystemExit('independent context differs from native')
    source=a.out/'decoder.c'
    source.write_text(DECODER.replace('static int blocks,bad_mode,rectangles;','static int blocks,bad_mode,rectangles,palettes,non_skip;static size_t frame_header_bytes;')
        .replace('if(aom_codec_decode(&codec,data,bytes,NULL))return 5;',
            'if(aom_codec_decode(&codec,data,bytes,NULL)){fprintf(stderr,"native decode: %s / %s\\n",aom_codec_error(&codec),aom_codec_error_detail(&codec));return 5;}')
        .replace('int mode=block->mode;','int mode=block->mode; frame_header_bytes=p->frame_header_size; if(block->palette_mode_info.palette_size[0]){palettes++;non_skip+=!block->skip_txfm;}')
        .replace('printf("\\n%d %d %d\\n",blocks,bad_mode,rectangles);','printf("\\n%d %d %d %d %d\\n%zu\\n",blocks,bad_mode,rectangles,palettes,non_skip,frame_header_bytes);'))
    decoder=a.out/'decoder';run(compiler+[str(source),str(library),'-Wl,--wrap=av1_read_mode_info','-lm','-o',str(decoder)])
    source=a.out/'encoder.c';source.write_text(ENCODER.replace('count>32','count>128').replace('cdf[32][17]','cdf[128][17]').replace('n[32]','n[128]'))
    encoder=a.out/'encoder';run(compiler+[str(source),str(a.aom/'aom_dsp/entenc.c'),str(library),'-lm','-o',str(encoder)])
    entropy=(a.aom/'av1/common/entropymode.c').read_text();product=(a.root/'src/entropy/palette_cdfs.zi').read_text()
    presence=cdfs(entropy,'default_palette_y_mode_cdf',21,2);sizes=cdfs(entropy,'default_palette_y_size_cdf',7,7)
    color_rows=[row for alphabet in range(2,9) for row in cdfs(entropy,'default_palette_y_color_index_cdf',5,alphabet)]
    for field,rows,stride in [('enabled',presence,3),('size',sizes,8),('color',color_rows,9)]:
        expected=[v for row in rows for v in row+[0]*(stride-len(row))]
        literal=re.search(r'\b'+field+r': \[\d+\]u16 = \.\[([^]]+)\]',product,re.S)
        if not literal or list(map(int,literal[1].split(',')))!=expected:raise SystemExit('palette CDF provenance mismatch: '+field)
    tables=cdfs(entropy,'default_partition_cdf',4,4)+cdfs(entropy,'default_partition_cdf',12,10)
    tables += [[31671,32768],[16515,32768],[4576,32768],
        [15588,17027,19338,20218,20682,21110,21825,23244,24189,28165,29093,30466,32768]]
    presence_at=len(tables);tables+=presence;size_at=len(tables);tables+=sizes;color_at=len(tables);tables+=color_rows
    literal_at=len(tables);tables.append([16384,32768])
    def encode(operations):
        text=str(len(tables))+'\n'+''.join(str(len(row))+' '+' '.join(map(str,row))+'\n' for row in tables)
        text+=str(len(operations))+'\n'+''.join(f'{table} {symbol} {adapt}\n' for table,symbol,adapt in operations)
        encoded=list(map(int,run([str(encoder)],input=text,text=True,capture_output=True).stdout.split()))
        if encoded[0]!=len(encoded)-1:raise SystemExit('native entropy length mismatch')
        return bytes(encoded[1:])
    def literal(operations,value,bits):
        operations.extend((literal_at,value>>shift&1,0) for shift in range(bits-1,-1,-1))
    def uniform(operations,value,n):
        width=(n-1).bit_length();cutoff=(1<<width)-n
        if value<cutoff:literal(operations,value,width-1)
        else:
            combined=value+cutoff;literal(operations,combined>>1,width-1);literal(operations,combined&1,1)
    def command(raw,obu,side,depth,params):
        return ['ffmpeg','-hide_banner','-loglevel','error','-f','rawvideo','-pixel_format','gray' if depth==8 else f'gray{depth}le',
            '-video_size',f'{side}x{side}','-framerate','1','-i',str(raw),'-frames:v','1','-c:v','libaom-av1','-threads','1','-row-mt','0',
            '-cpu-used','8','-crf','0','-b:v','0','-still-picture','1','-aom-params',params,'-f','obu','-y',str(obu)]
    params='enable-intrabc=0:enable-palette=1:enable-filter-intra=0:enable-restoration=0:enable-cdef=0:enable-directional-intra=0:enable-smooth-intra=0:enable-paeth-intra=0'
    cases=[];commands=[];cache_uses=0
    for depth in (8,10,12):
        maximum=(1<<depth)-1
        raw=a.out/f'template-{depth}.gray';raw.write_bytes(b''.join((maximum*((x//2+y//2)%2)).to_bytes(1 if depth==8 else 2,'little') for y in range(16) for x in range(16)))
        obu=raw.with_suffix('.obu');cmd=command(raw,obu,16,depth,params+':min-partition-size=16:max-partition-size=16');run(cmd);commands.append(cmd)
        output=run([str(decoder),str(obu)],text=True,capture_output=True).stdout.splitlines()
        if int(output[1].split()[3])<1:raise SystemExit('encoder template did not activate palette syntax')
        frame_header_bytes=int(output[2])
        if not 1<=frame_header_bytes<=16:raise SystemExit('native frame header size out of test bounds')
        template=obu.read_bytes();at=0
        while at<len(template):
            start=at;header=template[at];at+=1
            if header&4:at+=1
            length=shift=0
            while True:
                byte=template[at];at+=1;length|=(byte&127)<<shift;shift+=7
                if not byte&128:break
            if header>>3&15==6:break
            at+=length
        frame_header=template[at:at+frame_header_bytes]
        for n in range(2,9):
            pool=[i*maximum//(n+1) for i in range(n+2)]
            for part in range(10):
                operations=[(4,part,1)];blocks=rectangles(part);skips=[[0]*4 for _ in range(4)];palettes=[[[] for _ in range(4)] for _ in range(4)];samples=[0]*256
                for block,(row,column,width,height) in enumerate(blocks):
                    if part==3:operations.append((0,0,1))
                    r=row//4;c=column//4;ctx=(skips[r-1][c] if r else 0)+(skips[r][c-1] if c else 0)
                    operations += [(16+ctx,1,1),(19,0,1)]
                    size_context=width.bit_length()+height.bit_length()-8
                    above=palettes[r-1][c] if r else [];left=palettes[r][c-1] if c else []
                    neighbor=int(bool(above))+int(bool(left));cache=sorted(set(above+left));colors=sorted(pool[(i+block)%(n+2)] for i in range(n))
                    operations += [(presence_at+size_context*3+neighbor,1,1),(size_at+size_context,n-2,1)]
                    selected=0
                    for color in cache:
                        if selected==n:break
                        take=color in colors;literal(operations,int(take),1);selected+=take;cache_uses+=take
                    new=[color for color in colors if color not in cache]
                    if new:
                        literal(operations,new[0],depth)
                        if len(new)>1:
                            literal(operations,3,2);bits=depth
                            for previous,color in zip(new,new[1:]):
                                literal(operations,color-previous-1,bits);bits=min(bits,(maximum-color-1).bit_length() if maximum-color>1 else 0)
                    indices=[(x*3+y*5+(x//3)*(y//2)+block)%n for y in range(height) for x in range(width)]
                    uniform(operations,indices[0],n)
                    for diagonal in range(1,width+height-1):
                        for x in range(min(diagonal,width-1),max(0,diagonal-height+1)-1,-1):
                            y=diagonal-x;ctx,order=context(indices,width,y,x,n)
                            operations.append((color_at+(n-2)*5+ctx,order.index(indices[y*width+x]),1))
                    for y in range(height):
                        for x in range(width):samples[(row+y)*16+column+x]=colors[indices[y*width+x]]
                    for y in range(r,r+height//4):
                        for x in range(c,c+width//4):skips[y][x]=1;palettes[y][x]=colors
                tile=encode(operations);length=len(tile)+len(frame_header);leb=[]
                while length>=128:leb.append((length&127)|128);length>>=7
                leb.append(length);data=template[:start]+bytes([header]+leb)+frame_header+tile
                path=a.out/f'frame-{depth}-{n}-{part}.obu';path.write_bytes(data)
                output=run([str(decoder),str(path)],text=True,capture_output=True).stdout.splitlines()
                if list(map(int,output[0].split()))!=[16,depth]+samples:raise SystemExit('native palette frame mismatch: '+path.name)
                count,bad,rects,active,non_skip=map(int,output[1].split())
                if count!=len(blocks) or bad or active!=len(blocks):raise SystemExit('native palette block syntax mismatch')
                cases.append(dict(depth=depth,n=n,part=part,data=list(data),samples=samples,blocks=count))
    encoded=[]
    for depth in (8,10,12):
        maximum=(1<<depth)-1
        for side in (1,4,7,8,9,16,32,64):
            for n in (2,4,8):
                for variant in ((0,1) if side==32 else (0,)):
                    samples=[min(maximum,(((x//2+y//2*3)%n)*maximum//(n-1))+(x*y%3 if variant else 0)) for y in range(side) for x in range(side)]
                    raw=a.out/f'encoded-{depth}-{side}-{n}-{variant}.gray';raw.write_bytes(b''.join(v.to_bytes(1 if depth==8 else 2,'little') for v in samples))
                    obu=raw.with_suffix('.obu');cmd=command(raw,obu,side,depth,params);run(cmd);commands.append(cmd)
                    output=run([str(decoder),str(obu)],text=True,capture_output=True).stdout.splitlines()
                    if list(map(int,output[0].split()))!=[side,depth]+samples:raise SystemExit('native encoder palette plane mismatch')
                    blocks,bad,rects,active,non_skip=map(int,output[1].split())
                    if bad:raise SystemExit('forced DC encoder selected an unsupported mode')
                    encoded.append(dict(depth=depth,side=side,n=n,variant=variant,data=list(obu.read_bytes()),active=active,non_skip=non_skip,
                        encoded_sha256=digest(obu)))
    if not sum(c['active'] for c in encoded):raise SystemExit('real encoder palette coverage missing')
    rel=os.path.relpath(a.root/'src/decoder.zi',a.out);palette=os.path.relpath(a.root/'src/predict/palette.zi',a.out);bounds=os.path.relpath(a.root/'tests/av1_palette.zi',a.out)
    lines=['#import, file "'+rel+'";','#import, file "'+palette+'";','#import, file "'+bounds+'";',
        'using Codec :: #import "image_codec/api"','#program_export','main :: () -> s32 {','    if PaletteBoundaries() != 0 { return 99 }']
    for group in range((len(contexts)+31)//32):lines.append(f'    if Context{group}() != 0 {{ return {group+1} }}')
    for i in range(len(cases)):lines.append(f'    if Frame{i}() != 0 {{ return {i%250+1} }}')
    for i in range(len(encoded)):lines.append(f'    if Encoded{i}() != 0 {{ return {i+1} }}')
    lines+=['    return 0','}']
    for group in range((len(contexts)+31)//32):
        lines.append(f'Context{group} :: () -> s32 {{')
        for i in range(group*32,min(group*32+32,len(contexts))):
            n,r,c,data=contexts[i];values=list(map(int,native_contexts[i].split()))
            lines += ['    {','        map: [4]u8 = .['+','.join(map(str,data))+']',
                f'        result := PaletteColorContext(map[:], 2, {r}, {c}, {n})',
                f'        if result.error.code != DecodeOK || result.context != {values[0]} {{ return 1 }}',
                '        expected: [8]u8 = .['+','.join(map(str,values[1:]))+']',
                '        for i: 0..7 { if result.order[i] != expected[i] { return 2 } }','    }']
        lines += ['    return 0','}']
    helpers='';lines += ['PutBytes :: (target: []u8, offset: s64, source: []u8) { for i: 0..source.count - 1 { target[offset + i] = source[i] } }']
    for i,c in enumerate(cases):
        data,loader=zi_bytes('bytes',c['data'],f'Palette{i}');helpers+=loader
        lines += [f'Frame{i} :: () -> s32 {{',data.rstrip(),'    options := DefaultOptions()',
            '    budget := NewWorkerBudget(options.limits); options.workers = *budget',
            '    image := DecodeAv1YUV(BorrowBytes(bytes[:]), *options)','    if !image.is_ok { return 1 }',
            '    expected: [256]u16 = .['+','.join(map(str,c['samples']))+']',
            '    for pixel: 0..255 {','        value := cast(u32)image.y.storage[pixel * image.y.sample_bytes]',
            '        if image.y.sample_bytes == 2 { value |= cast(u32)image.y.storage[pixel * 2 + 1] << 8 }',
            '        if value != expected[pixel] { return 2 }','    }',
            '    if budget.active_workers != 0 || budget.working_bytes != 0 { return 3 }']
        if c['part']==0:
            lines += ['    for end: 0..bytes.count - 1 {','        truncated := DecodeAv1YUV(BorrowBytes(bytes[:end]), *options)',
                '        if truncated.is_ok || truncated.y.storage.count != 0 || budget.active_workers != 0 || budget.working_bytes != 0 { return 4 }','    }']
        lines += ['    return 0','}']
    for i,c in enumerate(encoded):
        data,loader=zi_bytes('bytes',c['data'],f'PaletteEncoded{i}');helpers+=loader
        side=c['side'];sample=1 if c['depth']==8 else 2;maximum=(1<<c['depth'])-1
        lines += [f'Encoded{i} :: () -> s32 {{',data.rstrip(),'    options := DefaultOptions()',
            '    budget := NewWorkerBudget(options.limits); options.workers = *budget',
            '    image := DecodeAv1YUV(BorrowBytes(bytes[:]), *options)',
            f'    if !image.is_ok || image.y.storage.count != {side*side*sample} || image.y.storage.capacity != {side*side*sample} {{ return 1 }}',
            f'    for y: 0..{side-1} {{',f'        for x: 0..{side-1} {{',
            '            at := y * image.y.stride + x * image.y.sample_bytes',
            '            value := cast(u32)image.y.storage[at]',
            '            if image.y.sample_bytes == 2 { value |= cast(u32)image.y.storage[at + 1] << 8 }',
            f'            expected := cast(u32)((x / 2 + y / 2 * 3) % {c["n"]}) * {maximum} / {c["n"]-1}']
        if c['variant']:lines += [f'            expected += cast(u32)((x * y) % 3); if expected > {maximum} {{ expected = {maximum} }}']
        lines += ['            if value != expected { return 2 }','        }','    }',
            '    if budget.active_workers != 0 || budget.working_bytes != 0 { return 3 }','    return 0','}']
    (a.out/'av1_palette_vectors.zi').write_text('\n'.join(lines)+'\n'+helpers)
    receipt=dict(revision=PIN,library_sha256=digest(library),scalar_context_vectors=len(contexts),native_whole_frames=len(cases),
        cache_color_reuses=cache_uses,encoded_frames=len(encoded),encoded_active_palette_blocks=sum(c['active'] for c in encoded),
        encoded_non_skipped_palette_blocks=sum(c['non_skip'] for c in encoded),depths=[8,10,12],palette_sizes=list(range(2,9)),
        partition_types=list(range(10)),commands=commands,encoded=encoded,
        scope='bounded monochrome palette/cache/index-map plus lossless reconstruction; general AV1 pending')
    (a.out/'oracle.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(f'Pinned palette oracle: {len(contexts)} contexts,{len(cases)} native whole frames,{len(encoded)} encoder frames;{receipt["encoded_active_palette_blocks"]} active/{receipt["encoded_non_skipped_palette_blocks"]} non-skipped palette blocks')


if __name__=='__main__':
    try:main()
    except subprocess.CalledProcessError as error:
        if error.stderr:sys.stderr.write(error.stderr)
        raise

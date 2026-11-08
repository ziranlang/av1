#!/usr/bin/env python3
"""Directional intra modes in native-decoded pinned libaom whole frames."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
from av1_coefficient_edges import HOST as ENCODER
from av1_filter_intra_oracle import cdfs, rectangles
from av1_lossless_oracle import PIN, digest, run
from av1_still_oracle import HOST as DECODER
from av1_frame_oracle import zi_bytes

def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True);p.add_argument('--aom',type=Path,required=True)
    a=p.parse_args();a.out.mkdir(parents=True,exist_ok=True)
    if run(['git','-C',str(a.aom),'rev-parse','HEAD'],text=True,capture_output=True).stdout.strip()!=PIN:raise SystemExit('libaom pin mismatch')
    run(['git','-C',str(a.aom),'diff','--quiet','HEAD','--'])
    library=Path(os.environ.get('AV1_AOM_LIBRARY',str(a.root/'build/native/aom/libaom.a')))
    config=Path(os.environ.get('AV1_AOM_CONFIG',str(a.root/'build/native/aom')))
    compiler=shlex.split(os.environ.get('CC','cc'))+['-std=c11','-O2','-I'+str(a.aom),'-I'+str(config)]
    header=(a.aom/'av1/common/reconintra.h').read_text()
    text=header.split('dr_intra_derivative[90] = {',1)[1].split('};',1)[0]
    values=list(map(int,re.findall(r'\d+',re.sub(r'//[^\n]*','',text))))
    product=(a.root/'src/predict/intra_predict.zi').read_text()
    table=re.search(r'IntraDerivatives :: s16\.\[([^]]+)\]',product)
    if len(values)!=90 or not table or list(map(int,table[1].split(',')))!=values:raise SystemExit('derivative provenance mismatch')
    source=a.out/'decoder.c';source.write_text(DECODER.replace(
        'bad_mode+=!supported;','bad_mode+=mode<DC_PRED||mode>PAETH_PRED;'))
    decoder=a.out/'decoder';run(compiler+[str(source),str(library),'-Wl,--wrap=av1_read_mode_info','-lm','-o',str(decoder)])
    source=a.out/'encoder.c';source.write_text(ENCODER.replace('count>32','count>128').replace('cdf[32][17]','cdf[128][17]').replace('n[32]','n[128]'))
    encoder=a.out/'encoder';run(compiler+[str(source),str(a.aom/'aom_dsp/entenc.c'),str(library),'-lm','-o',str(encoder)])
    entropy=(a.aom/'av1/common/entropymode.c').read_text()
    tables=cdfs(entropy,'default_partition_cdf',4,4)+cdfs(entropy,'default_partition_cdf',12,10)
    tables += [[31671,32768],[16515,32768],[4576,32768]]
    keyframe=len(tables);tables+=cdfs(entropy,'default_kf_y_mode_cdf',25,13)
    angle_table=len(tables);tables+=cdfs(entropy,'default_angle_delta_cdf',8,7)
    def encode(operations):
        text=str(len(tables))+'\n'+''.join(str(len(row))+' '+' '.join(map(str,row))+'\n' for row in tables)
        text+=str(len(operations))+'\n'+''.join(f'{table} {symbol} 1\n' for table,symbol in operations)
        result=list(map(int,run([str(encoder)],input=text,text=True,capture_output=True).stdout.split()))
        if result[0]!=len(result)-1:raise SystemExit('native entropy length mismatch')
        return bytes(result[1:])
    cases=[];commands=[];contexts=[0,1,2,3,4,4,4,4,3,0,1,2,0]
    for depth in (8,10,12):
        for enabled in (False,True):
            raw=a.out/f'{depth}-{int(enabled)}.gray';raw.write_bytes(((1<<(depth-1))+2).to_bytes(1 if depth==8 else 2,'little')*256)
            obu=raw.with_suffix('.obu')
            command=['ffmpeg','-hide_banner','-loglevel','error','-f','rawvideo','-pixel_format','gray' if depth==8 else f'gray{depth}le',
                '-video_size','16x16','-framerate','1','-i',str(raw),'-frames:v','1','-c:v','libaom-av1','-threads','1','-row-mt','0',
                '-cpu-used','8','-crf','0','-b:v','0','-still-picture','1','-aom-params',
                f'enable-intrabc=0:enable-palette=0:enable-filter-intra=0:enable-intra-edge-filter={int(enabled)}:enable-restoration=0:enable-cdef=0','-f','obu','-y',str(obu)]
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
            if template[at:at+2]!=bytes([16,0]):raise SystemExit('native frame template changed')
            for mode in range(1,9):
                for delta in range(-3,4):
                    part=(mode*7+delta+3)%10;operations=[(4,part)];skips=[[0]*4 for _ in range(4)];modes=[[0]*4 for _ in range(4)]
                    blocks=rectangles(part)
                    for row,column,width,height in blocks:
                        if part==3:operations.append((0,0))
                        r=row//4;c=column//4
                        context=(skips[r-1][c] if r else 0)+(skips[r][c-1] if c else 0)
                        above=contexts[modes[r-1][c]] if r else 0;left=contexts[modes[r][c-1]] if c else 0
                        operations += [(16+context,1),(keyframe+above*5+left,mode),(angle_table+mode-1,delta+3)]
                        for y in range(r,r+height//4):
                            for x in range(c,c+width//4):skips[y][x]=1;modes[y][x]=mode
                    tile=encode(operations)
                    if len(tile)+2>=128:raise SystemExit('fixture one-byte length exceeded')
                    data=template[:start]+bytes([header,len(tile)+2])+template[at:at+2]+tile
                    path=a.out/f'frame-{depth}-{int(enabled)}-{mode}-{delta}.obu';path.write_bytes(data)
                    output=run([str(decoder),str(path)],text=True,capture_output=True).stdout.splitlines()
                    values=list(map(int,output[0].split()));count,bad,rects=map(int,output[1].split())
                    if values[:2]!=[16,depth] or len(values)!=258 or count!=len(blocks) or bad:raise SystemExit('native directional frame rejected')
                    cases.append(dict(depth=depth,mode=mode,delta=delta,enabled=enabled,part=part,data=list(data),samples=values[2:]))
    decoder_rel=os.path.relpath(a.root/'src/decoder.zi',a.out)
    lines=['#import, file "'+decoder_rel+'";',
        'using Codec :: #import "image_codec/api"','#program_export','main :: () -> s32 {']
    for i in range(len(cases)):lines.append(f'    if Frame{i}() != 0 {{ return {i%250+1} }}')
    lines+=['    return 0','}']
    for i,c in enumerate(cases):
        data,_=zi_bytes('bytes',c['data'],f'Directional{i}')
        lines+=[f'Frame{i} :: () -> s32 {{',data.rstrip(),'    options := DefaultOptions()',
            '    budget := NewWorkerBudget(options.limits); options.workers = *budget',
            '    image := DecodeAv1YUV(BorrowBytes(bytes[:]), *options)',
            f'    if !image.is_ok || image.sequence.enable_intra_edge_filter != {str(c["enabled"]).lower()} {{ return 1 }}',
            '    expected: [256]u16 = .['+','.join(map(str,c['samples']))+']',
            '    for pixel: 0..255 {','        value := cast(u32)image.y.storage[pixel * image.y.sample_bytes]',
            '        if image.y.sample_bytes == 2 { value |= cast(u32)image.y.storage[pixel * 2 + 1] << 8 }',
            '        if value != expected[pixel] { return 2 }','    }',
            '    if budget.active_workers != 0 || budget.working_bytes != 0 { return 3 }','    return 0','}']
    (a.out/'av1_directional_vectors.zi').write_text('\n'.join(lines)+'\n')
    (a.out/'oracle.json').write_text(json.dumps(dict(revision=PIN,library_sha256=digest(library),native_whole_frames=len(cases),depths=[8,10,12],directional_modes=list(range(1,9)),angle_deltas=list(range(-3,4)),
        filter_types=[0,1],edge_filter_enabled=[False,True],partition_types=list(range(10)),commands=commands,
        scope='directional modes and angle deltas in native-decoded whole frames; kernels are covered by the intra prediction stage'),indent=2)+'\n')
    print(f'Pinned directional oracle: {len(cases)} native whole frames; 8 modes x 7 deltas, edge filter/upsampling, 8/10/12-bit')


if __name__=='__main__':main()

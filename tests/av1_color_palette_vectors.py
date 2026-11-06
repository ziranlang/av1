#!/usr/bin/env python3
"""Standard color-palette boundary frames accepted by pinned native AV1 decode.

The pinned arithmetic encoder writes normative symbol streams. Native whole
planes must match independently constructed palette maps before Ziran runs.
No product encoder/decoder implementation calls either native library.
"""
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
from av1_chroma_oracle import HOST as NATIVE_DECODER
from av1_coefficient_edges import HOST as NATIVE_ENCODER
from av1_filter_intra_oracle import cdfs, rectangles
from av1_frame_oracle import zi_bytes
from av1_lossless_oracle import PIN, digest, run
from av1_palette_oracle import context


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True);p.add_argument('--aom',type=Path,required=True)
    a=p.parse_args();a.out.mkdir(parents=True,exist_ok=True)
    if run(['git','-C',str(a.aom),'rev-parse','HEAD'],text=True,capture_output=True).stdout.strip()!=PIN:raise SystemExit('libaom pin mismatch')
    run(['git','-C',str(a.aom),'diff','--quiet','HEAD','--'])
    library=Path(os.environ.get('AV1_AOM_LIBRARY',str(a.root/'build/native/aom/libaom.a')))
    config=Path(os.environ.get('AV1_AOM_CONFIG',str(a.root/'build/native/aom')))
    compiler=shlex.split(os.environ.get('CC','cc'))+['-std=c11','-O2','-I'+str(a.aom),'-I'+str(config)]
    source=a.out/'decoder.c'
    source.write_text(NATIVE_DECODER.replace('static int blocks,cfl,palette_y,palette_uv;',
        'static int blocks,cfl,palette_y,palette_uv;static size_t frame_header_bytes;')
        .replace('const MB_MODE_INFO *b=d->xd.mi[0];','frame_header_bytes=p->frame_header_size;const MB_MODE_INFO *b=d->xd.mi[0];')
        .replace('return aom_codec_destroy(&codec);','printf("%zu\\n",frame_header_bytes);return aom_codec_destroy(&codec);'))
    decoder=a.out/'decoder'
    run(compiler+[str(source),str(library),'-Wl,--wrap=av1_read_mode_info','-lm','-o',str(decoder)])
    source=a.out/'encoder.c'
    source.write_text(NATIVE_ENCODER.replace('count>32','count>256').replace('cdf[32][17]','cdf[256][17]').replace('n[32]','n[256]'))
    encoder=a.out/'encoder'
    run(compiler+[str(source),str(a.aom/'aom_dsp/entenc.c'),str(library),'-lm','-o',str(encoder)])
    text=(a.aom/'av1/common/entropymode.c').read_text()
    tables=cdfs(text,'default_partition_cdf',4,4)+cdfs(text,'default_partition_cdf',12,10)
    tables += [[31671,32768],[16515,32768],[4576,32768],
        [15588,17027,19338,20218,20682,21110,21825,23244,24189,28165,29093,30466,32768]]
    uv_no=len(tables);tables+=cdfs(text,'default_uv_mode_cdf',1,13)
    uv_yes=len(tables);tables+=cdfs(text,'default_uv_mode_cdf',1,14)
    yp=len(tables);tables+=cdfs(text,'default_palette_y_mode_cdf',21,2)
    ys=len(tables);tables+=cdfs(text,'default_palette_y_size_cdf',7,7)
    yc=len(tables);tables+=[row for n in range(2,9) for row in cdfs(text,'default_palette_y_color_index_cdf',5,n)]
    up=len(tables);tables+=cdfs(text,'default_palette_uv_mode_cdf',2,2)
    us=len(tables);tables+=cdfs(text,'default_palette_uv_size_cdf',7,7)
    uc=len(tables);tables+=[row for n in range(2,9) for row in cdfs(text,'default_palette_uv_color_index_cdf',5,n)]
    literal_at=len(tables);tables.append([16384,32768])
    def encode(operations):
        data=str(len(tables))+'\n'+''.join(str(len(row))+' '+' '.join(map(str,row))+'\n' for row in tables)
        data+=str(len(operations))+'\n'+''.join(f'{table} {symbol} {adapt}\n' for table,symbol,adapt in operations)
        encoded=list(map(int,run([str(encoder)],input=data,text=True,capture_output=True).stdout.split()))
        if encoded[0]!=len(encoded)-1:raise SystemExit('native arithmetic stream length mismatch')
        return bytes(encoded[1:])
    def literal(ops,value,bits):
        if not 0<=value<1<<bits:raise SystemExit('test literal outside normative width')
        ops.extend((literal_at,value>>shift&1,0) for shift in range(bits-1,-1,-1))
    def uniform(ops,value,n):
        width=(n-1).bit_length();cutoff=(1<<width)-n
        if value<cutoff:literal(ops,value,width-1)
        else:
            combined=value+cutoff;literal(ops,combined>>1,width-1);literal(ops,combined&1,1)
    reuses=[0,0];duplicates=positive=negative=zeros=wraps=0;cases=[];invalid=[];commands=[]
    for di,depth in enumerate((8,10,12)):
        maximum=(1<<depth)-1;middle=1<<(depth-1)
        for fi,(name,sx,sy) in enumerate((('420',1,1),('422',1,0),('444',0,0))):
            cw=16>>sx;ch=16>>sy
            planes=[[maximum*((x//2+y//2)%2) for y in range(h) for x in range(w)] for w,h in [(16,16),(cw,ch),(cw,ch)]]
            raw=a.out/f'template-{name}-{depth}.yuv';raw.write_bytes(b''.join(v.to_bytes(1 if depth==8 else 2,'little') for plane in planes for v in plane))
            obu=raw.with_suffix('.obu');pixel='yuv'+name+'p'+(f'{depth}le' if depth!=8 else '')
            command=['ffmpeg','-hide_banner','-loglevel','error','-f','rawvideo','-pixel_format',pixel,
                '-video_size','16x16','-framerate','1','-i',str(raw),'-frames:v','1','-c:v','libaom-av1',
                '-threads','1','-row-mt','0','-cpu-used','8','-crf','0','-b:v','0','-still-picture','1','-aom-params',
                'enable-intrabc=0:enable-palette=1:enable-filter-intra=0:enable-restoration=0:enable-cdef=0:min-partition-size=16:max-partition-size=16',
                '-f','obu','-y',str(obu)]
            run(command);commands.append(command)
            output=run([str(decoder),str(obu)],text=True,capture_output=True).stdout.splitlines()
            if not any(map(int,output[4].split()[2:])):raise SystemExit('template did not enable palette syntax')
            prefix_size=int(output[5]);template=obu.read_bytes();at=0
            while at<len(template):
                start=at;header=template[at];at+=1
                if header&4:at+=1
                length=shift=0
                while True:
                    byte=template[at];at+=1;length|=(byte&127)<<shift;shift+=7
                    if not byte&128:break
                if header>>3&15==6:break
                at+=length
            else:raise SystemExit('native template has no combined frame OBU')
            if not 1<=prefix_size<=16:raise SystemExit('native frame header size outside test bounds')
            prefix=template[at:at+prefix_size]
            for n in range(2,9):
                for variant in range(3):
                    before=(list(reuses),duplicates,positive,negative,zeros,wraps)
                    part=(n-2+variant*3+fi+di*4)%10;blocks=rectangles(part)
                    ops=[(4,part,1)];skips=[[0]*4 for _ in range(4)]
                    palettes=[[[[] for _ in range(4)] for _ in range(4)] for _ in range(2)]
                    expected=[[middle]*256,[0]*(cw*ch),[0]*(cw*ch)]
                    active_uv=0;active_y=variant!=2
                    for block,(row,column,width,height) in enumerate(blocks):
                        if part==3:ops.append((0,0,1))
                        r=row//4;c=column//4
                        skipctx=(skips[r-1][c] if r else 0)+(skips[r][c-1] if c else 0)
                        ops.extend([(16+skipctx,1,1),(19,0,1)])
                        has_uv=not(height==4 and sy and r%2==0) and not(width==4 and sx and c%2==0)
                        uw=max(4,width>>sx);uh=max(4,height>>sy)
                        if has_uv:ops.append((uv_yes if uw==4 and uh==4 else uv_no,0,1))
                        size_ctx=width.bit_length()+height.bit_length()-8
                        y_above=palettes[0][r-1][c] if r else [];y_left=palettes[0][r][c-1] if c else []
                        ops.append((yp+size_ctx*3+int(bool(y_above))+int(bool(y_left)),int(active_y),1))
                        ycolors=[];ucolors=[];vcolors=[]
                        for plane,enabled in ((0,active_y),(1,has_uv)):
                            if plane and has_uv:ops.append((up+int(active_y),1,1));active_uv+=1
                            if not enabled:continue
                            ops.append(((us if plane else ys)+size_ctx,n-2,1))
                            above=palettes[plane][r-1][c] if r else [];left=palettes[plane][r][c-1] if c else []
                            cache=sorted(set(above+left));pool=[i*maximum//(n+2) for i in range(n+3)]
                            colors=sorted(pool[((i//2 if plane and variant==1 else i)+block)%(n+3)] for i in range(n))
                            if plane and variant==1:duplicates+=len(colors)-len(set(colors))
                            new=list(colors);selected=0
                            for color in cache:
                                if selected==n:break
                                take=color in new;literal(ops,int(take),1)
                                if take:new.remove(color);selected+=1;reuses[plane]+=1
                            if new:
                                literal(ops,new[0],depth)
                                if len(new)>1:
                                    literal(ops,3,2);bits=depth
                                    for previous,color in zip(new,new[1:]):
                                        literal(ops,color-previous-(not plane),bits)
                                        bits=min(bits,(maximum-color-(not plane)).bit_length())
                            if not plane:ycolors=colors;continue
                            ucolors=colors
                            if variant==0:
                                literal(ops,0,1);vcolors=[(i*97+block*23)&maximum for i in range(n)]
                                for value in vcolors:literal(ops,value,depth)
                            else:
                                literal(ops,1,1);literal(ops,3,2)
                                vcolors=[maximum-2 if variant==1 else 1];literal(ops,vcolors[0],depth)
                                deltas=[5,-7,0,13,-15,31,-29] if variant==1 else [-5,7,0,-13,15,-31,29]
                                for delta in deltas[:n-1]:
                                    literal(ops,abs(delta),depth-1)
                                    if delta:literal(ops,int(delta<0),1)
                                    raw_value=vcolors[-1]+delta;wraps+=not 0<=raw_value<=maximum
                                    positive+=delta>0;negative+=delta<0;zeros+=delta==0
                                    vcolors.append(raw_value%(maximum+1))
                        for plane,colors,bw,bh,px,py in [(0,ycolors,width,height,column,row),
                            (1,ucolors,uw,uh,(c>>sx)*4,(r>>sy)*4)]:
                            if not colors:continue
                            indices=[(x*3+y*5+(x//3)*(y//2)+block+plane)%n for y in range(bh) for x in range(bw)]
                            uniform(ops,indices[0],n)
                            for diagonal in range(1,bw+bh-1):
                                for x in range(min(diagonal,bw-1),max(0,diagonal-bh+1)-1,-1):
                                    y=diagonal-x;ctx,order=context(indices,bw,y,x,n)
                                    ops.append(((uc if plane else yc)+(n-2)*5+ctx,order.index(indices[y*bw+x]),1))
                            stride=cw if plane else 16
                            for y in range(bh):
                                for x in range(bw):
                                    index=indices[y*bw+x];expected[plane][(py+y)*stride+px+x]=colors[index]
                                    if plane:expected[2][(py+y)*stride+px+x]=vcolors[index]
                        for y in range(r,r+height//4):
                            for x in range(c,c+width//4):
                                skips[y][x]=1;palettes[0][y][x]=ycolors;palettes[1][y][x]=ucolors
                    tile=encode(ops);length=len(prefix)+len(tile);leb=[]
                    while length>=128:leb.append(length&127|128);length>>=7
                    leb.append(length);data=template[:start]+bytes([header]+leb)+prefix+tile
                    label=f'{name}-{depth}-{n}-{variant}-{part}';path=a.out/(label+'.obu');path.write_bytes(data)
                    decoded=subprocess.run([str(decoder),str(path)],text=True,capture_output=True)
                    if name=='422' and part in (2,6,7,9):
                        if decoded.returncode!=5:raise SystemExit('native accepted forbidden4:2:2 partition: '+label)
                        invalid.append(dict(label=label,data=list(data),sha256=digest(path)))
                        reuses,duplicates,positive,negative,zeros,wraps=before
                        continue
                    decoded.check_returncode();output=decoded.stdout.splitlines()
                    if list(map(int,output[0].split()))!=[16,16,depth,sx,sy]:raise SystemExit('native boundary format mismatch: '+label)
                    for plane in range(3):
                        if list(map(int,output[plane+1].split()))!=expected[plane]:raise SystemExit('native boundary plane mismatch: '+label+f' plane{plane}')
                    if list(map(int,output[4].split()))!=[len(blocks),0,len(blocks)*active_y,active_uv]:raise SystemExit('native palette syntax mismatch: '+label)
                    cases.append(dict(label=label,depth=depth,sx=sx,sy=sy,n=n,variant=variant,partition=part,
                        data=list(data),planes=expected,sha256=digest(path),y_blocks=len(blocks)*active_y,uv_blocks=active_uv))
    if {c['partition'] for c in cases}!=set(range(10)) or not all(reuses) or not all((duplicates,positive,negative,zeros,wraps)):
        raise SystemExit('native color palette boundary coverage incomplete')
    rel=os.path.relpath(a.root/'src/decoder.zi',a.out)
    lines=['#import, file "'+rel+'";','using Codec :: #import "image_codec/api"','#program_export','main :: () -> s32 {']
    lines += [f'    if Frame{i}() != 0 {{ return {i%250+1} }}' for i in range(len(cases))]
    lines += [f'    if Invalid{i}() != 0 {{ return {i%250+1} }}' for i in range(len(invalid))]+['    return 0','}',
        'PutBytes :: (target: []u8, offset: s64, source: []u8) { for i: 0..source.count - 1 { target[offset + i] = source[i] } }']
    helpers=''
    for i,c in enumerate(cases):
        data,loader=zi_bytes('bytes',c['data'],f'ColorPalette{i}');helpers+=loader
        lines += [f'Frame{i} :: () -> s32 {{','    // '+c['label'],data.rstrip(),'    options := DefaultOptions()',
            '    budget := NewWorkerBudget(options.limits); options.workers = *budget',
            '    image := DecodeAv1YUV(BorrowBytes(bytes[:]), *options)','    if !image.is_ok { return 100 + cast(s32)image.error.code }']
        for plane,name in enumerate(('y','u','v')):
            values=c['planes'][plane]
            lines += [f'    expected{plane}: [{len(values)}]u16 = .['+','.join(map(str,values))+']',f'    for pixel: 0..{len(values)-1} {{',
                f'        at := pixel * image.{name}.sample_bytes; value := cast(u32)image.{name}.storage[at]',
                f'        if image.{name}.sample_bytes == 2 {{ value |= cast(u32)image.{name}.storage[at + 1] << 8 }}',
                f'        if value != expected{plane}[pixel] {{ return {plane+1} }}','    }']
        lines += ['    if budget.active_workers != 0 || budget.working_bytes != 0 { return 4 }']
        if c['n']==2 and c['variant']==0:
            lines += ['    for end: 0..bytes.count - 1 {','        truncated := DecodeAv1YUV(BorrowBytes(bytes[:end]), *options)',
                '        if truncated.is_ok || truncated.y.storage.count != 0 || truncated.u.storage.count != 0 || truncated.v.storage.count != 0 || budget.active_workers != 0 || budget.working_bytes != 0 { return 5 }','    }']
        lines += ['    return 0','}']
    for i,c in enumerate(invalid):
        data,loader=zi_bytes('bytes',c['data'],f'InvalidColorPalette{i}');helpers+=loader
        lines += [f'Invalid{i} :: () -> s32 {{','    // '+c['label'],data.rstrip(),'    options := DefaultOptions()',
            '    budget := NewWorkerBudget(options.limits); options.workers = *budget',
            '    image := DecodeAv1YUV(BorrowBytes(bytes[:]), *options)',
            '    if image.is_ok || image.error.code != MalformedInput || image.y.storage.count != 0 || image.u.storage.count != 0 || image.v.storage.count != 0 || budget.active_workers != 0 || budget.working_bytes != 0 { return 1 }',
            '    return 0','}']
    (a.out/'av1_color_palette_vectors.zi').write_text('\n'.join(lines)+'\n'+helpers)
    (a.out/'oracle.json').write_text(json.dumps(dict(revision=PIN,library_sha256=digest(library),frames=len(cases),
        palette_sizes=list(range(2,9)),partition_types=list(range(10)),formats=['420','422','444'],depths=[8,10,12],
        cache_reuses_y=reuses[0],cache_reuses_u=reuses[1],duplicate_u_values=duplicates,
        positive_v_deltas=positive,negative_v_deltas=negative,zero_v_deltas=zeros,v_wraps=wraps,
        commands=commands,cases=cases,invalid_partition_frames=invalid,scope='native accepted standard palette boundary frames and forbidden4:2:2 rejection; not full AV1 conformance'),indent=2)+'\n')
    print(f'Pinned color palette boundaries: {len(cases)} native whole frames,2..8 colors/all10 partitions;{reuses[1]} U cache reuses,{duplicates} duplicate U values,{wraps} signed V wraps')


if __name__=='__main__':main()

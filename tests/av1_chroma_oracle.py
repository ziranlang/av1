#!/usr/bin/env python3
"""Real lossless color planes from pinned native AV1 decode, not RGB roundtrips."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
from av1_lossless_oracle import PIN, digest, run
from av1_frame_oracle import zi_bytes
from av1_filter_intra_oracle import cdfs

HOST=r'''
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include "aom/aom_decoder.h"
#include "aom/aomdx.h"
#include "av1/decoder/decoder.h"
#include "av1/decoder/decodemv.h"
static int blocks,cfl,palette_y,palette_uv;
void __real_av1_read_mode_info(AV1Decoder*,DecoderCodingBlock*,aom_reader*,int,int);
void __wrap_av1_read_mode_info(AV1Decoder *p,DecoderCodingBlock *d,aom_reader *r,int x,int y) {
  __real_av1_read_mode_info(p,d,r,x,y);const MB_MODE_INFO *b=d->xd.mi[0];blocks++;
  cfl+=d->xd.is_chroma_ref&&b->uv_mode==UV_CFL_PRED;palette_y+=b->palette_mode_info.palette_size[0]>0;palette_uv+=d->xd.is_chroma_ref&&b->palette_mode_info.palette_size[1]>0;
}
int main(int argc,char **argv) {
  if(argc!=2)return 1;FILE *f=fopen(argv[1],"rb");if(!f)return 2;
  unsigned char data[65536];size_t bytes=fread(data,1,sizeof(data),f);fclose(f);if(!bytes||bytes==sizeof(data))return 3;
  aom_codec_ctx_t codec={0};aom_codec_dec_cfg_t cfg={0};cfg.threads=1;cfg.allow_lowbitdepth=1;
  if(aom_codec_dec_init(&codec,aom_codec_av1_dx(),&cfg,0))return 4;
  if(aom_codec_decode(&codec,data,bytes,NULL)){fprintf(stderr,"native decode: %s\n",aom_codec_error(&codec));return 5;}
  aom_codec_iter_t it=NULL;aom_image_t *image=aom_codec_get_frame(&codec,&it);
  if(!image||image->d_w>64||image->d_h>64||!image->planes[1]||!image->planes[2]||!blocks)return 6;
  printf("%u %u %u %u %u\n",image->d_w,image->d_h,image->bit_depth,image->x_chroma_shift,image->y_chroma_shift);
  for(int plane=0;plane<3;plane++) {
    unsigned sx=plane?image->x_chroma_shift:0,sy=plane?image->y_chroma_shift:0;
    unsigned width=(image->d_w+((1u<<sx)-1))>>sx,height=(image->d_h+((1u<<sy)-1))>>sy;
    for(unsigned y=0;y<height;y++)for(unsigned x=0;x<width;x++) {
      unsigned value=image->fmt&AOM_IMG_FMT_HIGHBITDEPTH?((uint16_t*)(image->planes[plane]+y*image->stride[plane]))[x]:image->planes[plane][y*image->stride[plane]+x];
      printf("%u ",value);
    }printf("\n");
  }
  printf("%d %d %d %d\n",blocks,cfl,palette_y,palette_uv);return aom_codec_destroy(&codec);
}
'''


def expected(pattern,x,y,plane,width,height,sx,sy,depth):
    maximum=(1<<depth)-1;middle=1<<(depth-1)
    if pattern==0:return middle+2 if plane==0 else (middle//2 if plane==1 else maximum-3)
    if pattern==1:return middle+((x+y+plane)%2)*2-1
    if pattern>=3:
        index=(x//2+y//2*3)%8
        if pattern==3:return [0,maximum//3,maximum*2//3,maximum][(index+plane)%4]
        if pattern==4:
            if plane==0:return middle
            if plane==1:return (index//2)*maximum//3
            return [maximum-1,1,middle+3,middle-3,0,maximum,7,maximum-7][index]
        if pattern==5:
            if plane==0:return maximum*(index%2)
            return ((x*13+y*7+plane*29)^123)&maximum
        if pattern==6:
            if plane==0:return ((x*11+y*7+x*y*3)^123)&maximum
            return maximum*(index%4)//3 if plane==1 else maximum-maximum*(index%4)//3
        if pattern==7:
            value=maximum*((x//2+y//2*3)%4)//3
            return min(maximum,value+(x*y+plane)%3)
        raise ValueError('unknown palette pattern')
    def luma(x,y):return ((x*11+y*7+x*y*3)^123)&maximum
    if not plane:return luma(x,y)
    value=sum(luma(min(width-1,x*(1+sx)+dx),min(height-1,y*(1+sy)+dy)) for dy in range(sy+1) for dx in range(sx+1))//((1+sx)*(1+sy))
    return value if plane==1 else maximum-value


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True);p.add_argument('--aom',type=Path,required=True)
    p.add_argument('--palettes',action='store_true')
    p.add_argument('--key-frames',action='store_true')
    a=p.parse_args();a.out.mkdir(parents=True,exist_ok=True)
    if run(['git','-C',str(a.aom),'rev-parse','HEAD'],text=True,capture_output=True).stdout.strip()!=PIN:raise SystemExit('libaom pin mismatch')
    run(['git','-C',str(a.aom),'diff','--quiet','HEAD','--'])
    library=Path(os.environ.get('AV1_AOM_LIBRARY',str(a.root/'build/native/aom/libaom.a')))
    config=Path(os.environ.get('AV1_AOM_CONFIG',str(a.root/'build/native/aom')))
    native=HOST
    if a.key_frames:
        native=native.replace('static int blocks,cfl,palette_y,palette_uv;',
            'static int blocks,cfl,palette_y,palette_uv;static unsigned controls[11];')
        native=native.replace('blocks++;',
            'blocks++;controls[0]=p->common.seq_params->reduced_still_picture_hdr;controls[1]=p->common.current_frame.frame_type;controls[2]=p->common.show_frame;controls[3]=p->common.features.error_resilient_mode;controls[4]=p->common.features.primary_ref_frame;controls[5]=p->common.current_frame.refresh_frame_flags;controls[6]=p->common.features.disable_cdf_update;controls[7]=p->common.features.cur_frame_force_integer_mv;controls[8]=p->common.current_frame_id;controls[9]=p->common.current_frame.order_hint;controls[10]=p->common.frame_presentation_time;')
        native=native.replace('return aom_codec_destroy(&codec);','for(int i=0;i<11;i++)printf("%u ",controls[i]);printf("\\n");return aom_codec_destroy(&codec);')
    source=a.out/'oracle.c';source.write_text(native);host=a.out/'oracle'
    run(shlex.split(os.environ.get('CC','cc'))+['-std=c11','-O2','-I'+str(a.aom),'-I'+str(config),str(source),str(library),'-Wl,--wrap=av1_read_mode_info','-lm','-o',str(host)])
    mode_source=(a.aom/'av1/common/entropymode.c').read_text();mode_product=(a.root/'src/entropy/chroma_cdfs.zi').read_text()
    for field,name,count,alphabet in [('without_cfl','default_uv_mode_cdf',13,13),('with_cfl','default_uv_mode_cdf',13,14),
        ('signs','default_cfl_sign_cdf',1,8),('alphas','default_cfl_alpha_cdf',6,16),('palette','default_palette_uv_mode_cdf',2,2)]:
        rows=cdfs(mode_source,name,count,alphabet)
        literal=re.search(r'\b'+field+r': \[\d+\]u16 = \.\[([^]]+)\]',mode_product,re.S)
        if not literal or list(map(int,literal[1].split(',')))!=[v for row in rows for v in row+[0]]:raise SystemExit('chroma mode CDF provenance mismatch: '+field)
    if a.palettes:
        uv_product=(a.root/'src/entropy/palette_uv_cdfs.zi').read_text()
        for field,rows,stride in [('size',cdfs(mode_source,'default_palette_uv_size_cdf',7,7),8),
            ('color',[row for n in range(2,9) for row in cdfs(mode_source,'default_palette_uv_color_index_cdf',5,n)],9)]:
            literal=re.search(r'\b'+field+r': \[\d+\]u16 = \.\[([^]]+)\]',uv_product,re.S)
            if not literal or list(map(int,literal[1].split(',')))!=[v for row in rows for v in row+[0]*(stride-len(row))]:raise SystemExit('UV palette CDF provenance mismatch: '+field)
    cases=[]
    dimensions=[(4,4),(8,8),(16,16),(32,32),(64,64),(1,1),(3,5),(9,13),(23,10)]
    if a.key_frames:dimensions=[(4,4),(8,8),(16,16),(32,32),(64,64),(3,5),(9,13)]
    if a.palettes:dimensions=[(8,8),(16,16),(32,32),(64,64),(3,5),(9,13),(23,10)]
    for depth in (8,10,12):
        for name,sx,sy in [('420',1,1),('422',1,0),('444',0,0)]:
            for width,height in dimensions:
                for pattern in ((3,4,5,6,7) if a.palettes else (0,1,2)):
                    if not a.palettes and width==64 and pattern!=2:continue
                    if not a.palettes and width not in (4,8,16,32,64) and pattern==1:continue
                    planes=[]
                    for plane in range(3):
                        pw=(width+sx)//(1+sx) if plane else width;ph=(height+sy)//(1+sy) if plane else height
                        planes.append([expected(pattern,x,y,plane,width,height,sx,sy,depth) for y in range(ph) for x in range(pw)])
                    label=f'{name}-{depth}-{width}x{height}-{pattern}';raw=a.out/(label+'.yuv');obu=a.out/(label+'.obu')
                    raw.write_bytes(b''.join(v.to_bytes(1 if depth==8 else 2,'little') for samples in planes for v in samples))
                    pixel='yuv'+name+'p'+(f'{depth}le' if depth!=8 else '')
                    command=['ffmpeg','-hide_banner','-loglevel','error','-f','rawvideo','-pixel_format',pixel,
                        '-video_size',f'{width}x{height}','-framerate','1','-i',str(raw),'-frames:v','1','-c:v','libaom-av1',
                        '-threads','1','-row-mt','0','-cpu-used','8','-crf','0','-b:v','0','-still-picture','0' if a.key_frames else '1','-aom-params',
                        'enable-intrabc=0:enable-palette='+str(int(a.palettes))+':enable-filter-intra=1:enable-restoration=0:enable-cdef=0','-f','obu','-y',str(obu)]
                    run(command)
                    output=run([str(host),str(obu)],text=True,capture_output=True).stdout.splitlines()
                    if list(map(int,output[0].split()))!=[width,height,depth,sx,sy]:raise SystemExit('native format metadata mismatch: '+label)
                    for plane in range(3):
                        if list(map(int,output[plane+1].split()))!=planes[plane]:raise SystemExit('native lossless color plane mismatch: '+label)
                    blocks,cfl,pal_y,pal_uv=map(int,output[4].split())
                    if not a.palettes and (pal_y or pal_uv):raise SystemExit('encoder ignored palette-disabled contract')
                    controls=list(map(int,output[5].split())) if a.key_frames else []
                    if a.key_frames and controls[:6]!=[0,0,1,1,7,255]:raise SystemExit('native frame is not a shown full-header key frame: '+label)
                    cases.append(dict(controls=controls,name=label,width=width,height=height,depth=depth,sx=sx,sy=sy,pattern=pattern,
                        blocks=blocks,cfl=cfl,palette_y=pal_y,palette_uv=pal_uv,command=command,data=list(obu.read_bytes()),encoded_sha256=digest(obu)))
    if not a.palettes and not sum(c['cfl'] for c in cases):raise SystemExit('native corpus did not exercise actual chroma CfL blocks')
    if a.palettes and (not sum(c['palette_y'] for c in cases) or not sum(c['palette_uv'] for c in cases)):
        raise SystemExit('real encoder did not activate both Y and UV palettes')
    rel=os.path.relpath(a.root/'src/decoder.zi',a.out)
    lines=['#import, file "'+rel+'";','using Codec :: #import "image_codec/api"','#program_export','main :: () -> s32 {']
    for i in range(len(cases)):lines.append(f'    if Case{i}() != 0 {{ return {i+1} }}')
    lines+=['    return 0','}',
        'PutBytes :: (target: []u8, offset: s64, source: []u8) { for i: 0..source.count - 1 { target[offset + i] = source[i] } }',
        'ExpectedLuma :: (x: s32, y: s32, maximum: u32) -> u32 { return (cast(u32)(x * 11 + y * 7 + x * y * 3) ^ 123) & maximum }',
        'ExpectedColor :: (pattern: s32, x: s32, y: s32, plane: s32, width: s32, height: s32, sx: s32, sy: s32, depth: s32) -> u32 {',
        '    maximum := (cast(u32)1 << cast(u32)depth) - 1; middle := (maximum + 1) / 2',
        '    if pattern == 0 { if plane == 0 { return middle + 2 }; return ifx plane == 1 then middle / 2 else maximum - 3 }',
        '    if pattern == 1 { return middle + cast(u32)((x + y + plane) % 2) * 2 - 1 }',
        '    if pattern >= 3 {','        index := (x / 2 + y / 2 * 3) % 8',
        '        if pattern == 3 { return cast(u32)((index + plane) % 4) * maximum / 3 }',
        '        if pattern == 4 {','            if plane == 0 { return middle }; if plane == 1 { return cast(u32)(index / 2) * maximum / 3 }',
        '            values: [8]u32; values[0] = maximum - 1; values[1] = 1; values[2] = middle + 3; values[3] = middle - 3; values[4] = 0; values[5] = maximum; values[6] = 7; values[7] = maximum - 7',
        '            return values[index]','        }',
        '        if pattern == 5 { if plane == 0 { return maximum * cast(u32)(index % 2) }; return (cast(u32)(x * 13 + y * 7 + plane * 29) ^ 123) & maximum }',
        '        if pattern == 6 { if plane == 0 { return ExpectedLuma(x, y, maximum) }; value := maximum * cast(u32)(index % 4) / 3; return ifx plane == 1 then value else maximum - value }',
        '        value := maximum * cast(u32)(index % 4) / 3 + cast(u32)((x * y + plane) % 3); return ifx value > maximum then maximum else value','    }',
        '    if plane == 0 { return ExpectedLuma(x, y, maximum) }','    sum: u32',
        '    for dy: 0..sy { for dx: 0..sx {',
        '        px := x * (1 + sx) + cast(s32)dx; py := y * (1 + sy) + cast(s32)dy',
        '        if px >= width { px = width - 1 }; if py >= height { py = height - 1 }',
        '        sum += ExpectedLuma(px, py, maximum)','    } }',
        '    average := sum / cast(u32)((1 + sx) * (1 + sy))','    return ifx plane == 1 then average else maximum - average','}']
    helpers=''
    if a.key_frames:
        bounds=os.path.relpath(a.root/'tests/av1_key_headers.zi',a.out)
        lines.insert(0,'#import, file "'+bounds+'";')
        lines.insert(lines.index('main :: () -> s32 {')+1,'    if KeyHeaderBoundaries() != 0 { return 250 }')
    allocation_fixture=cases[0]
    allocation_data=','.join(map(str,allocation_fixture['data']))
    (a.out/'av1_chroma_allocation.zi').write_text('#import, file "'+rel+'";\nusing Codec :: #import "image_codec/api"\n#program_export\nAllocationFailure :: () -> s32 {\n'+
        f'    data: [{len(allocation_fixture["data"])}]u8 = .[{allocation_data}]\n'+
        '    options := DefaultOptions()\n    budget := NewWorkerBudget(options.limits); options.workers = *budget\n'+
        '    image := DecodeAv1YUV(BorrowBytes(data[:]), *options)\n'+
        '    if image.is_ok || image.error.code != AllocationFailed || image.y.storage.count != 0 || image.u.storage.count != 0 || image.v.storage.count != 0 || budget.active_workers != 0 || budget.working_bytes != 0 { return 1 }\n'+
        '    return 0\n}\n')
    for i,c in enumerate(cases):
        data,loader=zi_bytes('bytes',c['data'],f'Chroma{i}');helpers+=loader
        sample=1 if c['depth']==8 else 2;cw=(c['width']+c['sx'])//(1+c['sx']);ch=(c['height']+c['sy'])//(1+c['sy']);total=(c['width']*c['height']+2*cw*ch)*sample
        lines += [f'Case{i} :: () -> s32 {{','    // '+c['name'],data.rstrip(),'    options := DefaultOptions()',
            '    budget := NewWorkerBudget(options.limits); options.workers = *budget',
            '    image := DecodeAv1YUV(BorrowBytes(bytes[:]), *options)',
            '    if !image.is_ok { return 100 + cast(s32)image.error.code }',
            f'    if image.sequence.monochrome || image.sequence.bit_depth != {c["depth"]} || image.sequence.subsampling_x != {c["sx"]} || image.sequence.subsampling_y != {c["sy"]} {{ return 1 }}',
            '    for index: 0..2 {','        plane := *image.y',
            f'        width: s32 = {c["width"]}; height: s32 = {c["height"]}',
            f'        if index > 0 {{ width = {cw}; height = {ch}; if index == 1 {{ plane = *image.u }} else {{ plane = *image.v }} }}',
            f'        if plane.width != width || plane.height != height || plane.stride != width * {sample} || plane.sample_bytes != {sample} || plane.storage.count != cast(s64)width * height * {sample} || plane.storage.capacity != plane.storage.count {{ return 5 }}',
            '        for y: 0..height - 1 { for x: 0..width - 1 {',
            '            at := y * plane.stride + x * plane.sample_bytes; value := cast(u32)plane.storage[at]',
            '            if plane.sample_bytes == 2 { value |= cast(u32)plane.storage[at + 1] << 8 }',
            f'            if value != ExpectedColor({c["pattern"]}, cast(s32)x, cast(s32)y, cast(s32)index, {c["width"]}, {c["height"]}, {c["sx"]}, {c["sy"]}, {c["depth"]}) {{ return 2 + cast(s32)index }}','        } }','    }',
            '    if budget.active_workers != 0 || budget.working_bytes != 0 { return 6 }',
            f'    options.limits.max_output_bytes = {total-1}',
            '    bounded := DecodeAv1YUV(BorrowBytes(bytes[:]), *options)',
            '    if bounded.is_ok || bounded.error.code != LimitExceeded || bounded.y.storage.count != 0 || bounded.u.storage.count != 0 || bounded.v.storage.count != 0 || budget.active_workers != 0 || budget.working_bytes != 0 { return 7 }']
        if a.key_frames:
            fields=['image.sequence.reduced_still_picture_header','image.frame.frame_type','image.frame.show_frame',
                'image.frame.error_resilient_mode','image.frame.primary_ref_frame','image.frame.refresh_frame_flags',
                'image.frame.disable_cdf_update','image.frame.force_integer_mv','image.frame.current_frame_id',
                'image.frame.order_hint','image.frame.frame_presentation_time']
            booleans={0,2,3,6,7}
            for field,(name,value) in enumerate(zip(fields,c['controls'])):
                # libaom's cur_frame_force_integer_mv retains the raw hint.
                # The normative uncompressed_header forces1 for intra frames.
                if field==7:continue
                control_value=('true' if value else 'false') if field in booleans else str(value)
                lines.append(f'    if {name} != {control_value} {{ return {20+field} }}')
            lines.append('    if !image.frame.force_integer_mv { return 27 }')
        if c['width']==4 and c['pattern']==0:
            lines += ['    options = DefaultOptions(); options.workers = *budget','    for end: 0..bytes.count - 1 {',
                '        truncated := DecodeAv1YUV(BorrowBytes(bytes[:end]), *options)',
                '        if truncated.is_ok || truncated.y.storage.count != 0 || truncated.u.storage.count != 0 || truncated.v.storage.count != 0 || budget.active_workers != 0 || budget.working_bytes != 0 { return 8 }','    }']
            other=next(item for item in cases if all(item[key]==c[key] for key in ('width','height','depth','sx','sy')) and item['pattern']==2)
            other_data,other_loader=zi_bytes('other_bytes',other['data'],f'OtherChroma{i}');helpers+=other_loader
            lines += [other_data.rstrip(),'    for repeat: 0..7 {',
                '        other := DecodeAv1YUV(BorrowBytes(other_bytes[:]), *options)',
                '        if !other.is_ok || budget.active_workers != 0 || budget.working_bytes != 0 { return 9 }','    }',
                '    for byte: 0..bytes.count - 1 { bytes[byte] = 0 }',
                '    for index: 0..2 {','        plane := *image.y; if index == 1 { plane = *image.u } else if index == 2 { plane = *image.v }',
                '        value := cast(u32)plane.storage[0]; if plane.sample_bytes == 2 { value |= cast(u32)plane.storage[1] << 8 }',
                f'        if value != ExpectedColor(0, 0, 0, cast(s32)index, 4, 4, {c["sx"]}, {c["sy"]}, {c["depth"]}) {{ return 10 }}','    }']
        lines += ['    return 0','}']
    (a.out/'av1_chroma_vectors.zi').write_text('\n'.join(lines)+'\n'+helpers)
    (a.out/'oracle.json').write_text(json.dumps(dict(revision=PIN,library_sha256=digest(library),frames=len(cases),
        cfl_coding_blocks=sum(c['cfl'] for c in cases),palette_y_blocks=sum(c['palette_y'] for c in cases),palette_uv_blocks=sum(c['palette_uv'] for c in cases),depths=[8,10,12],formats=['420','422','444'],dimensions=dimensions,cases=cases,
        scope=('real lossless shown key-frame full headers and color planes' if a.key_frames else 'real lossless reduced-still color planes')+(' including active Y/UV palettes' if a.palettes else '')+'; intraBC/lossy/generalAV1 remain unsupported'),indent=2)+'\n')
    print(f'Pinned color oracle: {len(cases)} frames, {sum(c["cfl"] for c in cases)} native CfL blocks, {sum(c["palette_y"] for c in cases)} Y/{sum(c["palette_uv"] for c in cases)} UV palette blocks;420/422/444,8/10/12-bit, tiny/odd/64 dimensions')


if __name__=='__main__':main()

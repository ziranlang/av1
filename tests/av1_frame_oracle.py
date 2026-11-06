#!/usr/bin/env python3
"""Compare owned reduced-still frame control fields to decoded dav1d pictures.
The upstream decoder is a read-only test oracle and is never linked to product
AV1 modules. These tests compare header state, not authored decoded planes.
"""
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
from av1_header_oracle import PIN, leb

FIELDS = [
    ('width','f->width[0]'),('upscaled_width','f->width[1]'),('height','f->height'),
    ('render_width','f->render_width'),('render_height','f->render_height'),
    ('superres_denominator','f->super_res.width_scale_denominator'),
    ('disable_cdf_update','f->disable_cdf_update'),('allow_screen_content_tools','f->allow_screen_content_tools'),
    ('allow_intrabc','f->allow_intrabc'),('base_q_idx','f->quant.yac'),('using_qmatrix','f->quant.qm'),
    ('segmentation_enabled','f->segmentation.enabled'),('segment_id_pre_skip','f->segmentation.seg_data.preskip'),
    ('last_active_segment_id','f->segmentation.seg_data.last_active_segid'),
    ('delta_q_present','f->delta.q.present'),('delta_q_res','f->delta.q.res_log2'),
    ('delta_lf_present','f->delta.lf.present'),('delta_lf_res','f->delta.lf.res_log2'),('delta_lf_multi','f->delta.lf.multi'),
    ('coded_lossless','f->all_lossless'),('all_lossless','f->all_lossless && !f->super_res.enabled'),
    ('loop_filter_sharpness','f->loopfilter.sharpness'),
    ('cdef_damping','f->cdef.damping ? f->cdef.damping : 3'),('cdef_bits','f->cdef.n_bits'),
    ('tx_mode','f->txfm_mode'),('reduced_tx_set','f->reduced_txtp_set'),
    ('tiles.columns','f->tiling.cols'),('tiles.rows','f->tiling.rows'),
    ('tiles.column_log2','f->tiling.log2_cols'),('tiles.row_log2','f->tiling.log2_rows'),
    ('tiles.context_update_tile_id','f->tiling.update'),('tiles.size_bytes','f->tiling.n_bytes')]
BOOLS = {'disable_cdf_update','allow_screen_content_tools','allow_intrabc','using_qmatrix',
    'segmentation_enabled','segment_id_pre_skip','delta_q_present','delta_lf_present','delta_lf_multi',
    'coded_lossless','all_lossless','reduced_tx_set'}
for i, field in enumerate(['ydc_delta','udc_delta','uac_delta','vdc_delta','vac_delta']):
    FIELDS.append((f'delta_q[{i}]', f'f->quant.{field}'))
for i in range(8):
    FIELDS += [(f'lossless[{i}]',f'f->segmentation.lossless[{i}]'),
        (f'loop_filter_ref_deltas[{i}]',f'f->loopfilter.mode_ref_deltas.ref_delta[{i}]')]
    BOOLS.add(f'lossless[{i}]')
    for component in ['y','uv']:
        FIELDS += [(f'cdef_{component}_primary[{i}]',f'f->cdef.{component}_strength[{i}] >> 2'),
            (f'cdef_{component}_secondary[{i}]',f'(f->cdef.{component}_strength[{i}] & 3) == 3 ? 4 : f->cdef.{component}_strength[{i}] & 3')]
for i in range(2):
    FIELDS.append((f'loop_filter_mode_deltas[{i}]',f'f->loopfilter.mode_ref_deltas.mode_delta[{i}]'))
for i in range(4):
    expression = f'f->loopfilter.level_y[{i}]' if i < 2 else 'f->loopfilter.level_' + ('u' if i == 2 else 'v')
    FIELDS.append((f'loop_filter_levels[{i}]',expression))
for i in range(3): FIELDS.append((f'restoration_types[{i}]',f'f->restoration.type[{i}]'))


def run(args, **kw): return subprocess.run(args, check=True, text=True, **kw)


# One array literal, 64 values per source line. The second value is kept for
# callers that append helper declarations; it is always empty.
def zi_bytes(name,data,prefix):
    rows=[','.join(map(str,data[i:i+64])) for i in range(0,len(data),64)]
    return f'    {name}: [{len(data)}]u8 = .[\n        '+',\n        '.join(rows)+'\n    ]\n',''



# Large inputs as static hexadecimal text decoded at run time. An array
# literal lowers to one store per element in generated C, which makes big
# corpus modules slow to compile; string literals stay static data on every
# target. Sources cap a line, and a raw #string body, at 4095 characters, so
# the text is split into 4000-digit literals.
UNHEX='''
// Decodes the hexadecimal PARTS, in order, into exactly OUTPUT.count bytes.
Unhex :: (parts: []string, output: []u8) -> bool {
    written: s64
    for part: 0..parts.count - 1 {
        text := parts[part]
        if text.count % 2 != 0 { return false }
        at: s64
        while at < text.count {
            if written >= output.count { return false }
            high := cast(s32)text[at]; low := cast(s32)text[at + 1]
            high = ifx high <= 57 then high - 48 else high - 87
            low = ifx low <= 57 then low - 48 else low - 87
            if high < 0 || high > 15 || low < 0 || low > 15 { return false }
            output[written] = cast(u8)(high * 16 + low); written += 1; at += 2
        }
    }
    return written == output.count
}
'''


def zi_hex(name,data,prefix):
    """(body, declaration) filling a local [N]u8 from hex string literals;
    the declaration is always empty. The module must contain UNHEX once."""
    text=bytes(data).hex();parts=[text[i:i+4000] for i in range(0,len(text),4000)] or ['']
    body=(f'    {name}: [{len(data)}]u8\n    {prefix}_hex: [{len(parts)}]string = .[\n        '+
        ',\n        '.join(f'"{part}"' for part in parts)+
        f'\n    ]\n    if !Unhex({prefix}_hex[:], {name}[:]) {{ return 9 }}\n')
    return body,''

def fixture_frames(root, name):
    """(sequence header, frame) pairs of a fixture's AV1 items: alpha then color."""
    for part in ('alpha', 'color'):
        path = root / f'{name}.{part}.obu'
        if path.exists(): yield from raw_frames(path)


def raw_frames(path):
    data=path.read_bytes();at=0;seq=None
    while at<len(data):
        h=data[at];at+=1
        if h&4:at+=1
        if not h&2:raise ValueError('unsized fixture OBU')
        n=shift=0
        while at<len(data):
            b=data[at];at+=1;n|=(b&127)<<shift;shift+=7
            if b<128:break
        if n>len(data)-at:raise ValueError('truncated fixture OBU')
        payload=data[at:at+n];at+=n
        if h>>3==1:seq=payload
        elif h>>3==6 and seq is not None:yield seq,payload


def main():
    p = argparse.ArgumentParser()
    for name in ['root','out','dav1d','library']: p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--extras',type=Path)
    a = p.parse_args()
    revision = run(['git','-C',str(a.dav1d),'rev-parse','HEAD'],capture_output=True).stdout.strip()
    if revision != PIN: raise SystemExit('dav1d source revision mismatch')
    body = ['#include <stdio.h>\n#include <stdint.h>\n#include <string.h>\n#include "dav1d/dav1d.h"\n',
        'int main(void){unsigned n,x;uint8_t input[65536];fprintf(stderr,"%s\\n",dav1d_version());\n',
        'while(scanf("%u",&n)==1){if(n>sizeof(input))return 2;for(unsigned i=0;i<n;i++){if(scanf("%u",&x)!=1||x>255)return 3;input[i]=x;}\n',
        'Dav1dSettings settings;dav1d_default_settings(&settings);settings.n_threads=1;settings.max_frame_delay=1;settings.apply_grain=0;settings.strict_std_compliance=1;\n',
        'Dav1dContext *c=NULL;int e=dav1d_open(&c,&settings);if(e)return 4;Dav1dData data={0};uint8_t *buf=dav1d_data_create(&data,n);if(!buf)return 5;memcpy(buf,input,n);\n',
        'e=dav1d_send_data(c,&data);Dav1dPicture pic={0};if(!e)e=dav1d_get_picture(c,&pic);printf("%d",e);if(!e){const Dav1dFrameHeader *f=pic.frame_hdr;\n']
    for _, expr in FIELDS: body.append(f'printf(" %lld",(long long)({expr}));\n')
    body += ['int shift=pic.seq_hdr->sb128?5:4;\n',
        'for(int i=0;i<=f->tiling.cols;i++)printf(" %d",i==f->tiling.cols?2*((f->width[0]+7)>>3):f->tiling.col_start_sb[i]<<shift);\n',
        'for(int i=0;i<=f->tiling.rows;i++)printf(" %d",i==f->tiling.rows?2*((f->height+7)>>3):f->tiling.row_start_sb[i]<<shift);\n',
        '}printf("\\n");dav1d_picture_unref(&pic);dav1d_data_unref(&data);dav1d_close(&c);}\nreturn 0;}\n']
    source = a.out / 'frame_oracle.c'; source.write_text(''.join(body)); host = a.out / 'frame_oracle'
    run(shlex.split(os.environ.get('CC','cc')) + ['-std=c11','-O2','-I'+str(a.dav1d/'include'),
        '-I'+str(a.library.parent.parent/'include'),str(source),str(a.library),'-pthread','-lm','-ldl','-o',str(host)])
    cases=[]
    for name in ['rgba8','rgba10','rgba12','gray8','gray10','gray12','opaque']:
        for j,(seq,frame) in enumerate(fixture_frames(a.root/'tests/fixtures',name)): cases.append((f'{name}-{j}',seq,frame))
    if a.extras:
        for item in json.loads(a.extras.read_text())['cases']:
            for j,(seq,frame) in enumerate(raw_frames(Path(item['path']))):cases.append((f'{item["name"]}-{j}',seq,frame))
    protocol=''
    for _,seq,frame in cases:
        obu=bytes([0x0a])+leb(len(seq))+seq+bytes([0x32])+leb(len(frame))+frame
        protocol+=' '.join(map(str,[len(obu)]+list(obu)))+'\n'
    result=run([str(host)],input=protocol,capture_output=True)
    if result.stderr.strip() != '1.5.4-0-g'+PIN[:8]: raise SystemExit('dav1d library version mismatch')
    rows=result.stdout.splitlines()
    if len(rows)!=len(cases):raise SystemExit('incomplete dav1d frame oracle response')
    zi=['#import, file "../../../src/syntax/headers.zi";\n#import, file "../../../src/syntax/frame.zi";\n#import, file "../../../src/syntax/tilegroup.zi";\nusing Codec :: #import "image_codec/api"\n']
    zi+=['PutBytes :: (target: []u8, offset: s64, source: []u8) {\n    for i: 0..source.count - 1 { target[offset + i] = source[i] }\n}\n']
    for i,((name,seq,frame),row) in enumerate(zip(cases,rows)):
        values=list(map(int,row.split()))
        if values[0]:raise SystemExit(f'dav1d frame decode failed {name}: {values[0]}')
        expected_fields=values[1:1+len(FIELDS)]
        assert len(expected_fields)==len(FIELDS)
        seq_body,seq_helpers=zi_bytes('seq',seq,f'{i}Seq')
        data_body,data_helpers=zi_bytes('data',frame,f'{i}Data')
        zi+=[seq_helpers,data_helpers]
        zi += [f'// {name}\nCase{i} :: () -> s32 {{\n',
            seq_body,data_body,
            f'    sequence := ParseSequenceHeader(BorrowBytes(seq[:]),0,{len(seq)},DefaultLimits())\n',
            '    if sequence.error.code != DecodeOK { return 1 }\n',
            f'    parsed := ParseStillFrameHeader(BorrowBytes(data[:]),0,{len(frame)},sequence.header,DefaultLimits())\n',
            '    if parsed.error.code != DecodeOK { return 2 }\n']
        for (field,_),expected in zip(FIELDS,expected_fields):
            literal=('true' if expected else 'false') if field in BOOLS else str(expected)
            zi.append(f'    if parsed.header.{field} != {literal} {{ return 3 }}\n')
        metadata=dict(zip((field for field,_ in FIELDS),expected_fields))
        cols,rows=metadata['tiles.columns'],metadata['tiles.rows']
        boundaries=values[1+len(FIELDS):]
        assert len(boundaries)==cols+rows+2
        for j,value in enumerate(boundaries[:cols+1]):zi.append(f'    if parsed.header.tiles.column_starts[{j}] != {value} {{ return 5 }}\n')
        for j,value in enumerate(boundaries[cols+1:]):zi.append(f'    if parsed.header.tiles.row_starts[{j}] != {value} {{ return 6 }}\n')
        zi += [f'    group := NewTileGroup(BorrowBytes(data[:]),parsed.next,{len(frame)},parsed.header.tiles)\n',
            '    tiles: s32\n    previous := parsed.next\n',
            '    while true {\n        span := NextTileSpan(*group)\n',
            '        if span.error.code != DecodeOK { return 7 }\n        if !span.has_tile { break }\n',
            '        if span.tile.index != tiles || span.tile.begin < previous || span.tile.end <= span.tile.begin { return 8 }\n',
            '        previous = span.tile.end; tiles += 1\n    }\n',
            f'    if tiles != {cols*rows} || group.at != {len(frame)} {{ return 9 }}\n',
            '    end: s64\n    while end < parsed.next {\n',
            '        short := ParseStillFrameHeader(BorrowBytes(data[:]),0,end,sequence.header,DefaultLimits())\n',
            '        if short.error.code != TruncatedInput || short.error.offset != end { return 4 }\n',
            '        end += 1\n    }\n']
        if cols*rows>1:
            zi += ['    token: Cancellation\n',
                f'    group = NewTileGroup(BorrowBytes(data[:]),parsed.next,{len(frame)},parsed.header.tiles,*token)\n',
                '    first := NextTileSpan(*group)\n    stopped := group.at\n    Cancel(*token)\n',
                '    cancelled := NextTileSpan(*group)\n',
                '    if cancelled.error.code != DecodeCancelled || group.at != stopped { return 10 }\n']
            if metadata['tiles.size_bytes']>=2:
                zi += [f'    group = NewTileGroup(BorrowBytes(data[:]),parsed.next,{len(frame)},parsed.header.tiles)\n',
                    '    for b: 0..group.size_bytes - 1 { data[group.at + b] = 255 }\n',
                    '    huge := NextTileSpan(*group)\n',
                    f'    if huge.error.code != TruncatedInput || huge.error.offset != {len(frame)} {{ return 11 }}\n']
        zi += ['    return 0\n}\n']
    zi += ['#program_export\nmain :: () -> s32 {\n']
    for i in range(len(cases)):zi.append(f'    if Case{i}() != 0 {{ return {i+1} }}\n')
    zi += ['    return 0\n}\n']
    (a.out/'av1_frame_vectors.zi').write_text(''.join(zi))
    (a.out/'frame_oracle.json').write_text(json.dumps({'dav1d_revision':PIN,'fixture_frames':len(cases),
        'fields_per_frame':len(FIELDS),'tile_boundaries':True,'tile_span_coverage':True,
        'scope':'header state/tile framing differential, not authored decoded planes'},indent=2)+'\n')
    print(f'dav1d frame oracle: {len(cases)} real still headers / {len(FIELDS)} fields; every header prefix truncated')


if __name__=='__main__':main()

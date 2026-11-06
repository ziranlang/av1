#!/usr/bin/env python3
"""Sequence syntax differential against pinned dav1d, plus real AVIF payloads.

Generated inputs use actual AV1 sequence_header_obu syntax from the pinned
primary spec, not a codec-specific test dialect. These are sequence primitive
tests, not full frame conformance vectors.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import shlex
import subprocess

PIN = "54706fc6bc0cdecab7e9593974a4039cc038fca7"
FIELDS = [
    ("profile", "profile"), ("max_frame_width", "max_width"), ("max_frame_height", "max_height"),
    ("bit_depth", "8 + 2 * s.hbd"), ("monochrome", "monochrome"),
    ("subsampling_x", "ss_hor"), ("subsampling_y", "ss_ver"),
    ("color_primaries", "pri"), ("transfer_characteristics", "trc"), ("matrix_coefficients", "mtrx"),
    ("chroma_sample_position", "chr"), ("full_range", "color_range"),
    ("still_picture", "still_picture"), ("reduced_still_picture_header", "reduced_still_picture_header"),
    ("timing_info_present", "timing_info_present"), ("num_units_in_display_tick", "num_units_in_tick"),
    ("time_scale", "time_scale"), ("equal_picture_interval", "equal_picture_interval"),
    ("num_ticks_per_picture_minus_1", "s.equal_picture_interval ? s.num_ticks_per_picture - 1 : 0"),
    ("decoder_model_info_present", "decoder_model_info_present"), ("buffer_delay_length", "encoder_decoder_buffer_delay_length"),
    ("num_units_in_decoding_tick", "num_units_in_decoding_tick"), ("buffer_removal_time_length", "buffer_removal_delay_length"),
    ("frame_presentation_time_length", "frame_presentation_delay_length"),
    ("initial_display_delay_present", "display_model_info_present"), ("operating_point_count", "num_operating_points"),
    ("frame_width_bits", "width_n_bits"), ("frame_height_bits", "height_n_bits"),
    ("frame_id_numbers_present", "frame_id_numbers_present"), ("delta_frame_id_length", "delta_frame_id_n_bits"),
    ("frame_id_length", "frame_id_n_bits"), ("use_128x128_superblock", "sb128"),
    ("enable_filter_intra", "filter_intra"), ("enable_intra_edge_filter", "intra_edge_filter"),
    ("enable_interintra_compound", "inter_intra"), ("enable_masked_compound", "masked_compound"),
    ("enable_warped_motion", "warped_motion"), ("enable_dual_filter", "dual_filter"), ("enable_order_hint", "order_hint"),
    ("enable_jnt_comp", "jnt_comp"), ("enable_ref_frame_mvs", "ref_frame_mvs"),
    ("force_screen_content_tools", "screen_content_tools"), ("force_integer_mv", "force_integer_mv"),
    ("order_hint_bits", "order_hint_n_bits"), ("enable_superres", "super_res"),
    ("enable_cdef", "cdef"), ("enable_restoration", "restoration"),
    ("color_description_present", "color_description_present"), ("separate_uv_delta_q", "separate_uv_delta_q"),
    ("film_grain_params_present", "film_grain_present")]
BOOLS = {name for name, _ in FIELDS if name in {
    "monochrome", "full_range", "still_picture", "reduced_still_picture_header", "timing_info_present",
    "equal_picture_interval", "decoder_model_info_present", "initial_display_delay_present", "frame_id_numbers_present",
    "use_128x128_superblock", "enable_filter_intra", "enable_intra_edge_filter", "enable_interintra_compound",
    "enable_masked_compound", "enable_warped_motion", "enable_dual_filter", "enable_order_hint", "enable_jnt_comp",
    "enable_ref_frame_mvs", "enable_superres", "enable_cdef", "enable_restoration", "color_description_present",
    "separate_uv_delta_q", "film_grain_params_present"}}


def run(argv, **kw):
    return subprocess.run(argv, check=True, text=True, **kw)


def leb(value):
    out = bytearray()
    while value >= 128:
        out.append(128 | (value % 128)); value //= 128
    out.append(value)
    return out


class Bits:
    def __init__(self): self.text = ""
    def f(self, value, width=1):
        assert 0 <= value < 2 ** width
        self.text += f"{value:0{width}b}"
    def uvlc(self, value):
        if value == 2 ** 32 - 1:
            self.text += '0' * 32 + '1'
            return
        text = bin(value + 1)[2:]
        self.text += "0" * (len(text) - 1) + text
    def finish(self):
        self.text += "1"
        self.text += "0" * (-len(self.text) % 8)
        return int(self.text, 2).to_bytes(len(self.text) // 8, "big")


def generated_sequence(case, rng, interval=17, matrix=1):
    bits = Bits()
    profile = case % 3
    reduced = (case // 3) % 2
    bits.f(profile, 3); bits.f(1); bits.f(reduced)
    if reduced:
        bits.f(case % 24, 5)
    else:
        timing = case % 4 != 0; model = timing and case % 5 != 0
        bits.f(timing)
        if timing:
            bits.f(1001, 32); bits.f(60000, 32); bits.f(case % 2)
            if case % 2: bits.uvlc(interval)
            bits.f(model)
            if model:
                bits.f(31, 5); bits.f(999, 32); bits.f(15, 5); bits.f(23, 5)
        display = case % 3 != 0
        bits.f(display)
        ops = 32 if case % 12 == 0 else 1 + case % 4
        bits.f(ops - 1, 5)
        for i in range(ops):
            bits.f(0x100 | (i + 1), 12)
            level = (case + i) % 24
            bits.f(level, 5)
            if level > 7: bits.f(i % 2)
            if model:
                bits.f(i % 2)
                if i % 2:
                    bits.f(0xfffffff0 + i % 16, 32); bits.f(i * 65537, 32); bits.f(1)
            if display:
                bits.f(i % 2)
                if i % 2: bits.f(i % 16, 4)
    width, height = rng.randrange(1, 32769), rng.randrange(1, 1025)
    bits.f(15, 4); bits.f(15, 4); bits.f(width - 1, 16); bits.f(height - 1, 16)
    if not reduced:
        frame_id = case % 2
        bits.f(frame_id)
        if frame_id: bits.f(5, 4); bits.f(3, 3)
    for _ in range(3): bits.f(rng.randrange(2))
    if not reduced:
        for _ in range(4): bits.f(rng.randrange(2))
        order = case % 2
        bits.f(order)
        if order:
            bits.f(1); bits.f(0)
        screen = case % 3
        bits.f(screen == 2)
        if screen != 2: bits.f(screen)
        if screen > 0:
            bits.f(case % 2)
            if case % 2 == 0: bits.f(1)
        if order: bits.f(5, 3)
    for _ in range(3): bits.f(rng.randrange(2))
    high = (case // 6) % 2
    bits.f(high)
    depth = 8 + 2 * high
    if profile == 2 and high:
        twelve = (case // 12) % 2
        bits.f(twelve); depth += 2 * twelve
    mono = profile != 1 and (case // 12) % 2
    if profile != 1: bits.f(mono)
    bits.f(1)
    bits.f(1, 8); bits.f(1, 8); bits.f(matrix, 8)
    bits.f(case % 2)
    if not mono:
        sx, sy = int(profile != 1), int(profile == 0)
        if profile == 2 and depth == 12:
            sx = (case // 24) % 2; bits.f(sx)
            if sx: sy = (case // 48) % 2; bits.f(sy)
        if sx and sy: bits.f(case % 3, 2)
        bits.f(case % 2)
    bits.f(case % 2)
    return bits.finish()


def fixture_obus(root, name):
    """OBU payloads of a fixture's AV1 items: alpha then color, as stored in its mdat."""
    for part in ('alpha', 'color'):
        path = root / f'{name}.{part}.obu'
        if not path.exists(): continue
        data = path.read_bytes(); cur = 0
        while cur < len(data):
            header = data[cur]; cur += 1
            if header & 4: cur += 1
            if not header & 2: raise ValueError(f'{path.name}: unsized OBU')
            length = shift = 0
            while cur < len(data):
                byte = data[cur]; cur += 1
                length |= (byte & 127) << shift; shift += 7
                if byte < 128: break
            if length > len(data) - cur: raise ValueError(f'{path.name}: truncated OBU')
            yield path, header >> 3 & 15, data[cur:cur + length]
            cur += length


def fixture_sequences(root, name):
    for _, kind, payload in fixture_obus(root, name):
        if kind == 1: yield payload


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, required=True); p.add_argument("--out", type=Path, required=True)
    p.add_argument("--dav1d", type=Path, required=True); p.add_argument("--library", type=Path, required=True)
    args = p.parse_args()
    revision = run(["git", "-C", str(args.dav1d), "rev-parse", "HEAD"], capture_output=True).stdout.strip()
    if revision != PIN: raise SystemExit(f"dav1d revision mismatch: {revision}")
    run(['git', '-C', str(args.dav1d), 'diff', '--quiet', 'HEAD', '--'])
    if not args.library.is_file(): raise SystemExit("pinned dav1d native recipe library missing")
    lines = ['#include <stdint.h>\n#include <stdio.h>\n#include <stddef.h>\n#include "dav1d/headers.h"\n',
        'extern int dav1d_parse_sequence_header(Dav1dSequenceHeader*,const uint8_t*,size_t);\n',
        'extern const char *dav1d_version(void);\n',
        'int main(void) { fprintf(stderr,"%s\\n",dav1d_version()); unsigned n,x; uint8_t data[2048]; while(scanf("%u",&n)==1) {\n',
        'if(n>sizeof(data))return 2;for(unsigned i=0;i<n;i++){if(scanf("%u",&x)!=1||x>255)return 3;data[i]=x;}\n',
        'Dav1dSequenceHeader s; int e=dav1d_parse_sequence_header(&s,data,n); printf("%d",e); if(!e){\n']
    for _, expression in FIELDS:
        expr = expression if " " in expression else "s." + expression
        lines += [f'printf(" %llu",(unsigned long long)({expr}));\n']
    lines += ['for(unsigned i=0;i<s.num_operating_points;i++){\n',
        'unsigned level=(s.reduced_still_picture_header?s.operating_points[i].major_level:s.operating_points[i].major_level-2)*4+s.operating_points[i].minor_level;\n',
        'printf(" %u %u %u %u %u %u %u %u",s.operating_points[i].idc,level,s.operating_points[i].tier,s.operating_points[i].decoder_model_param_present,s.operating_points[i].initial_display_delay,s.operating_parameter_info[i].decoder_buffer_delay,s.operating_parameter_info[i].encoder_buffer_delay,s.operating_parameter_info[i].low_delay_mode);}\n',
        '}printf("\\n");} return 0;}\n']
    source = args.out / 'oracle.c'; source.write_text(''.join(lines))
    host = args.out / 'oracle'
    run(shlex.split(os.environ.get('CC', 'cc')) + ['-std=c11', '-O2', '-I' + str(args.dav1d / 'include'),
        str(source), str(args.library), '-pthread', '-lm', '-ldl', '-o', str(host)])
    rng = random.Random(0x534551)
    cases = [(f'generated-{i}', generated_sequence(i, rng)) for i in range(144)]
    cases += [('interval-zero', generated_sequence(1, rng, 0)),
        ('interval-max', generated_sequence(1, rng, 2 ** 32 - 2))]
    for name in ['rgba8', 'rgba10', 'rgba12', 'gray8', 'gray10', 'gray12', 'opaque', 'animated']:
        for j, payload in enumerate(fixture_sequences(args.root / 'tests/fixtures', name)):
            cases.append((f'{name}-{j}', payload))
    protocol = ''
    for _, payload in cases:
        obu = bytes([0x0a]) + leb(len(payload)) + payload
        protocol += ' '.join(map(str, [len(obu)] + list(obu))) + '\n'
    execution = run([str(host)], input=protocol, capture_output=True)
    if execution.stderr.strip() != '1.5.4-0-g' + PIN[:8]: raise SystemExit('dav1d oracle library version mismatch: ' + execution.stderr.strip())
    rows = execution.stdout.splitlines()
    if len(rows) != len(cases): raise SystemExit('incomplete dav1d oracle response')
    zi = ['#import, file "../../../src/syntax/headers.zi";\nusing Codec :: #import "image_codec/api"\n']
    for i, ((name, payload), row) in enumerate(zip(cases, rows)):
        values = list(map(int, row.split()))
        if values[0] != 0: raise SystemExit(f'dav1d rejected {name}: {values[0]}')
        zi += [f'// {name}\nCase{i} :: () -> s32 {{\n',
            f'    data: [{len(payload)}]u8 = .[' + ','.join(map(str, payload)) + ']\n',
            f'    parsed := ParseSequenceHeader(BorrowBytes(data[:]), 0, {len(payload)}, DefaultLimits())\n',
            '    if parsed.error.code != DecodeOK { return 1 }\n']
        for (field, _), expected in zip(FIELDS, values[1:]):
            literal = ('true' if expected else 'false') if field in BOOLS else str(expected)
            zi += [f'    if parsed.header.{field} != {literal} {{ return 2 }}\n']
        tail = values[1 + len(FIELDS):]
        assert len(tail) % 8 == 0
        for op in range(len(tail) // 8):
            for field, expected in zip(['idc','level','tier','decoder_model_present','initial_display_delay','decoder_buffer_delay','encoder_buffer_delay','low_delay_mode'], tail[op * 8:op * 8 + 8]):
                literal = ('true' if expected else 'false') if field in ['decoder_model_present','low_delay_mode'] else str(expected)
                zi += [f'    if parsed.header.operating_points[{op}].{field} != {literal} {{ return 3 }}\n']
        zi += ['    return 0\n}\n']
    invalid_interval = generated_sequence(1, rng, 2 ** 32 - 1)
    invalid_matrix = generated_sequence(12, rng, matrix=0)
    zi += ['BadInterval :: () -> s32 {\n',
        f'    data: [{len(invalid_interval)}]u8 = .[' + ','.join(map(str, invalid_interval)) + ']\n',
        f'    parsed := ParseSequenceHeader(BorrowBytes(data[:]),0,{len(invalid_interval)},DefaultLimits())\n',
        '    if parsed.error.code != MalformedInput { return 1 }\n    return 0\n}\n',
        'BadMatrix :: () -> s32 {\n',
        f'    data: [{len(invalid_matrix)}]u8 = .[' + ','.join(map(str, invalid_matrix)) + ']\n',
        f'    parsed := ParseSequenceHeader(BorrowBytes(data[:]),0,{len(invalid_matrix)},DefaultLimits())\n',
        '    if parsed.error.code != MalformedInput { return 1 }\n    return 0\n}\n',
        '#program_export\nmain :: () -> s32 {\n']
    for i in range(len(cases)): zi += [f'    if Case{i}() != 0 {{ return {i + 1} }}\n']
    zi += ['    if BadMatrix() != 0 { return 159 }\n    return BadInterval()\n}\n']
    (args.out / 'av1_header_vectors.zi').write_text(''.join(zi))
    digest = hashlib.sha256()
    with args.library.open('rb') as binary:
        for chunk in iter(lambda: binary.read(1024 * 1024), b''): digest.update(chunk)
    (args.out / 'oracle.json').write_text(json.dumps({'dav1d_revision': PIN, 'library_version':execution.stderr.strip(),
        'library_sha256':digest.hexdigest(), 'seed': '0x534551',
        'generated_cases':146, 'fixture_sequences': len(cases)-146,'fields_per_header':len(FIELDS),
        'semantic_boundary_cases':2}, indent=2)+'\n')
    print(f'dav1d sequence oracle: {len(cases)} headers / {len(FIELDS)} fields + operating-point parameters')


if __name__ == '__main__': main()

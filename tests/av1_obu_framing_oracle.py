#!/usr/bin/env python3
"""OBU framing of KEY stills against the pinned libaom decoder.

Test-only native code. Sources are the pinned SVC conformance KEY unit
(frame header plus three tile groups, two operating points) and libaom
encodes split into several tile groups. Each variant reorders, drops, copies
or inserts OBUs; the pinned libaom decoder gives its verdict on every variant
and the authored decoder must match it: the same rows when libaom decodes,
MalformedInput/TruncatedInput when libaom rejects, and UnsupportedCapability
only for the pinned metadata gap.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import struct
import subprocess
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent))
from av1_conformance_fetch import pins
from av1_conformance_oracle import COMMON, HOST, PIN, SHARD_BYTES, obus, temporal_units
from av1_frame_oracle import UNHEX, zi_hex
from av1_intra_frames_oracle import ENCODER as INTRA_ENCODER

SVC='av1-1-b8-22-svc-L1T2.ivf'
# w h depth format q sb128 tile_columns_log2 tile_rows_log2 screen aq deltaq
# lossless reduced seed filters speed sharpness tile_groups
ENCODES=[
    (160,96,8,0,30,0,1,1,0,0,0,0,0,1,7,6,0,2),
    (200,130,10,2,40,0,2,1,0,0,0,0,0,2,7,6,0,3),
    (96,72,12,1,20,0,1,0,0,0,0,0,0,3,7,6,0,2),
    (129,65,8,3,50,0,1,1,0,0,0,0,0,4,7,6,0,4),
]
ERRORS={'malformed':'MalformedInput','truncated':'TruncatedInput'}
EXTRA='''
// 0 when the decoder rejects with exactly the expected error code.
CheckError :: (input: BorrowedBytes, code: s32) -> s32 {
    options := DefaultOptions(); budget := NewWorkerBudget(options.limits); options.workers = *budget
    image := DecodeAv1YUV(input, *options)
    if budget.active_workers != 0 || budget.working_bytes != 0 { return 2 }
    if image.is_ok { return 7 }
    return ifx image.error.code == code then 0 else 200 + cast(s32)image.error.code
}
'''


def leb128(value):
    out=bytearray()
    while True:
        byte=value&127;value>>=7
        out.append(byte|(128 if value else 0))
        if not value:return bytes(out)


def obu(kind,payload,ext=None):
    header=bytes([kind<<3|2|(4 if ext is not None else 0)])
    if ext is not None:header+=bytes([ext[0]<<5|ext[1]<<3])
    return header+leb128(len(payload))+payload


def split(unit):
    """[kind, extension or None, payload] for every OBU of a unit."""
    items=[]
    for kind,whole,payload in obus(unit):
        ext=(whole[1]>>5,whole[1]>>3&3) if whole[0]&4 else None
        items.append([kind,ext,payload])
    return items


def join(items):
    return b''.join(obu(kind,payload,ext) for kind,ext,payload in items)


def operating_idc(sequence):
    bits=''.join(f'{byte:08b}' for byte in sequence)
    if bits[4]=='1':return 0 # reduced still picture header
    if bits[5]!='0':raise SystemExit('source sequence header signals timing info; idc parse not covered')
    return int(bits[12:24],2)


def as_frame(header,group):
    """OBU_FRAME payload from a frame header payload and its first tile group.
    That group signals tile start/end, which an OBU_FRAME must not."""
    bits=''.join(f'{byte:08b}' for byte in header).rstrip('0')
    if not bits.endswith('1'):raise SystemExit('frame header has no trailing bit')
    bits=bits[:-1];bits+='0'*(-len(bits)%8)
    return bytes(int(bits[i:i+8],2) for i in range(0,len(bits),8))+group


def variants(items,idc):
    """(name, items, expectation) for one source; expectation is 'exact',
    'unsupported' or an ERRORS key. libaom confirms every one."""
    groups=[i for i,(kind,_,_) in enumerate(items) if kind==4]
    header=next(i for i,(kind,_,_) in enumerate(items) if kind==3)
    sequence=next(i for i,(kind,_,_) in enumerate(items) if kind==1)
    if len(groups)<2 or groups[0]!=header+1:raise SystemExit('source is not a frame header followed by tile groups')
    def edit(f):
        copy=[list(item) for item in items];f(copy);return copy
    out=[('exact',items,'exact')]
    first,last=groups[0],groups[-1]
    out.append(('frame_then_groups',edit(lambda c:c.__setitem__(slice(header,first+1),[[6,c[header][1],as_frame(c[header][2],c[first][2])]])),'malformed'))
    out.append(('padding_between_groups',edit(lambda c:c.insert(first+1,[15,None,b'\x00\x11\x80\x00'])),'exact'))
    out.append(('empty_padding',edit(lambda c:c.insert(first+1,[15,None,b''])),'exact'))
    out.append(('reserved_obu',edit(lambda c:c.insert(sequence+1,[9,None,b'\x01\x02'])),'exact'))
    out.append(('redundant_copy',edit(lambda c:c.insert(first+1,[7,c[header][1],c[header][2]])),'exact'))
    out.append(('padding_unterminated',edit(lambda c:c.insert(first+1,[15,None,b'\x11\x01'])),'malformed'))
    out.append(('reserved_zero',edit(lambda c:c.insert(sequence+1,[10,None,b'\x00\x00'])),'malformed'))
    out.append(('redundant_mismatch',edit(lambda c:c.insert(first+1,[7,c[header][1],bytes([c[header][2][0]^1])+c[header][2][1:]])),'malformed'))
    out.append(('redundant_before_header',edit(lambda c:c.insert(header,[7,c[header][1],c[header][2]])),'malformed'))
    out.append(('header_twice',edit(lambda c:c.insert(first,[3,c[header][1],c[header][2]])),'malformed'))
    out.append(('group_dropped',edit(lambda c:c.pop(first)),'malformed'))
    out.append(('groups_swapped',edit(lambda c:c.__setitem__(slice(first,first+2),[c[first+1],c[first]])),'malformed'))
    out.append(('group_before_header',edit(lambda c:c.insert(header,c.pop(first))),'malformed'))
    out.append(('group_after_frame',edit(lambda c:c.append(list(c[last]))),'malformed'))
    out.append(('delimiter_inside_frame',edit(lambda c:c.insert(first+1,[2,None,b''])),'malformed'))
    out.append(('last_group_missing',edit(lambda c:c.pop(last)),'truncated'))
    out.append(('metadata',edit(lambda c:c.insert(sequence+1,[5,None,b'\x01\x00\x10\x00\x20\x80'])),'unsupported'))
    if idc:
        # Operating point 0 drops OBUs of other layers before any parsing.
        temporal=next(t for t in range(8) if not idc>>t&1);spatial=next(s for s in range(4) if not idc>>(s+8)&1)
        def foreign(c):
            c.insert(first+1,[4,(temporal,0),b'\xff\xff\xff']);c.insert(header,[3,(0,spatial),b'\xff\x00'])
        out.append(('foreign_layers_dropped',edit(foreign),'exact'))
        def own(c):
            for item in c:
                if item[0] in (3,4):item[1]=(0,0)
        out.append(('own_layer_extensions',edit(own),'exact'))
    else:
        def any_layer(c):
            for item in c:
                if item[0] in (3,4):item[1]=(3,2)
        out.append(('extensions_without_operating_points',edit(any_layer),'exact'))
    return out


def ivf(unit):
    head=b'DKIF'+struct.pack('<HHIHHIII',0,32,0x31305641,16,16,30,1,1)+b'\0\0\0\0'
    return head+struct.pack('<IQ',len(unit),0)+unit


def native(host,directory,name,unit):
    path=directory/f'{name}.ivf';path.write_bytes(ivf(unit))
    result=subprocess.run([str(host),str(path),'0'],text=True,capture_output=True)
    if result.returncode==7:return None
    if result.returncode!=0:raise SystemExit(f'{name}: native host exit {result.returncode}: {result.stderr.strip()}')
    frames=[];rows=[]
    for line in result.stdout.splitlines():
        fields=line.split()
        if fields[0]=='F':frames.append(list(map(int,fields[3:])))
        elif fields[0]=='R':rows.append(list(map(int,fields[2:])))
    if len(frames)!=1:raise SystemExit(f'{name}: native decode showed {len(frames)} frames')
    return frames[0],rows


def main():
    p=argparse.ArgumentParser()
    for name in ('root','out','aom','cache'):p.add_argument('--'+name,type=Path,required=True)
    a=p.parse_args();a.out.mkdir(parents=True,exist_ok=True)
    if subprocess.run(['git','-C',str(a.aom),'rev-parse','HEAD'],check=True,text=True,capture_output=True).stdout.strip()!=PIN:
        raise SystemExit('libaom pin mismatch')
    subprocess.run(['git','-C',str(a.aom),'diff','--quiet','HEAD','--'],check=True)
    library=Path(os.environ.get('AV1_AOM_LIBRARY',str(a.root/'build/native/aom/libaom.a')))
    config=Path(os.environ.get('AV1_AOM_CONFIG',str(a.root/'build/native/aom')))
    encoder_library=Path(os.environ.get('AV1_AOM_ENCODER_LIBRARY',str(library)))
    cc=shlex.split(os.environ.get('CC','cc'))
    source=a.out/'decoder.c';source.write_text(HOST);host=a.out/'decoder'
    subprocess.run(cc+['-std=c11','-O2','-I'+str(a.aom),'-I'+str(config),str(source),str(a.aom/'common/md5_utils.c'),
        str(library),'-lm','-lpthread','-o',str(host)],check=True)
    encoder_source=INTRA_ENCODER
    for old,new in (('if(argc!=19)return 1;','if(argc!=20)return 1;'),
        ('control(&codec,AV1E_SET_REDUCED_TX_TYPE_SET,reduced))return 4;',
         'control(&codec,AV1E_SET_REDUCED_TX_TYPE_SET,reduced)||control(&codec,AV1E_SET_NUM_TG,atoi(argv[19])))return 4;')):
        if encoder_source.count(old)!=1:raise SystemExit('intra encoder host changed; update the tile-group patch')
        encoder_source=encoder_source.replace(old,new)
    source=a.out/'encoder.c';source.write_text(encoder_source);encoder=a.out/'encoder'
    subprocess.run(cc+['-std=c11','-O2','-I'+str(a.aom),'-I'+str(encoder_library.parent),str(source),str(encoder_library),
        '-lm','-lpthread','-o',str(encoder)],check=True)
    work=a.out/'native';work.mkdir(exist_ok=True)
    sources=[]
    path=a.cache/SVC
    if hashlib.sha1(path.read_bytes()).hexdigest()!=pins(a.aom)[SVC]:raise SystemExit(f'{SVC} differs from the pinned test-data.sha1')
    sources.append((SVC+'#0',next(iter(temporal_units(path)))))
    for params in ENCODES:
        name='encode-'+'-'.join(map(str,params));stream=work/f'{name}.obu'
        subprocess.run([str(encoder),str(stream)]+[str(v) for v in params],check=True)
        sources.append((name,stream.read_bytes()))
    cases=[];counts={}
    for name,unit in sources:
        items=split(unit);kinds=[kind for kind,_,_ in items]
        if kinds.count(3)!=1 or kinds.count(4)<2 or 6 in kinds:raise SystemExit(f'{name}: expected one frame header and several tile groups, got {kinds}')
        idc=operating_idc(next(payload for kind,_,payload in items if kind==1))
        base=native(host,work,name+'.base',unit)
        if base is None:raise SystemExit(f'{name}: pinned libaom rejects the source')
        for variant,edited,expect in variants(items,idc):
            data=join(edited);key=f'{name}:{variant}'
            verdict=native(host,work,f'{name}.{variant}',data)
            if expect=='exact':
                if verdict is None or verdict!=base:raise SystemExit(f'{key}: pinned libaom does not decode it to the source frame')
            elif expect=='unsupported':
                if verdict is None or verdict!=base:raise SystemExit(f'{key}: pinned libaom does not decode the unsupported case')
            elif verdict is not None:raise SystemExit(f'{key}: pinned libaom accepts a case pinned as {expect}')
            (w,h,depth,mono,sx,sy),rows=base
            cases.append(dict(key=key,data=data,expect=expect,width=w,height=h,depth=depth,mono=mono,sx=sx,sy=sy,rows=rows))
            counts[expect]=counts.get(expect,0)+1
    # Checked IR grows with embedded bytes: shards stay under the conformance size.
    shards=[];size=0
    for case in cases:
        if not shards or size+len(case['data'])>SHARD_BYTES:shards.append([]);size=0
        shards[-1].append(case);size+=len(case['data'])
    decoder=a.root/'src/decoder.zi'
    for number,shard in enumerate(shards):
        directory=a.out/f'shard{number}';directory.mkdir(exist_ok=True)
        lines=['#import, file "'+os.path.relpath(decoder,directory)+'";','using Codec :: #import "image_codec/api"',COMMON,EXTRA,UNHEX]
        for i,case in enumerate(shard):
            body,declaration=zi_hex('bytes',case['data'],f'Framing{i}')
            lines.append(declaration)
            lines += [f'// {case["key"]}',f'Case{i} :: () -> s32 {{',body.rstrip('\n'),'    input := BorrowBytes(bytes[:])']
            if case['expect']=='exact':
                names=[]
                for plane,values in enumerate(case['rows']):
                    chunks=[','.join(map(str,values[j:j+16])) for j in range(0,len(values),16)]
                    lines.append(f'    rows{plane}: [{len(values)}]s64 = .[\n        '+',\n        '.join(chunks)+'\n    ]');names.append(f'rows{plane}[:]')
                while len(names)<3:names.append('rows0[:0]')
                lines.append(f'    return CheckFrame(input, {case["width"]}, {case["height"]}, {case["depth"]}, {str(bool(case["mono"])).lower()}, {case["sx"]}, {case["sy"]}, {", ".join(names)})')
            elif case['expect']=='unsupported':lines.append('    return CheckRejected(input)')
            else:lines.append(f'    return CheckError(input, {ERRORS[case["expect"]]})')
            lines.append('}')
        lines += ['#program_export','main :: () -> s32 {']
        lines += [f'    if Case{i}() != {1 if case["expect"]=="unsupported" else 0} {{ return {i+1} }}' for i,case in enumerate(shard)]
        lines += ['    return 0','}','']
        (directory/'av1_obu_framing_vectors.zi').write_text('\n'.join(lines))
        (directory/'cases.json').write_text(json.dumps([c['key'] for c in shard],indent=1)+'\n')
    (a.out/'oracle.json').write_text(json.dumps(dict(revision=PIN,library_sha256=hashlib.sha256(library.read_bytes()).hexdigest(),
        sources=[name for name,_ in sources],cases=len(cases),shards=len(shards),expected_outcomes=counts,
        scope='OBU framing of KEY stills: split frame headers and tile groups, OBU_FRAME continued by tile groups, redundant headers, '
            'padding, reserved OBUs, operating-point layer dropping and malformed orders, each verdict confirmed by pinned libaom; '
            'metadata OBUs remain UnsupportedCapability'),indent=2)+'\n')
    print(f'AV1 OBU framing: {len(sources)} sources, {len(cases)} variants in {len(shards)} shards confirmed by pinned libaom {counts}')


if __name__=='__main__':main()

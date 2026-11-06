#!/usr/bin/env python3
"""Pinned libaom decoder test vectors against the authored AV1 decoder.

Test-only native code: the pinned libaom decodes every temporal unit of each
vector in libaom's own test/test_vectors.cc list, and each output frame's MD5
must equal the upstream .md5 file. Every temporal unit whose first frame is a
KEY frame becomes a case (the latest sequence header is prepended when the
unit has none), plus the first small inter unit of each vector. The authored
decoder must reproduce the native per-row hashes of a decodable KEY frame
exactly, or reject with UnsupportedCapability. The checked-in
tests/av1_conformance_expected.json pins which of the two each case is, so a
regression and an improvement both fail until the file is updated.
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
from av1_conformance_fetch import pins, vectors
from av1_frame_oracle import UNHEX, zi_hex

PIN='d772e334cc724105040382a977ebb10dfd393293'
SHARD_BYTES=400000
INTER_LIMIT=8192
HOST=r'''
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include "aom/aom_decoder.h"
#include "aom/aomdx.h"
#include "common/md5_utils.h"
static unsigned get32(const unsigned char *p){return p[0]|p[1]<<8|p[2]<<16|(unsigned)p[3]<<24;}
static unsigned sample(const aom_image_t *image,int plane,unsigned x,unsigned y){
  const unsigned char *row=image->planes[plane]+(size_t)y*image->stride[plane];
  return image->fmt&AOM_IMG_FMT_HIGHBITDEPTH?((const uint16_t*)row)[x]:row[x];
}
int main(int argc,char **argv) {
  // argv[2]: comma-separated temporal-unit indices whose frames report rows.
  if(argc!=3)return 1;FILE *f=fopen(argv[1],"rb");if(!f)return 2;
  fseek(f,0,SEEK_END);long size=ftell(f);fseek(f,0,SEEK_SET);
  unsigned char *data=malloc(size);if(!data||fread(data,1,size,f)!=(size_t)size)return 3;fclose(f);
  if(size<32||memcmp(data,"DKIF",4))return 4;
  aom_codec_ctx_t codec={0};aom_codec_dec_cfg_t cfg={0};cfg.threads=1;cfg.allow_lowbitdepth=1;
  if(aom_codec_dec_init(&codec,aom_codec_av1_dx(),&cfg,0))return 5;
  long at=data[6]|data[7]<<8;int unit=0;
  while(at+12<=size) {
    unsigned bytes=get32(data+at);at+=12;if(at+bytes>size)return 6;
    if(aom_codec_decode(&codec,data+at,bytes,NULL)){fprintf(stderr,"native decode: %s\n",aom_codec_error(&codec));return 7;}
    int dump=0;char *list=argv[2];
    while(*list){char *end;long value=strtol(list,&end,10);if(end==list)break;if(value==unit)dump=1;list=*end?end+1:end;}
    aom_codec_iter_t it=NULL;aom_image_t *image;
    while((image=aom_codec_get_frame(&codec,&it))) {
      MD5Context md5;MD5Init(&md5);int bps=image->fmt&AOM_IMG_FMT_HIGHBITDEPTH?2:1;
      for(int plane=0;plane<3;plane++) {
        unsigned sx=plane?image->x_chroma_shift:0,sy=plane?image->y_chroma_shift:0;
        unsigned width=(image->d_w+sx)>>sx,height=(image->d_h+sy)>>sy;
        for(unsigned y=0;y<height;y++)MD5Update(&md5,image->planes[plane]+(size_t)y*image->stride[plane],width*bps);
      }
      unsigned char digest[16];MD5Final(digest,&md5);
      printf("F %d ",unit);for(int i=0;i<16;i++)printf("%02x",digest[i]);
      printf(" %u %u %u %d %u %u\n",image->d_w,image->d_h,image->bit_depth,image->monochrome,image->x_chroma_shift,image->y_chroma_shift);
      if(!dump)continue;
      for(int plane=0;plane<(image->monochrome?1:3);plane++) {
        unsigned sx=plane?image->x_chroma_shift:0,sy=plane?image->y_chroma_shift:0;
        unsigned width=(image->d_w+sx)>>sx,height=(image->d_h+sy)>>sy;
        printf("R %d",plane);
        for(unsigned y=0;y<height;y++) {
          long long hash=0;
          for(unsigned x=0;x<width;x++) {
            unsigned value=sample(image,plane,x,y);
            hash=(hash*131+(value&255)+1)%2147483629LL;
            if(image->bit_depth>8)hash=(hash*131+(value>>8)+1)%2147483629LL;
          }
          printf(" %lld",hash);
        }
        printf("\n");
      }
    }
    at+=bytes;unit++;
  }
  printf("U %d\n",unit);free(data);
  return aom_codec_destroy(&codec);
}
'''


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def leb128(data,at):
    value=shift=0
    while True:
        byte=data[at];at+=1;value|=(byte&127)<<shift;shift+=7
        if not byte&128:return value,at


def temporal_units(path):
    data=path.read_bytes()
    if data[:4]!=b'DKIF':raise SystemExit(f'{path.name} is not IVF')
    at=struct.unpack('<H',data[6:8])[0]
    while at<len(data):
        size=struct.unpack('<I',data[at:at+4])[0];yield data[at+12:at+12+size];at+=12+size


def obus(unit):
    """(type, whole OBU bytes, payload) of a sized low-overhead unit."""
    at=0
    while at<len(unit):
        start=at;header=unit[at];at+=1
        if header&4:at+=1
        if not header&2:raise SystemExit('unsized OBU in an IVF temporal unit')
        size,at=leb128(unit,at)
        yield header>>3&15,unit[start:at+size],unit[at:at+size];at+=size


def classify(unit):
    """'key' when the first frame header is a KEY frame, else 'inter'/'none'."""
    for kind,_,payload in obus(unit):
        if kind in (3,6):
            if payload[0]>>7:return 'existing'
            return 'key' if (payload[0]>>5)&3==0 else 'inter'
    return 'none'


def with_sequence(unit,sequence):
    if any(kind==1 for kind,_,_ in obus(unit)):return unit
    parts=[whole for _,whole,_ in obus(unit)]
    at=1 if parts and parts[0][0]>>3&15==2 else 0
    return b''.join(parts[:at])+sequence+b''.join(parts[at:])


COMMON='''
// Per-row hash over owned output bytes (little-endian samples), as the host.
CheckPlane :: (plane: *Plane, rows: []s64) -> s32 {
    if plane.height != rows.count { return 1000 }
    for row: 0..rows.count - 1 {
        hash: s64; base := row * plane.stride
        for i: 0..cast(s64)plane.width * plane.sample_bytes - 1 { hash = (hash * 131 + cast(s64)plane.storage[base + i] + 1) % 2147483629 }
        if hash != rows[row] { return cast(s32)row + 1 }
    }
    return 0
}
// 0: bit-exact; 1: UnsupportedCapability; anything else fails.
CheckFrame :: (input: BorrowedBytes, width: s32, height: s32, depth: s32, mono: bool, sx: s32, sy: s32,
    y: []s64, u: []s64, v: []s64) -> s32 {
    options := DefaultOptions(); budget := NewWorkerBudget(options.limits); options.workers = *budget
    image := DecodeAv1YUV(input, *options)
    if budget.active_workers != 0 || budget.working_bytes != 0 { return 2 }
    if !image.is_ok { return ifx image.error.code == UnsupportedCapability then 1 else 100 + cast(s32)image.error.code }
    sample: s32 = ifx depth == 8 then 1 else 2
    if image.y.width != width || image.y.height != height || image.y.sample_bytes != sample || image.sequence.bit_depth != depth { return 3 }
    if image.sequence.monochrome != mono || (!mono && (image.sequence.subsampling_x != sx || image.sequence.subsampling_y != sy)) { return 4 }
    check := CheckPlane(*image.y, y)
    if check != 0 { return 10000 + check }
    if mono { return ifx image.u.storage.count == 0 && image.v.storage.count == 0 then 0 else 5 }
    check = CheckPlane(*image.u, u)
    if check != 0 { return 20000 + check }
    check = CheckPlane(*image.v, v)
    if check != 0 { return 30000 + check }
    return 0
}
// A unit with no single native output frame must be rejected.
CheckRejected :: (input: BorrowedBytes) -> s32 {
    options := DefaultOptions(); budget := NewWorkerBudget(options.limits); options.workers = *budget
    image := DecodeAv1YUV(input, *options)
    if budget.active_workers != 0 || budget.working_bytes != 0 { return 2 }
    if image.is_ok { return 7 }
    return ifx image.error.code == UnsupportedCapability then 1 else 100 + cast(s32)image.error.code
}
'''


def main():
    p=argparse.ArgumentParser()
    for name in ('root','out','aom','cache'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--discover',action='store_true');a=p.parse_args();a.out.mkdir(parents=True,exist_ok=True)
    if subprocess.run(['git','-C',str(a.aom),'rev-parse','HEAD'],check=True,text=True,capture_output=True).stdout.strip()!=PIN:
        raise SystemExit('libaom pin mismatch')
    subprocess.run(['git','-C',str(a.aom),'diff','--quiet','HEAD','--'],check=True)
    library=Path(os.environ.get('AV1_AOM_LIBRARY',str(a.root/'build/native/aom/libaom.a')))
    config=Path(os.environ.get('AV1_AOM_CONFIG',str(a.root/'build/native/aom')))
    source=a.out/'decoder.c';source.write_text(HOST);host=a.out/'decoder'
    subprocess.run(shlex.split(os.environ.get('CC','cc'))+['-std=c11','-O2','-I'+str(a.aom),'-I'+str(config),str(source),
        str(a.aom/'common/md5_utils.c'),str(library),'-lm','-lpthread','-o',str(host)],check=True)
    expected_path=a.root/'tests/av1_conformance_expected.json'
    expected=None if a.discover else json.loads(expected_path.read_text())
    table=pins(a.aom);cases=[];streams=[];frames_checked=0
    for name in vectors(a.aom):
        if not name.endswith('.ivf'):continue # .mkv vectors need a WebM reader
        path=a.cache/name;md5=a.cache/(name+'.md5')
        if hashlib.sha1(path.read_bytes()).hexdigest()!=table[name] or hashlib.sha1(md5.read_bytes()).hexdigest()!=table[name+'.md5']:
            raise SystemExit(f'{name} differs from the pinned test-data.sha1; run av1_conformance_fetch.py')
        units=list(temporal_units(path));sequence=None;chosen=[];inter=None
        for index,unit in enumerate(units):
            for kind,whole,_ in obus(unit):
                if kind==1:sequence=whole
            kind=classify(unit)
            if kind=='key':
                if sequence is None:raise SystemExit(f'{name} unit {index}: KEY frame before any sequence header')
                chosen.append((index,with_sequence(unit,sequence)))
            elif kind=='inter' and inter is None and len(unit)<=INTER_LIMIT and sequence is not None:
                inter=(index,with_sequence(unit,sequence))
        lines=subprocess.run([str(host),str(path),','.join(str(i) for i,_ in chosen)],check=True,text=True,capture_output=True).stdout.splitlines()
        official=[line.split()[0] for line in md5.read_text().splitlines() if line.strip()]
        outputs={};frame=0;rows={}
        for line in lines:
            fields=line.split()
            if fields[0]=='F':
                if frame>=len(official) or fields[2]!=official[frame]:raise SystemExit(f'{name} frame {frame}: pinned libaom MD5 differs from the upstream .md5')
                outputs.setdefault(int(fields[1]),[]).append(list(map(int,fields[3:])));frame+=1;current=int(fields[1])
            elif fields[0]=='R':rows.setdefault(current,[]).append(list(map(int,fields[2:])))
            elif fields[0]=='U' and int(fields[1])!=len(units):raise SystemExit(f'{name}: native unit count differs')
        if frame!=len(official):raise SystemExit(f'{name}: {frame} native frames, {len(official)} upstream MD5 lines')
        frames_checked+=frame
        for index,data in chosen+([inter] if inter else []):
            key=f'{name}#{index}';shown=outputs.get(index,[])
            case=dict(key=key,data=data,kind='key' if inter is None or index!=inter[0] else 'inter')
            if case['kind']=='key' and len(shown)==1:
                w,h,depth,mono,sx,sy=shown[0];case.update(width=w,height=h,depth=depth,mono=mono,sx=sx,sy=sy,rows=rows[index])
            cases.append(case)
        streams.append(dict(name=name,units=len(units),frames=frame,key_units=len(chosen),inter_case=inter[0] if inter else None))
    if expected is not None and sorted(expected)!=sorted(c['key'] for c in cases):
        raise SystemExit('tests/av1_conformance_expected.json does not list exactly the generated cases')
    shards=[];size=0
    for case in cases:
        if not shards or size+len(case['data'])>SHARD_BYTES:shards.append([]);size=0
        shards[-1].append(case);size+=len(case['data'])
    decoder=a.root/'src/decoder.zi'
    for number,shard in enumerate(shards):
        directory=a.out/f'shard{number}';directory.mkdir(exist_ok=True)
        lines=['#import, file "'+os.path.relpath(decoder,directory)+'";','using Codec :: #import "image_codec/api"',COMMON,UNHEX]
        for i,case in enumerate(shard):
            body,declaration=zi_hex('bytes',case['data'],f'Conformance{i}')
            lines.append(declaration)
            lines += [f'// {case["key"]}',f'Case{i} :: () -> s32 {{',body.rstrip('\n'),'    input := BorrowBytes(bytes[:])']
            if 'rows' in case:
                names=[]
                for plane,values in enumerate(case['rows']):
                    # Sources cap lines at 4095 characters: 16 row hashes per line.
                    chunks=[','.join(map(str,values[i:i+16])) for i in range(0,len(values),16)]
                    lines.append(f'    rows{plane}: [{len(values)}]s64 = .[\n        '+',\n        '.join(chunks)+'\n    ]');names.append(f'rows{plane}[:]')
                while len(names)<3:names.append('rows0[:0]')
                lines.append(f'    return CheckFrame(input, {case["width"]}, {case["height"]}, {case["depth"]}, {str(bool(case["mono"])).lower()}, {case["sx"]}, {case["sy"]}, {", ".join(names)})')
            else:lines.append('    return CheckRejected(input)')
            lines.append('}')
        lines += ['// Outcome of one case, for the discovery harness.','#program_export','Probe :: (index: s32) -> s32 {']
        lines += [f'    if index == {i} {{ return Case{i}() }}' for i in range(len(shard))]
        lines += ['    return -1','}','#program_export','main :: () -> s32 {']
        if expected is not None:
            lines += [f'    if Case{i}() != {expected[case["key"]]} {{ return {i+1} }}' for i,case in enumerate(shard)]
        lines += ['    return 0','}','']
        (directory/'av1_conformance_vectors.zi').write_text('\n'.join(lines))
        (directory/'cases.json').write_text(json.dumps([c['key'] for c in shard])+'\n')
    outcome={}
    if expected is not None:
        for value in expected.values():outcome[{0:'bit_exact',1:'unsupported'}[value]]=outcome.get({0:'bit_exact',1:'unsupported'}[value],0)+1
    (a.out/'oracle.json').write_text(json.dumps(dict(revision=PIN,library_sha256=digest(library),native_host_sha256=hashlib.sha256(HOST.encode()).hexdigest(),
        vector_list='libaom test/test_vectors.cc (.ivf; the two .mkv vectors need a WebM reader)',streams=len(streams),
        native_frames_md5_checked=frames_checked,cases=len(cases),key_cases=sum(c['kind']=='key' for c in cases),
        inter_cases=sum(c['kind']=='inter' for c in cases),shards=len(shards),expected_outcomes=outcome,stream_records=streams,
        scope='KEY temporal units of the pinned libaom decoder test vectors: bit-exact against native rows (and transitively the upstream MD5) or UnsupportedCapability; inter frames reject'),indent=2)+'\n')
    print(f'Pinned libaom test vectors: {len(streams)} streams, {frames_checked} native frames MD5-checked, {len(cases)} cases in {len(shards)} shards')


if __name__=='__main__':main()

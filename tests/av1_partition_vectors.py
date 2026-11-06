#!/usr/bin/env python3
"""Standard AV1 rectangular partitions with real upstream arithmetic coding.
Native full-frame decode and actual coding-block shapes validate every vector.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
from av1_coefficient_edges import HOST as ENCODER
from av1_lossless_oracle import PIN, run


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True);p.add_argument('--aom',type=Path,required=True)
    a=p.parse_args()
    if run(['git','-C',str(a.aom),'rev-parse','HEAD'],text=True,capture_output=True).stdout.strip()!=PIN:raise SystemExit('libaom pin mismatch')
    run(['git','-C',str(a.aom),'diff','--quiet','HEAD','--'])
    source=a.out/'partition_encoder.c';source.write_text(ENCODER);host=a.out/'partition_encoder'
    library=Path(os.environ.get('AV1_AOM_LIBRARY',str(a.root/'build/native/aom/libaom.a')))
    config=Path(os.environ.get('AV1_AOM_CONFIG',str(a.root/'build/native/aom')))
    run(shlex.split(os.environ.get('CC','cc'))+['-std=c11','-O2','-I'+str(a.aom),'-I'+str(config),
        str(source),str(a.aom/'aom_dsp/entenc.c'),str(library),'-lm','-o',str(host)])
    upstream=(a.aom/'av1/common/entropymode.c').read_text()
    text=upstream.split('default_partition_cdf',1)[1].split('= {',1)[1]
    # All 20 contexts: width8 CDF4, width16-64 CDF10, width128 CDF8.
    defaults=[]
    for m in re.finditer(r'AOM_CDF(4|8|10)\(([^)]+)\)',text):
        defaults.append([int(x.strip()) for x in m[2].split(',')]+[32768])
        if len(defaults)==20:break
    literal=re.search(r'cells: \[220\]u16 = \.\[([^]]+)\]',(a.root/'src/syntax/partition.zi').read_text(),re.S)
    expected=[v for row in defaults for v in row+[0]*(11-len(row))]
    if len(defaults)!=20 or literal is None or list(map(int,literal[1].replace('\n','').split(',')))!=expected:
        raise SystemExit('partition default constants differ from pinned source')
    # The 16x16 fixture encoder uses the width8-64 tables; its own tables follow.
    tables=defaults[:16]
    tables += [[31671,32768],[16515,32768],[4576,32768],
        [15588,17027,19338,20218,20682,21110,21825,23244,24189,28165,29093,30466,32768],[5892,32768]]
    def encode(operations):
        data=str(len(tables))+'\n'+''.join(str(len(row))+' '+' '.join(map(str,row))+'\n' for row in tables)
        data+=str(len(operations))+'\n'+''.join(f'{table} {symbol} 1\n' for table,symbol in operations)
        result=list(map(int,run([str(host)],input=data,text=True,capture_output=True).stdout.split()))
        if result[0]!=len(result)-1:raise SystemExit('encoder size mismatch')
        return bytes(result[1:])
    cases=[]
    for depth in (8,10,12):
        template=(a.out/f'16-{depth}-constant-default.obu').read_bytes()
        # Independently parse low-overhead OBU sizes to preserve the original
        # real sequence and its two-byte reduced/lossless frame header.
        at=0
        while at<len(template):
            start=at;header=template[at];at+=1
            if header&4:at+=1
            length=shift=0
            while True:
                byte=template[at];at+=1;length|=(byte&127)<<shift;shift+=7
                if not byte&128:break
            if header>>3&15==6:break
            at+=length
        if template[at:at+2]!=bytes([16,0]):raise SystemExit('native frame template header changed')
        def rectangles(part):
            if part==0:return [(0,0,16,16)]
            if part==1:return [(0,0,16,8),(8,0,16,8)]
            if part==2:return [(0,0,8,16),(0,8,8,16)]
            if part==3:return [(0,0,8,8),(0,8,8,8),(8,0,8,8),(8,8,8,8)]
            if part==4:return [(0,0,8,8),(0,8,8,8),(8,0,16,8)]
            if part==5:return [(0,0,16,8),(8,0,8,8),(8,8,8,8)]
            if part==6:return [(0,0,8,8),(8,0,8,8),(0,8,8,16)]
            if part==7:return [(0,0,8,16),(0,8,8,8),(8,8,8,8)]
            if part==8:return [(row,0,16,4) for row in (0,4,8,12)]
            return [(0,column,4,16) for column in (0,4,8,12)]
        for variant in (0,1):
          for part in range(10):
              operations=[(4,part)];skips=[[0]*4 for _ in range(4)]
              blocks=rectangles(part)
              for row,column,width,height in blocks:
                  if part==3:operations.append((0,0))
                  r=row//4;c=column//4
                  context=(skips[r-1][c] if r else 0)+(skips[r][c-1] if c else 0)
                  operations += [(16+context,variant),(19,0)]
                  if variant==0:operations += [(20,1)]*((width//4)*(height//4))
                  for y in range(r,r+height//4):
                      for x in range(c,c+width//4):skips[y][x]=variant
              tile=encode(operations)
              if len(tile)+2>=128:raise SystemExit('tile length exceeds single-byte fixture length')
              data=template[:start]+bytes([header,len(tile)+2])+template[at:at+2]+tile
              name=f'partition-{depth}-{part}-{variant}.obu';path=a.out/name;path.write_bytes(data)
              output=run([str(a.out/'oracle'),str(path)],text=True,capture_output=True).stdout.splitlines()
              if list(map(int,output[0].split()))!=[16,depth]+[1<<(depth-1)]*256:raise SystemExit('native partition plane mismatch')
              actual_count,modes,rects=map(int,output[1].split())
              if actual_count!=len(blocks) or modes or rects!=sum(w!=h for _,_,w,h in blocks):
                  raise SystemExit('native coding-block shape mismatch')
              cases.append(dict(name=name,depth=depth,part=part,blocks=len(blocks),rectangles=rects,data=list(data)))
    rel=os.path.relpath(a.root/'src/decoder.zi',a.out)
    lines=['#import, file "'+rel+'";','#program_export','main :: () -> s32 {']
    for i,c in enumerate(cases):
        lines+=['    {','        bytes: ['+str(len(c['data']))+']u8 = .['+','.join(map(str,c['data']))+']',
            '        options := DefaultOptions()',
            '        budget := NewWorkerBudget(options.limits); options.workers = *budget',
            '        image := DecodeAv1YUV(BorrowBytes(bytes[:]), *options)',
            f'        if !image.is_ok {{ return {i+1} }}','        for pixel: 0..255 {',
            '            value := cast(u32)image.y.storage[pixel * image.y.sample_bytes]',
            '            if image.y.sample_bytes == 2 { value |= cast(u32)image.y.storage[pixel * 2 + 1] << 8 }',
            f'            if value != {1<<(c["depth"]-1)} {{ return 100 }}','        }',
            '        if budget.active_workers != 0 || budget.working_bytes != 0 { return 101 }','    }']
    lines+=['    return 0','}']
    (a.out/'av1_partition_vectors.zi').write_text('\n'.join(lines)+'\n')
    receipt=dict(revision=PIN,frames=len(cases),partition_types=list(range(10)),depths=[8,10,12],
        coding_blocks=sum(c['blocks'] for c in cases),rectangular_blocks=sum(c['rectangles'] for c in cases),
        scope='native-decoded standard NONE/HORZ/VERT/SPLIT/A/B/1:4 whole-frame vectors; general conformance pending')
    (a.out/'partition_oracle.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(f'Pinned partition oracle: {len(cases)} native whole frames, all10 types, {receipt["rectangular_blocks"]} rectangular blocks')


if __name__=='__main__':main()

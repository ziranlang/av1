#!/usr/bin/env python3
"""Record logical record bounds and current native C stack evidence.
This is not a measurement or guarantee of process RSS, allocator size classes,
Go/VM/runtime housekeeping, or caller-retained output objects.
"""
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True)
    p.add_argument('--toolchain',type=Path,required=True);a=p.parse_args()
    types=['OperatingPoint','BitReader','RangeDecoder','SequenceHeader','TileLayout','StillFrameHeader',
        'Av1YUVResult','PartitionCDF',
        'IntraModeCDF','FilterIntraCDF','PaletteCDF','PaletteUVCDF','PaletteBlock','PaletteCache','PaletteIndexContext','ChromaModeCDF','ChromaModeResult','Residual4x4',
        'FilmGrainParams','FilmGrainResult','GrainRandom','GrainNumber','GrainScaling','GrainNoise','GrainPlanes',
        'IntraFrame','IntraTile','IntraBlock','IntraTileCDFs','IntraEdges','CoefficientSummary',
        'TransformCoefficientContexts','TransformTypeResult','QuantizerIndex','CoefficientSymbol','TileGroupCursor','TileSpanResult']
    host=a.out/'native_layout.c'
    # Every generated product header; the test entry's own header declares main.
    headers=''.join(f'#include "{path.name}"\n' for path in sorted((a.out/'source').glob('*.h')) if not path.name.startswith('av1_'))
    host.write_text(headers+'#include <stdio.h>\n'+'int main(void) {\n'+
        ''.join(f'printf("{name} %zu\\n",sizeof({name}));\n' for name in types)+'return 0;\n}\n')
    command=shlex.split(os.environ.get('CC','cc'))+['-std=c11','-O2','-I'+str(a.out/'source'),
        '-I'+str(a.toolchain/'include'),str(host),'-o',str(a.out/'native_layout')]
    subprocess.run(command,check=True)
    output=subprocess.run([str(a.out/'native_layout')],check=True,text=True,capture_output=True).stdout
    sizes={name:int(size) for name,size in (line.split() for line in output.splitlines())}
    functions={}
    for path in sorted((a.out/'source').glob('*.su')):
        with path.open() as source:
            for line in source:
                columns=line.rstrip().split('\t')
                if len(columns)!=3:raise SystemExit('unrecognized native stack-usage record')
                name=columns[0].rsplit(':',1)[-1]
                if name.startswith('zir_main_'):continue # caller/test harness
                if 'dynamic' in columns[2] and 'bounded' not in columns[2]:
                    raise SystemExit('unbounded native frame: '+name)
                functions[name]=max(functions.get(name,0),int(columns[1]))
    recursion=[size for name,size in functions.items() if 'DecodeIntraPartition' in name]
    if not functions or not recursion:raise SystemExit('native -fstack-usage evidence missing')
    # Every nonrecursive generated function is counted simultaneously, which
    # deliberately overcounts sequential helpers/factories. Add six extra
    # copies of the largest recursive frame; actual depth is six including
    # the 128 root and the 4x4 leaf.
    native_bound=sum(functions.values())+6*max(recursion)
    copies={'SequenceHeader':4,'StillFrameHeader':4,
        'Av1YUVResult':2,'PartitionCDF':3,'IntraModeCDF':3,'FilterIntraCDF':3,'ChromaModeCDF':3,'ChromaModeResult':2,
        'PaletteCDF':3,'PaletteUVCDF':3,'PaletteBlock':1,'PaletteCache':3,'PaletteIndexContext':3,'Residual4x4':2,
        'FilmGrainParams':10,'FilmGrainResult':4,'GrainRandom':4,'GrainNumber':6,'GrainScaling':8,'GrainNoise':4,'GrainPlanes':3,
        'IntraFrame':2,'IntraTile':2,'IntraBlock':2,'IntraTileCDFs':4,'IntraEdges':2,'CoefficientSummary':3,
        'TransformCoefficientContexts':2,'TransformTypeResult':3,'QuantizerIndex':3,'CoefficientSymbol':2,'TileGroupCursor':2,'TileSpanResult':2}
    # Tile state already embeds its result/CDF arrays and the coefficient and
    # dequantization buffers; these separate copies
    # intentionally count factories, return values and caller temporaries too.
    record_bound=sum(sizes[name]*count for name,count in copies.items())
    fixed_leaf_recursion_and_scalar_allowance=32768
    # Three copies of the once-per-generation Gaussian table (factory/local/
    # return). GrainNoise counts above cover the noise templates; the grain
    # stripe scratch is frame heap (GrainScratchCount), reserved with the frame.
    grain_scalar_scratch_allowance=3*2048*2
    logical_bound=record_bound+fixed_leaf_recursion_and_scalar_allowance+grain_scalar_scratch_allowance
    reserved=1048576
    if native_bound>=reserved or logical_bound>=reserved:raise SystemExit('AV1 working reservation is insufficient')
    receipt=dict(reserved_working_bytes=reserved,maximum_planes=3,maximum_partition_recursion_frames=6,
        heap_reservation='separate per frame: padded u16 planes, 16-byte 4x4 mode records, line contexts, palette lines, CDEF and restoration units (PrepareIntraFrame.working_bytes), plus GrainScratchCount(width) s16 grain stripes when grain applies',
        record_sizes_native_64bit=sizes,conservative_resident_copy_counts=copies,
        record_bytes=record_bound,fixed_scalar_recursion_allowance=fixed_leaf_recursion_and_scalar_allowance,
        grain_scalar_scratch_allowance=grain_scalar_scratch_allowance,
        logical_codec_bound_bytes=logical_bound,native_stack_function_bytes=functions,
        conservative_native_generated_stack_bytes=native_bound,
        native_stack_flags='-std=c11 -O2 -fstack-usage',layout_command=command,
        exclusions=['caller input','caller-retained output planes','allocator size classes',
            'Go/VM/runtime housekeeping','physical process RSS'],
        scope='fixed records and native stack of the scalar intra still path including whole-frame film grain; frame heap reserved separately; recompute on expanded scratch/kernels')
    (a.out/'working-memory.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(f'AV1 fixed codec records bound {logical_bound}B; conservative generated-C stack {native_bound}B; reservation {reserved}B (runtime/RSS excluded)')


if __name__=='__main__':main()

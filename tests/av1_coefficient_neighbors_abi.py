#!/usr/bin/env python3
"""Test-only forged descriptors must reject before reading null storage."""
import argparse
from pathlib import Path
import os
import re
import shlex
import subprocess


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True)
    p.add_argument('--toolchain',type=Path,required=True);a=p.parse_args()
    header=(a.out/'av1_coefficient_neighbors_abi.h').read_text()
    match=re.search(r'int32_t (\w*ProbeNeighborDescriptors)\(Slice',header)
    if not match:raise SystemExit('Cannot locate selected native neighbor-descriptor entry')
    entry=match.group(1);host=a.out/'host.c'
    host.write_text('#include "av1_coefficient_neighbors_abi.h"\n#include <stdint.h>\n#include <stddef.h>\n'
        'int main(void) {\n'
        'int64_t counts[]={INT64_MIN,-1,2,15,16,17,INT32_MAX,2147483648LL,INT64_MAX};\n'
        f'for(int kind=0;kind<4;kind++)for(unsigned i=0;i<sizeof(counts)/sizeof(*counts);i++)if({entry}((Slice){{NULL,counts[i]}},kind))return 1;\n'
        f'for(int kind=2;kind<4;kind++)if({entry}((Slice){{NULL,1}},kind))return 2;\n'
        f'return {entry}((Slice){{NULL,0}},4);\n}}\n')
    binary=a.out/'test'
    subprocess.run(shlex.split(os.environ.get('CC','cc'))+['-std=c11','-O1','-g',
        '-fsanitize=address,undefined','-fno-sanitize-recover=all','-fno-omit-frame-pointer',
        '-I'+str(a.out),'-I'+str(a.toolchain/'include')]+list(map(str,sorted(a.out.glob('*.c'))))+['-lm','-o',str(binary)],check=True)
    subprocess.run([str(binary)],check=True,env=dict(os.environ,ASAN_OPTIONS='detect_leaks=1',UBSAN_OPTIONS='halt_on_error=1'))
    print('AV1 neighbors native ABI:38 invalid null-data descriptors reject before access; empty spans succeed')


if __name__=='__main__':main()

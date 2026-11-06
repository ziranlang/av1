#!/usr/bin/env python3
"""Test only: forge native descriptors at the public compiler-owned Slice ABI."""
import argparse
from pathlib import Path
import os
import re
import shlex
import subprocess


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True)
    p.add_argument('--toolchain',type=Path,required=True);a=p.parse_args()
    header=(a.out/'av1_quantization_abi.h').read_text()
    match=re.search(r'int32_t (\w*ProbeQuantizationDescriptors)\(Slice',header)
    if not match:raise SystemExit('Cannot locate selected native descriptor probe entry')
    entry=match.group(1)
    host=a.out/'host.c'
    host.write_text('#include "av1_quantization_abi.h"\n#include <stdint.h>\n#include <stddef.h>\n'
        'int main(void) {\n'
        'int64_t quant_counts[]={INT64_MIN,-1,0,1,15,17,1023,1025,INT32_MAX,2147483648LL,INT64_MAX};\n'
        'int64_t weight_counts[]={INT64_MIN,-1,1,15,17,1023,1025,INT32_MAX,2147483648LL,INT64_MAX};\n'
        f'for(unsigned i=0;i<sizeof(quant_counts)/sizeof(*quant_counts);i++)if({entry}((Slice){{NULL,quant_counts[i]}},(Slice){{NULL,0}},0))return 1;\n'
        f'for(unsigned i=0;i<sizeof(weight_counts)/sizeof(*weight_counts);i++)if({entry}((Slice){{NULL,0}},(Slice){{NULL,weight_counts[i]}},1))return 2;\n'
        f'return {entry}((Slice){{NULL,0}},(Slice){{NULL,0}},2);\n}}\n')
    binary=a.out/'test'
    subprocess.run(shlex.split(os.environ.get('CC','cc'))+['-std=c11','-O1','-g',
        '-fsanitize=address,undefined','-fno-sanitize-recover=all','-fno-omit-frame-pointer',
        '-I'+str(a.out),'-I'+str(a.toolchain/'include')]+list(map(str,sorted(a.out.glob('*.c'))))+['-lm','-o',str(binary)],check=True)
    subprocess.run([str(binary)],check=True,env=dict(os.environ,ASAN_OPTIONS='detect_leaks=1',UBSAN_OPTIONS='halt_on_error=1'))
    print('AV1 quantization native ABI:21 invalid null-data descriptors reject before arithmetic/access; empty matrix descriptor succeeds')


if __name__=='__main__':main()

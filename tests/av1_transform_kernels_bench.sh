#!/bin/sh
set -eu
. "$(dirname -- "$0")/common.sh"
out="$root/build/scratch/av1-transform-kernels-bench"
mkdir -p "$out"
aom=${AV1_AOM_SOURCE:-$("$ziran" pkg path libaom)}
"$ziran" build --target=c --entry av1_transform_kernels_bench:KernelBench --root "$root/tests" \
    --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std" \
    -o "$out/generated" "$root/tests/av1_transform_kernels_bench.zi"
${CC:-cc} -std=c11 -O3 -flto=auto -I"$toolchain/include" -I"$out/generated" \
    -I"$aom" -I"$AV1_AOM_CONFIG" "$root/tests/av1_transform_kernels_bench.c" \
    "$out/generated/"*.c "$AV1_AOM_LIBRARY" -lm -lpthread -o "$out/run"
taskset -c "${AV1_BENCH_CPU:-2}" "$out/run" "${AV1_BENCH_DEPTH:-8}"

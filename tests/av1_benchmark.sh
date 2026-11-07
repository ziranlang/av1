#!/bin/sh
# Baseline timing of the authored decoder against pinned libaom and dav1d on
# one core. Run through build-queue. Records numbers, not claims.
set -eu
. "$(dirname -- "$0")/common.sh"
out="$root/build/scratch/av1-benchmark"
rm -rf "$out"; mkdir -p "$out"
aom=${AV1_AOM_SOURCE:-$("$ziran" pkg path libaom)}
dav1d=${AV1_DAV1D_SOURCE:-$("$ziran" pkg path dav1d)}
if test ! -f "$root/build/native/dav1d/lib/libdav1d.a"; then sh tests/av1_headers.sh; fi
"$ziran" build --target=c --no-main --entry av1_benchmark:Av1BenchDecode --root tests \
    --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std" -o "$out/authored" tests/av1_benchmark.zi
python3 tests/av1_benchmark.py --root "$root" --out "$out" --aom "$aom" --authored "$out/authored" --toolchain "$toolchain" \
    --dav1d-root "$root/build/native/dav1d" --dav1d-source "$dav1d" --cpu "${AV1_BENCH_CPU:-2}"
printf '%s\n' "AV1 benchmark: receipt in build/scratch/av1-benchmark/receipt.json"

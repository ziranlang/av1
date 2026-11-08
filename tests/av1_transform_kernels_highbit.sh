#!/bin/sh
set -eu
. "$(dirname -- "$0")/common.sh"
out="$root/build/scratch/av1-transform-kernels-highbit"
mkdir -p "$out"
entry=av1_transform_kernels_highbit
sha256sum "$root/src/transform/inverse_transform.zi" "$root/src/transform/dct8x8.zi" \
    "$root/src/transform/kernels_8bit.zi" "$root/src/transform/kernels_highbit.zi" \
    "$root/tests/$entry.zi" "$root/tests/$entry.sh" "$root/tests/${entry}_host.c" \
    "$root/tests/common.sh" "$ziran" "$bin/zi2zir" "$bin/zi2c" "$bin/zi2go" "$bin/zi2zx" > "$out/inputs.sha256"
set -- --root "$root/tests" --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std"
"$ziran" ir "$@" -o "$out/ir" "$root/tests/$entry.zi"
for form in source saved; do
    input="$root/tests/$entry.zi"
    set -- --root "$root/tests" --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std"
    if test "$form" = saved; then
        input="$out/ir/$entry.zir"
        set -- --root "$out/ir" --module-path "$out/ir"
    fi
    "$ziran" build --target=c --entry "$entry:HighTestCheck" "$@" -o "$out/$form" "$input"
    ${CC:-cc} -O3 -DZIRAN_ASM_CHECK -I"$toolchain/include" -I"$out/$form" \
        "$root/tests/av1_transform_kernels_highbit_host.c" "$out/$form/"*.c -o "$out/$form/$entry"
    for tier in 1 2 3 4; do
        ZIRAN_SIMD_TIER=$tier "$out/$form/$entry"
    done
    "$ziran" bundle --entry "$entry:PortableMain" "$@" -o "$out/$form.zx" "$input"
    test "$("$bin/zi2zx" run --vm "$out/$form.zx")" = 0
    "$ziran" build --target=go --pkg main --entry "$entry:main" "$@" -o "$out/$form-go" "$input"
    printf 'package main\nfunc main() { if Av1TransformKernelsHighbit_Main() != 0 { panic("AV1 transform kernels") } }\n' > "$out/$form-go/main.go"
    (cd "$out/$form-go" && GO111MODULE=off go build -o run . && ./run)
done
CFLAGS='-O0 -g -fsanitize=address,undefined -fno-omit-frame-pointer' LDFLAGS='-fsanitize=address,undefined' \
    "$ziran" build --target=c --exe --entry "$entry:main" --root "$root/tests" \
    --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std" \
    -o "$out/sanitized" "$root/tests/$entry.zi"
ASAN_OPTIONS=detect_leaks=1 UBSAN_OPTIONS=halt_on_error=1 "$out/sanitized/$entry"
sha256sum --check --status "$out/inputs.sha256"
printf '%s\n' 'AV1 transform kernels: all size/type pairs, dirty boxes, in-place outputs and rejection preservation; source/saved C tiers 1-4, Go, VM, ASan/UBSan passed'

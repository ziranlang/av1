#!/bin/sh
set -eu
. "$(dirname -- "$0")/common.sh"
out="$root/build/scratch/av1-coefficient-symbols"
module=av1_coefficient_symbols
mkdir -p "$out"
"$ziran" ir --root "$root/tests" --module-path "image_codec=$foundation/src" \
    --module-path "std=$toolchain/std" -o "$out/ir" "$root/tests/$module.zi"
for form in source saved; do
    input="$root/tests/$module.zi"
    set -- --root "$root/tests" --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std"
    if test "$form" = saved; then input="$out/ir/$module.zir"; set -- --root "$out/ir" --module-path "$out/ir"; fi
    "$ziran" build --target=c --exe --entry "$module:main" "$@" -o "$out/$form" "$input"
    "$out/$form/$module"
    "$ziran" bundle --entry "$module:main" "$@" -o "$out/$form.zx" "$input"
    test "$("$bin/zi2zx" run --vm "$out/$form.zx")" = 0
    "$ziran" build --target=go --pkg main --entry "$module:main" "$@" -o "$out/$form-go" "$input"
    printf 'package main\nfunc main() { if Av1CoefficientSymbols_Main() != 0 { panic("AV1 coefficient symbols") } }\n' > "$out/$form-go/main.go"
    (cd "$out/$form-go" && GO111MODULE=off go build -o run . && ./run)
done
CFLAGS='-O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer' LDFLAGS='-fsanitize=address,undefined' \
    "$ziran" build --target=c --exe --entry "$module:main" --root "$root/tests" \
    --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std" \
    -o "$out/sanitized" "$root/tests/$module.zi"
ASAN_OPTIONS=detect_leaks=1 UBSAN_OPTIONS=halt_on_error=1 "$out/sanitized/$module"
printf '%s\n' 'AV1 coefficient symbols: fixed CDF/state parity, source/saved C/Go/VM + ASan/UBSan passed'

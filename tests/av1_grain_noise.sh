#!/bin/sh
set -eu
. "$(dirname -- "$0")/common.sh"
out="$root/build/scratch/av1-grain-noise"
aom=${AV1_AOM_SOURCE:-$("$ziran" pkg path libaom)}
python3 tests/av1_grain_noise_oracle.py --root "$root" --out "$out" --aom "$aom"
module=av1_grain_noise_vectors
for shard in 0 1 2 3 4 5; do
group="$out/shard$shard"
printf '%s\n' "AV1 Gaussian/AR corpus shard $shard/5"
"$ziran" ir --root "$group" --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std" -o "$group/ir" "$group/$module.zi"
for form in source saved; do
    input="$group/$module.zi"
    set -- --root "$group" --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std"
    if test "$form" = saved; then input="$group/ir/$module.zir"; set -- --root "$group/ir" --module-path "$group/ir"; fi
    "$ziran" build --target=c --exe --entry "$module:main" "$@" -o "$group/$form" "$input"
    "$group/$form/$module"
    "$ziran" bundle --entry "$module:main" "$@" -o "$group/$form.zx" "$input"
    test "$("$bin/zi2zx-vm" run "$group/$form.zx")" = 0
    "$ziran" build --target=go --pkg main --entry "$module:main" "$@" -o "$group/$form-go" "$input"
    printf 'package main\nfunc main() { if Av1GrainNoiseVectors_Main() != 0 { panic("AV1 scalar Gaussian/AR grain noise") } }\n' > "$group/$form-go/main.go"
    (cd "$group/$form-go" && GO111MODULE=off go build -o run . && ./run)
done
CFLAGS='-O0 -g -fsanitize=address,undefined -fno-omit-frame-pointer' LDFLAGS='-fsanitize=address,undefined' \
    "$ziran" build --target=c --exe --entry "$module:main" --root "$group" \
    --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std" -o "$group/sanitized" "$group/$module.zi"
ASAN_OPTIONS=detect_leaks=1 UBSAN_OPTIONS=halt_on_error=1 "$group/sanitized/$module"
done
python3 - "$out" <<'PY'
import json,pathlib,sys
out=pathlib.Path(sys.argv[1]);receipt=json.loads((out/'oracle.json').read_text())
receipt.update(command='build-queue -- sh tests/av1_grain_noise.sh',exit_code=0,
    forms=['source','saved IR'],targets=['C','Go','direct zi2zx-vm'],sanitizers=['ASan','UBSan'],sanitizer_optimization='-O0')
(out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
PY
printf '%s\n' 'AV1 scalar Gaussian/AR grain noise: pinned vectors, source/saved C/Go/directVM + ASan/UBSan passed'

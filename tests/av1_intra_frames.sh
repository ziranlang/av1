#!/bin/sh
set -eu
. "$(dirname -- "$0")/common.sh"
out="$root/build/scratch/av1-intra-frames"
mkdir -p "$out"
aom=${AV1_AOM_SOURCE:-$("$ziran" pkg path libaom)}
python3 tests/av1_intra_frames_oracle.py --root "$root" --out "$out" --aom "$aom"
if test "${AV1_INTRA_FRAMES_PREPARE_ONLY:-0}" = 1; then exit 0; fi
module=av1_intra_frames_vectors
"$ziran" ir --root "$out" --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std" -o "$out/ir" "$out/$module.zi"
for form in source saved; do
    input="$out/$module.zi"
    set -- --root "$out" --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std"
    if test "$form" = saved; then input="$out/ir/$module.zir"; set -- --root "$out/ir" --module-path "$out/ir"; fi
    "$ziran" build --target=go --pkg main --entry "$module:main" "$@" -o "$out/$form-go" "$input"
    printf 'package main\nfunc main() { if r := Av1IntraFramesVectors_Run(); r != 0 { panic(r) } }\n' > "$out/$form-go/main.go"
    (cd "$out/$form-go" && GO111MODULE=off go build -o run . && ./run)
    "$ziran" build --target=c --exe --entry "$module:main" "$@" -o "$out/$form" "$input"
    "$out/$form/$module"
    "$ziran" bundle --entry "$module:main" "$@" -o "$out/$form.zx" "$input"
    test "$("$bin/zi2zx" run --vm "$out/$form.zx")" = 0
done
CFLAGS='-O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer' LDFLAGS='-fsanitize=address,undefined' \
    "$ziran" build --target=c --exe --entry "$module:main" --root "$out" \
    --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std" -o "$out/sanitized" "$out/$module.zi"
ASAN_OPTIONS=detect_leaks=1 UBSAN_OPTIONS=halt_on_error=1 "$out/sanitized/$module"
python3 - "$out" <<'PY'
import json,pathlib,sys
out=pathlib.Path(sys.argv[1]);receipt=json.loads((out/'oracle.json').read_text())
receipt.update(command='build-queue -- sh tests/av1_intra_frames.sh',exit_code=0,
    forms=['source','saved IR'],targets=['Go','C','zi2zx run --vm'],sanitizers=['ASan','UBSan'])
(out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
PY
printf '%s\n' 'AV1 intra frames: pinned libaom lossy/lossless stills (tiles, 128 SB, palette, CfL, filter intra, segmentation, delta q, 8/10/12-bit, mono/420/422/444) with and without deblocking/CDEF/loop restoration bit-exact, source/saved Go/C/VM + ASan/UBSan passed'

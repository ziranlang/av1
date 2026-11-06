#!/bin/sh
set -eu
. "$(dirname -- "$0")/common.sh"
out="$root/build/scratch/av1-scalar"
mkdir -p "$out"
if test -f "$out/receipt.json"; then mv "$out/receipt.json" "$out/previous-receipt.json"; fi
aom=${AV1_AOM_SOURCE:-$("$ziran" pkg path libaom)}
python3 tests/av1_scalar_oracle.py --root "$root" --out "$out" --aom "$aom"
module=av1_scalar_vectors
"$ziran" ir --root "$out" --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std" -o "$out/ir" "$out/$module.zi"
for form in source saved; do
    input="$out/$module.zi"
    set -- --root "$out" --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std"
    if test "$form" = saved; then input="$out/ir/$module.zir"; set -- --root "$out/ir" --module-path "$out/ir"; fi
    "$ziran" build --target=c --exe --entry "$module:main" "$@" -o "$out/$form" "$input"
    "$out/$form/$module"
    "$ziran" bundle --entry "$module:main" "$@" -o "$out/$form.zx" "$input"
    test "$("$bin/zi2zx" run --vm "$out/$form.zx")" = 0
    "$ziran" build --target=go --pkg main --entry "$module:main" "$@" -o "$out/$form-go" "$input"
    printf 'package main\nfunc main() { if Av1ScalarVectors_Main() != 0 { panic("AV1 scalar WHT") } }\n' > "$out/$form-go/main.go"
    (cd "$out/$form-go" && GO111MODULE=off go build -o run . && ./run)
done
CFLAGS='-O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer' LDFLAGS='-fsanitize=address,undefined' \
    "$ziran" build --target=c --exe --entry "$module:main" --root "$out" \
    --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std" -o "$out/sanitized" "$out/$module.zi"
ASAN_OPTIONS=detect_leaks=1 UBSAN_OPTIONS=halt_on_error=1 "$out/sanitized/$module"
printf '%s\n' 'AV1 scalar lossless WHT: signed native/spec vectors, source/saved C/Go/VM + ASan/UBSan passed'
sh tests/av1_quantization.sh
python3 - "$out" "$root" <<'PY'
import json,pathlib,sys
out,root=map(pathlib.Path,sys.argv[1:])
receipt=json.loads((out/'oracle.json').read_text())
receipt.update(command='build-queue -- sh tests/av1_scalar_kernels.sh',exit_code=0,
    forms=['source','saved IR'],targets=['C','Go','zi2zx run --vm'],sanitizers=['ASan','UBSan'])
receipt['quantization']=json.loads((root/'build/scratch/av1-quantization/receipt.json').read_text())
(out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
PY
printf '%s\n' 'AV1 scalar WHT and quantization suite passed; general lossy inverse transforms and frame integration remain pending'

#!/bin/sh
set -eu
. "$(dirname -- "$0")/common.sh"
out="$root/build/scratch/av1-coefficient-scan"
mkdir -p "$out"
python3 - "$root" "$toolchain" "$out" <<'PY'
import hashlib,json,pathlib,sys
root,toolchain,out=map(pathlib.Path,sys.argv[1:])
files=[root/'src/entropy/coefficient_scan.zi']+[root/'tests'/name for name in (
    'av1_coefficient_scan.zi','av1_coefficient_scan.sh','av1_coefficient_scan_oracle.py','av1_quantization_oracle.py','common.sh')]
import os
# The critical path runs on one binary snapshot; hash the binaries in use.
bin_dir=pathlib.Path(os.environ.get('ZIRAN_BIN_DIR') or toolchain/'build/bin')
files += [bin_dir/name for name in ('ziran','zi2zir','zi2c','zi2go','zi2zx')]
inputs={}
for path in files:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    inputs[str(path)]=h.hexdigest()
(out/'inputs-before.json').write_text(json.dumps(inputs,indent=2)+'\n')
PY
aom=${AV1_AOM_SOURCE:-$("$ziran" pkg path libaom)}
python3 tests/av1_coefficient_scan_oracle.py --root "$root" --out "$out" --aom "$aom"
module=av1_coefficient_scan_vectors
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
    printf 'package main\nfunc main() { if Av1CoefficientScanVectors_Main() != 0 { panic("AV1 coefficient scans") } }\n' > "$out/$form-go/main.go"
    (cd "$out/$form-go" && GO111MODULE=off go build -o run . && ./run)
done
CFLAGS='-O0 -g -fsanitize=address,undefined -fno-omit-frame-pointer' LDFLAGS='-fsanitize=address,undefined' \
    "$ziran" build --target=c --exe --entry "$module:main" --root "$out" \
    --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std" -o "$out/sanitized" "$out/$module.zi"
ASAN_OPTIONS=detect_leaks=1 UBSAN_OPTIONS=halt_on_error=1 "$out/sanitized/$module"
python3 - "$out" <<'PY'
import hashlib,json,pathlib,sys
out=pathlib.Path(sys.argv[1]);receipt=json.loads((out/'oracle.json').read_text())
inputs=json.loads((out/'inputs-before.json').read_text())
for name,expected in inputs.items():
    h=hashlib.sha256()
    with open(name,'rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    if h.hexdigest()!=expected:raise SystemExit('Coefficient scan input changed during run: '+name)
receipt.update(command='build-queue -- sh tests/av1_coefficient_scan.sh',exit_code=0,
    forms=['source','saved IR'],targets=['C','Go','zi2zx run --vm'],sanitizers=['ASan','UBSan'],sanitizer_optimization='-O0',
    inputs=inputs,inputs_unchanged=True)
(out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
PY
printf '%s\n' 'AV1 coefficient scans: pinned normative/native tables, source/saved C/Go/VM + ASan/UBSan passed; general entropy integration pending'

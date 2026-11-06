#!/bin/sh
set -eu
. "$(dirname -- "$0")/common.sh"
out="$root/build/scratch/av1-coefficient-block"
mkdir -p "$out"
python3 - "$root" "$toolchain" "$out" <<'PY'
import hashlib,json,pathlib,sys
root,toolchain,out=map(pathlib.Path,sys.argv[1:])
files=[root/'src'/name for name in ('entropy/coefficient_block.zi','entropy/coefficient_geometry.zi','entropy/coefficient_cdfs.zi','entropy/coefficient_cdf_data.zi','entropy/coefficient_cdf_symbols.zi','entropy/coefficient_context.zi','entropy/coefficient_scan.zi','entropy/range.zi',)]
files += [root/'tests'/name for name in ('av1_coefficient_block.zi','av1_coefficient_block.sh',
    'av1_coefficient_block_oracle.py','av1_coefficient_scan_oracle.py','av1_quantization_oracle.py','common.sh')]
files += [toolchain/'build/bin'/name for name in ('ziran','zi2zir','zi2c','zi2go','zi2zx','zi2zx-vm')]
inputs={}
for path in files:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    inputs[str(path)]=h.hexdigest()
(out/'inputs-before.json').write_text(json.dumps(inputs,indent=2)+'\n')
PY
aom=${AV1_AOM_SOURCE:-$("$ziran" pkg path libaom)}
python3 tests/av1_coefficient_block_oracle.py --root "$root" --out "$out" --aom "$aom"
if test "${AV1_COEFFICIENT_PREPARE_ONLY:-0}" = 1; then exit 0; fi
while IFS= read -r module; do
    printf '%s\n' "AV1 coefficient-block corpus: $module"
    "$ziran" ir --root "$out" --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std" -o "$out/$module/ir" "$out/$module.zi"
    for form in source saved; do
        input="$out/$module.zi"
        set -- --root "$out" --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std"
        if test "$form" = saved; then input="$out/$module/ir/$module.zir"; set -- --root "$out/$module/ir" --module-path "$out/$module/ir"; fi
        "$ziran" build --target=c --exe --entry "$module:main" "$@" -o "$out/$module/$form" "$input"
        "$out/$module/$form/$module"
        "$ziran" bundle --entry "$module:main" "$@" -o "$out/$module/$form.zx" "$input"
        test "$("$bin/zi2zx-vm" run "$out/$module/$form.zx")" = 0
        "$ziran" build --target=go --pkg main --entry "$module:main" "$@" -o "$out/$module/$form-go" "$input"
        printf 'package main\nfunc main() { if Av1CoefficientBlockVectors%s_Main() != 0 { panic("AV1 coefficient blocks") } }\n' "${module##*_}" > "$out/$module/$form-go/main.go"
        (cd "$out/$module/$form-go" && GO111MODULE=off go build -o run . && ./run)
    done
    CFLAGS='-O0 -g -fsanitize=address,undefined -fno-omit-frame-pointer' LDFLAGS='-fsanitize=address,undefined' \
        "$ziran" build --target=c --exe --entry "$module:main" --root "$out" \
        --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std" -o "$out/$module/sanitized" "$out/$module.zi"
    ASAN_OPTIONS=detect_leaks=1 UBSAN_OPTIONS=halt_on_error=1 "$out/$module/sanitized/$module"
done < "$out/modules.txt"
python3 - "$out" <<'PY'
import hashlib,json,pathlib,sys
out=pathlib.Path(sys.argv[1]);receipt=json.loads((out/'oracle.json').read_text())
inputs=json.loads((out/'inputs-before.json').read_text())
for name,expected in inputs.items():
    h=hashlib.sha256()
    with open(name,'rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    if h.hexdigest()!=expected:raise SystemExit('Coefficient block input changed during run: '+name)
receipt.update(command='build-queue -- sh tests/av1_coefficient_block.sh',exit_code=0,
    forms=['source','saved IR'],targets=['C','Go','direct zi2zx-vm'],sanitizers=['ASan','UBSan'],sanitizer_optimization='-O0',
    inputs=inputs,inputs_unchanged=True)
(out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
PY
printf '%s\n' 'AV1 coefficient blocks: pinned native writer syntax/CDF parity, source/saved C/Go/directVM + ASan/UBSan passed; lossy frame integration pending'

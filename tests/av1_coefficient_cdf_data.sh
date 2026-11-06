#!/bin/sh
set -eu
. "$(dirname -- "$0")/common.sh"
out="$root/build/scratch/av1-coefficient-cdfs"
aom=${AV1_AOM_SOURCE:-$("$ziran" pkg path libaom)}
python3 tests/av1_coefficient_cdfs_oracle.py --root "$root" --out "$out" --aom "$aom"
while IFS= read -r module; do
    printf '%s\n' "AV1 owned coefficient-CDF data corpus: $module"
    "$ziran" ir --root "$out" --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std" -o "$out/$module/ir" "$out/$module.zi"
    for form in source saved; do
        input="$out/$module.zi"
        set -- --root "$out" --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std"
        if test "$form" = saved; then input="$out/$module/ir/$module.zir"; set -- --root "$out/$module/ir" --module-path "$out/$module/ir"; fi
        "$ziran" build --target=c --exe --entry "$module:main" "$@" -o "$out/$module/$form" "$input"
        "$out/$module/$form/$module"
        "$ziran" bundle --entry "$module:main" "$@" -o "$out/$module/$form.zx" "$input"
        test "$("$bin/zi2zx" run --vm "$out/$module/$form.zx")" = 0
        "$ziran" build --target=go --pkg main --entry "$module:main" "$@" -o "$out/$module/$form-go" "$input"
        printf 'package main\nfunc main() { if Av1CoefficientCdfDataVectors%s_Main() != 0 { panic("AV1 owned coefficient CDF data") } }\n' "${module##*_}" > "$out/$module/$form-go/main.go"
        (cd "$out/$module/$form-go" && GO111MODULE=off go build -o run . && ./run)
    done
    CFLAGS='-O0 -g -fsanitize=address,undefined -fno-omit-frame-pointer' LDFLAGS='-fsanitize=address,undefined' \
        "$ziran" build --target=c --exe --entry "$module:main" --root "$out" \
        --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std" -o "$out/$module/sanitized" "$out/$module.zi"
    ASAN_OPTIONS=detect_leaks=1 UBSAN_OPTIONS=halt_on_error=1 "$out/$module/sanitized/$module"
done < "$out/data-modules.txt"
python3 - "$out" <<'PY'
import json,pathlib,sys
out=pathlib.Path(sys.argv[1]);receipt=json.loads((out/'oracle.json').read_text())
for field in ('valid_selector_cases','native_symbol_streams','decoded_symbols','fixtures_sha256'):
    receipt.pop(field,None)
receipt.update(command='build-queue -- sh tests/av1_coefficient_cdf_data.sh',exit_code=0,
    forms=['source','saved IR'],targets=['C','Go','zi2zx run --vm'],sanitizers=['ASan','UBSan'],sanitizer_optimization='-O0',
    constructor_cases=8,
    scope='owned coefficient default data and constructor only; selector and symbol operations are tested separately')
(out/'data-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
PY
printf '%s\n' 'AV1 owned coefficient-CDF data/constructor passed; selector and symbol operations are tested separately'

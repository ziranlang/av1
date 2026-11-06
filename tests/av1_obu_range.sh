#!/bin/sh
set -eu
. "$(dirname -- "$0")/common.sh"
out="$root/build/scratch/av1"
mkdir -p "$out"
aom=${AV1_AOM_SOURCE:-$("$ziran" pkg path libaom)}
python3 tests/av1_oracle.py --root "$root" --out "$out" --aom "$aom"
for module in av1_obu_range av1_oracle_vectors; do
    source_root="$root/tests"
    if test "$module" = av1_oracle_vectors; then source_root="$out"; fi
    suite="$out/$module"
    mkdir -p "$suite"
    "$ziran" ir --root "$source_root" --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std" -o "$suite/ir" "$source_root/$module.zi"
    for form in source saved; do
        input="$source_root/$module.zi"
        set -- --root "$source_root" --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std"
        if test "$form" = saved; then input="$suite/ir/$module.zir"; set -- --root "$suite/ir" --module-path "$suite/ir"; fi
        "$ziran" build --target=c --exe --entry "$module:main" "$@" -o "$suite/$form" "$input"
        "$suite/$form/$module"
        "$ziran" bundle --entry "$module:main" "$@" -o "$suite/$form.zx" "$input"
        test "$("$bin/zi2zx-vm" run "$suite/$form.zx")" = 0
        "$ziran" build --target=go --pkg main --entry "$module:main" "$@" -o "$suite/$form-go" "$input"
        go_entry=$(python3 -c 'import sys; print("".join(x.capitalize() for x in sys.argv[1].split("_")) + "_Main")' "$module")
        printf 'package main\nfunc main() { if %s() != 0 { panic("AV1 parity") } }\n' "$go_entry" > "$suite/$form-go/main.go"
        (cd "$suite/$form-go" && GO111MODULE=off go build -o run . && ./run)
    done
    CFLAGS='-O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer' LDFLAGS='-fsanitize=address,undefined' \
        "$ziran" build --target=c --exe --entry "$module:main" --root "$source_root" \
        --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std" -o "$suite/sanitized" "$source_root/$module.zi"
    ASAN_OPTIONS=detect_leaks=1 UBSAN_OPTIONS=halt_on_error=1 "$suite/sanitized/$module"
done
"$ziran" build --target=c --entry av1_abi:ProbeCDF --root "$root/tests" \
    --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std" -o "$out/abi" "$root/tests/av1_abi.zi"
python3 tests/av1_oracle.py --root "$root" --out "$out" --aom "$aom" --abi "$out/abi"
python3 - "$out" <<'PY'
import json, pathlib, sys
out = pathlib.Path(sys.argv[1])
receipt = json.loads((out / 'oracle.json').read_text())
receipt.update(command='build-queue -- sh tests/av1_obu_range.sh', exit_code=0,
    forms=['source', 'saved IR'], targets=['C', 'Go', 'direct zi2zx-vm'],
    sanitizers=['ASan', 'UBSan'], native_invalid_descriptors=9)
(out / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
PY
printf '%s\n' 'AV1 framing/range: source + saved IR C/Go + direct portable VM + ASan/UBSan passed'

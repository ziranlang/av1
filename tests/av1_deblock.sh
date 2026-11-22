#!/bin/sh
set -eu
. "$(dirname -- "$0")/common.sh"
out="$root/build/scratch/av1-deblock"
aom=${AV1_AOM_SOURCE:-$("$ziran" pkg path libaom)}
rm -f "$out/receipt.json"
python3 tests/av1_deblock_oracle.py --root "$root" --out "$out" --aom "$aom"
module=av1_deblock_vectors
check_native() {
    "$ziran" ir --root "$out" --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std" -o "$out/ir" "$out/$module.zi"
    for form in source saved; do
        input="$out/$module.zi"
        set -- --root "$out" --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std"
        if test "$form" = saved; then input="$out/ir/$module.zir"; set -- --root "$out/ir" --module-path "$out/ir"; fi
        "$ziran" build --target=c --exe --entry "$module:main" "$@" -o "$out/$form" "$input"
        for tier in 1 2 3 4; do
            printf 'AV1 deblock %s SIMD tier %s\n' "$form" "$tier"
            ZIRAN_SIMD_TIER=$tier "$out/$form/$module"
        done
        "$ziran" build --target=go --pkg main --entry "$module:main" "$@" -o "$out/$form-go" "$input"
        printf 'package main\nfunc main() { if Av1DeblockVectors_Main() != 0 { panic("AV1 deblock") } }\n' > "$out/$form-go/main.go"
        (cd "$out/$form-go" && GO111MODULE=off go build -o run . && ./run)
    done
    CFLAGS='-O0 -g -fsanitize=address,undefined -fno-omit-frame-pointer' LDFLAGS='-fsanitize=address,undefined' \
        "$ziran" build --target=c --exe --entry "$module:main" --root "$out" \
        --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std" -o "$out/sanitized" "$out/$module.zi"
    ASAN_OPTIONS=detect_leaks=1 UBSAN_OPTIONS=halt_on_error=1 "$out/sanitized/$module"
}
check_engine() {
    shard="$out/$1"
    source="$shard/$module.zi"
    saved="$shard/ir/$module.zir"
    set -- --root "$shard" --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std"
    printf 'AV1 deblock %s source engine\n' "${shard##*/}"
    "$ziran" bundle --entry "$module:main" "$@" -o "$shard/source.zx" "$source"
    result=$("$bin/zi2zx" run --backend engine "$shard/source.zx")
    if test "$result" != 0; then printf '%s source engine returned %s\n' "$shard" "$result" >&2; return 1; fi
    printf 'AV1 deblock %s saved engine\n' "${shard##*/}"
    "$ziran" ir "$@" -o "$shard/ir" "$source"
    "$ziran" bundle --entry "$module:main" --root "$shard/ir" --module-path "$shard/ir" -o "$shard/saved.zx" "$saved"
    result=$("$bin/zi2zx" run --backend engine "$shard/saved.zx")
    if test "$result" != 0; then printf '%s saved engine returned %s\n' "$shard" "$result" >&2; return 1; fi
}
shards=$(python3 -c 'import json,sys; print(*(s["directory"] for s in json.load(open(sys.argv[1]))["engine_shards"]))' "$out/oracle.json")
check_native &
pids=$!
for shard in $shards; do
    check_engine "$shard" &
    pids="$pids $!"
done
status=0
for pid in $pids; do wait "$pid" || status=1; done
test "$status" = 0
python3 - "$out" <<'PY'
import json,pathlib,sys
out=pathlib.Path(sys.argv[1]);receipt=json.loads((out/'oracle.json').read_text())
receipt.update(command='sh tests/av1_deblock.sh',exit_code=0,
    forms=['source','saved IR'],targets=['C','Go','zi2zx run --backend engine'],c_simd_tiers=[1,2,3,4],sanitizers=['ASan','UBSan'],sanitizer_optimization='-O0')
(out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
PY
printf '%s\n' 'AV1 deblock: pinned line and four-line segment vectors, source/saved C/Go/engine + ASan/UBSan passed'

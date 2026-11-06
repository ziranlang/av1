#!/bin/sh
# OBU framing of KEY stills against pinned libaom verdicts. Run through build-queue.
set -eu
. "$(dirname -- "$0")/common.sh"
out="$root/build/scratch/av1-obu-framing"
cache="$root/build/corpus/libaom"
aom=${AV1_AOM_SOURCE:-$("$ziran" pkg path libaom)}
rm -rf "$out"/shard*
python3 tests/av1_conformance_fetch.py --aom "$aom" --cache "$cache" --ivf-only
python3 tests/av1_obu_framing_oracle.py --root "$root" --out "$out" --aom "$aom" --cache "$cache"
module=av1_obu_framing_vectors
shards=$(ls -d "$out"/shard* | wc -l)
shard=0
while test "$shard" -lt "$shards"; do
group="$out/shard$shard"
printf '%s\n' "AV1 OBU framing shard $shard/$((shards - 1))"
"$ziran" ir --root "$group" --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std" -o "$group/ir" "$group/$module.zi"
for form in source saved; do
    input="$group/$module.zi"
    set -- --root "$group" --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std"
    if test "$form" = saved; then input="$group/ir/$module.zir"; set -- --root "$group/ir" --module-path "$group/ir"; fi
    "$ziran" build --target=c --exe --entry "$module:main" "$@" -o "$group/$form" "$input"
    "$group/$form/$module"
    "$ziran" bundle --entry "$module:main" "$@" -o "$group/$form.zx" "$input"
    test "$("$bin/zi2zx" run --vm "$group/$form.zx")" = 0
    "$ziran" build --target=go --pkg main --entry "$module:main" "$@" -o "$group/$form-go" "$input"
    printf 'package main\nfunc main() { if r := Av1ObuFramingVectors_Main(); r != 0 { panic(r) } }\n' > "$group/$form-go/main.go"
    (cd "$group/$form-go" && GO111MODULE=off go build -o run . && ./run)
done
CFLAGS='-O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer' LDFLAGS='-fsanitize=address,undefined' \
    "$ziran" build --target=c --exe --entry "$module:main" --root "$group" \
    --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std" -o "$group/sanitized" "$group/$module.zi"
ASAN_OPTIONS=detect_leaks=1 UBSAN_OPTIONS=halt_on_error=1 "$group/sanitized/$module"
shard=$((shard + 1))
done
python3 - "$out" <<'PY'
import json,pathlib,sys
out=pathlib.Path(sys.argv[1]);receipt=json.loads((out/'oracle.json').read_text())
receipt.update(command='build-queue -- sh tests/av1_obu_framing.sh',exit_code=0,
    forms=['source','saved IR'],targets=['C','Go','zi2zx run --vm'],sanitizers=['ASan','UBSan'],sanitizer_optimization='-O1')
(out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
PY
printf '%s\n' 'AV1 OBU framing: split headers, tile groups, redundant/padding/reserved OBUs and layer dropping match pinned libaom verdicts, source/saved C/Go/VM + ASan/UBSan passed'

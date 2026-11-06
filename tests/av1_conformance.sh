#!/bin/sh
# Pinned libaom decoder test vectors. Run through build-queue.
# AV1_CONFORMANCE_DISCOVER=1 only reports each case's outcome (Go) into
# build/scratch/av1-conformance/discovered.json for review; it never passes.
# AV1_CONFORMANCE_SHARDS=prepare only builds the shards; FIRST-LAST or one
# index then runs those shards in their own build-queue ticket, and the
# receipt is written once every shard has passed since the last prepare.
set -eu
. "$(dirname -- "$0")/common.sh"
out="$root/build/scratch/av1-conformance"
cache="$root/build/corpus/libaom"
mkdir -p "$out"
aom=${AV1_AOM_SOURCE:-$("$ziran" pkg path libaom)}
python3 tests/av1_conformance_fetch.py --aom "$aom" --cache "$cache" --ivf-only
discover=${AV1_CONFORMANCE_DISCOVER:-0}
select=${AV1_CONFORMANCE_SHARDS:-}
if test "$discover" = 1 && test -n "$select"; then
    printf 'AV1_CONFORMANCE_DISCOVER runs every shard; unset AV1_CONFORMANCE_SHARDS\n' >&2; exit 1
fi
if test -z "$select" || test "$select" = prepare; then
    rm -f "$out/receipt.json" "$out"/shard*/passed
    if test "$discover" = 1; then
        python3 tests/av1_conformance_oracle.py --root "$root" --out "$out" --aom "$aom" --cache "$cache" --discover
    else
        python3 tests/av1_conformance_oracle.py --root "$root" --out "$out" --aom "$aom" --cache "$cache"
    fi
fi
shards=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["shards"])' "$out/oracle.json")
if test "$select" = prepare; then printf 'AV1 conformance: %s shards prepared\n' "$shards"; exit 0; fi
module=av1_conformance_vectors
shard=0; last=$((shards - 1))
if test -n "$select"; then
    shard=${select%-*}; last=${select#*-}
    case "$shard$last" in *[!0-9]*|'') printf 'AV1_CONFORMANCE_SHARDS must be prepare, N or FIRST-LAST\n' >&2; exit 1;; esac
    if test "$last" -ge "$shards" || test "$shard" -gt "$last"; then
        printf 'AV1_CONFORMANCE_SHARDS=%s is outside shards 0-%s\n' "$select" "$((shards - 1))" >&2; exit 1
    fi
fi
while test "$shard" -le "$last"; do
group="$out/shard$shard"
printf '%s\n' "AV1 conformance shard $shard/$((shards - 1))"
if test "$discover" = 1; then
    # Probe is the entry: the Go target keeps only exports reachable from it.
    "$ziran" build --target=go --pkg main --entry "$module:Probe" --root "$group" --module-path "image_codec=$foundation/src" \
        --module-path "std=$toolchain/std" -o "$group/discover-go" "$group/$module.zi"
    count=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))))' "$group/cases.json")
    printf 'package main\nimport "fmt"\nfunc main() { for i := int32(0); i < %s; i++ { fmt.Println(i, Av1ConformanceVectors_Probe(i)) } }\n' "$count" > "$group/discover-go/main.go"
    (cd "$group/discover-go" && GO111MODULE=off go build -o run . && ./run > "$group/discovered.txt")
    shard=$((shard + 1)); continue
fi
"$ziran" ir --root "$group" --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std" -o "$group/ir" "$group/$module.zi"
for form in source saved; do
    input="$group/$module.zi"
    set -- --root "$group" --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std"
    if test "$form" = saved; then input="$group/ir/$module.zir"; set -- --root "$group/ir" --module-path "$group/ir"; fi
    "$ziran" build --target=c --exe --entry "$module:main" "$@" -o "$group/$form" "$input"
    "$group/$form/$module"
    "$ziran" build --target=go --pkg main --entry "$module:main" "$@" -o "$group/$form-go" "$input"
    printf 'package main\nfunc main() { if r := Av1ConformanceVectors_Main(); r != 0 { panic(r) } }\n' > "$group/$form-go/main.go"
    (cd "$group/$form-go" && GO111MODULE=off go build -o run . && ./run)
    "$ziran" bundle --entry "$module:main" "$@" -o "$group/$form.zx" "$input"
    test "$("$bin/zi2zx" run --vm "$group/$form.zx")" = 0
done
CFLAGS='-O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer' LDFLAGS='-fsanitize=address,undefined' \
    "$ziran" build --target=c --exe --entry "$module:main" --root "$group" \
    --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std" -o "$group/sanitized" "$group/$module.zi"
ASAN_OPTIONS=detect_leaks=1 UBSAN_OPTIONS=halt_on_error=1 "$group/sanitized/$module"
: > "$group/passed"
shard=$((shard + 1))
done
if test "$discover" = 1; then
    python3 - "$out" "$shards" <<'PY'
import json,pathlib,sys
out=pathlib.Path(sys.argv[1]);result={};other={}
for shard in range(int(sys.argv[2])):
    keys=json.loads((out/f'shard{shard}/cases.json').read_text())
    for line in (out/f'shard{shard}/discovered.txt').read_text().splitlines():
        index,code=map(int,line.split());(result if code in (0,1) else other)[keys[index]]=code
(out/'discovered.json').write_text(json.dumps(dict(sorted(result.items())),indent=1)+'\n')
(out/'discovered-failures.json').write_text(json.dumps(dict(sorted(other.items())),indent=1)+'\n')
print(f'Discovered {sum(v==0 for v in result.values())} bit-exact, {sum(v==1 for v in result.values())} unsupported, {len(other)} failing cases')
PY
    exit 1
fi
shard=0
while test "$shard" -lt "$shards"; do
    if test ! -f "$out/shard$shard/passed"; then
        printf 'AV1 conformance shards %s passed\n' "$select"; exit 0
    fi
    shard=$((shard + 1))
done
python3 - "$out" <<'PY'
import json,pathlib,sys
out=pathlib.Path(sys.argv[1]);receipt=json.loads((out/'oracle.json').read_text())
receipt.update(command='build-queue -- sh tests/av1_conformance.sh',exit_code=0,
    forms=['source','saved IR'],targets=['C','Go','zi2zx run --vm'],sanitizers=['ASan','UBSan'],sanitizer_optimization='-O1')
(out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
PY
printf '%s\n' 'AV1 conformance: pinned libaom test-vector KEY units bit-exact or UnsupportedCapability as pinned, source/saved C/Go/VM + ASan/UBSan passed'

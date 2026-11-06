#!/bin/sh
set -eu
. "$(dirname -- "$0")/common.sh"
out="$root/build/scratch/av1-tiles"
"$ziran" ir --root tests --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std" -o "$out/ir" tests/av1_entropy_tiles.zi
for form in source saved; do
    input="$root/tests/av1_entropy_tiles.zi"
    set -- --root tests --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std"
    if test "$form" = saved; then input="$out/ir/av1_entropy_tiles.zir"; set -- --root "$out/ir" --module-path "$out/ir"; fi
    "$ziran" build --target=c --exe --entry av1_entropy_tiles:main "$@" -o "$out/$form" "$input"
    "$out/$form/av1_entropy_tiles"
    "$ziran" bundle --entry av1_entropy_tiles:main "$@" -o "$out/$form.zx" "$input"
    test "$("$bin/zi2zx" run --vm "$out/$form.zx")" = 0
    "$ziran" build --target=go --pkg main --entry av1_entropy_tiles:main "$@" -o "$out/$form-go" "$input"
    printf 'package main\nfunc main() { if Av1EntropyTiles_Main() != 0 { panic("AV1 tile framing") } }\n' > "$out/$form-go/main.go"
    (cd "$out/$form-go" && GO111MODULE=off go build -o run . && ./run)
done
CFLAGS='-O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer' LDFLAGS='-fsanitize=address,undefined' \
    "$ziran" build --target=c --exe --entry av1_entropy_tiles:main --root tests \
    --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std" -o "$out/sanitized" tests/av1_entropy_tiles.zi
ASAN_OPTIONS=detect_leaks=1 UBSAN_OPTIONS=halt_on_error=1 "$out/sanitized/av1_entropy_tiles"
printf '%s\n' 'AV1 tile group framing/cancellation: source/saved C/Go + direct VM + ASan/UBSan passed; entropy decode pending'
sh tests/av1_coefficient_scan.sh
sh tests/av1_coefficient_context.sh
sh tests/av1_coefficient_cdfs.sh
sh tests/av1_coefficient_block.sh
sh tests/av1_coefficient_neighbors.sh
python3 - "$out" "$root" <<'PY'
import json,pathlib,sys
out,root=map(pathlib.Path,sys.argv[1:])
receipt=dict(command='build-queue -- sh tests/av1_entropy_tiles.sh',exit_code=0,
    forms=['source','saved IR'],targets=['C','Go','zi2zx run --vm'],sanitizers=['ASan','UBSan'],
    scope='bounded tile-group spans/cancellation, coefficient scans/contexts/default CDFs/adaptive symbols/nonzero blocks/neighbor contexts; general tile/frame entropy integration incomplete',
    scans=json.loads((root/'build/scratch/av1-coefficient-scan/receipt.json').read_text()),
    contexts=json.loads((root/'build/scratch/av1-coefficient-context/receipt.json').read_text()),
    cdfs=json.loads((root/'build/scratch/av1-coefficient-cdfs/receipt.json').read_text()),
    coefficient_blocks=json.loads((root/'build/scratch/av1-coefficient-block/receipt.json').read_text()),
    neighbors=json.loads((root/'build/scratch/av1-coefficient-neighbors/receipt.json').read_text()))
(out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
PY
printf '%s\n' 'AV1 tile-group framing and coefficient scan/context/CDF/block suite passed; general tile/frame integration remains pending'

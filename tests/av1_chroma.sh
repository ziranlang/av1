#!/bin/sh
set -eu
. "$(dirname -- "$0")/common.sh"
out="$root/build/scratch/av1-chroma"
set --
if test "${AV1_COLOR_PALETTE_TEST:-0}" = 1; then out="$root/build/scratch/av1-color-palette"; set -- --palettes; fi
if test "${AV1_KEY_FRAME_TEST:-0}" = 1; then out="$root/build/scratch/av1-key-frames"; set -- --key-frames; fi
aom=${AV1_AOM_SOURCE:-$("$ziran" pkg path libaom)}
python3 tests/av1_chroma_oracle.py --root "$root" --out "$out" --aom "$aom" "$@"
module=av1_chroma_vectors
"$ziran" ir --root "$out" --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std" -o "$out/ir" "$out/$module.zi"
for form in source saved; do
    input="$out/$module.zi"
    set -- --root "$out" --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std"
    if test "$form" = saved; then input="$out/ir/$module.zir"; set -- --root "$out/ir" --module-path "$out/ir"; fi
    "$ziran" build --target=c --exe --entry "$module:main" "$@" -o "$out/$form" "$input"
    "$out/$form/$module"
    "$ziran" bundle --entry "$module:main" "$@" -o "$out/$form.zx" "$input"
    test "$("$bin/zi2zx-vm" run "$out/$form.zx")" = 0
    "$ziran" build --target=go --pkg main --entry "$module:main" "$@" -o "$out/$form-go" "$input"
    printf 'package main\nfunc main() { if Av1ChromaVectors_Main() != 0 { panic("AV1 color planes") } }\n' > "$out/$form-go/main.go"
    (cd "$out/$form-go" && GO111MODULE=off go build -o run . && ./run)
done
# This large literal-vector host is separately exercised as optimized C above.
# O0 avoids excessive debug/alias analysis while preserving ASan/UBSan coverage.
CFLAGS='-O0 -g -fsanitize=address,undefined -fno-omit-frame-pointer' LDFLAGS='-fsanitize=address,undefined' \
    "$ziran" build --target=c --exe --entry "$module:main" --root "$out" \
    --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std" -o "$out/sanitized" "$out/$module.zi"
ASAN_OPTIONS=detect_leaks=1 UBSAN_OPTIONS=halt_on_error=1 "$out/sanitized/$module"
"$ziran" build --target=c --no-main --entry av1_chroma_allocation:AllocationFailure --root "$out" \
    --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std" -o "$out/allocation" "$out/av1_chroma_allocation.zi"
cat > "$out/allocation/host.c" <<'C'
#include "av1_chroma_allocation.h"
#include <stddef.h>
void *__real_realloc(void*,size_t);
static int calls,fail_at;
void *__wrap_realloc(void *pointer,size_t bytes) {
    calls++;if(calls==fail_at)return NULL;return __real_realloc(pointer,bytes);
}
int main(void) {
    for(fail_at=1;fail_at<=3;fail_at++) { calls=0;if(AllocationFailure()!=0||calls!=fail_at)return 1; }
    return 0;
}
C
"${CC:-cc}" -std=c11 -O1 -g -fno-builtin-realloc -fsanitize=address,undefined -fno-sanitize-recover=all \
    -I"$toolchain/include" -I"$out/allocation" "$out/allocation"/*.c -Wl,--wrap=realloc -lm -o "$out/allocation/test"
ASAN_OPTIONS=detect_leaks=1 UBSAN_OPTIONS=halt_on_error=1 "$out/allocation/test"
python3 - "$out" <<'PY'
import json,pathlib,sys
out=pathlib.Path(sys.argv[1]);receipt=json.loads((out/'oracle.json').read_text())
receipt.update(command='build-queue -- sh tests/av1_'+('color_palette' if out.name=='av1-color-palette' else ('key_frames' if out.name=='av1-key-frames' else 'chroma'))+'.sh',exit_code=0,
    forms=['source','saved IR'],targets=['C','Go','direct zi2zx-vm'],sanitizers=['ASan','UBSan'],sanitizer_optimization='-O0',
    allocation_failures=[1,2,3],retained_three_plane_results=out.name!='av1-color-palette',input_independence=out.name!='av1-color-palette')
(out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
PY
printf '%s\n' 'AV1 color planes predictors: pinned vectors, source/saved C/Go/directVM + ASan/UBSan passed'

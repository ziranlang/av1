#!/bin/sh
set -eu
. "$(dirname -- "$0")/common.sh"
out="$root/build/scratch/av1-engine"
aom=${AV1_AOM_SOURCE:-$("$ziran" pkg path libaom)}
for depth in 8 10 12; do
    python3 tests/av1_flat_fixture.py --root "$root" --out "$out/fixture-$depth" --aom "$aom" --depth "$depth"
done
"$ziran" ir --root tests --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std" -o "$out/ir" tests/av1_engine_parity.zi
for form in source saved; do
    input="$root/tests/av1_engine_parity.zi"
    set -- --root tests --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std"
    if test "$form" = saved; then input="$out/ir/av1_engine_parity.zir"; set -- --root "$out/ir" --module-path "$out/ir"; fi
    CFLAGS="${CFLAGS:-} -fstack-usage" "$ziran" build --target=c --exe --entry av1_engine_parity:main "$@" -o "$out/$form" "$input"
    "$out/$form/av1_engine_parity"
    "$ziran" bundle --entry av1_engine_parity:main "$@" -o "$out/$form.zx" "$input"
    test "$("$bin/zi2zx-vm" run "$out/$form.zx")" = 0
    "$ziran" build --target=go --pkg main --entry av1_engine_parity:main "$@" -o "$out/$form-go" "$input"
    printf 'package main\nfunc main() { if Av1EngineParity_Main() != 0 { panic("AV1 zero-residual still parity") } }\n' > "$out/$form-go/main.go"
    (cd "$out/$form-go" && GO111MODULE=off go build -o run . && ./run)
done
CFLAGS='-O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer' LDFLAGS='-fsanitize=address,undefined' \
    "$ziran" build --target=c --exe --entry av1_engine_parity:main --root tests \
    --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std" -o "$out/sanitized" tests/av1_engine_parity.zi
ASAN_OPTIONS=detect_leaks=1 UBSAN_OPTIONS=halt_on_error=1 "$out/sanitized/av1_engine_parity"
"$ziran" build --target=c --no-main --entry av1_allocation:AllocationFailure --root tests \
    --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std" -o "$out/allocation" tests/av1_allocation.zi
cat > "$out/allocation/host.c" <<'C'
#include "av1_allocation.h"
#include <stddef.h>
static int calls;
void *__wrap_realloc(void *pointer,size_t bytes) {
    (void)pointer;(void)bytes;calls++;return NULL;
}
int main(void) { return AllocationFailure()==0 && calls==1 ? 0 : 1; }
C
# Preserve the allocation call for the linker wrapper: optimizing compilers
# may otherwise replace realloc(NULL,n) with malloc(n).
"${CC:-cc}" -std=c11 -O1 -g -fno-builtin-realloc -fsanitize=address,undefined -fno-sanitize-recover=all \
    -I"$toolchain/include" -I"$out/allocation" "$out/allocation"/*.c -Wl,--wrap=realloc -lm -o "$out/allocation/test"
ASAN_OPTIONS=detect_leaks=1 UBSAN_OPTIONS=halt_on_error=1 "$out/allocation/test"
# Emit stack evidence beside stable test output; the compiler driver cleans its
# private intermediate files, so measure the same generated translation units
# explicitly. This changes only a disposable local-test executable.
"${CC:-cc}" -std=c11 -O2 -fstack-usage -I"$toolchain/include" -I"$out/source" \
    "$out/source"/*.c -lm -o "$out/source/av1_engine_parity"
"$out/source/av1_engine_parity"
python3 tests/av1_working_memory.py --out "$out" --toolchain "$toolchain"
python3 - "$out" <<'PY'
import json,pathlib,sys
out=pathlib.Path(sys.argv[1])
receipt=dict(command='build-queue -- sh tests/av1_engine_parity.sh',exit_code=0,
    scope='8x8 monochrome DC zero-residual reduced still only; full engine pending',
    depths=[8,10,12], forms=['source','saved IR'], targets=['C','Go','direct zi2zx-vm'],
    sanitizers=['ASan','UBSan'],repeated_decodes=32,invalid_prefixes=14,
    allocation_failure='one allocation fails, no pixels/worker/scratch retained',
    fixtures=[json.loads((out/f'fixture-{d}'/'fixture.json').read_text()) for d in (8,10,12)],
    working_memory=json.loads((out/'working-memory.json').read_text()))
(out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
PY
printf '%s\n' 'AV1 initial zero-residual still: owned YUV parity, limits/cancellation/reuse, source/saved C/Go/directVM + ASan/UBSan passed; full engine pending'

#!/bin/sh
set -eu
. "$(dirname -- "$0")/common.sh"
out="$root/build/scratch/av1-headers"
mkdir -p "$out"
if test "${AV1_HEADERS_INITIAL_ONLY:-0}" != 1; then
    dav1d=${AV1_DAV1D_SOURCE:-$("$ziran" pkg path dav1d)}
    # Test-only dav1d oracle built from its pinned package source with meson.
    library=${AV1_DAV1D_LIBRARY:-$root/build/native/dav1d/lib/libdav1d.a}
    if test ! -f "$library"; then
        if test "$library" != "$root/build/native/dav1d/lib/libdav1d.a"; then
            printf 'Missing pinned dav1d oracle: %s\n' "$library" >&2
            exit 1
        fi
        test "$(git -C "$dav1d" rev-parse HEAD)" = 54706fc6bc0cdecab7e9593974a4039cc038fca7
        git -C "$dav1d" diff --quiet HEAD --
        meson setup "$root/build/native/dav1d-build" "$dav1d" --prefix "$root/build/native/dav1d" \
            --libdir=lib --buildtype=release --default-library=static \
            -Denable_tools=false -Denable_tests=false -Denable_examples=false
        meson compile -C "$root/build/native/dav1d-build" -j 1
        meson install -C "$root/build/native/dav1d-build" --no-rebuild
    fi
    python3 tests/av1_header_oracle.py --root "$root" --out "$out" --dav1d "$dav1d" --library "$library"
    python3 tests/av1_extra_frames.py --out "$out/extra"
    python3 tests/av1_frame_oracle.py --root "$root" --out "$out" --dav1d "$dav1d" --library "$library" --extras "$out/extra/fixtures.json"
fi
modules=av1_headers
if test "${AV1_HEADERS_INITIAL_ONLY:-0}" != 1; then modules="$modules av1_header_vectors av1_frame_vectors"; fi
for module in $modules; do
    source_root="$root/tests"
    if test "$module" != av1_headers; then source_root="$out"; fi
    suite="$out/$module"
    "$ziran" ir --root "$source_root" --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std" -o "$suite/ir" "$source_root/$module.zi"
    for form in source saved; do
        input="$source_root/$module.zi"
        set -- --root "$source_root" --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std"
        if test "$form" = saved; then input="$suite/ir/$module.zir"; set -- --root "$suite/ir" --module-path "$suite/ir"; fi
        "$ziran" build --target=c --exe --entry "$module:main" "$@" -o "$suite/$form" "$input"
        "$suite/$form/$module"
        "$ziran" bundle --entry "$module:main" "$@" -o "$suite/$form.zx" "$input"
        test "$("$bin/zi2zx" run --vm "$suite/$form.zx")" = 0
        "$ziran" build --target=go --pkg main --entry "$module:main" "$@" -o "$suite/$form-go" "$input"
        go_entry=$(python3 -c 'import sys; print("".join(x.capitalize() for x in sys.argv[1].split("_")) + "_Main")' "$module")
        printf 'package main\nfunc main() { if %s() != 0 { panic("AV1 header parity") } }\n' "$go_entry" > "$suite/$form-go/main.go"
        (cd "$suite/$form-go" && GO111MODULE=off go build -o run . && ./run)
    done
    CFLAGS='-O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer' LDFLAGS='-fsanitize=address,undefined' \
        "$ziran" build --target=c --exe --entry "$module:main" --root "$source_root" \
        --module-path "image_codec=$foundation/src" --module-path "std=$toolchain/std" -o "$suite/sanitized" "$source_root/$module.zi"
    ASAN_OPTIONS=detect_leaks=1 UBSAN_OPTIONS=halt_on_error=1 "$suite/sanitized/$module"
done
if test "${AV1_HEADERS_INITIAL_ONLY:-0}" != 1; then
    python3 - "$out" <<'PY'
import json, pathlib, sys
out = pathlib.Path(sys.argv[1])
receipt = json.loads((out / 'oracle.json').read_text())
receipt['frame_oracle'] = json.loads((out / 'frame_oracle.json').read_text())
receipt.update(command='build-queue -- sh tests/av1_headers.sh', exit_code=0,
    forms=['source', 'saved IR'], targets=['C', 'Go', 'zi2zx run --vm'], sanitizers=['ASan', 'UBSan'])
(out / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
PY
fi
printf '%s\n' 'AV1 headers: source/saved C/Go + direct VM + ASan/UBSan passed'

#!/bin/sh
set -eu
unset DISPLAY WAYLAND_DISPLAY WAYLAND_SOCKET XAUTHORITY DBUS_SESSION_BUS_ADDRESS DBUS_SYSTEM_BUS_ADDRESS SESSION_MANAGER XDG_RUNTIME_DIR
export ZIRAN_DESKTOP_RECOVERY=0 HARMONY_DESKTOP_RECOVERY=0 YUE_DESKTOP_RECOVERY=0
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
toolchain=${ZIRAN_ROOT:-$root/../../ziran}
if test ! -x "$toolchain/build/bin/ziran"; then toolchain=$(ziran pkg path ziran); fi
# Ziran binaries; the critical path runs on one snapshot of them.
bin=${ZIRAN_BIN_DIR:-$toolchain/build/bin}
ziran=${ZIRAN_BIN:-$bin/ziran}
foundation=${IMAGE_CODEC_ROOT:-$("$ziran" pkg path image_codec)}
export ZIRAN="$ziran"
mkdir -p "$root/build/scratch/tmp" "$root/build/bin"
export TMPDIR="$root/build/scratch/tmp"
ulimit -c 0
# Test-only libaom oracle: one encoder+decoder build of the pinned package
# source, with its own config headers. No product module links it. The
# default build records its revision and is rebuilt when the pin moves.
native="$root/build/native"
aom_pin=b141d9c45db236335351c8a4b2a14b3ade27c937
export AV1_AOM_LIBRARY="${AV1_AOM_LIBRARY:-$native/aom/libaom.a}"
export AV1_AOM_CONFIG="${AV1_AOM_CONFIG:-$native/aom}"
export AV1_AOM_ENCODER_LIBRARY="${AV1_AOM_ENCODER_LIBRARY:-$AV1_AOM_LIBRARY}"
if test "$AV1_AOM_LIBRARY" = "$native/aom/libaom.a" && test -f "$AV1_AOM_LIBRARY" &&
    test "$(cat "$native/aom/revision" 2>/dev/null)" != "$aom_pin"; then
    rm -rf "$native/aom"
fi
if test ! -f "$AV1_AOM_LIBRARY"; then
    if test "$AV1_AOM_LIBRARY" != "$native/aom/libaom.a"; then
        printf 'Missing pinned libaom oracle: %s\n' "$AV1_AOM_LIBRARY" >&2
        exit 1
    fi
    aom_source=${AV1_AOM_SOURCE:-$("$ziran" pkg path libaom)}
    test "$(git -C "$aom_source" rev-parse HEAD)" = "$aom_pin"
    git -C "$aom_source" diff --quiet HEAD --
    cmake -S "$aom_source" -B "$native/aom" -DCMAKE_BUILD_TYPE=Release \
        -DENABLE_DOCS=0 -DENABLE_EXAMPLES=0 -DENABLE_TESTS=0 -DENABLE_TOOLS=0 \
        -DCONFIG_AV1_ENCODER=1 -DCONFIG_AV1_DECODER=1
    cmake --build "$native/aom" --target aom --parallel 1
    printf '%s\n' "$aom_pin" > "$native/aom/revision"
fi
cd "$root"

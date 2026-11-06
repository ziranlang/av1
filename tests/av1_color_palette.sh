#!/bin/sh
set -eu
AV1_COLOR_PALETTE_TEST=1
export AV1_COLOR_PALETTE_TEST
exec sh "$(dirname -- "$0")/av1_chroma.sh"

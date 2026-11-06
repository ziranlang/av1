#!/bin/sh
set -eu
AV1_KEY_FRAME_TEST=1
export AV1_KEY_FRAME_TEST
exec sh "$(dirname -- "$0")/av1_chroma.sh"

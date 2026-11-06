# AV1 fixtures

The `.obu` files are the AV1 item payloads of the avif package's regression
fixtures (`tests/fixtures` there), byte for byte: `<name>.color.obu` is the
primary item and `<name>.alpha.obu` its alpha auxiliary item. The avif package
extracted them with `tests/pin_container.py` from tiny Kryon patterns encoded
with libavif avifenc 1.2.1 and CPU libaom, under Kryon's BSD-3-Clause license.
`manifest.json` pins their SHA-256 digests, which match the avif manifest.
Tests read them in mdat order, alpha before color, and never regenerate them.

# av1

Owned AV1 decoder in handwritten Ziran. `DecodeAv1YUV` from `av1/decoder`
decodes one shown KEY frame to source-depth YUV planes, including deblocking,
CDEF, loop restoration and whole-frame film grain. Inter frames, intra block
copy, superres, non-flat quantizer matrices and metadata OBUs reject with
`UnsupportedCapability`.

It matches the pinned libaom decoder bit-exactly on the local test corpus and
on 275 of the 279 KEY temporal units in libaom's pinned test vectors. That is
not a full AV1 conformance claim. The authored decoder remains slower than
libaom on five of the six benchmark clips; `tests/av1_benchmark.sh` measures it. See
[docs/AV1.md](docs/AV1.md) for the module layout, contracts and tests.

```sh
sh tests/av1_critical_path.sh
```

libaom and dav1d are pinned test oracles only; no product module calls a native
codec. `tests/common.sh` and `tests/av1_headers.sh` build them under ignored
`build/native` when missing.
Ported libaom constants keep their notices in `src/LICENSE.libaom` and
`src/PATENTS.libaom`.

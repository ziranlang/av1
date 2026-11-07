# Changelog

## [Unreleased]

- Owned scalar AV1 decoder for shown KEY frames, moved here from the avif package.
- KEY frames may arrive as a frame header plus tile groups, with layer selection by operating point 0.
- Conformance test over libaom's pinned decoder test vectors.
- Benchmark against libaom and dav1d; faster self-guided restoration, CDEF and inverse transforms.
- Faster coefficient decoding, CDEF and self-guided restoration.
- Faster decoding with shared constant tables and direct restoration output.
- Faster CDEF, Wiener and self-guided restoration.
- Faster entropy decoding, inverse transforms, dequantization and 4-wide CDEF.
- Faster CDEF direction search and filtering.
- Faster self-guided restoration.
- CDEF filtering on SIMD vectors.
- Tests run bundles on the portable VM with `zi2zx run --vm`.
- The critical path runs each stage and conformance shard in its own build-queue ticket.
- Inverse transforms on SIMD vectors.
- Deblocking on SIMD vectors.
- The critical path queues a ticket the queue failed to run again and can resume a stopped run.
- Faster palette index maps.
- CDEF block copies on SIMD vectors.
- Self-guided restoration on SIMD vectors.
- Faster coefficient signs, dequantization, intra edges and range decoding.

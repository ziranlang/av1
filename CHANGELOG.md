# Changelog

## [Unreleased]

- Owned scalar AV1 decoder for shown KEY frames, moved here from the avif package.
- KEY frames may arrive as a frame header plus tile groups, with layer selection by operating point 0.
- Conformance test over libaom's pinned decoder test vectors.
- Benchmark against libaom and dav1d; faster self-guided restoration, CDEF and inverse transforms.

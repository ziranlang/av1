# Owned AV1 engine

This package is the authored AV1 engine. `DecodeAv1YUV` in `src/decoder.zi`
(exported as `av1/decoder`) decodes one shown KEY frame to owned source-depth YUV planes. It covers 8/10/12-bit monochrome,
4:2:0, 4:2:2 and 4:4:4, reduced still and full headers, multiple tiles, 64x64
and 128x128 superblocks and all ten partition types. Prediction covers all
thirteen intra modes and seven angle deltas, edge filtering/upsampling,
chroma-from-luma, the five filter-intra predictors and Y/UV palettes. Residuals
use every lossy transform size and type or the lossless WHT, with segmentation
and delta q/lf. The frame then passes through deblocking, CDEF and loop
restoration (Wiener and self-guided). Film grain is applied in place to the
whole frame.

Superres, non-flat quantizer matrices, intra block copy, metadata OBUs, and
inter, intra-only or hidden frames reject with
`UnsupportedCapability`. Output matches pinned libaom bit-exactly on the local
corpus described below, including 275 of the 279 KEY temporal units in
libaom's pinned decoder test vectors. That is not a full AV1 conformance
claim. The scalar decoder is still several times slower than libaom and dav1d
(see Performance baseline below). The avif package imports this package as a dependency; its
facade still uses the bridge by default.

Product codec code is handwritten `.zi`. Generated C/Go and portable `.zx`
execute the same scalar semantics. No upstream decoder is called by the
authored modules. Native upstream libraries are test oracles only. Modules
import each other with package-local file imports; `decoder` is the only export.

## Layout

- `src/decoder.zi`: the `DecodeAv1YUV` entry, limits, leases and output planes.
- `src/syntax/`: bit reader, OBUs, sequence and frame headers, profiles, tile
  layout, tile groups, partition contexts and frame state.
- `src/entropy/`: range decoder, mode, chroma and palette probabilities, and the
  coefficient probabilities, contexts, scans and block reader.
- `src/predict/`: intra prediction for every size and mode, CfL, filter-intra
  probabilities and palettes.
- `src/transform/`: transform-type selection, lossy inverse transforms, the
  lossless WHT, quantization and quantizer tables.
- `src/filter/`: deblocking, CDEF, Wiener and self-guided kernels, and the
  frame post-filter scheduler.
- `src/grain/`: film-grain parameters, scaling, noise and application.
- `src/decode/`: frame state (`intra_frame.zi`), mode info (`mode_info.zi`),
  residual reconstruction (`residual.zi`) and the partition and tile walk
  (`tile_walk.zi`).

## V01 contracts

- `NewBits(input,begin,end)` creates an MSB-first syntax `BitReader` over an
  absolute byte interval. `ReadBits(reader*,count)` reads 0–32 bits. Failed
  reads return zero, preserve the cursor, and retain the first `DecodeError`.
  `AlignBits` requires zero alignment bits; `FinishBits` requires a one followed
  by zero bits to the interval end. Neither operation decodes entropy symbols.
  `ReadSignedBits` handles 1–32-bit two's-complement literals; `ReadNS` handles
  bounded AV1 non-symmetric literals, including `n=1` without consuming input.
- `ReadLEB128(input,at,end)` returns `value`, `bytes`, `next`, and `error`.
  Non-minimal encodings are legal, including eight-byte encodings. Values
  greater than UINT32_MAX, or continuation on byte eight, are malformed.
- `ReadOBU(input,at,end,external_size=false)` returns an `ObuView` with absolute
  `start`, `payload`, `end`, type and layer IDs. Reserved types remain skippable.
  Missing size fields require an explicit enclosing one-OBU length. With
  `external_size=true`, an internal length must agree with that enclosing end.
  Header forbidden/reserved bits are validated; payload interpretation belongs
  to the next stage.
- `NewRange(input,begin,end,disable_cdf_update=false)` constructs the AV1 symbol
  decoder. `ReadSymbol(decoder*,cdf)` takes an **ascending** Q15 CDF: 2–16
  probabilities in `[0,32768]`, nondecreasing, terminal probability 32768,
  followed by the adaptation count in `[0,32]`. Equal probabilities are tested
  against libaom. This differs from the inverse CDF representation in native
  oracles. `ReadBool` and `ReadLiteral` decode entropy literals. They are not
  replacements for syntax `ReadBits`.
- Symbol decoding uses the specification's complemented range value and
  bounded virtual zero padding. The value tops a 64-bit window of the next
  complemented input bits, refilled a byte at a time. `RangeBitPosition` is
  where the specification's bit reader stands, and error offsets use its byte. Reads past the permitted padding return
  `TruncatedInput` at the interval end. `FinishRange` verifies `exit_symbol`'s
  one bit and zero tail and rejects reuse after success. Callers must finish a
  tile; decoding a symbol alone does not validate its termination. `DecodeSymbol` is the same decode without the CDF scan, for CDFs the decoder owns;
  `AdaptCdf` is its adaptation step alone.

Input slices borrow caller storage synchronously; keep it alive and unchanged.
There are no allocations, threads, globals or retained callbacks in V01.
Arithmetic and source bounds are checked before indexing, addition or narrowing
that depends on untrusted lengths. Errors use `image_codec.DecodeError`, with
absolute byte offsets; callers check `error.code` before using results.
Cancellation/working-memory/owned-plane scheduling belongs to the experimental
engine entry described below; primitive header parsers do not acquire leases.

## Intra frame decode

`DecodeAv1YUV(input,options*) -> Av1YUVResult` is internal and experimental.
It accepts one sequence header and one shown KEY frame, as libaom's OBU loop
does: an OBU_FRAME (which carries every tile), or a frame header followed by
tile groups in tile order, with matching redundant header copies, temporal
delimiters, padding and reserved OBUs. Operating point 0 drops OBUs of other
layers. Frame size is bounded by the caller's
`DecodeLimits` only. Full headers may override frame dimensions within the
sequence maximum. `decode/tile_walk.zi` walks tiles and the partition tree,
`decode/mode_info.zi` reads mode info and palettes, and `decode/residual.zi`
reads coefficients, dequantizes, inverse-transforms and predicts.
`filter/post_filter.zi` then schedules the scalar kernels over the whole frame,
in the specification order:

- Deblocking runs when either luma level is nonzero. It filters vertical then
  horizontal edges per plane, takes transform sizes from the decoded blocks and
  derives each edge's level from segment features, delta lf and sharpness.
- CDEF runs per 8x8 block that is not fully skipped. It finds the direction on
  deblocked luma and uses the 64x64 strength index and the chroma direction
  mapping. Samples outside the frame's MI grid are treated as missing.
- Loop restoration reads CDEF output inside 64-row stripes. Rows beyond a
  stripe come from the deblocked frame, as the specification requires. It
  applies each unit's Wiener or self-guided filter.

Each enabled filter adds one frame-sized sample buffer to the reserved working
bytes; identity filters allocate nothing. This function calls no native codec.

`build-queue -- sh tests/av1_intra_frames.sh` encodes 46 stills with the pinned
libaom and compares every row of every plane with libaom's decoder. Rows cover
8/10/12-bit mono/420/422/444, odd and tiny sizes, 2x2 tiles, 128x128
superblocks, palettes, segmentation, delta q, lossless and reduced transform
sets, each post filter alone and together, sharpness, delta lf and 1-8 CDEF
strengths. The corpus decodes 12 Wiener and 16 self-guided restoration units.
Truncated inputs must fail with no planes and released leases. Source and saved
IR run in Go, C and the direct VM, plus ASan/UBSan.

The encoder limits what the corpus can reach. libaom never codes skip=1 on intra
blocks, so that path has no oracle vector. Loop restoration is only searched at
GOOD speed 0 for frames this small, so restoration rows use speed 0. Sharpness
and perceptual delta q need all-intra usage, and ROI segmentation needs realtime
speed 7 or higher.

`Av1YUVResult` contains `is_ok`, `error`, sequence/frame metadata and shared
owned `Plane` fields `y`, `u`, `v`. For monochrome only Y is populated. Color
returns all three planes with chroma dimensions rounded up from source subsampling. Samples
are one byte at depth8 and little-endian two-byte values at depth10/12, tightly
packed with `stride = width * sample_bytes`. Input can be released or changed after the call. Normal
owned Vec lifetime/move rules apply; no input/reference/callback is retained.
The planes hold source-depth displayed YUV samples, including signalled film grain,
without range expansion, RGB conversion,
alpha composition or premultiplication. Sequence fields `bit_depth`,
`monochrome`, `profile`, `color_description_present`, `color_primaries`,
`transfer_characteristics`, `matrix_coefficients`, `full_range`,
`subsampling_x/y` and `chroma_sample_position` preserve the AV1 metadata.
The container/color layer must reconcile its BMFF metadata separately.

The entry validates options/input, leases one caller worker and reserves a
conservative 1MiB bound for control/CDF scratch before parsing. Once the frame
header is known it also reserves the sample planes, mode records and post-filter
buffers. It launches no threads. The sum of returned plane byte counts is charged separately to
`max_output_bytes`. `VecResizeBounded` caps each backing capacity to its exact plane
byte count; tests inspect capacity as well as length. Worker and scratch leases release on every return. This
per-call accounting is not a process-RSS cap or an aggregate limit on outputs
that callers deliberately retain. Cancellation is cooperative at stage/row
checkpoints under the shared single-thread token contract.

The memory receipt records the actual generated native sizes of the frame,
tile, block, edge and CDF records, coefficient and dequantization blocks,
sequence/frame headers and the public result. It conservatively counts two to
four copies of each record for factory results, locals and returns. Grain
control records, noise arrays, lookup tables and plane views have separate
conservative copy counts.
Embedded arrays are intentionally counted again in those separate copies.
The bound adds 32KiB for scalar helpers and at most six recursive partition
frames, plus 12,288 bytes for Gaussian-table temporaries. Grain stripe scratch
is frame heap, `GrainScratchCount(width)` s16 cells reserved with the frame.
The current logical codec bound is 458,690 bytes.

The native local test recompiles the actual generated translation units with
`-O2 -fstack-usage`. Summing every nonrecursive function frame together and
adding six extra copies of the largest recursive frame gives a separate native
stack bound, currently 583,000 bytes. Both bounds must fit the 1MiB reservation for the local test to pass. Exact sizes,
copy counts, function evidence and commands are saved in the engine's memory
receipt. This derivation covers the current scalar intra path, including
whole-frame grain, and must be recomputed when scratch or kernels expand.
Allocator size classes, Go/VM/runtime housekeeping, caller-retained outputs and
physical RSS are excluded; this is not a process memory limit.

Run `build-queue -- sh tests/av1_engine_parity.sh`. Real 8/10/12-bit AV1 still
fixtures are independently decoded and symbol-traced by pinned libaom. Native
source/saved IR, generated Go and direct VM must return the same owned planes.
Tests cover repeated ownership/drop, every truncated prefix, cancellation,
worker saturation, scratch/output caps and scalar stride/padding/overflow.
Native allocator fault injection and sanitizers verify allocation failure and
lease cleanup. FFmpeg/libaom is used only to generate test inputs; its command,
version and fixture digests are recorded under ignored build artifacts.

These flat stills test the entry contract, not AVIF fixture compatibility or
conformance. Do not select this engine as the AVIF facade default yet. RGB
conversion and alpha composition belong to the avif package.

`InverseLossless4x4(quant,depth,token)` in `transform/lossless.zi` is the
scalar dequantization/inverse WHT reference. It widens arithmetic, clamps intermediate values as specified and
rejects nonconforming residual ranges without exposing samples. Output
residuals are signed row-major values; clipping to the sample depth happens
after adding the prediction. `decode/residual.zi` uses it for every lossless
4x4 transform.

`syntax/partition.zi` owns the partition probability contexts. The tile walk
traverses NONE/HORZ/VERT/SPLIT, A/B and 1:4 nodes in the specified order, with
at most six recursive frames from a 128x128 root. Block-size and skip grids,
coefficient levels and DC signs are tile-owned state. They update across coding
blocks, not separately for each 8x8 block. Boundary
nodes outside both half-block boundaries force a split. A node crossing just
one boundary uses the specification's transient split-or-horizontal/vertical
CDF; this does not adapt the underlying partition distribution. Reconstruction
uses the padded MiCols/MiRows footprint and crops samples into the actual output
dimensions without retaining padded bytes in the returned plane.

`build-queue -- sh tests/av1_still_partitions.sh` compares 108 larger native
lossless inputs, including encoder-forced 8x8 split trees and intra controls.
All 108 planes match libaom across 1,189 coding blocks. Separate native-decoded standard whole-frame
vectors exercise all ten partition types, including rectangular blocks.
Both skipped blocks and explicit empty coefficient transforms are covered.
The native oracle reports actual block sizes and modes instead of assuming the
encoder honored its controls. Sizes16/32/64 and all three bit depths run from
source/saved IR in C, Go, direct VM and under ASan/UBSan.

`build-queue -- sh tests/av1_boundaries.sh` adds78 real tiny, odd and rectangular
frames at8/10/12 bits. It includes1x1,3x5,9x9,10x23,24x40,33x17 and63x64
boundaries. Native planes, exact backing capacity, source/saved IR, C/Go/direct
VM, output limits and sanitizers are verified.

`build-queue -- sh tests/av1_coefficients.sh` tests 36 real encoder-produced
lossless inputs. Every plane matches pinned libaom pixels, including nonzero
DC, signed checkerboards, an impulse and exact vertical/horizontal prediction.
Every input prefix rejects without planes or retained leases. The same suite
encodes 19 whole lossless frames with the pinned arithmetic encoder, with the DC
coefficient at each Golomb boundary through the 20-bit mask and one invalid
Golomb length. Conforming frames must match native libaom's plane. Frames
whose residual exceeds the specification's 1+BitDepth-bit range, which libaom
clips and decodes anyway, and the invalid length must reject with
`MalformedInput`. `build-queue -- sh
tests/av1_scalar_kernels.sh` compares 387 signed impulse/random WHT vectors
against the integer specification and the pinned scalar libaom kernel.
Both suites run source/saved IR through C, Go, direct VM and ASan/UBSan.

`predict/intra_predict.zi` is the scalar reference for all thirteen intra
modes at every transform size from 4x4 to 64x64: DC, vertical/horizontal,
the three directional zones with edge filtering and upsampling, the Smooth
family, Paeth, the recursive filter-intra predictor and CfL application.
`build-queue -- sh tests/av1_intra_predict.sh` compares it with libaom's
reconintra functions at all 19 sizes and 8/10/12 bits on source/saved C, Go,
direct VM and sanitizers.

Filter intra predicts each 4x2 group from its preceding predicted row before
residual addition. `predict/filter_intra.zi` owns the adaptive filter-presence
probabilities by coding-block size and the shared five-symbol filter-mode CDF.
`build-queue -- sh tests/av1_filter_intra.sh` compares 180 native-decoded
standard whole frames covering all five filters,
inactive filter flags, all ten partition types and all three sample depths.
Another 18 encoder-produced nonzero residual inputs exercise sequence-enabled
filter syntax, including 13 active filter blocks recorded by the native oracle.
Prefix truncation, source/saved C/Go/direct VM and ASan/UBSan are included.
Default CDFs and tap constants are checked against the pinned upstream
definitions.

Decoded flags select above-right and below-left availability for directional
prediction in actual partition decode order.
`build-queue -- sh tests/av1_directional.sh` compares 336 native-decoded
standard whole frames across eight directional modes, seven angle deltas, all
ten partition types, both edge-filter states and all three sample depths. It
runs source/saved IR through C, Go, direct VM and ASan/UBSan; derivative
constants are provenance-checked.

`decode/intra_frame.zi` owns the frame planes and neighbor state. It reconstructs U
followed by V, sharing the normative UV probability models while retaining
separate level/DC contexts. Subsampled 4x4 luma blocks consume chroma only at
the specified reference block. Directional chroma
edges use plane-specific decoded flags. `entropy/chroma_modes.zi` reads UV modes,
shared angle probabilities and signed CfL alpha values. For lossless coding,
CfL is allowed exactly when the chroma coding footprint is 4x4.

`predict/cfl.zi` performs bounded source-luma subsampling, edge repetition,
mean subtraction, signed alpha scaling and sample clipping for every chroma
transform size; the intra prediction suite compares it with libaom.

`build-queue -- sh tests/av1_chroma.sh` compares all three source-domain planes
for 189 actual lossless encoder inputs at8/10/12 bits and 4:2:0/4:2:2/4:4:4.
It includes tiny/odd dimensions through64, independent chroma patterns,
correlated luma/chroma, native CfL block observations and pinned UV mode
probability provenance. Plane metadata/capacity, aggregate output limits, truncation,
retained results across repeated decodes, input independence and failure of
each of the three allocations are covered. Optimized C, Go, direct VM, saved
IR and ASan/UBSan run separately.

`predict/palette.zi` owns luma/chroma palette parsing and fixed index-map storage.
`BuildPaletteCache` sorts and deduplicates the bounded above/left palette union.
`PaletteColorContext` derives the stable color order and context from the left,
diagonal and above indices; `ReadPaletteMap` ranks the same three neighbours
and decodes each token straight to its color. `ReadPaletteColors` reads sizes two through eight,
cache selection and source-depth colors. Luma deltas are positive; U permits
duplicate colors, while V uses literals or signed modular deltas. Metadata
for both palettes precedes `ReadPaletteMap`, which reads the non-symmetric first
index and adaptive diagonal tokens. The map extends cropped indices to the coding-block edge
and commits its active count only on success. Tile-owned neighbor palette
records update across coding blocks. Reconstruction adds lossless residuals to
palette predictions; active palettes omit filter-intra syntax as specified.
The UV map selects paired U/V colors. UV cache records follow the luma MI grid,
including leaves without chroma, separately from chroma prediction availability.
`entropy/palette_uv_cdfs.zi` owns the independent adapted UV size/index probabilities.

`build-queue -- sh tests/av1_palette.sh` compares all 1,365 possible neighbor
contexts for sizes two through eight against the pinned native context routine.
It also verifies 210 native-decoded standard whole frames across all ten
partition types, cache reuse/union, palette sizes and all sample depths. Another
81 encoder-produced inputs include tiny/odd sizes, 28 active palette blocks and
28 non-skipped palette blocks. The native oracle records the actual frame-header
length; no fixed prefix length is assumed. Default probability tables are
provenance-checked. Cache/descriptor bounds, prefix truncation, exact output
capacity, lease cleanup, source/saved C/Go/direct VM and ASan/UBSan are included.

`build-queue -- sh tests/av1_color_palette.sh` adds 315 encoder-produced color
inputs with 767 active Y and 538 active UV palette blocks, across three depths
and subsampling modes, tiny/odd sizes and dimensions through64. All source
planes match native decoding. `build-queue -- sh
tests/av1_color_palette_boundaries.sh` verifies another164 native-decoded
standard whole frames with every palette size, all ten partition types,
cache reuse, duplicate U entries, both signs/zero V deltas and modular wraps.
It also rejects25 native-rejected portrait4:2:2 partition streams, as required
by the specification's `Subsampled_Size` table. Those invalid frames contribute
no valid-vector coverage counts. Both tests run source/saved IR through C, Go,
direct VM and ASan/UBSan. These bounded tests do not establish full conformance.

`build-queue -- sh tests/av1_key_frames.sh` adds 153 native-decoded full-header
shown-key color inputs at8/10/12 bits, including actual CfL, tiny/odd dimensions,
header-field comparisons, truncation, retained results and plane allocation
failures. `build-queue -- sh tests/av1_key_controls.sh` verifies36 additional
whole frames at all three depths, mono and color, with sequence maxima8/64,
explicit size overrides, frame IDs and equal-interval/decoder-model timing.
These cases compare header state and every source plane against the pinned
native decoder. Their encoder is the package's test-only libaom oracle; product
code never calls it.
Both stages run source/saved IR through C, Go, direct VM and ASan/UBSan.

## V02 header contracts

`ParseSequenceHeader(input,begin,end,limits)` consumes one complete sequence
payload and its trailing bits, returning `SequenceHeaderResult`. It describes
profiles 0/1/2, 8/10/12-bit samples, monochrome, color, timing, decoder-model
fields and up to 32 operating points in fixed storage. Dimension and metadata
limits apply before further use. Invalid profile/tool combinations, duplicate
operating points and malformed syntax reject explicitly.

`ParseStillFrameHeader(input,begin,end,sequence,limits,combined=true)` currently
accepts shown KEY pictures with reduced or full headers. It returns a `StillFrameHeaderResult`
with parsed control state and absolute `next` byte position. `combined=true`
validates byte alignment within OBU_FRAME, leaving the tile-group header at
`next`; `false` validates the complete OBU_FRAME_HEADER trailing bits. Parsed
state includes superresolution/render sizes, tile geometry, quantization,
segmentation, lossless flags, deblocking/CDEF/restoration parameters and transform
flags. `ReadTileLayout` uses fixed 65-entry boundary arrays and enforces 64
rows/columns before storing. Full headers also preserve current frame IDs,
order hints, frame/operating-point timing, size override and CDF controls.
KEY pictures use primary-reference NONE, refresh all reference slots and force
integer motion as specified. The experimental entry retains no references;
hidden, inter, intra-only and existing-reference pictures reject explicitly.
Film-grain parameters are bounded and parsed into owned control records.
Parsing other tools' parameters does not imply that reconstruction implements
those tools.

These header APIs expose borrowed input and fixed control records internally.
They do not create references, retain planes, schedule workers or return images.
Retained reference state for inter frames remains unfinished.

`NewTileGroup(input,begin,end,layout,cancellation=null)` and
`NextTileSpan(group*)` provide allocation-free tile-group framing. A cursor
validates explicit group ranges, zero alignment, little-endian tile lengths,
final-tile remainder and cancellation before each span. Spans carry absolute
byte intervals and frame tile indices. Input/token storage must outlive the
cursor. This serial iterator launches no workers and decodes no coefficients.
Its initial local suite is `build-queue -- sh tests/av1_entropy_tiles.sh`;
source/saved C/Go, direct VM and sanitizers cover one-tile groups, lengths,
fragments, malformed/truncated headers and mid-group cancellation. Tiles are
decoded serially; worker scheduling is not implemented.

Run their local suite with:

```sh
build-queue -- sh tests/av1_headers.sh
```

The default suite requires a test-only dav1d built from the pinned package
source; `tests/av1_headers.sh` builds it with meson under ignored
`build/native/dav1d` when it is missing. It compares 158
sequence headers (146 generated according to the primary AV1 syntax plus 12
sequences from the AV1 items of the avif package fixtures, pinned in
`tests/fixtures`): 50 fields plus all operating
point/delay parameters, including minimum/maximum legal picture intervals, and
rejects the forbidden saturated interval and invalid monochrome identity
matrix/subsampling combination. It also compares 94 fields in 13 real reduced
still frame headers against dav1d's decoded frame metadata and rejects every
truncated header prefix. These cover existing 8/10/12-bit, color and monochrome
fixtures plus three actual multi-tile lossless/lossy inputs. Actual tile
boundaries and payload spans are compared too. Source and saved IR run in C,
Go and the direct portable interpreter, and native runs use ASan/UBSan.
Header and tile-span parity alone does not establish plane parity; the intra
frame suite covers that.

The oracle is dav1d 1.5.4 at revision
`54706fc6bc0cdecab7e9593974a4039cc038fca7`. Tests verify clean tracked source,
the library's `1.5.4-0-g54706fc6` identity and its SHA256. Override its artifact
path with `AV1_DAV1D_LIBRARY` and source with `AV1_DAV1D_SOURCE` if integration
stages the same pinned recipe elsewhere. Local result receipts are written
under ignored `build/scratch/av1{,-headers}/receipt.json` after complete runs.

## Scalar filter references

`filter/deblock.zi` provides allocation-free `FilterDeblockEdge` for input ordered
`p7..p0,q0..q7`. It derives the edge, high-variance and flat masks from scaled
thresholds, then applies the narrow or 6/8/14-sample wide filter. Scheduling and
adaptive threshold selection remain caller responsibilities. Input and threshold
bounds are validated before filtering; failure returns no committed samples.
`FilterDeblockLines` filters the four lines of an edge segment in place, one
line per `std/simd` `s32x4` lane, with the same masks as lane masks. It is a
`#target` procedure, reads only the samples the filter length covers and writes
only those it can change. A sample above the depth, or any tap outside the
buffer, rejects the whole segment unwritten. The frame loop filter uses it for
every edge.

`build-queue -- sh tests/av1_deblock.sh` passed 3,072 vectors against both
horizontal and vertical pinned libaom scalar functions and an independent
normative reference. It covers 8/10/12-bit samples, enabled/disabled edges,
high variance, flat and wider-flat cases, signed rounding boundaries and 5,136
changed samples. Native oracle guard cells and every inactive product sample
are checked. 1,024 four-line segments, each with four different lines and 363
with differing decisions between lines, must match the pinned libaom functions
across horizontal and vertical edges, leaving every guard cell unchanged.

`filter/cdef.zi` provides allocation-free `FindCdefDirection` and `FilterCdefBlock`.
The filter consumes a caller-resolved two-pixel halo, uses 30000 only for
unavailable neighbors, and returns a block with stride 8. Center samples must
be real samples. Bounds, source depth, strengths, direction and damping are
validated before filtering; errors and cancellation return no committed samples.
The primary strength accepts the intermediate high-bit-depth values produced
by normative luma variance adjustment. `CdefFilterPadded` is the filter alone for a validated block padded to 16
samples per row, written at any output stride; the frame CDEF pass uses it.
It filters eight samples per `std/simd` `s16x8` vector and is a `#target`
procedure, so native x86-64 C picks SSE2, SSSE3, SSE4.1 or AVX2 at run time;
every target gives the same bits.
`CdefDirectionOf` is the direction search alone for an 8x8 block of real
samples, also used by the frame pass.

`build-queue -- sh tests/av1_cdef.sh` passed 768 direction and 1,806 filter
vectors against the pinned libaom scalar functions and an independent reference
of the pinned specification. Coverage includes all eight directions, 8/10/12-bit
samples, four 4/8 block shapes, all 16 missing-halo masks, syntax strengths and
every between-scale primary strength at higher bit depths. The vectors exercise
14,326 changed samples. Malformed input, cancellation and inactive output cells
are verified too.

`filter/wiener.zi` provides allocation-free `FilterWienerBlock` for blocks up to 8x8.
It consumes a caller-resolved three-pixel halo and separate horizontal and
vertical three-tap coefficient sets. It validates the normative tap ranges,
performs horizontal filtering before vertical filtering, and applies the
bit-depth-specific signed rounding and intermediate clipping. Chroma callers
must supply outer tap zero. Output has stride 8 and commits only on success.
The frame post-filter calls the unchecked kernel `WienerFilterInto` on its
in-range unit parameters, writing straight into the frame at its stride.

`build-queue -- sh tests/av1_wiener.sh` passed 576 vectors against the pinned
libaom scalar function and an independent normative reference, covering
8/10/12-bit samples, all 27 combinations of minimum/zero/maximum taps, random
taps, identity and chroma five-tap filters. The corpus exercises three
intermediate clips and 1,586 final clips, including 1x1 and rectangular blocks.

`filter/sgr.zi` provides allocation-free `FilterSgrBlock` for self-guided restoration
up to 8x8 with the same three-pixel halo contract. It computes the two
normative box-filter passes and the signed projection four columns at a time
in 32-bit lanes. The variance term is capped where the normative strength
already saturates at 255, so every product fits 32 bits. All 16 parameter
sets are supported. Projection coefficients use `LrSgrXqd` semantics,
including the implicit values for disabled passes. The frame post-filter
calls the unchecked kernel `SgrFilterInto` the same way, on blocks up to
64x8.

`build-queue -- sh tests/av1_sgr.sh` passed 1,824 vectors against the pinned
libaom scalar function and an independent normative reference. It covers
all parameter sets, 8/10/12-bit samples, signed projection boundaries, both
single-radius cases, 1x1 and rectangular blocks, 288 wide blocks up to 64x8
with every vector tail (30 of them at the maximum sample everywhere),
29,794 changed samples and 1,273 clipped outputs. Invalid implicit
coefficients reject explicitly.

These tests pass source and saved IR in C, Go and the direct portable VM, with
ASan/UBSan on generated C. Receipts are saved under ignored
`build/scratch/av1-{deblock,cdef,wiener,sgr}`. These are kernel math tests;
frame scheduling in `filter/post_filter.zi` is covered by the intra frame suite.

`build-queue -- sh tests/av1_filter_kernels.sh` combines the four scalar test
stages. It records source and compiler hashes and rejects changes to those inputs
during its run. Its receipt is separate from the encoded-frame engine suite.

## Film-grain syntax and scaling primitives

`grain/grain_params.zi` provides `ReadStillFilmGrain` for displayed non-inter frames.
It reads from the caller's exact MSB syntax position, checks point counts and
strictly increasing coordinates, enforces the 4:2:0 chroma-pair constraint, and
returns signed AR coefficients. Absence resets all fields. Truncation, malformed
input and cancellation commit no parameters. Inter-frame inheritance is outside
this API's contract.

`build-queue -- sh tests/av1_grain_params.sh` passed 128 parameter streams
produced and read by the pinned unmodified native film-grain syntax functions.
It compares 160 fields and bit positions across mono/420/422/444, every starting
bit position and all four AR lags. It also verifies 5,060 truncated byte prefixes
and 380 count, ordering and chroma-pair mutations. These are authoritative syntax
symbol streams, not encoded-frame conformance vectors.

`grain/grain_scaling.zi` provides caller-owned `NextGrainRandom`, `BuildGrainScaling`
and constant-time `SampleGrainScaling`. It supports zero-point curves, up to
14 sorted points, signed interpolation and the final high-bit-depth lookup bin.
Random state commits only after successful validation and cancellation checks.

`build-queue -- sh tests/av1_grain_scaling.sh` passed 4,096 native random-state
transitions at bit widths 1 through 16, 128 native scaling curves (32,768 table
entries), and 24,576 sampled values across 8/10/12-bit depth. An independent
reference agrees with the native results. Invalid input and cancellation are
covered too. Both grain component tests pass source and saved IR in C, Go and
the direct portable VM, with ASan/UBSan on generated C. Their receipts live under
ignored `build/scratch/av1-grain-{params,scaling}`.

`grain/grain_noise.zi` provides allocation-free `GenerateGrainNoise`. It obtains the
normative Gaussian table once, generates the seeded white noise, applies luma AR
filtering and chroma/luma correlation, and clips to the source-depth grain range.
The owned arrays have stride 82; luma is 82x73 and chroma is 44/82 by 38/73.
Inactive planes and cells are zero. Validation and cancellation failures commit
no arrays.

`build-queue -- sh tests/av1_grain_noise.sh` passed 48 native Gaussian/AR cases
with 861,984 complete-array sample comparisons across mono/420/422/444,
8/10/12-bit depth, every AR lag, grain shift and AR shift, including inactive
planes and independent chroma. Every sample passes source and saved IR in C,
Go and the direct portable VM, with C sanitizers. The corpus is divided into
six programs to respect the portable reader's 100,000-record IR table limit;
large literal comparisons are split into bounded functions.

`grain/grain_apply.zi` provides allocation-free `ApplyFilmGrain`, which updates
borrowed source planes of any size in place, one 32-row luma stripe at a time.
It builds each stripe's noise blocks, blends horizontal and vertical block
boundaries (the previous stripe's two overlap rows are carried in the scratch),
scales chroma using the stripe's original luma, applies signed chroma mixing and
clips active components to full or restricted range, including the
identity-matrix chroma limit. The caller lends `GrainScratchCount(width)` s16
scratch cells: three planes of 36 rows of `GrainStripeColumns(width)` columns.
Geometry, parameters and scratch are validated before the first write; a sample
above the bit depth or a cancellation returns an error with the planes partly
updated, and the decoder then discards them.

`build-queue -- sh tests/av1_grain_apply.sh` passed all 1,072,962 visible
samples of 96 raw native pictures, including 915,917 changed samples. It covers
mono/420/422/444, 8/10/12-bit depth, 1x1, odd 7x5, 33x35 and 64x64 dimensions,
and 48 pictures above 64x64 (65x65, 129x35, 66x131, 257x97) with several grain
blocks, stripes and partial last stripes on padded strides, both overlap
states, restricted/full clipping and identity-matrix chroma. Samples outside
the picture must keep their fill. All eight corpus programs pass source/saved
C, Go and direct VM plus sanitizers.
Raw native picture parity is separate from encoded-frame decoder parity.

`build-queue -- sh tests/av1_grain_kernels.sh` combines the four grain
component tests and records unchanged source/toolchain fingerprints. Its receipt
is separate from the encoded-frame engine suite.

The private decoder calls these modules for grain-bearing lossless KEY pictures.
`build-queue -- sh tests/av1_grain_frames.sh` passed 72 actual encoded pictures
against the pinned native decoder: 160 grain fields per case and all 611,192
displayed samples. The corpus covers mono/420/422/444, 8/10/12-bit depth and
1x1, 7x5, 33x35 and 64x64 lossless dimensions, plus 24 frames above 64x64
(65x66, 130x97, 96x129, 200x72), half of them lossy at q40 with deblocking
(5 frames), CDEF (12) and loop restoration (12) active before the grain. Frames
up to 64x64 compare every sample; larger ones compare per-row hashes. All twelve
corpus programs pass source and saved IR in C, Go and the direct portable VM,
plus C sanitizers. Tests also cover 1,166 truncated prefixes, each of the 12
allocations of a large lossy grain frame failing in turn, exact
plane capacities, independent retained outputs and complete worker/scratch
lease cleanup. The fresh engine memory test includes the grain path's scratch.

Both header parsers check the sequence maxima with the shared
`ValidatePixelDimensions`, which has no RGBA8 byte floor. The output cap is the
exact aggregate source-YUV byte count of the displayed frame; the grain suite
checks that one byte less fails with `LimitExceeded`.

## Scalar quantization and dequantization

`transform/quantization.zi` provides allocation-free `ResolveQuantizerIndex` for base,
current delta-Q and segment alternate-Q controls, and `GetQuantizationSteps` for
DC/AC lookup with signed seven-bit plane deltas. The 1,536 normative lookup
values in `transform/quant_tables.zi` agree exactly with the pinned specification and
native functions at every depth. The selected table is obtained once for both
DC/AC lookups.

`DequantizeBlock` accepts all nineteen transform sizes. Input coefficients are
the compact row-major `min(width,32) * min(height,32)` signed 20-bit array after
syntax masking. Empty matrix weights select ordinary DC/AC quantizers; otherwise
the caller supplies the selected positive inverse weights for that compact
rectangle. Matrix level, transform-type and lossless gating remain the caller's
responsibility. Products use widened arithmetic, the normative 24-bit magnitude
mask, transform-size division and source-depth clipping. The owned 4,096-cell
result uses the full transform width; cells outside the coded upper-left
rectangle and inactive cells remain zero. Errors and cancellation return no
coefficients. Borrowed inputs remain unchanged. `DequantizeInto` writes the same values into a caller buffer.
`DequantizeSparseInto` does so with flat weights from a list of the coded
positions that may be nonzero, reading only those coefficients.

`build-queue -- sh tests/av1_quantization.sh` passed 98,304 lookup/delta cases and
114 dequantization vectors across 8/10/12-bit depth and all nineteen sizes.
`DequantizeSparseInto` matches `DequantizeBlock` on 342 sparse blocks of every
size and depth, from one coefficient to all of them in random order, with
samples past the block untouched.
Native inverse-matrix data covers all fifteen active levels and both plane
classes. The normative coefficient reference checks 44,640 compact coefficients
and all 466,944 output cells, including 28,885 product wraps and 41,495 clips.
All twenty corpus programs pass source/saved C, Go and direct VM plus C
sanitizers. Twenty-one forged invalid native slice counts reject null data
before arithmetic/access; a null-data empty matrix succeeds. Fourteen source and
compiler fingerprints remain unchanged during the run.

`build-queue -- sh tests/av1_scalar_kernels.sh` now combines these tests with the
387-vector native/spec WHT corpus and passed. The scalar suite writes its final
receipt only after both components succeed. The frame decoder dequantizes
every lossy block with these kernels; non-flat quantizer matrices reject.
Matrix-data kernel tests are not encoded-frame conformance tests.

## Coefficient scan ordering

`entropy/coefficient_scan.zi` provides allocation-free
`BuildCoefficientScan(width,height,mode,cancellation) -> CoefficientScan`.
`ScanDefault` covers all nineteen transform sizes with the normative upper-left
32 coefficient mapping. `ScanRows` and `ScanColumns` cover the nine sizes whose
longest side is at most 16. The result owns forward and inverse 1,024-entry
`u16` maps, the compact coded dimensions and the active count. Inactive entries
remain zero; invalid dimensions/modes and cancellation commit no maps. `CoefficientScanPrefix` fills only the first positions, up to a block's end.

The tile caller selects the scan mode from `PlaneTxType`, including selecting
the default scan for transforms with a 64-pixel side before one-dimensional type
mapping. Transform-set legality remains the caller's responsibility. The helper
generates the diagonal, row and column orders directly; product code contains
no native lookup calls or copied scan arrays.

`build-queue -- sh tests/av1_coefficient_scan.sh` passed all 37 normative scans:
9,008 forward positions and 75,776 complete forward/inverse/inactive cells.
Each expected position agrees with both the pinned specification's chapter10
tables and the pinned native arrays after normalizing their transposed storage.
Source/saved C, Go, direct VM and C sanitizers pass, with twelve controlled
source/compiler fingerprints unchanged. Invalid shapes/modes, cancellation and
zero failure outputs are covered too.

`build-queue -- sh tests/av1_entropy_tiles.sh` combines these tests with bounded
tile-group framing/cancellation and the coefficient contexts below, and passed.
Its receipt is written only after all components succeed. The frame decoder
uses these scan maps for every coefficient block.

## Coefficient entropy contexts

`entropy/coefficient_context.zi` provides allocation-free `GetCoefficientContexts` over
borrowed unsigned magnitudes in compact row-major storage. It returns the
selected base or base-eob CDF index and the BR index, using bounded neighbor
reads, magnitude clipping, source transform aspect and AV1 2D/horizontal/vertical
classes. These classes are distinct from the scan-mode constants. The caller
provides a matching scan index/position and validates frame transform legality.
Default contexts cover all nineteen sizes; one-dimensional contexts cover the
nine sizes with longest side at most 16. No CDF, input or decoder state mutates.
Invalid dimensions, positions, classes, slice counts and cancellation return
zero context fields with an error. `CoefficientContextsAt` selects over levels padded with four zero columns and rows (`CoefficientLevelStride`).
`CoefficientEOBContext`, `CoefficientBaseContext` and `CoefficientBrContext`
are its three selections, for callers that need only one.

`build-queue -- sh tests/av1_coefficient_context.sh` passed 111 native level
planes and 108,096 context-field comparisons at 27,024 positions. Zero,
saturated and seeded-random magnitudes cover all three context classes and
their scan orders. Native functions agree with an independent reference using
the exact pinned specification offset tables. The nineteen corpus programs
pass source/saved C, Go, direct VM and C sanitizers; fourteen controlled
source/compiler fingerprints remain unchanged. Pinned parsing and scan-table
documents have fixed, verified digests. The expanded tile/entropy suite passes
these tests too. The frame decoder uses these contexts.

## Owned coefficient probabilities

`entropy/coefficient_cdfs.zi` provides `NewCoefficientCDFs` with 3,835 owned probability
cells, including independent adaptation counters. The four normative default
categories use the frame's base quantizer, with boundaries at 20, 60 and 120.
Block delta-Q does not select the category. The pinned libaom probability data
retains its copyright, BSD license and patent notice.

`entropy/coefficient_cdf_symbols.zi` provides bounded `SelectCoefficientCDF` address
metadata and `ReadCoefficientSymbol`. The latter selects and updates the row
synchronously within the state owner, without returning a borrowed slice.
Seven families cover skip, DC sign, base, base-eob, BR, EOB extra and all seven
EOB areas. BR clamps the transform-size context to 32. Invalid selectors,
factory errors, entropy errors and cancellation return explicit errors.

`build-queue -- sh tests/av1_coefficient_cdfs.sh` passed 6,536 row selections
and 30,680 probability-cell comparisons at eight quantizer boundaries, plus
52 native entropy streams with 3,328 symbols. Final adapted probabilities
agree with libaom; unrelated rows and independent fresh states remain unchanged.
Source and saved IR pass C, Go and the direct VM, with C sanitizers and unchanged
source/compiler fingerprints. These are component symbol streams; general
coefficient integration is described below and used by the frame decoder.

## General coefficient block syntax

`entropy/coefficient_block.zi` provides allocation-free `ReadCoefficientSkip` and
`ReadNonzeroCoefficients`. Keeping them separate lets the caller read luma
transform-type syntax between skip and nonzero coefficients, as required by
the specification. The caller resolves the legal transform class and neighbor
contexts. No transform-type syntax is omitted inside this component. `DecodeNonzeroCoefficients` writes the coefficients into a caller buffer instead of returning them,
using a caller `CoefficientScratch` for the levels, scan positions and the
coded positions of nonzero levels, which the summary counts; the tile keeps
one.

The reader covers nineteen transform sizes, the permitted 2D/horizontal/vertical
coefficient classes, all EOB areas, reverse base/BR levels, forward signs and
bounded Golomb extensions. It returns owned compact row-major signed quantized
coefficients, cumulative level and DC category. Raw dimensions select entropy
contexts; a 64-pixel side codes only its upper-left 32 coefficients. Errors
return a zero block; consumed entropy/CDF state requires discarding the tile.

`build-queue -- sh tests/av1_coefficient_block.sh` passed 224 streams emitted by
the unmodified pinned `av1_write_coeffs_txb`, covering both plane categories,
all four base-Q probability categories, empty/DC-only/full/random EOBs, signed
levels, the 20-bit mask boundary and disabled adaptation. It compares all
229,376 coefficient cells and 859,040 probability cells, including inactive
storage. Thirty-two seeded arbitrary buffers test deterministic errors and
zero output on failure. Source/saved C, Go, direct VM and C sanitizers pass
with unchanged controlled source/compiler inputs. These are authoritative
coefficient syntax streams; complete lossy frames are covered by the intra
frame suite.

`entropy/coefficient_neighbors.zi` provides `GetTransformCoefficientContexts` over
caller-selected level and DC edge spans. It computes all thirteen skip contexts
and three DC sign contexts, including chroma's DC-only nonzero edge state.
Spans may end at the frame boundary; missing entries contribute zero. The
function validates transform/residual-block geometry, span lengths, level/DC
ranges and cancellation before returning contexts. It never updates the edge
arrays. `entropy/coefficient_geometry.zi` holds the shared bounded size-context mapping.

`build-queue -- sh tests/av1_coefficient_neighbors.sh` passed 5,232 native and
independent specification cases across nineteen transform and twenty-two
residual-block sizes, comparing 10,464 context fields. Coverage includes zero,
small and saturated levels, every sign combination, shortened spans and
invalid dimensions, lengths, levels and signs. Source and saved IR pass C, Go,
the direct VM and C sanitizers. The native oracle packs clipped levels and DC
signs into bytes; the owned implementation keeps the specification's separate
arrays. Tile edge scheduling remains outside this component's coverage.

## Lossy inverse transforms

`transform/inverse_transform.zi` implements AV1 7.13 for all nineteen lossy
transform sizes: DCT 4–64, ADST 4–16 and identity 4–32 in both directions,
including the rectangular 1/sqrt2 scaling, `Transform_Row_Shift`, the row/column
clamp ranges and the specification's Hadamard clamps. The scalar reference
lane uses s64 intermediates. Each pass runs four rows or columns at once on
`std/simd` `s32x4` vectors in a `#target` procedure, so native x86-64 C picks
SSE2 to AVX2 at run time. A vector pass stays exact while every value is within
2^17 (ADST4 inputs within 2^16). A pass that exceeds that bound, and every
12-bit row pass, runs on the s64 lane instead, so the results are identical.

`InverseTransform2D(dequant,residual,width,height,tx_type,bit_depth,token)`
reads the row-major `width x height` Dequant array produced by `DequantizeBlock`
and ignores cells outside the upper-left 32x32, as the specification requires.
Inputs must lie in the (BitDepth+8)-bit Dequant range. It writes the unflipped
row-major Residual and may run in place. `AddResidual(samples,stride,residual,...)`
applies the type's vertical/horizontal flips and Clip1 to caller-owned samples.
Any failure clears the residual or leaves the samples unchanged; neither
function allocates. Transform-set legality belongs to `transform/transform_type.zi`;
these kernels accept every computable direction/length combination.

`build-queue -- sh tests/av1_inverse_transform.sh` builds the pinned libaom
forward kernels into a test-only host, transforms bounded residual patterns,
rounds them to dequantizer steps and checks libaom's `av1_inv_txfm2d_add_*_c`
output. An independent Python transcription of 7.13, with its conformance range
checks, must agree with libaom on every pixel. The suite covers 843 conforming
cases across all 185 legal size/type pairs at depths 8/10/12, plus 66 extreme
nonconforming inputs whose deterministic s64 results come from the reference
alone. Boundary cases cover invalid geometry, types and depths, out-of-range
input, aliasing, ignored high-frequency cells, flips, clipping and cancellation.
Source and saved IR run in C, Go and the direct VM, and under ASan/UBSan. The
frame decoder calls these kernels for every lossy transform block.

## Reproducible local tests

Run from this package:

```sh
build-queue -- sh tests/av1_obu_range.sh
```

`sh tests/av1_critical_path.sh` runs 26 combined owned stages, each stage and
each conformance shard in its own build-queue ticket so other projects build
between them. A ticket the queue fails to run (exit 78) is queued again, at
most three times. `sh tests/av1_critical_path.sh resume` continues a stopped
run from its first stage or conformance shard that has not passed. They include the transform, prediction, filter and grain kernels, encoded-grain
frames, the 46 libaom intra frames, OBU framing and the conformance corpus.
`tests/av1_obu_framing.sh` checks 96 framing variants (split headers, tile
groups, redundant/padding/reserved OBUs, layer dropping and malformed orders);
pinned libaom gives the verdict on each one. `tests/av1_conformance.sh`
fetches libaom's 240 IVF test vectors into ignored `build/corpus`, verified
against the pinned `test-data.sha1`. Every native frame must match the
upstream MD5. Each KEY unit, plus one small inter unit per vector, must then
be bit-exact or reject as pinned in `tests/av1_conformance_expected.json`.
`AV1_CONFORMANCE_DISCOVER=1` only reports outcomes for review.
`AV1_CONFORMANCE_SHARDS=prepare` only builds the shards; `N` or `FIRST-LAST`
then runs those shards, and the stage receipt appears once all have passed.
The critical path receipt
records the tested source hashes, five canonical compiler binary hashes and
individual stage receipts under ignored build artifacts. It rejects source/test
or compiler-binary changes during its run. A previous
receipt remains a historical snapshot when later implementation is in progress.

The tests need a test-only libaom built from the pinned package source.
`tests/common.sh` resolves it through `ziran pkg path libaom`, verifies the
exact revision and a clean tree, and builds one encoder+decoder archive with
its config headers under ignored `build/native/aom` when it is missing.
`AV1_AOM_SOURCE`, `AV1_AOM_LIBRARY`, `AV1_AOM_CONFIG` and
`AV1_AOM_ENCODER_LIBRARY` may point to the same verified pinned oracle
elsewhere. Product modules never link it. The tests use the canonical
toolchain selected by `tests/common.sh`, including `ZIRAN_ROOT`, take its
binaries from `ZIRAN_BIN_DIR` (default `build/bin`) and run bundles with
`zi2zx run --vm`, so they execute on the portable VM rather than as native
code. The critical path copies the five binaries once into ignored
`build/toolchain` and runs every stage on that copy, so a rebuild of the shared toolchain cannot change them mid-run.

The deterministic seed `0x415631` covers 192 libaom symbol/CDF cases (10,538
accepted symbols), counters 0/15/16/31/32, update enable/disable, flat and endpoint
CDF cells, short-input overread, 132 integer-oracle syntax cases, 512 randomized
LEB128/OBU boundary cases, and fixed bit-order/length/trailing-bit cases. Another
90 streams (2,190 known symbols) are produced by the pinned upstream entropy
encoder and validate termination, zero padding, corrupted tails and reuse.
Nine
forged invalid native slice counts, including INT64_MIN and INT64_MAX, must
reject a null data pointer before arithmetic or access. Source and saved `.zir`
run in native C, generated Go and the direct portable interpreter. Native C and
the descriptor regression run under ASan/UBSan. Test artifacts and generated
oracle source stay under ignored `build/scratch/av1`.

Random entropy bytes exercise symbol primitives; they are not conforming frame
vectors. Replacing the bridge still needs real AVIF fixtures and inter frames.

## Performance baseline

`build-queue -- sh tests/av1_benchmark.sh` encodes six KEY frames with the
pinned libaom encoder, from 352x288 to 1080p, 8 and 10 bits, 4:2:0 and 4:4:4,
natural and screen content. It decodes each one in one process with the
authored decoder (generated C, `-O3`), libaom and dav1d, all on one thread
and one pinned core. libaom and dav1d use their own SIMD. Each iteration is a
one-shot decode from memory: setup, decode and release. A row counts only when
the three sample hashes agree. The receipt
`build/scratch/av1-benchmark/receipt.json` records the medians, iteration
counts and allocation calls per decode for one machine.

On 2026-11-06, on a Ryzen 9 9950X, the authored decoder first took 28 to 576
ms per frame. It makes 9 to 11 allocation calls per decode at every frame
size, so no allocation happens per block. gprof profiles then guided scalar
changes, each with identical output:

- `filter/sgr.zi`: column box sums, shift rounding and a reciprocal table.
- `filter/cdef.zi`: taps resolved per block, zero-strength taps and the
  redundant clamp skipped, output written straight into the frame.
- `transform/inverse_transform.zi`: a swap-network bit reversal; zero rows
  and columns skip the 1D kernels.
- `syntax/bits.zi` and `entropy/range.zi`: byte-wise bit reads, a dedicated
  boolean decode, and decoder-owned CDFs read without the per-symbol scan.
- `entropy/coefficient_*.zi`: the scan built only up to the end of block,
  and context neighbors read from zero-padded levels.
- `transform/quantization.zi` and `decode/residual.zi`: coefficients and
  dequantized values go to tile buffers, not 4 to 16 KB returned structs.

After them it took 16 to 360 ms per frame. The decoder then moved onto
newer Ziran code generation, again with identical output:

- Fixed tables are module constants, shared read-only data: transform
  cosines, quantizers, smooth weights, filter-intra taps and the self-guided
  parameters and reciprocals. Coefficient, transform-type and grain tables
  stay procedures, since each tile or frame copies them into mutable state.
- `entropy/range.zi`: renormalization shifts by `clz` of the range.
- `filter/post_filter.zi`: restoration halos resolve each row and column
  once, and Wiener and self-guided blocks are written straight into the frame.
- `filter/cdef.zi`: each block row filters eight columns at once in 16-bit
  values, with indices the compiler proves in range, so the C compiler
  vectorizes the tap loop. Missing neighbors provably add nothing and need
  no test.
- `filter/sgr.zi` and `filter/wiener.zi`: blocks span up to 64 columns of a
  restoration unit, box sums slide along each row, and Wiener rounding
  shifts instead of dividing.

A further set, again with identical output:

- `entropy/range.zi` and `syntax/bits.zi`: renormalization reads up to 16
  bits from one three-byte window and returns at once when no shift is due.
- `entropy/coefficient_*.zi`: levels and scan positions live in tile scratch,
  and the BR context is selected only for levels above two.
- `transform/inverse_transform.zi`: 1D kernels index one 64-value lane by
  masked positions the compiler proves in range, and cosines come from one
  signed table.
- `transform/quantization.zi`: dequantization shifts instead of dividing.
- `filter/cdef.zi`: 4-wide blocks filter two rows per pass, filling all
  eight lanes.

Then, on newer Ziran range proofs and the `#cold` modifier:

- `filter/cdef.zi`: the direction search runs in 32-bit values without
  revalidating frame samples, and every tap read of the filter is proven in
  range.
- `syntax/bits.zi`: `ReadBits` is one out-of-line cold definition, which
  keeps the symbol decoder's inlined renormalization small.
- `filter/sgr.zi`: short guards restate the caller's bounds, so the box sums
  and the projection read and write without bounds checks.

Then, on Ziran's `std/simd` vectors, with SSE2 to AVX2 chosen at run time:

- `filter/cdef.zi`: the filter runs each pass of eight samples as one
  `s16x8` vector.
- `transform/inverse_transform.zi`: each transform pass runs four rows or
  columns as `s32x4` vectors and skips all-zero groups.
- `filter/deblock.zi`: each edge segment filters its four lines as `s32x4`
  vectors, validated once per segment.
- `filter/post_filter.zi`: CDEF copies its luma direction block and, away
  from the frame edges, each padded row as `u16x8` vectors.
- `filter/sgr.zi`: both box-filter passes and the projection run four
  columns at a time as `s32x4` vectors, with one gather per reciprocal
  lookup.

And in scalar code:

- `predict/palette.zi`: each index ranks its three neighbours once and maps
  the token straight to its color, and a map checks its five CDFs once
  instead of on every token.
- `entropy/coefficient_block.zi`: signs and Golomb extensions are read for
  the nonzero levels only, and `DequantizeSparseInto` reads just those
  coefficients.
- `predict/intra_predict.zi`: edge samples are written in place and checked
  against the bit depth once.
- `entropy/range.zi`: renormalization shifts a 64-bit window and loads input
  a byte at a time, and coefficient skip, end-of-block and DC sign symbols
  read their CDF rows directly.
- `decoder.zi`: CDEF and loop restoration each write into a copy of their
  input that then takes the frame's place, instead of a zeroed frame copied
  back, and output rows are stored with proven indices.
- `predict/intra_predict.zi` and `filter/cdef.zi`: recursive intra
  prediction and the CDEF direction table check their block once on entry,
  so their reads need no bounds checks.

It now takes 6 to 134 ms per frame, the medians of the fastest of three
benchmark runs on a busy machine: 0.9 to 3.3 times libaom and 1.7 to 6.1 times
dav1d. The screen-content clip decodes faster than libaom. Entropy decoding is
the largest remaining cost. The generated C still bounds-checks indices it
cannot prove, such as row times stride, and calls the symbol decoder out of
line. CDEF, the inverse transforms, deblocking and self-guided restoration are
the only hand-written SIMD paths; no thread, other machine, cold start or
energy figure has been measured.

## Specification and provenance

The independent scalar implementation follows the [AV1 bitstream and decoding
process specification](https://aomediacodec.github.io/av1-spec/), pinned for these
tests at revision `5e04f3f75e73a5898d7616c47c52f032144b8f80`:
`04.conventions.md`, `06.bitstream.syntax.md`, `07.bitstream.semantics.md` and
`08.decoding.process.md`, `09.parsing.process.md` in [AOMediaCodec/av1-spec](https://github.com/AOMediaCodec/av1-spec/tree/5e04f3f75e73a5898d7616c47c52f032144b8f80).

The differential oracle is [libaom](https://aomedia.googlesource.com/aom/+/b141d9c45db236335351c8a4b2a14b3ade27c937/),
revision `b141d9c45db236335351c8a4b2a14b3ade27c937`, using its unmodified
`od_ec_decode_cdf_q15`, `od_ec_dec_tell` and `update_cdf`. It is a pinned
correctness reference, not a current performance comparison. The upstream
source retains its BSD 2-Clause and AOM Patent licenses in its source package;
The named default probability constants, normative Gaussian sequence and quantizer lookup tables are
ported into product codec code, with retained notices and full licenses in
`src/LICENSE.libaom` and `src/PATENTS.libaom`.

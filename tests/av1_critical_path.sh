#!/bin/sh
# Run directly, not under build-queue: the setup, every stage and each
# conformance shard take their own build-queue ticket, so other projects
# build between them. This suite reports the current authored scope
# explicitly; no full-AV1 claim is implied and the AVIF facade default is unchanged.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
# Every stage runs on one private copy of the Ziran binaries, so peers may
# rebuild the shared toolchain while this suite runs.
snapshot="$root/build/toolchain"
queue=
if command -v build-queue >/dev/null 2>&1; then queue='build-queue --'; fi
step=${1:-}
case "$step" in
'')
    cd "$root"
    $queue sh tests/av1_critical_path.sh setup
    for stage in obu_range headers entropy_tiles coefficient_cdf_data scalar_kernels transform_type inverse_transform intra_predict filter_intra directional palette chroma color_palette color_palette_boundaries key_frames key_controls coefficients still_partitions boundaries filter_kernels grain_kernels grain_frames intra_frames engine_parity obu_framing conformance; do
        if test "$stage" = conformance; then
            $queue env ZIRAN_BIN_DIR="$snapshot" ZIRAN_BIN="$snapshot/ziran" AV1_CONFORMANCE_SHARDS=prepare sh tests/av1_conformance.sh
            shards=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["shards"])' build/scratch/av1-conformance/oracle.json)
            shard=0
            while test "$shard" -lt "$shards"; do
                $queue env ZIRAN_BIN_DIR="$snapshot" ZIRAN_BIN="$snapshot/ziran" AV1_CONFORMANCE_SHARDS="$shard" sh tests/av1_conformance.sh
                shard=$((shard + 1))
            done
        else
            $queue env ZIRAN_BIN_DIR="$snapshot" ZIRAN_BIN="$snapshot/ziran" sh "tests/av1_$stage.sh"
        fi
        printf '%s\n' "$stage" >> build/scratch/av1/stages-passed.txt
    done
    $queue sh tests/av1_critical_path.sh finish
    exit 0;;
setup|finish) ;;
*) printf 'usage: sh tests/av1_critical_path.sh\n' >&2; exit 1;;
esac
. "$root/tests/common.sh"
if test "$step" = setup; then
python3 - "$root" "$toolchain" "$bin" "$ziran" "$snapshot" <<'PY'
import hashlib,json,pathlib,shutil,subprocess,sys
root,toolchain,bin,ziran,snapshot=map(pathlib.Path,sys.argv[1:6]);out=root/'build/scratch/av1'
out.mkdir(parents=True,exist_ok=True)
def digest(path):
    hasher=hashlib.sha256()
    with path.open('rb') as binary:
        for chunk in iter(lambda:binary.read(1024*1024),b''):hasher.update(chunk)
    return hasher.hexdigest()
def tree(path):
    return {str(file.relative_to(path)):digest(file) for file in sorted(path.rglob('*')) if file.is_file()}
shutil.rmtree(snapshot,ignore_errors=True);snapshot.mkdir(parents=True)
binaries={}
for name in ('ziran','zi2zir','zi2c','zi2go','zi2zx'):
    source=ziran if name=='ziran' else bin/name
    before=digest(source);shutil.copy2(source,snapshot/name)
    if digest(source)!=before or digest(snapshot/name)!=before:raise SystemExit(f'Ziran binary changed while copying: {source}')
    binaries[name]=before
git=lambda *args:subprocess.run(['git','-C',str(toolchain),*args],check=True,text=True,capture_output=True).stdout
(out/'toolchain-start.json').write_text(json.dumps(dict(
    revision=git('rev-parse','HEAD').strip(),uncommitted=git('status','--porcelain').splitlines(),
    binary_sha256=binaries,std_sha256=tree(toolchain/'std')),indent=2)+'\n')
files=sorted((root/'src').rglob('*'))+sorted((root/'tests').glob('av1_*'))+[root/'tests/common.sh',root/'docs/AV1.md']
(out/'sources-start.json').write_text(json.dumps({str(path.relative_to(root)):digest(path) for path in files if path.is_file()},indent=2)+'\n')
(out/'stages-passed.txt').write_text('')
PY
exit 0
fi
python3 - "$root" "$toolchain" "$snapshot" <<'PY'
import datetime,hashlib,json,os,pathlib,subprocess,sys
root,toolchain,snapshot=map(pathlib.Path,sys.argv[1:4])
def digest(path):
    hasher=hashlib.sha256()
    with path.open('rb') as binary:
        for chunk in iter(lambda:binary.read(1024*1024),b''):hasher.update(chunk)
    return hasher.hexdigest()
files=sorted((root/'src').rglob('*'))+sorted((root/'tests').glob('av1_*'))+[root/'tests/common.sh',root/'docs/AV1.md']
before=json.loads((root/'build/scratch/av1/toolchain-start.json').read_text())
binaries={name:digest(snapshot/name) for name in ('ziran','zi2zir','zi2c','zi2go','zi2zx')}
if before['binary_sha256']!=binaries:raise SystemExit('Toolchain snapshot changed during AV1 suite; no integrated pass receipt written')
std={str(file.relative_to(toolchain/'std')):digest(file) for file in sorted((toolchain/'std').rglob('*')) if file.is_file()}
sources={str(path.relative_to(root)):digest(path) for path in files if path.is_file()}
if json.loads((root/'build/scratch/av1/sources-start.json').read_text())!=sources:
    raise SystemExit('Owned AV1 source/test inputs changed during AV1 suite; no integrated pass receipt written')
stages='obu_range headers entropy_tiles coefficient_cdf_data scalar_kernels transform_type inverse_transform intra_predict filter_intra directional palette chroma color_palette color_palette_boundaries key_frames key_controls coefficients still_partitions boundaries filter_kernels grain_kernels grain_frames intra_frames engine_parity obu_framing conformance'.split()
if (root/'build/scratch/av1/stages-passed.txt').read_text().splitlines()!=stages:
    raise SystemExit('Owned AV1 stage completion list is incomplete; no integrated pass receipt written')
receipt=dict(command='sh tests/av1_critical_path.sh',exit_code=0,
    recorded_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    dependency_source_overrides={key:os.environ[key] for key in ('IMAGE_CODEC_ROOT','AV1_AOM_SOURCE','AV1_DAV1D_SOURCE') if key in os.environ},
    implementation_status='incomplete: shown KEY intra frames decode bit-exact against pinned libaom on the local corpus and the pinned libaom test-vector KEY units; inter frames and the tools below reject',
    supported_authored_decode='shown KEY frames within DecodeLimits, mono/420/422/444 at 8/10/12-bit, reduced or full headers, multiple tiles, 64/128 superblocks, all 10 partition types, all 13 intra modes and 7 angle deltas, CfL, filter intra, Y/UV palettes, segmentation, delta q/lf, all lossy transform sizes/types and lossless WHT, deblocking, CDEF, Wiener and self-guided loop restoration; whole-frame film grain; OBU_FRAME or frame header plus tile groups, redundant headers, padding/reserved OBUs and operating-point 0 layer selection',
    full_av1_conformance=False,facade_default_changed=False,committed=False,pushed=False,released=False,installed=False,
    unimplemented=['inter, intra-only and hidden frames and retained reference state','intra block copy','superres','non-flat quantizer matrices',
        'metadata OBUs (UnsupportedCapability)','AVIF facade default switch','performance measurement'],
    source_sha256=sources,source_inputs_unchanged=True,stages_passed=stages,
    stage_receipts={name:json.loads((root/'build/scratch'/name/'receipt.json').read_text())
        for name in ('av1','av1-headers','av1-tiles','av1-lossless','av1-scalar','av1-transform-type','av1-inverse-transform','av1-intra-predict','av1-filter-intra','av1-directional','av1-palette','av1-chroma','av1-color-palette','av1-color-palette-boundaries','av1-key-frames','av1-key-controls','av1-still','av1-boundaries','av1-filter-kernels','av1-grain-kernels','av1-grain-frames','av1-intra-frames','av1-engine','av1-obu-framing','av1-conformance')},
    toolchain_revision_start=before['revision'],toolchain_uncommitted_start=before['uncommitted'],
    toolchain_revision=subprocess.run(['git','-C',str(toolchain),'rev-parse','HEAD'],check=True,text=True,capture_output=True).stdout.strip(),
    toolchain_binary_snapshot=str(snapshot.relative_to(root)),toolchain_binary_sha256=binaries,toolchain_binaries_unchanged=True,
    std_unchanged=before['std_sha256']==std,std_changed=sorted(name for name in set(std)|set(before['std_sha256']) if std.get(name)!=before['std_sha256'].get(name)))
(root/'build/scratch/av1/critical-path-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
PY
printf '%s\n' 'Owned AV1 local stage suite passed (26 stages); inter frames, intra block copy and performance remain unfinished'

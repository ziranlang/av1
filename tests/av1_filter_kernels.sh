#!/bin/sh
set -eu
. "$(dirname -- "$0")/common.sh"
out="$root/build/scratch/av1-filter-kernels"
mkdir -p "$out"
python3 - "$root" "$bin" "$out" before <<'PY'
import hashlib,json,pathlib,sys
root,bin,out=map(pathlib.Path,sys.argv[1:4])
def digest(path):
    value=hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda:source.read(1024*1024),b''):value.update(block)
    return value.hexdigest()
files=[root/'src'/name for name in ('filter/deblock.zi','filter/cdef.zi','filter/wiener.zi','filter/sgr.zi',)]
files += [root/'tests/common.sh',root/'tests/av1_lossless_oracle.py',root/'tests/av1_filter_kernels.sh']
for name in ('deblock','cdef','wiener','sgr'):files.extend(sorted((root/'tests').glob(f'av1_{name}.*')));files.append(root/'tests'/f'av1_{name}_oracle.py')
files += [bin/name for name in ('ziran','zi2zir','zi2c','zi2go','zi2zx')]
(out/'inputs-before.json').write_text(json.dumps({str(p.resolve()):digest(p) for p in files},indent=2)+'\n')
PY
for stage in deblock cdef wiener sgr; do
    printf '%s\n' "AV1 scalar filter stage: $stage"
    sh "tests/av1_$stage.sh"
done
python3 - "$root" "$out" <<'PY'
import hashlib,json,pathlib,sys
root,out=map(pathlib.Path,sys.argv[1:])
before=json.loads((out/'inputs-before.json').read_text())
for name,expected in before.items():
    value=hashlib.sha256()
    with open(name,'rb') as source:
        for block in iter(lambda:source.read(1024*1024),b''):value.update(block)
    if value.hexdigest()!=expected:raise SystemExit(f'Filter suite input changed during run: {name}')
stages={name:json.loads((root/'build/scratch'/f'av1-{name}'/'receipt.json').read_text()) for name in ('deblock','cdef','wiener','sgr')}
if any(stage['exit_code']!=0 for stage in stages.values()):raise SystemExit('Incomplete scalar filter stage')
(out/'receipt.json').write_text(json.dumps(dict(command='build-queue -- sh tests/av1_filter_kernels.sh',exit_code=0,inputs=before,stages=stages,
    scope='allocation-free scalar kernels only',pending=['frame edge/threshold scheduling','CDEF frame integration','restoration stripe source selection','film grain']),indent=2)+'\n')
PY
printf '%s\n' 'AV1 scalar filter suite passed; frame filtering and film grain remain pending'

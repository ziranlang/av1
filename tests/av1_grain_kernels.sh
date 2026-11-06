#!/bin/sh
set -eu
. "$(dirname -- "$0")/common.sh"
out="$root/build/scratch/av1-grain-kernels"
mkdir -p "$out"
python3 - "$root" "$bin" "$out" <<'PY'
import hashlib,json,pathlib,sys
root,bin,out=map(pathlib.Path,sys.argv[1:])
def digest(path):
    value=hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda:source.read(1024*1024),b''):value.update(block)
    return value.hexdigest()
files=[root/'src'/name for name in ('syntax/bits.zi','grain/grain_params.zi','grain/grain_scaling.zi','grain/gaussian.zi','grain/grain_noise.zi','grain/grain_apply.zi',)]
files += [root/'tests/common.sh',root/'tests/av1_lossless_oracle.py',root/'tests/av1_grain_kernels.sh']
for name in ('params','scaling','noise','apply'):files.extend(sorted((root/'tests').glob(f'av1_grain_{name}.*')));files.append(root/'tests'/f'av1_grain_{name}_oracle.py')
files += [bin/name for name in ('ziran','zi2zir','zi2c','zi2go','zi2zx','zi2zx-vm')]
(out/'inputs-before.json').write_text(json.dumps({str(p.resolve()):digest(p) for p in files},indent=2)+'\n')
PY
for stage in params scaling noise apply; do
    printf '%s\n' "AV1 grain component stage: $stage"
    sh "tests/av1_grain_$stage.sh"
done
python3 - "$root" "$out" <<'PY'
import hashlib,json,pathlib,sys
root,out=map(pathlib.Path,sys.argv[1:])
before=json.loads((out/'inputs-before.json').read_text())
for name,expected in before.items():
    value=hashlib.sha256()
    with open(name,'rb') as source:
        for block in iter(lambda:source.read(1024*1024),b''):value.update(block)
    if value.hexdigest()!=expected:raise SystemExit(f'Grain suite input changed during run: {name}')
stages={name:json.loads((root/'build/scratch'/f'av1-grain-{name}'/'receipt.json').read_text()) for name in ('params','scaling','noise','apply')}
if any(stage['exit_code']!=0 for stage in stages.values()):raise SystemExit('Incomplete grain component stage')
(out/'receipt.json').write_text(json.dumps(dict(command='build-queue -- sh tests/av1_grain_kernels.sh',exit_code=0,inputs=before,stages=stages,
    scope='non-inter parameters, PRNG/scaling, full Gaussian/AR arrays and bounded raw-picture application',outside_component_coverage=['active-grain encoded-frame parity','decoder integration']),indent=2)+'\n')
PY
printf '%s\n' 'AV1 grain component suite passed; encoded-frame decoder parity is covered separately by av1_grain_frames.sh'

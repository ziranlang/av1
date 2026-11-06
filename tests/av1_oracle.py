#!/usr/bin/env python3
"""Generate bounded symbol differentials from the package's pinned libaom.

The C host below calls upstream directly; it is test scaffolding, never a
product decoder. Arbitrary entropy bytes test the symbol primitive and are
not presented as conforming AV1 frames. Syntax expectations use Python's
unbounded integers and the pinned AV1 specification, independently of Ziran.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import shlex
import subprocess

PIN = "d772e334cc724105040382a977ebb10dfd393293"
SPEC = "5e04f3f75e73a5898d7616c47c52f032144b8f80"
HOST = r'''
#include <stdio.h>
#include <stdint.h>
#include "aom_dsp/entdec.h"
#include "aom_dsp/prob.h"
int main(void) {
  unsigned n, count, disable, size;
  while (scanf("%u%u%u%u", &n, &count, &disable, &size) == 4) {
    uint16_t cdf[17]; unsigned char data[96]; unsigned x;
    if (n < 2 || n > 16 || count > 96 || size == 0 || size > 96) return 2;
    for (unsigned i = 0; i <= n; ++i) {
      if (scanf("%u", &x) != 1 || x > 32768) return 3;
      cdf[i] = (uint16_t)(i < n ? 32768 - x : x);
    }
    for (unsigned i = 0; i < size; ++i) {
      if (scanf("%u", &x) != 1 || x > 255) return 4;
      data[i] = (unsigned char)x;
    }
    od_ec_dec dec; od_ec_dec_init(&dec, data, size);
    unsigned accepted = 0; int overread = 0; int symbols[96];
    for (unsigned i = 0; i < count; ++i) {
      int s = od_ec_decode_cdf_q15(&dec, cdf, n);
      if (od_ec_dec_tell(&dec) > (int)(size * 8)) { overread = 1; break; }
      symbols[accepted++] = s;
      if (!disable) update_cdf(cdf, s, n);
    }
    printf("%u %d", accepted, overread);
    for (unsigned i = 0; i < accepted; ++i) printf(" %d", symbols[i]);
    for (unsigned i = 0; i <= n; ++i)
      printf(" %u", i < n ? 32768 - cdf[i] : cdf[i]);
    printf("\n");
  }
  return ferror(stdin) || ferror(stdout);
}
'''
ENCODER = r'''
#include <stdio.h>
#include <stdint.h>
#include "aom_dsp/entenc.h"
#include "aom_dsp/prob.h"
int main(void) {
  unsigned n,count,disable,x;
  while(scanf("%u%u%u",&n,&count,&disable)==3) {
    uint16_t cdf[17]; od_ec_enc enc;
    if(n<2||n>16||count>96)return 2;
    for(unsigned i=0;i<=n;i++){
      if(scanf("%u",&x)!=1||x>32768)return 3;
      cdf[i]=i<n?32768-x:x;
    }
    od_ec_enc_init(&enc,4096);
    for(unsigned i=0;i<count;i++){
      if(scanf("%u",&x)!=1||x>=n)return 4;
      od_ec_encode_cdf_q15(&enc,x,cdf,n);
      if(!disable)update_cdf(cdf,x,n);
    }
    uint32_t size=0;
    unsigned char *data=od_ec_enc_done(&enc,&size);
    if(!data||!size||size>2048)return 5;
    printf("%u",size);
    for(unsigned i=0;i<size;i++)printf(" %u",data[i]);
    for(unsigned i=0;i<=n;i++)printf(" %u",i<n?32768-cdf[i]:cdf[i]);
    printf("\n");
    od_ec_enc_clear(&enc);
  }
  return ferror(stdin)||ferror(stdout);
}
'''


def run(argv, **kwargs):
    return subprocess.run(argv, check=True, text=True, **kwargs)


def array(name, kind, values):
    return f"    {name}: [{len(values)}]{kind} = .[" + ",".join(map(str, values)) + "]\n"


def leb_reference(data, begin, end):
    if begin < 0 or end < begin or end > len(data):
        return 2, begin, 0, begin
    encoded = bytearray()
    for at in range(begin, min(end, begin + 8)):
        byte = data[at]
        encoded.append(byte & 127)
        # int.from_bytes uses base 256; a little-endian base 128 expansion
        # independently computes the specification's arbitrary precision value.
        value = sum(b * 128 ** i for i, b in enumerate(encoded))
        if value > 2 ** 32 - 1:
            return 4, at, 0, at
        if byte < 128:
            return 0, -1, value, at + 1
        if len(encoded) == 8:
            return 4, at, 0, at + 1
    return 3, end, 0, end


def obu_reference(data, begin, end, external):
    if begin < 0 or end < begin or end > len(data):
        return 2, begin, None
    if begin == end:
        return 3, end, None
    header = data[begin]
    if header & 0x81:
        return 4, begin, None
    at = begin + 1
    temporal = spatial = 0
    if header & 4:
        if at == end:
            return 3, end, None
        if data[at] & 7:
            return 4, at, None
        temporal, spatial = data[at] // 32, data[at] // 8 % 4
        at += 1
    if header & 2:
        code, error_at, value, at = leb_reference(data, at, end)
        if code:
            return code, error_at, None
        if value > end - at:
            return 3, end, None
        stop = at + value
        if external and stop != end:
            return 4, stop, None
    elif external:
        stop = end
    else:
        return 4, begin, None
    return 0, -1, (header // 8 % 16, temporal, spatial, at, stop)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--aom", type=Path, required=True)
    parser.add_argument("--abi", type=Path)
    args = parser.parse_args()
    root, out, aom = args.root, args.out, args.aom
    if args.abi:
        import re
        header = args.abi / "av1_abi.h"
        text = header.read_text()
        match = re.search(r"int32_t (\w+)\(Slice cdf\);", text)
        if not match:
            raise SystemExit("cannot locate generated ProbeCDF ABI")
        source = args.abi / "host.c"
        source.write_text('#include "av1_abi.h"\n#include <limits.h>\n'
            'int main(void) {\nint64_t counts[] = {INT64_MIN,-1,0,1,2,18,INT32_MAX,2147483648LL,INT64_MAX};\n'
            'for (unsigned i=0;i<sizeof(counts)/sizeof(counts[0]);i++) {\n'
            f'if ({match[1]}((Slice){{NULL,counts[i]}}) != 2) return 1;\n}}\nreturn 0;\n}}\n')
        run(shlex.split(os.environ.get("CC", "cc")) + ['-std=c11', '-O1', '-g',
            '-fsanitize=address,undefined', '-fno-sanitize-recover=all', '-fno-omit-frame-pointer',
            '-I' + str(args.abi)] + [str(x) for x in args.abi.glob('*.c')] + ['-lm', '-o', str(args.abi / 'run')])
        run([str(args.abi / 'run')], env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=1', 'UBSAN_OPTIONS': 'halt_on_error=1'})
        print('AV1 native ABI: nine invalid slice counts rejected before data access')
        return
    revision = run(["git", "-C", str(aom), "rev-parse", "HEAD"], capture_output=True).stdout.strip()
    if revision != PIN:
        raise SystemExit(f"libaom oracle revision mismatch: {revision}")
    run(["git", "-C", str(aom), "diff", "--quiet", "HEAD", "--"])
    library = Path(os.environ.get("AV1_AOM_LIBRARY", str(root / "build/native/aom/libaom.a")))
    config = Path(os.environ.get("AV1_AOM_CONFIG", str(root / "build/native/aom")))
    if not library.is_file() or not (config / "config/aom_config.h").is_file():
        raise SystemExit("Pinned AVIF native recipe must be built first (libaom/config unavailable)")
    host = out / "oracle.c"
    host.write_text(HOST)
    executable = out / "oracle"
    run(shlex.split(os.environ.get("CC", "cc")) + ["-std=c11", "-O2", "-I" + str(aom),
        "-I" + str(config), str(host), str(library), "-lm", "-o", str(executable)])
    encoder_source = out / "encoder.c"
    encoder_source.write_text(ENCODER)
    encoder = out / "encoder"
    run(shlex.split(os.environ.get("CC", "cc")) + ["-std=c11", "-O2", "-I" + str(aom),
        "-I" + str(config), str(encoder_source), str(aom / "aom_dsp/entenc.c"), str(library), "-lm", "-o", str(encoder)])
    rng = random.Random(0x415631)
    cases = []
    for case in range(192):
        n = 2 + case % 15
        # Include empty-probability bins: AV1's minimum probability still makes
        # these decodable. Counter transitions 15/16/31/32 are exercised.
        cut = sorted(rng.randrange(32769) for _ in range(n - 1))
        if case % 8 == 0:
            cut = [0] * (n - 1)
        elif case % 8 == 1:
            cut = [32768] * (n - 1)
        cdf = cut + [32768, [0, 15, 16, 31, 32][case % 5]]
        size = [1, 2, 3, 8, 64, 96][case % 6]
        data = [rng.randrange(256) for _ in range(size)]
        if case % 11 == 0:
            data = [0] * size
        elif case % 11 == 1:
            data = [255] * size
        cases.append((n, 96, case % 2, size, cdf, data))
    protocol = "".join(" ".join(map(str, [n, count, disable, size] + cdf + data)) + "\n"
        for n, count, disable, size, cdf, data in cases)
    rows = run([str(executable)], input=protocol, capture_output=True).stdout.splitlines()
    if len(rows) != len(cases):
        raise SystemExit("incomplete libaom oracle response")
    zi = ['#import, file "../../../src/syntax/obu.zi";\n#import, file "../../../src/syntax/bits.zi";\n#import, file "../../../src/entropy/range.zi";\n']
    total = 0
    for number, (case, row) in enumerate(zip(cases, rows)):
        n, count, disable, size, cdf, data = case
        values = list(map(int, row.split()))
        accepted, overflow = values[:2]
        symbols = values[2:2 + accepted]
        final_cdf = values[2 + accepted:]
        assert len(final_cdf) == n + 1
        total += accepted
        zi += [f"Case{number} :: () -> s32 {{\n", array("data", "u8", data),
            array("cdf", "u16", cdf), array("expected", "s32", symbols),
            array("final_cdf", "u16", final_cdf),
            f"    decoder := NewRange(BorrowBytes(data[:]), 0, {size}, {'true' if disable else 'false'})\n",
            f"    for i: 0..{accepted - 1} {{\n" if accepted else "    for i: 0..-1 {\n",
            "        if ReadSymbol(*decoder, cdf[:]) != expected[i] || decoder.error.code != DecodeOK { return 1 }\n    }\n",
            f"    for i: 0..{n} {{ if cdf[i] != final_cdf[i] {{ return 2 }} }}\n"]
        if overflow:
            zi += ["    discarded := ReadSymbol(*decoder, cdf[:])\n",
                f"    if decoder.error.code != TruncatedInput || decoder.error.offset != {size} {{ return 3 }}\n"]
        zi += ["    return 0\n}\n"]
    # Independent bit-reader oracle across every width, byte offsets and EOF.
    zi += ["Syntax :: () -> s32 {\n"]
    for index in range(132):
        data = bytes(rng.randrange(256) for _ in range(12))
        offset, width = index % 8, index % 33
        expected = (int.from_bytes(data, "big") >> (96 - offset - width)) & ((1 << width) - 1)
        zi += [array(f"data{index}", "u8", data),
            f"    bits{index} := NewBits(BorrowBytes(data{index}[:]), 0, 12)\n",
            f"    skip{index} := ReadBits(*bits{index}, {offset})\n",
            f"    if ReadBits(*bits{index}, {width}) != {expected} || bits{index}.error.code != DecodeOK {{ return 4 }}\n"]
    zi += ["    return 0\n}\n"]
    malformed = []
    for number in range(512):
        data = [rng.randrange(256) for _ in range(rng.randrange(1, 18))]
        begin = rng.randrange(-1, len(data) + 2)
        end = rng.randrange(-1, len(data) + 2)
        external = bool(number % 2)
        # Every fourth input is framed to reach the payload/length path.
        if number % 4 == 0:
            begin, end = 0, len(data)
            data[0] &= 0x7a
            if data[0] & 4 and len(data) > 1:
                data[1] &= 0xf8
        if number % 8 == 0:
            data = [0x80] * len(data)
        code, at, value, next_at = leb_reference(data, begin, end)
        obu_code, obu_at, obu = obu_reference(data, begin, end, external)
        body = [f"Malformed{number} :: () -> s32 {{\n", array("data", "u8", data),
            f"    leb := ReadLEB128(BorrowBytes(data[:]), {begin}, {end})\n",
            f"    if leb.error.code != {code} {{ return 1 }}\n"]
        if code:
            body += [f"    if leb.error.offset != {at} {{ return 2 }}\n"]
        else:
            body += [f"    if leb.value != {value} || leb.next != {next_at} {{ return 3 }}\n"]
        body += [f"    obu := ReadOBU(BorrowBytes(data[:]), {begin}, {end}, {'true' if external else 'false'})\n",
            f"    if obu.error.code != {obu_code} {{ return 4 }}\n"]
        if obu_code:
            body += [f"    if obu.error.offset != {obu_at} {{ return 5 }}\n"]
        else:
            kind, temporal, spatial, start, stop = obu
            body += [f"    if obu.obu.kind != {kind} || obu.obu.temporal_id != {temporal} || obu.obu.spatial_id != {spatial} || obu.obu.payload != {start} || obu.obu.end != {stop} {{ return 6 }}\n"]
        body += ["    return 0\n}\n"]
        malformed += body
    zi += malformed
    encoded_cases = []
    for i in range(90):
        n = 2 + i % 15
        cdf = [(j + 1) * 32768 // n for j in range(n)] + [i % 33]
        count = [0, 1, 2, 15, 32, 96][i % 6]
        symbols = [rng.randrange(n) for _ in range(count)]
        encoded_cases.append((n, count, i % 2, cdf, symbols))
    protocol = ''.join(' '.join(map(str, [n, count, disable] + cdf + symbols)) + '\n'
        for n, count, disable, cdf, symbols in encoded_cases)
    encoded_rows = run([str(encoder)], input=protocol, capture_output=True).stdout.splitlines()
    if len(encoded_rows) != len(encoded_cases): raise SystemExit('incomplete libaom encoder response')
    encoded_symbols = 0
    for i, (case, row) in enumerate(zip(encoded_cases, encoded_rows)):
        n, count, disable, cdf, symbols = case
        values = list(map(int, row.split())); size = values[0]
        data, final = values[1:size+1], values[size+1:]
        assert len(final) == n + 1
        encoded_symbols += count
        zi += [f'Encoded{i} :: () -> s32 {{\n', array('data','u8',data + [0]),
            array('cdf','u16',cdf), array('final_cdf','u16',final),
            f'    decoder := NewRange(BorrowBytes(data[:]), 0, {size}, {"true" if disable else "false"})\n']
        if count:
            zi += [array('symbols','s32',symbols),
                f'    for j: 0..{count-1} {{ if ReadSymbol(*decoder,cdf[:]) != symbols[j] || decoder.error.code != DecodeOK {{ return 1 }} }}\n']
        zi += [f'    for j: 0..{n} {{ if cdf[j] != final_cdf[j] {{ return 2 }} }}\n',
            '    if FinishRange(*decoder).code != DecodeOK { return 3 }\n',
            '    if FinishRange(*decoder).code != InvalidInput { return 4 }\n',
            '    for mutation: 0..1 {\n',
            f'        data[{size}] = cast(u8)mutation\n',
            array('fresh','u16',cdf),
            f'        decoder = NewRange(BorrowBytes(data[:]), 0, {size + 1}, {"true" if disable else "false"})\n']
        if count:
            zi += [f'        for j: 0..{count-1} {{ if ReadSymbol(*decoder,fresh[:]) != symbols[j] || decoder.error.code != DecodeOK {{ return 5 }} }}\n']
        zi += ['        error := FinishRange(*decoder)\n',
            f'        if mutation == 0 && error.code != DecodeOK {{ return 6 }}\n',
            f'        if mutation == 1 && (error.code != MalformedInput || error.offset != {size}) {{ return 7 }}\n',
            '    }\n    return 0\n}\n']
    zi += ["#program_export\nmain :: () -> s32 {\n"]
    for i in range(len(cases)):
        zi += [f"    if Case{i}() != 0 {{ return {i + 1} }}\n"]
    for i in range(512):
        zi += [f"    if Malformed{i}() != 0 {{ return 193 }}\n"]
    for i in range(len(encoded_cases)):
        zi += [f'    if Encoded{i}() != 0 {{ return 194 }}\n']
    zi += ["    return Syntax()\n}\n"]
    (out / "av1_oracle_vectors.zi").write_text("".join(zi))
    receipt = {"spec_revision": SPEC, "aom_revision": PIN, "seed": "0x415631",
        "symbol_cases": len(cases), "symbols": total, "syntax_cases": 132, "malformed_cases": 512,
        "encoded_streams": len(encoded_cases), "encoded_symbols": encoded_symbols,
        "scope": "symbol primitive differential; not full AV1 frame conformance"}
    digest = hashlib.sha256()
    with library.open('rb') as binary:
        for chunk in iter(lambda: binary.read(1024 * 1024), b''): digest.update(chunk)
    receipt['oracle_library_sha256'] = digest.hexdigest()
    (out / "oracle.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(f"libaom oracle: {len(cases)} cases / {total} symbols; 132 syntax + 512 framing; {len(encoded_cases)} encoded streams / {encoded_symbols} symbols + termination")


if __name__ == "__main__":
    main()

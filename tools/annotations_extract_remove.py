#!/usr/bin/env python3
"""
annotations_extract_remove.py

  - Saves all annotations >>> _annotations.txt
  - Writes a NEW clean EDF >>> _no_annotations.edf
  - Original file is NEVER modified

Usage:
    python3 annotations_extract_remove.py <input.edf> <output_dir>

    python3 /Users/knix/Library/CloudStorage/Box-Box/NeuroGate_Protocol/annotations_extract_remove.py \
    /Users/knix/Library/CloudStorage/Box-Box/data/HUP282.edf \
    /Users/knix/Library/CloudStorage/Box-Box/NeuroGate_Protocol

Example:
    python3 annotations_extract_remove.py \
        /Users/knix/Library/CloudStorage/Box-Box/data/HUP282.edf \
        /Users/knix/Library/CloudStorage/Box-Box/NeuroGate_Protocol
"""

import os
import argparse


def parse_args():
    parser = argparse.ArgumentParser(
        description='Extract and remove annotations from an EDF/EDF+ file.'
    )
    parser.add_argument('input_edf',  help='Path to the input .edf file')
    parser.add_argument('output_dir', help='Directory to save outputs')
    return parser.parse_args()


# ── EDF header parsers ────────────────────────────────────────────────────────

def read_main_header(f):
    def s(pos, n):
        f.seek(pos)
        return f.read(n).decode('ascii', errors='replace').strip()
    h = {}
    h['version']        = s(0,   8)
    h['patient']        = s(8,   80)
    h['recording']      = s(88,  80)
    h['startdate']      = s(168, 8)
    h['starttime']      = s(176, 8)
    h['n_header_bytes'] = int(s(184, 8))
    h['reserved']       = s(192, 44)
    h['n_records']      = int(s(236, 8))
    h['record_dur']     = float(s(244, 8) or 0)
    h['n_signals']      = int(s(252, 4))
    return h


def read_signal_headers(f, ns):
    def block(n): return [f.read(n).decode('ascii', errors='replace').strip() for _ in range(ns)]
    labels    = block(16)
    block(80)   # transducer type
    block(8)    # physical dimension
    block(8)    # physical minimum
    block(8)    # physical maximum
    block(8)    # digital minimum
    block(8)    # digital maximum
    block(80)   # prefiltering
    n_samples = [int(x) for x in block(8)]
    block(32)   # reserved
    return labels, n_samples


# ── TAL parser ────────────────────────────────────────────────────────────────

def parse_tal(raw: bytes) -> list[tuple]:
    annotations = []
    for tal in raw.split(b'\x00'):
        if not tal:
            continue
        parts = tal.split(b'\x14')
        if not parts or not parts[0]:
            continue

        time_part = parts[0]
        if b'\x15' in time_part:
            t_bytes, d_bytes = time_part.split(b'\x15', 1)
        else:
            t_bytes, d_bytes = time_part, b'0'

        try:
            onset    = float(t_bytes.decode('ascii', errors='replace').lstrip('+-').strip())
            duration = float(d_bytes.decode('ascii', errors='replace').strip() or '0')
        except ValueError:
            continue

        for ann_bytes in parts[1:]:
            desc = ann_bytes.decode('utf-8', errors='replace').strip()
            if desc:
                annotations.append((onset, duration, desc))

    return annotations


# ── Validate: confirm annotations are gone from clean EDF ─────────────────────

def validate_annotations_removed(clean_edf_path: str, n_hdr: int, n_records: int,
                                   rec_bytes: int, annot_offset: int, annot_len: int):
    print(f"\n→ Validating: {clean_edf_path}")

    all_tal_bytes = b''
    with open(clean_edf_path, 'rb') as f:
        for rec in range(n_records):
            f.seek(n_hdr + rec * rec_bytes + annot_offset)
            all_tal_bytes += f.read(annot_len)

    # Check 1: all bytes are zero
    non_zero = sum(1 for b in all_tal_bytes if b != 0)
    print(f"  Non-zero bytes in annotation channel : {non_zero}")

    # Check 2: no parseable TALs
    found = parse_tal(all_tal_bytes)
    print(f"  Annotations parsed from clean EDF    : {len(found)}")

    if non_zero == 0 and len(found) == 0:
        print("Validation passed — annotation channel is fully cleared\n")
    else:
        print("Validation FAILED — some annotation data may remain\n")
        for a in found:
            print(f"   {a}")


# ── Main ──────────────────────────────────────────────────────────────────────

def process(input_path: str, base: str):
    txt_path    = base + '_annotations.txt'
    output_path = base + '_no_annotations.edf'

    print(f"\n→ Parsing header: {input_path}")

    with open(input_path, 'rb') as f:
        h              = read_main_header(f)
        f.seek(256)    # always start of signal headers
        labels, n_samp = read_signal_headers(f, h['n_signals'])

    n_hdr     = h['n_header_bytes']
    n_records = h['n_records']
    rec_bytes = sum(n * 2 for n in n_samp)

    print(f"  Signals  : {h['n_signals']}")
    print(f"  Records  : {n_records}")
    print(f"  Rec size : {rec_bytes:,} bytes")

    # Find EDF Annotations channel
    annot_idx = next(
        (i for i, lbl in enumerate(labels)
         if 'EDF Annotations' in lbl or 'BDF Annotations' in lbl),
        None
    )
    if annot_idx is None:
        print("\n⚠ No 'EDF Annotations' channel found — plain EDF, nothing to do.")
        return

    annot_offset = sum(n_samp[i] * 2 for i in range(annot_idx))
    annot_len    = n_samp[annot_idx] * 2
    blank        = b'\x00' * annot_len

    print(f"  Annot ch : index {annot_idx}, {annot_len} bytes/record")

    # ── Step 2 : extract annotations ─────────────────────────────────────────
    print("\n→ Extracting annotations (reading annot channel only)...")

    # Concatenate all annotation bytes first, then parse once
    # — handles annotations that span record boundaries
    all_tal_bytes = b''
    with open(input_path, 'rb') as f:
        for rec in range(n_records):
            f.seek(n_hdr + rec * rec_bytes + annot_offset)
            all_tal_bytes += f.read(annot_len)

    all_annotations = parse_tal(all_tal_bytes)

    with open(txt_path, 'w', encoding='utf-8') as f:
        f.write('onset_sec\tduration_sec\tdescription\n')
        for onset, dur, desc in all_annotations:
            f.write(f'{onset:.6f}\t{dur:.6f}\t{desc}\n')

    print(f"✓ {len(all_annotations)} annotations saved → {txt_path}")

    # ── Step 3 : write NEW clean EDF (original untouched) ────────────────────
    print(f"\n→ Writing clean EDF → {output_path}")
    print("  (streaming record-by-record, only annotation bytes replaced)")

    with open(input_path, 'rb') as src, open(output_path, 'wb') as dst:

        # Copy header verbatim
        dst.write(src.read(n_hdr))

        # Stream records, zeroing only the annotation channel bytes
        for rec in range(n_records):
            record = bytearray(src.read(rec_bytes))
            record[annot_offset : annot_offset + annot_len] = blank
            dst.write(record)

            if rec % 1000 == 0:
                pct = rec / n_records * 100
                print(f"  {rec:>6}/{n_records}  ({pct:.1f}%)", end='\r')

    # ── Validate ──────────────────────────────────────────────────────────────
    validate_annotations_removed(output_path, n_hdr, n_records,
                                  rec_bytes, annot_offset, annot_len)

    print(f"\n✓ Clean EDF saved → {output_path}")
    print("✓ Original file unchanged\n")
    print(f"  Version  : '{h['version']}'")
    print(f"  Patient  : '{h['patient']}'")
    print(f"  Date     : {h['startdate']}  Time: {h['starttime']}")
    print(f"  Hdr bytes: {h['n_header_bytes']}")
    print(f"  Records  : {h['n_records']}")
    print(f"  Signals  : {h['n_signals']}")


if __name__ == '__main__':
    args = parse_args()
    
    # patient name derived from EDF filename (e.g. HUP282)
    patient_id  = os.path.splitext(os.path.basename(args.input_edf))[0]
    patient_dir = os.path.join(args.output_dir, patient_id)
    
    os.makedirs(patient_dir, exist_ok=True)
    
    base = os.path.join(patient_dir, patient_id)
    process(args.input_edf, base)

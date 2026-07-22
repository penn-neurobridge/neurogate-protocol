#!/usr/bin/env python3
"""
annotations_extract_remove.py

  - Saves all annotations >>> _annotations.txt
  - Writes a NEW clean EDF >>> _no_annotations.edf
  - Original file is NEVER modified

Usage:
    python3 annotations_extract_remove.py <input.edf> <output_dir>
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

def extract_record_onset(annot_bytes: bytes) -> float:
    """
    Reads just the time-keeping onset from one record's raw annotation
    channel bytes (the value before the first \\x14), ignoring any
    duration or description. Returns 0.0 if it can't be parsed.
    """
    idx = annot_bytes.find(b'\x14')
    if idx == -1:
        return 0.0
    onset_field = annot_bytes[:idx].decode('ascii', errors='replace').strip()
    onset_field = onset_field.split('\x15')[0]  # drop duration if present
    try:
        return float(onset_field.lstrip('+'))
    except ValueError:
        return 0.0
 
 
def build_timekeeping_tal(onset: float, length: int) -> bytes:
    """
    Builds a spec-compliant EDF+ 'time-keeping TAL' — the mandatory
    per-record marker (onset, no duration, no description) required by
    the EDF+ format even when a record has no real annotations. This is
    what makes a record with annotations *removed* still spec-valid,
    as opposed to a fully zeroed (and therefore non-compliant) channel.
    """
    sign = '+' if onset >= 0 else '-'
    tal = f'{sign}{abs(onset):.6f}'.encode('ascii') + b'\x14\x14\x00'
    return tal.ljust(length, b'\x00')[:length]

# ── Anonymize EDF header fields  ─────────────────────

def anonymize_header_bytes(raw_header: bytes) -> bytes:
    """
    Anonymizes patient/recording identification per EDF+ spec section 2.1.3,
    items 3-4: unknown/anonymized subfields become 'X', separated by spaces.
    Startdate field kept but zeroed to 01.01.01; starttime preserved.
    """
    header = bytearray(raw_header)
 
    def write_field(pos, n, value):
        field = value.encode('ascii', errors='replace')[:n].ljust(n, b' ')
        header[pos:pos+n] = field
 
    write_field(8,   80, 'X X X X')                       # patient: code sex birthdate name
    write_field(88,  80, 'Startdate 01-JAN-2001 X X X')    # recording: date must match startdate field below
    write_field(168, 8,  '01.01.01')                      # startdate -> 2001-01-01 (dd.mm.yy)
    # starttime (176, 8) intentionally untouched
 
    return bytes(header)

# ── Validate: confirm annotations are gone from clean EDF ─────────────────────

def validate_annotations_removed(clean_edf_path: str, n_hdr: int, n_records: int,
                                   rec_bytes: int, annot_offset: int, annot_len: int):
    """
    Validates two ways, deliberately independent of each other:
      1. parse_tal() finds zero real (non-empty) annotations -- catches
         "is there decodable annotation text left anywhere".
      2. A raw byte-pattern check per record -- catches "does this record's
         annotation channel match the EXACT expected shape" (onset, then
         \\x14\\x14\\x00, then nothing but zero padding).
    """
    import re
 
    print(f"\n→ Validating annotations: {clean_edf_path}")
 
    all_tal_bytes = b''
    per_record_bytes = []
    with open(clean_edf_path, 'rb') as f:
        for rec in range(n_records):
            f.seek(n_hdr + rec * rec_bytes + annot_offset)
            chunk = f.read(annot_len)
            per_record_bytes.append(chunk)
            all_tal_bytes += chunk
 
    # Check 1: parser-based -- no real (non-empty) annotations
    found = parse_tal(all_tal_bytes)
    print(f"  Annotations parsed from clean EDF     : {len(found)}")
 
    # Check 2: raw structural check, independent of parse_tal.
    # Expected shape per record: onset, then \x14\x14\x00, then all \x00.
    pattern = re.compile(rb'^[+-]\d+(\.\d+)?\x14\x14\x00\x00*$')
    malformed = [i for i, chunk in enumerate(per_record_bytes) if not pattern.match(chunk)]
    print(f"  Records matching exact expected shape : {n_records - len(malformed)}/{n_records}")
 
    if len(found) == 0 and not malformed:
        print("Validation passed — all event annotations removed, time-keeping TALs preserved\n")
        return True
    else:
        print("Validation FAILED — some annotation data may remain\n")
        for a in found:
            print(f"   parsed leftover: {a}")
        for i in malformed:
            print(f"   record {i} does not match expected shape: {per_record_bytes[i][:30]!r}")
        return False

# ── Validate: confirm EDFs have been anonymized ─────────────────────

def validate_header_anonymized(clean_edf_path: str):
    """
    Confirms patient/recording fields were properly anonymized and
    startdate was zeroed, without assuming the write succeeded.
    """
    print(f"\n→ Validating header: {clean_edf_path}")
 
    with open(clean_edf_path, 'rb') as f:
        def s(pos, n):
            f.seek(pos)
            return f.read(n).decode('ascii', errors='replace')
 
        patient   = s(8,   80)
        recording = s(88,  80)
        startdate = s(168, 8)
        starttime = s(176, 8)
 
    ok = True
 
    if patient.strip() != 'X X X X':
        print(f"  FAILED — patient field not fully anonymized: {patient.strip()!r}")
        ok = False
    else:
        print("  Patient field anonymized")
 
    if recording.strip() != 'Startdate 01-JAN-2001 X X X':
        print(f"  FAILED — recording field not fully anonymized: {recording.strip()!r}")
        ok = False
    else:
        print("  Recording field anonymized (date matches startdate field)")
 
    if startdate.strip() != '01.01.01':
        print(f"  FAILED — startdate not zeroed: {startdate.strip()!r}")
        ok = False
    else:
        print("  Startdate zeroed to 01.01.01")
 
    if len(starttime.strip()) != 8 or not all(c.isdigit() or c == '.' for c in starttime.strip()):
        print(f"  WARNING — starttime looks unexpected: {starttime.strip()!r}")
    else:
        print(f"  Starttime preserved: {starttime.strip()}")
 
    if ok:
        print("Header validation passed\n")
    else:
        print("Header validation FAILED — PHI may remain\n")
 
    return ok

# ── Main ──────────────────────────────────────────────────────────────────────

def process(input_path: str, base: str):
    txt_path    = base + '_annotations.txt'
    output_path = base + '_no_annotations.edf'

    print(f"\n→ Parsing header: {input_path}")

    with open(input_path, 'rb') as f:
        h              = read_main_header(f)
        f.seek(256)    # always start of signal headers
        labels, n_samp = read_signal_headers(f, h['n_signals'])
        f.seek(0)
        raw_header     = f.read(h['n_header_bytes'])

    n_hdr     = h['n_header_bytes']
    n_records = h['n_records']
    rec_bytes = sum(n * 2 for n in n_samp)

    print(f"  Signals  : {h['n_signals']}")
    print(f"  Records  : {n_records}")
    print(f"  Rec size : {rec_bytes:,} bytes")

    # Find any EDF Annotations channel
    annot_indices = [
        i for i, lbl in enumerate(labels)
        if 'EDF Annotations' in lbl or 'BDF Annotations' in lbl
    ]
    if not annot_indices:
        print("\n No 'EDF Annotations' channel found — plain EDF, nothing to do.")
        return

   # Compute (offset, length) within a record for each annotation channel
    annot_channels = []
    for idx in annot_indices:
        offset = sum(n_samp[i] * 2 for i in range(idx))
        length = n_samp[idx] * 2
        annot_channels.append((idx, offset, length))

    print(f"  Annot ch : {len(annot_channels)} found — "
          f"{', '.join(f'index {i} ({l} bytes/record)' for i, _, l in annot_channels)}")

    # ── Step 2 : extract annotations ─────────────────────────────────────────
    print("\n→ Extracting annotations (reading annot channel(s) only)...")
 
    all_rows = []  # (channel_label, onset, duration, description)
    with open(input_path, 'rb') as f:
        for idx, offset, length in annot_channels:
            channel_bytes = b''
            for rec in range(n_records):
                f.seek(n_hdr + rec * rec_bytes + offset)
                channel_bytes += f.read(length)
 
            for onset, dur, desc in parse_tal(channel_bytes):
                all_rows.append((f'{labels[idx]} [ch {idx}]', onset, dur, desc))
 
    with open(txt_path, 'w', encoding='utf-8') as f:
        f.write('channel\tonset_sec\tduration_sec\tdescription\n')
        for channel, onset, dur, desc in all_rows:
            f.write(f'{channel}\t{onset:.6f}\t{dur:.6f}\t{desc}\n')
 
    print(f"✓ {len(all_rows)} annotations saved → {txt_path}")

    # ── Step 3 : anonymize header ─────────────────────────────────────────────
    print("\n→ Anonymizing header (patient/recording/startdate)...")
    anon_header = anonymize_header_bytes(raw_header)
    print("✓ Header anonymized")

    # ── Step 4 : write NEW clean EDF (original untouched) ────────────────────
    print(f"\n→ Writing clean EDF → {output_path}")
    print("  (streaming record-by-record, only annotation bytes replaced)")
 
    with open(input_path, 'rb') as src, open(output_path, 'wb') as dst:
 
        # Write anonymized header
        dst.write(anon_header)
 
        # Stream records: keep only the mandatory time-keeping TAL per
        # record, per annotation channel (spec-compliant), strip
        # everything else (event text)
        src.read(n_hdr)
        for rec in range(n_records):
            record = bytearray(src.read(rec_bytes))
            for idx, offset, length in annot_channels:
                orig_annot = bytes(record[offset : offset + length])
                onset = extract_record_onset(orig_annot)
                record[offset : offset + length] = build_timekeeping_tal(onset, length)
            dst.write(record)
 
            if rec % 1000 == 0:
                pct = rec / n_records * 100
                print(f"  {rec:>6}/{n_records}  ({pct:.1f}%)", end='\r')

    # ── Validate ──────────────────────────────────────────────────────────────
    ann_ok = all(
        validate_annotations_removed(output_path, n_hdr, n_records, rec_bytes, offset, length)
        for _, offset, length in annot_channels
    )
    hdr_ok  = validate_header_anonymized(output_path)

    print(f"\n Clean EDF saved → {output_path}")
    print(" Original file unchanged\n")
    print(f"  Version  : '{h['version']}'")
    print(f"  Patient  : '{h['patient']}'")
    print(f"  Date     : {h['startdate']}  Time: {h['starttime']}")
    print(f"  Hdr bytes: {h['n_header_bytes']}")
    print(f"  Records  : {h['n_records']}")
    print(f"  Signals  : {h['n_signals']}")

    return ann_ok and hdr_ok

if __name__ == '__main__':
    args = parse_args()
    
    # patient name derived from EDF filename (e.g. HUP282)
    patient_id  = os.path.splitext(os.path.basename(args.input_edf))[0]
    patient_dir = os.path.join(args.output_dir, patient_id)
    
    os.makedirs(patient_dir, exist_ok=True)
    
    base = os.path.join(patient_dir, patient_id)
    process(args.input_edf, base)

"""
edf_qa.py

Assembles the single, human-readable QA report a reviewer opens to
manually confirm de-identification. No visualizations -- plain,
structured printouts, organized so each section answers one specific
"how do we know X" question.
"""

from __future__ import annotations
from dataclasses import dataclass

from edf_header import EdfHeader, MainHeader, SignalHeader, validate_header_anonymized
from edf_records import PassResult, validate_tal_shape, TAL_SAMPLE_N_RECORDS, TAL_SAMPLE_MATCH_FRACTION


SEP = "=" * 78
SUBSEP = "-" * 78


def _fmt_dynamic_table(headers: list[str], rows: list[list[str]], indent: str = "  ") -> list[str]:
    """Builds a table whose column widths are computed from the actual
    content (headers + all rows), so alignment holds regardless of how
    long any individual value is -- no fixed-width truncation/overflow."""
    n_cols = len(headers)
    widths = [len(headers[c]) for c in range(n_cols)]
    for row in rows:
        for c in range(n_cols):
            widths[c] = max(widths[c], len(str(row[c])))
    gap = 2
    lines = []
    header_line = indent + "".join(h.ljust(widths[c] + gap) for c, h in enumerate(headers))
    lines.append(header_line.rstrip())
    for row in rows:
        lines.append(indent + "".join(str(row[c]).ljust(widths[c] + gap) for c in range(n_cols)).rstrip())
    return lines


def build_report(
    patient_id: str,
    input_path: str,
    original_header: EdfHeader,
    anonymized_header: EdfHeader,
    annot_indices: list[int],
    pass_result: PassResult,
    annotations_csv_path: str,
    output_edf_path: str,
    total_runtime_sec: float,
    manual_annot_indices: list[int] | None = None,
) -> str:
    manual_annot_indices = manual_annot_indices or []
    lines = []
    w = lines.append

    # ── 1. Run info ──────────────────────────────────────────────────────
    w(SEP)
    w("EDF DE-IDENTIFICATION QA REPORT")
    w(SEP)
    w(f"Patient ID       : {patient_id}")
    w(f"Input file       : {input_path}")
    w(f"Output EDF       : {output_edf_path}")
    w(f"Annotations file : {annotations_csv_path}")
    w(f"Records          : {original_header.main.n_records}")
    w(f"Total runtime    : {total_runtime_sec:.1f}s")
    w("")
    w("  Timing breakdown (combined pass, wall clock = "
      f"{pass_result.elapsed_sec:.2f}s):")
    for line in pass_result.timings.as_report_lines(pass_result.elapsed_sec):
        w(line)
    w("")

    # ── 2. Channel list (ALL channels, not just flagged ones) ──────────────
    w(SUBSEP)
    w("CHANNEL LIST (all channels)")
    w(SUBSEP)
    dur = original_header.main.record_dur or 1
    chan_list_rows = []
    for i, s in enumerate(original_header.signals):
        rate = s.n_samples / dur if dur else 0
        if i in manual_annot_indices:
            flagged = 'YES (manual)'
        elif i in annot_indices:
            flagged = 'YES (auto)'
        else:
            flagged = 'no'
        chan_list_rows.append([str(i), s.label, str(s.n_samples), f"{rate:.2f}", flagged])
    for line in _fmt_dynamic_table(['#', 'Label', 'Samples/rec', 'Rate (Hz)', 'Annotation?'], chan_list_rows):
        w(line)
    w("")
    w(f"  -> {len(annot_indices)} channel(s) identified as annotation channels "
      f"({len(annot_indices) - len(manual_annot_indices)} auto-detected, "
      f"{len(manual_annot_indices)} manually specified via --annotation-channels).")
    w(f"     Review the full list above for any unexpected or unrecognized")
    w(f"     channel labels that were NOT flagged but perhaps should be.")
    w("")

    # ── 3. Full header field dump, original vs anonymized ──────────────────
    w(SUBSEP)
    w("HEADER FIELD DUMP -- original vs. anonymized")
    w(SUBSEP)
    w("  Main header:")
    om, am = original_header.main, anonymized_header.main
    for fname in ('patient', 'recording', 'startdate', 'starttime', 'reserved'):
        w(f"  {fname}:")
        w(f"    original   : {getattr(om, fname)!r}")
        w(f"    anonymized : {getattr(am, fname)!r}")
    w("")
    w("  Per-channel free-text fields (transducer / prefiltering / reserved):")
    w("  These are NOT touched by anonymization -- review for any leaked text.")
    chan_rows = []
    any_nonempty = False
    for i, s in enumerate(original_header.signals):
        if s.transducer or s.prefiltering or s.reserved.strip():
            any_nonempty = True
        chan_rows.append([str(i), s.label, s.transducer, s.prefiltering, s.reserved])
    for line in _fmt_dynamic_table(['#', 'Label', 'Transducer', 'Prefiltering', 'Reserved'], chan_rows):
        w(line)
    if not any_nonempty:
        w("  (all per-channel transducer/prefiltering/reserved fields are empty)")
    w("")

    hdr_ok, hdr_problems = validate_header_anonymized(am)
    w(f"  Header anonymization check: {'PASS' if hdr_ok else 'FAIL'}")
    for p in hdr_problems:
        w(f"    - {p}")
    w("")

    # ── 4. Annotation extraction summary ────────────────────────────────────
    w(SUBSEP)
    w("ANNOTATION EXTRACTION SUMMARY")
    w(SUBSEP)
    w(f"  Annotation channels found : {len(annot_indices)}")
    w(f"  Annotations extracted     : {len(pass_result.annotations)}")
    w(f"  Saved to                  : {annotations_csv_path}")
    w("")

    w("  TAL shape validation (clean output must be time-keeping-only per record):")
    n_records = original_header.main.n_records
    tal_ok = True
    for idx in annot_indices:
        s = original_header.signals[idx]
        chan_len = s.n_samples * 2
        raw_out = pass_result.channel_raw_output.get(idx, b'')
        _, malformed = validate_tal_shape(raw_out, n_records, chan_len)
        ok = len(malformed) == 0
        tal_ok = tal_ok and ok
        w(f"    Channel {idx} ({s.label}): "
          f"{n_records - len(malformed)}/{n_records} records match expected shape"
          f"{' -- PASS' if ok else ' -- FAIL'}")
        if malformed:
            preview = malformed[:10]
            more = f" (+{len(malformed) - 10} more)" if len(malformed) > 10 else ""
            w(f"      Malformed record indices: {preview}{more}")
    w("")

    # ── 5. Integrity check ──────────────────────────────────────────────────
    w(SUBSEP)
    w("INTEGRITY CHECK (non-annotation bytes, original vs. output)")
    w(SUBSEP)
    w(f"  Hash (original) : {pass_result.hash_original_hex}")
    w(f"  Hash (output)   : {pass_result.hash_output_hex}")
    w(f"  Result          : {'MATCH' if pass_result.hashes_match else 'MISMATCH -- INVESTIGATE'}")
    w("")

    # ── 6. Unrecognized annotation-like channels ────────────────────────────
    w(SUBSEP)
    w("UNRECOGNIZED ANNOTATION-LIKE CHANNELS")
    w(f"  Structural check: first {TAL_SAMPLE_N_RECORDS} records start with a valid "
      f"TAL onset pattern in >= {TAL_SAMPLE_MATCH_FRACTION:.0%} of samples")
    w(SUBSEP)
    flagged_rows = []
    for idx, tal_frac in sorted(pass_result.tal_sample_match.items()):
        if idx in annot_indices:
            continue  # already-recognized annotation channels, not the point of this check
        if tal_frac >= TAL_SAMPLE_MATCH_FRACTION:
            label = original_header.signals[idx].label
            flagged_rows.append([str(idx), label, f"{tal_frac:.0%}"])
    if not flagged_rows:
        w("  No unrecognized channels structurally resemble an annotation channel.")
    else:
        w(f"  {len(flagged_rows)} channel(s) flagged -- REVIEW EACH BELOW:")
        for line in _fmt_dynamic_table(['#', 'Label', 'TAL-sample match'], flagged_rows):
            w(line)
    w("")

    # ── 7. Overall ───────────────────────────────────────────────────────────
    w(SUBSEP)
    overall_ok = hdr_ok and tal_ok and pass_result.hashes_match and not flagged_rows
    w(f"OVERALL: {'PASS' if overall_ok else 'NEEDS REVIEW'}")
    if not overall_ok:
        reasons = []
        if not hdr_ok:
            reasons.append("header anonymization incomplete")
        if not tal_ok:
            reasons.append("annotation channel(s) did not reduce to clean time-keeping TAL")
        if not pass_result.hashes_match:
            reasons.append("integrity hash mismatch")
        if flagged_rows:
            reasons.append(f"{len(flagged_rows)} unrecognized channel(s) structurally resemble an annotation channel")
        w("  Reason(s): " + "; ".join(reasons))
    w(SEP)

    return "\n".join(lines)

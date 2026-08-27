"""
edf_records.py

Single combined streaming pass over EDF record data: extracts and
removes annotation event text and computes an integrity hash. See
README.md for design rationale.
"""

from __future__ import annotations
import hashlib
import re
import time
from dataclasses import dataclass, field

from edf_header import EdfHeader, record_bytes_per_signal


PROGRESS_INTERVAL_SEC = 0.5

# Structural check for unrecognized TAL-like channels: a real EDF+
# annotation channel's records must start with onset[,duration]\x14 --
# checked on the first N records only (mandatory in every record, so a
# small sample is as informative as the whole file; see DESIGN.md).
TAL_START_PATTERN = re.compile(rb'^[+-]\d+(\.\d+)?(\x15\d+(\.\d+)?)?\x14')
TAL_SAMPLE_N_RECORDS = 20
TAL_SAMPLE_MATCH_FRACTION = 0.9  # flag if >= this fraction of sampled records match


@dataclass
class AnnotationChannel:
    index: int
    label: str
    offset: int   # byte offset within a record
    length: int   # bytes for this channel, within a record


@dataclass
class StageTimings:
    """Cumulative time spent in each sub-operation of the combined pass,
    in seconds. Lets you see whether I/O, hashing, or scanning is the
    actual bottleneck if this starts taking a long time on real files."""
    read_sec: float = 0.0
    annotation_sec: float = 0.0   # extract + build replacement TAL
    hash_sec: float = 0.0
    write_sec: float = 0.0

    @property
    def total_sec(self) -> float:
        return self.read_sec + self.annotation_sec + self.hash_sec + self.write_sec

    def as_report_lines(self, wall_clock_sec: float) -> list[str]:
        lines = []
        total = self.total_sec
        for name, val in [
            ('Read (disk)', self.read_sec),
            ('Annotation extract/rebuild', self.annotation_sec),
            ('Integrity hashing', self.hash_sec),
            ('Write (disk)', self.write_sec),
        ]:
            pct = (val / wall_clock_sec * 100) if wall_clock_sec else 0
            lines.append(f"    {name:<28}: {val:8.2f}s  ({pct:5.1f}% of wall clock)")
        overhead = wall_clock_sec - total
        lines.append(f"    {'Unaccounted (loop overhead)':<28}: {overhead:8.2f}s  "
                      f"({(overhead / wall_clock_sec * 100) if wall_clock_sec else 0:5.1f}% of wall clock)")
        return lines


@dataclass
class PassResult:
    annotations: list[tuple]                     # (channel_index, channel_label, onset, dur, desc)
    hash_original_hex: str
    hash_output_hex: str
    hashes_match: bool
    n_records_processed: int
    elapsed_sec: float
    timings: StageTimings = field(default_factory=StageTimings)
    # Per-channel raw annotation bytes as written to the OUTPUT file,
    # keyed by channel index -- kept so the QA report can independently
    # re-validate the clean TAL shape without re-reading the file.
    channel_raw_output: dict = field(default_factory=dict)
    # Structural TAL-sample match fraction per channel index -- flags
    # channels that structurally resemble a real annotation channel but
    # weren't recognized by label.
    tal_sample_match: dict = field(default_factory=dict)


def _annotation_channels(header: EdfHeader, annot_indices: list[int]) -> list[AnnotationChannel]:
    sizes = record_bytes_per_signal(header.signals)
    chans = []
    for idx in annot_indices:
        offset = sum(sizes[i] for i in range(idx))
        length = sizes[idx]
        chans.append(AnnotationChannel(index=idx, label=header.signals[idx].label,
                                        offset=offset, length=length))
    return chans


def parse_tal(raw: bytes) -> list[tuple]:
    """Parses onset/duration/description triples out of a TAL byte stream."""
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
            onset = float(t_bytes.decode('ascii', errors='replace').lstrip('+-').strip())
            duration = float(d_bytes.decode('ascii', errors='replace').strip() or '0')
        except ValueError:
            continue

        for ann_bytes in parts[1:]:
            desc = ann_bytes.decode('utf-8', errors='replace').strip()
            if desc:
                annotations.append((onset, duration, desc))

    return annotations


def _extract_record_onset(annot_bytes: bytes) -> float:
    idx = annot_bytes.find(b'\x14')
    if idx == -1:
        return 0.0
    onset_field = annot_bytes[:idx].decode('ascii', errors='replace').strip()
    onset_field = onset_field.split('\x15')[0]
    try:
        return float(onset_field.lstrip('+'))
    except ValueError:
        return 0.0


def _build_timekeeping_tal(onset: float, length: int) -> bytes:
    sign = '+' if onset >= 0 else '-'
    tal = f'{sign}{abs(onset):.6f}'.encode('ascii') + b'\x14\x14\x00'
    return tal.ljust(length, b'\x00')[:length]


def _non_annotation_mask_bytes(record: bytes, annot_ranges: list[tuple[int, int]]) -> bytes:
    """Returns the record with annotation-channel byte ranges removed
    (concatenation of the surrounding pieces), for hashing/scanning
    purposes. Order is preserved; annotation bytes are simply excluded."""
    if not annot_ranges:
        return record
    pieces = []
    cursor = 0
    for start, end in sorted(annot_ranges):
        if start > cursor:
            pieces.append(record[cursor:start])
        cursor = max(cursor, end)
    if cursor < len(record):
        pieces.append(record[cursor:])
    return b''.join(pieces)



def structural_tal_sample_check(input_path: str, header: EdfHeader,
                                 all_channels: list[AnnotationChannel]) -> dict:
    """
    Reads only the first TAL_SAMPLE_N_RECORDS records and checks, per
    channel, what fraction start with a valid TAL onset+delimiter
    pattern. A real annotation channel matches on effectively every
    record, by spec; ordinary signal data essentially never does. Cheap
    -- fixed-size read regardless of file size, no full-file pass needed.
    Returns {channel_index: match_fraction}.
    """
    n_hdr = header.main.n_header_bytes
    rec_bytes = sum(c.length for c in all_channels)
    n_sample = min(TAL_SAMPLE_N_RECORDS, header.main.n_records)

    with open(input_path, 'rb') as f:
        f.seek(n_hdr)
        sample = f.read(n_sample * rec_bytes)

    match_fractions = {}
    for c in all_channels:
        matches = 0
        for i in range(n_sample):
            chunk = sample[i * rec_bytes + c.offset: i * rec_bytes + c.offset + c.length]
            if TAL_START_PATTERN.match(chunk):
                matches += 1
        match_fractions[c.index] = matches / n_sample if n_sample else 0.0
    return match_fractions


def process_records(input_path: str, output_path: str, header: EdfHeader,
                     annot_indices: list[int], progress_cb=None) -> PassResult:
    """
    The single combined streaming pass. Reads input_path record-by-record,
    writes the de-identified version to output_path, and accumulates
    annotations and integrity hashes along the way. The structural
    TAL-sample check is run separately, since it only needs the first
    few records.

    progress_cb(records_done, total_records, elapsed_sec), if given, is
    called periodically (not more than every PROGRESS_INTERVAL_SEC).
    """
    n_hdr = header.main.n_header_bytes
    n_records = header.main.n_records
    sizes = record_bytes_per_signal(header.signals)
    rec_bytes = sum(sizes)

    annot_channels = _annotation_channels(header, annot_indices)
    annot_ranges = [(c.offset, c.offset + c.length) for c in annot_channels]

    # Full channel list (every signal, not just annotation ones).
    all_channels = _annotation_channels(header, list(range(len(header.signals))))

    channel_raw = {c.index: bytearray() for c in annot_channels}
    channel_raw_output = {c.index: bytearray() for c in annot_channels}
    hash_orig = hashlib.blake2b()
    hash_out = hashlib.blake2b()

    start = time.perf_counter()
    last_update = start
    timings = StageTimings()

    with open(input_path, 'rb') as src, open(output_path, 'wb') as dst:
        dst.write(header.raw_bytes)  # caller passes in the (already anonymized) header bytes
        src.read(n_hdr)

        for rec in range(n_records):
            t0 = time.perf_counter()
            record = bytearray(src.read(rec_bytes))
            orig_record = bytes(record)  # keep a copy before we mutate it
            t1 = time.perf_counter()
            timings.read_sec += t1 - t0

            for c in annot_channels:
                orig_annot = orig_record[c.offset:c.offset + c.length]
                channel_raw[c.index] += orig_annot
                onset = _extract_record_onset(orig_annot)
                new_annot = _build_timekeeping_tal(onset, c.length)
                record[c.offset:c.offset + c.length] = new_annot
                channel_raw_output[c.index] += new_annot
            out_record = bytes(record)
            t2 = time.perf_counter()
            timings.annotation_sec += t2 - t1

            # Integrity hashing: only the non-annotation bytes should match.
            orig_non_annot = _non_annotation_mask_bytes(orig_record, annot_ranges)
            out_non_annot = _non_annotation_mask_bytes(out_record, annot_ranges)
            hash_orig.update(orig_non_annot)
            hash_out.update(out_non_annot)
            t3 = time.perf_counter()
            timings.hash_sec += t3 - t2

            dst.write(out_record)
            t4 = time.perf_counter()
            timings.write_sec += t4 - t3

            now = t4
            if progress_cb and (now - last_update >= PROGRESS_INTERVAL_SEC or rec == n_records - 1):
                progress_cb(rec + 1, n_records, now - start)
                last_update = now

    annotations = []
    for c in annot_channels:
        for onset, dur, desc in parse_tal(bytes(channel_raw[c.index])):
            annotations.append((c.index, c.label, onset, dur, desc))

    hash_orig_hex = hash_orig.hexdigest()
    hash_out_hex = hash_out.hexdigest()

    tal_sample_match = structural_tal_sample_check(input_path, header, all_channels)

    return PassResult(
        annotations=annotations,
        hash_original_hex=hash_orig_hex,
        hash_output_hex=hash_out_hex,
        hashes_match=(hash_orig_hex == hash_out_hex),
        n_records_processed=n_records,
        elapsed_sec=time.perf_counter() - start,
        timings=timings,
        channel_raw_output={k: bytes(v) for k, v in channel_raw_output.items()},
        tal_sample_match=tal_sample_match,
    )


def validate_tal_shape(channel_raw_bytes: bytes, n_records: int, channel_length: int) -> tuple[int, list[int]]:
    """
    Independent structural check: does each record's annotation channel
    match the exact expected shape (onset, \\x14\\x14\\x00, then zero
    padding -- i.e. time-keeping only, no leftover event text)?
    Returns (n_records, list_of_malformed_record_indices).
    """
    import re
    pattern = re.compile(rb'^[+-]\d+(\.\d+)?\x14\x14\x00\x00*$')
    malformed = []
    for i in range(n_records):
        chunk = channel_raw_bytes[i * channel_length:(i + 1) * channel_length]
        if not pattern.match(chunk):
            malformed.append(i)
    return n_records, malformed

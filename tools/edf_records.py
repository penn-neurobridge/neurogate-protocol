"""
edf_records.py

Single combined streaming pass over EDF record data: extracts and
removes annotation event text, computes an integrity hash, and runs a
text-leak scan. See README.md for design rationale.
"""

from __future__ import annotations
import hashlib
import time
from dataclasses import dataclass, field

import numpy as np

from edf_header import EdfHeader, record_bytes_per_signal


PRINTABLE_RUN_THRESHOLD = 20

# Accepted "text-like" character set: letters, digits, space, . , - / : '
# See README.md for rationale.
_TEXT_LIKE_CHARS = (
    set(range(ord('A'), ord('Z') + 1)) |
    set(range(ord('a'), ord('z') + 1)) |
    set(range(ord('0'), ord('9') + 1)) |
    {ord(c) for c in " .,-/:'"}
)
_IS_TEXT_LIKE = np.zeros(256, dtype=bool)
_IS_TEXT_LIKE[list(_TEXT_LIKE_CHARS)] = True

PROGRESS_INTERVAL_SEC = 0.5

# Records are batched into buffers of this size before each scan call.
# See README.md for why.
SCAN_BATCH_RECORDS = 500



@dataclass
class AnnotationChannel:
    index: int
    label: str
    offset: int   # byte offset within a record
    length: int   # bytes for this channel, within a record


@dataclass
class PrintableRunFinding:
    record_index: int
    byte_offset: int      # offset within the record's non-annotation bytes
    length: int
    preview: str
    channel_index: int = -1
    channel_label: str = "?"


@dataclass
class StageTimings:
    """Cumulative time spent in each sub-operation of the combined pass,
    in seconds. Lets you see whether I/O, hashing, or scanning is the
    actual bottleneck if this starts taking a long time on real files."""
    read_sec: float = 0.0
    annotation_sec: float = 0.0   # extract + build replacement TAL
    hash_sec: float = 0.0
    scan_sec: float = 0.0
    write_sec: float = 0.0

    @property
    def total_sec(self) -> float:
        return self.read_sec + self.annotation_sec + self.hash_sec + self.scan_sec + self.write_sec

    def as_report_lines(self, wall_clock_sec: float) -> list[str]:
        lines = []
        total = self.total_sec
        for name, val in [
            ('Read (disk)', self.read_sec),
            ('Annotation extract/rebuild', self.annotation_sec),
            ('Integrity hashing', self.hash_sec),
            ('Text-like-character scan', self.scan_sec),
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
    annotations: list[tuple]                     # (channel_label, onset, dur, desc)
    printable_findings: list[PrintableRunFinding]
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


def _scan_buffer_raw(chunk: bytes) -> list[tuple[int, int, str]]:
    """Vectorized scan for runs of >= PRINTABLE_RUN_THRESHOLD consecutive
    text-like bytes. Returns (start_offset, length, preview) tuples;
    record/channel attribution is added by the caller (_ScanBatch)."""
    if not chunk:
        return []

    arr = np.frombuffer(chunk, dtype=np.uint8)
    mask = _IS_TEXT_LIKE[arr]

    padded = np.concatenate(([False], mask, [False]))
    diff = np.diff(padded.astype(np.int8))
    starts = np.flatnonzero(diff == 1)
    ends = np.flatnonzero(diff == -1)  # exclusive
    lengths = ends - starts

    long_enough = lengths >= PRINTABLE_RUN_THRESHOLD
    starts = starts[long_enough]
    ends = ends[long_enough]
    lengths = lengths[long_enough]

    results = []
    for s, e, run_len in zip(starts.tolist(), ends.tolist(), lengths.tolist()):
        run_bytes = chunk[s:e]
        if len(set(run_bytes)) <= 1:
            continue  # single repeated byte -- not text
        if _is_periodic_plateau(run_bytes):
            continue  # signal plateau artifact -- not text
        preview = run_bytes.decode('ascii', errors='replace')
        results.append((s, run_len, preview))
    return results


def _is_periodic_plateau(run_bytes: bytes) -> bool:
    """True if every even- or every odd-position byte in run_bytes is a
    single constant value (signature of a 2-byte sample sitting at a
    fixed amplitude -- see README.md)."""
    if len(run_bytes) < 6:
        return False
    even_phase = run_bytes[0::2]
    odd_phase = run_bytes[1::2]
    return len(set(even_phase)) <= 1 or len(set(odd_phase)) <= 1


def _build_piece_map(rec_bytes: int, annot_ranges: list[tuple[int, int]]) -> list[tuple[int, int, int]]:
    """
    Non-annotation bytes for one record are the record's bytes with the
    annotation-channel ranges cut out (see _non_annotation_mask_bytes).
    This describes, for that same record, how a position in the
    resulting "gaps removed" byte stream maps back to a position in
    the original, full record layout -- i.e. which real channel it
    falls in. Returns a list of (piece_start_in_non_annot_bytes,
    corresponding_start_in_full_record, length), one entry per
    contiguous stretch of non-annotation bytes.
    """
    pieces = []
    cursor = 0
    piece_pos = 0
    for start, end in sorted(annot_ranges):
        if start > cursor:
            length = start - cursor
            pieces.append((piece_pos, cursor, length))
            piece_pos += length
        cursor = max(cursor, end)
    if cursor < rec_bytes:
        pieces.append((piece_pos, cursor, rec_bytes - cursor))
    return pieces


def _non_annot_offset_to_channel(non_annot_offset: int, piece_map: list[tuple[int, int, int]],
                                  all_channels: list[AnnotationChannel]) -> AnnotationChannel | None:
    """Maps an offset within one record's non-annotation byte stream
    back to which real channel (by full-record byte offset) it falls
    in. all_channels here means every signal channel, not just the
    annotation ones -- reusing the AnnotationChannel shape since it's
    just (index, label, offset, length)."""
    record_offset = non_annot_offset
    for piece_start, rec_start, length in piece_map:
        if piece_start <= non_annot_offset < piece_start + length:
            record_offset = rec_start + (non_annot_offset - piece_start)
            break
    for ch in all_channels:
        if ch.offset <= record_offset < ch.offset + ch.length:
            return ch
    return None


class _ScanBatch:
    """Batches non-annotation bytes across records, scans once per
    batch, and maps findings back to a record index, offset, and
    channel. See README.md for the batching rationale. Edge case: a run
    straddling two records is attributed to the record it starts in."""
    def __init__(self, piece_map: list[tuple[int, int, int]], all_channels: list[AnnotationChannel],
                 batch_records: int = SCAN_BATCH_RECORDS):
        self.piece_map = piece_map
        self.all_channels = all_channels
        self.batch_records = batch_records
        self._buffer = bytearray()
        self._record_starts: list[int] = []   # buffer offset each record began at
        self._record_indices: list[int] = []  # record_index for each entry above
        self._records_in_batch = 0

    def add(self, record_index: int, chunk: bytes) -> list[PrintableRunFinding]:
        self._record_starts.append(len(self._buffer))
        self._record_indices.append(record_index)
        self._buffer += chunk
        self._records_in_batch += 1
        if self._records_in_batch >= self.batch_records:
            return self.flush()
        return []

    def flush(self) -> list[PrintableRunFinding]:
        if not self._buffer:
            return []
        raw = _scan_buffer_raw(bytes(self._buffer))
        findings = [self._attribute(s, length, preview) for s, length, preview in raw]
        self._buffer = bytearray()
        self._record_starts = []
        self._record_indices = []
        self._records_in_batch = 0
        return findings

    def _attribute(self, buffer_offset: int, length: int, preview: str) -> PrintableRunFinding:
        import bisect
        i = bisect.bisect_right(self._record_starts, buffer_offset) - 1
        i = max(i, 0)
        record_index = self._record_indices[i]
        rel_offset = buffer_offset - self._record_starts[i]

        ch = _non_annot_offset_to_channel(rel_offset, self.piece_map, self.all_channels)
        channel_index = ch.index if ch else -1
        channel_label = ch.label if ch else "?"

        return PrintableRunFinding(record_index=record_index, byte_offset=rel_offset,
                                    length=length, preview=preview,
                                    channel_index=channel_index, channel_label=channel_label)


def process_records(input_path: str, output_path: str, header: EdfHeader,
                     annot_indices: list[int], progress_cb=None) -> PassResult:
    """
    The single combined streaming pass. Reads input_path record-by-record,
    writes the de-identified version to output_path, and accumulates
    annotations, integrity hashes, and printable-run findings along the way.

    progress_cb(records_done, total_records, elapsed_sec), if given, is
    called periodically (not more than every PROGRESS_INTERVAL_SEC).
    """
    n_hdr = header.main.n_header_bytes
    n_records = header.main.n_records
    sizes = record_bytes_per_signal(header.signals)
    rec_bytes = sum(sizes)

    annot_channels = _annotation_channels(header, annot_indices)
    annot_ranges = [(c.offset, c.offset + c.length) for c in annot_channels]

    # Full channel list (every signal, not just annotation ones) -- used
    # to resolve which channel a scan finding actually falls in.
    all_channels = _annotation_channels(header, list(range(len(header.signals))))
    piece_map = _build_piece_map(rec_bytes, annot_ranges)

    channel_raw = {c.index: bytearray() for c in annot_channels}
    channel_raw_output = {c.index: bytearray() for c in annot_channels}
    printable_findings: list[PrintableRunFinding] = []
    scan_batch = _ScanBatch(piece_map=piece_map, all_channels=all_channels)
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

            # Printable-ASCII leak scan: add to the batch, only actually
            # scans (numpy call) once every SCAN_BATCH_RECORDS records.
            printable_findings.extend(scan_batch.add(rec, orig_non_annot))
            t4 = time.perf_counter()
            timings.scan_sec += t4 - t3

            dst.write(out_record)
            t5 = time.perf_counter()
            timings.write_sec += t5 - t4

            now = t5
            if progress_cb and (now - last_update >= PROGRESS_INTERVAL_SEC or rec == n_records - 1):
                progress_cb(rec + 1, n_records, now - start)
                last_update = now

        # Flush any remaining partial batch (fewer than SCAN_BATCH_RECORDS
        # records left over at end of file).
        t_flush0 = time.perf_counter()
        printable_findings.extend(scan_batch.flush())
        timings.scan_sec += time.perf_counter() - t_flush0

    annotations = []
    for c in annot_channels:
        for onset, dur, desc in parse_tal(bytes(channel_raw[c.index])):
            annotations.append((f'{c.label} [ch {c.index}]', onset, dur, desc))

    hash_orig_hex = hash_orig.hexdigest()
    hash_out_hex = hash_out.hexdigest()

    return PassResult(
        annotations=annotations,
        printable_findings=printable_findings,
        hash_original_hex=hash_orig_hex,
        hash_output_hex=hash_out_hex,
        hashes_match=(hash_orig_hex == hash_out_hex),
        n_records_processed=n_records,
        elapsed_sec=time.perf_counter() - start,
        timings=timings,
        channel_raw_output={k: bytes(v) for k, v in channel_raw_output.items()},
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

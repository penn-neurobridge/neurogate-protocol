"""
edf_header.py

EDF/EDF+ header parsing, anonymization, and full field-level reporting.

An EDF file's header has two parts:
  1. The 256-byte "main" header (version, patient, recording, dates, etc.)
  2. Per-signal header blocks (one entry per channel: label, transducer,
     physical/digital min-max, prefiltering, reserved, ...), each field
     stored as a fixed-width block across all channels.

All fields in both parts are stored as literal ASCII text (space-padded),
per the EDF spec -- including numeric fields like record count.
"""

from __future__ import annotations
import datetime as _dt
from dataclasses import dataclass, field


# ── Field layout (byte offset, width) within the 256-byte main header ────────
MAIN_HEADER_FIELDS = {
    'version':        (0,   8),
    'patient':        (8,   80),
    'recording':      (88,  80),
    'startdate':      (168, 8),
    'starttime':      (176, 8),
    'n_header_bytes': (184, 8),
    'reserved':       (192, 44),
    'n_records':      (236, 8),
    'record_dur':     (244, 8),
    'n_signals':      (252, 4),
}

# Per-signal fields, in the order they appear, as (name, width_per_channel)
SIGNAL_HEADER_FIELDS = [
    ('label',            16),
    ('transducer',       80),
    ('phys_dim',         8),
    ('phys_min',         8),
    ('phys_max',         8),
    ('dig_min',          8),
    ('dig_max',          8),
    ('prefiltering',     80),
    ('n_samples',        8),
    ('reserved',         32),
]


@dataclass
class MainHeader:
    version: str
    patient: str
    recording: str
    startdate: str
    starttime: str
    n_header_bytes: int
    reserved: str
    n_records: int
    record_dur: float
    n_signals: int


@dataclass
class SignalHeader:
    """Full per-channel header fields, one instance per channel."""
    label: str
    transducer: str
    phys_dim: str
    phys_min: str
    phys_max: str
    dig_min: str
    dig_max: str
    prefiltering: str
    n_samples: int
    reserved: str


@dataclass
class EdfHeader:
    main: MainHeader
    signals: list[SignalHeader] = field(default_factory=list)
    raw_bytes: bytes = b''  # the exact bytes this header was parsed from


def _decode(raw: bytes) -> str:
    return raw.decode('ascii', errors='replace').strip()


def parse_main_header(raw: bytes) -> MainHeader:
    """raw must be the first 256 bytes of the file."""
    def field_str(name):
        pos, n = MAIN_HEADER_FIELDS[name]
        return _decode(raw[pos:pos + n])

    return MainHeader(
        version=field_str('version'),
        patient=field_str('patient'),
        recording=field_str('recording'),
        startdate=field_str('startdate'),
        starttime=field_str('starttime'),
        n_header_bytes=int(field_str('n_header_bytes')),
        reserved=field_str('reserved'),
        n_records=int(field_str('n_records')),
        record_dur=float(field_str('record_dur') or 0),
        n_signals=int(field_str('n_signals')),
    )


def parse_signal_headers(raw: bytes, n_signals: int) -> list[SignalHeader]:
    """
    raw must be the signal-header region only (i.e. file bytes[256:n_header_bytes]).
    Fields are stored blocked, not interleaved: all labels, then all
    transducers, etc. -- so we walk the block layout sequentially.
    """
    pos = 0
    blocks = {}
    for name, width in SIGNAL_HEADER_FIELDS:
        vals = []
        for _ in range(n_signals):
            vals.append(_decode(raw[pos:pos + width]))
            pos += width
        blocks[name] = vals

    signals = []
    for i in range(n_signals):
        signals.append(SignalHeader(
            label=blocks['label'][i],
            transducer=blocks['transducer'][i],
            phys_dim=blocks['phys_dim'][i],
            phys_min=blocks['phys_min'][i],
            phys_max=blocks['phys_max'][i],
            dig_min=blocks['dig_min'][i],
            dig_max=blocks['dig_max'][i],
            prefiltering=blocks['prefiltering'][i],
            n_samples=int(blocks['n_samples'][i]),
            reserved=blocks['reserved'][i],
        ))
    return signals


def parse_header(f) -> EdfHeader:
    """Parse the complete header (main + all signal blocks) from an open
    binary file handle. Leaves the file position at end of header."""
    f.seek(0)
    main_raw = f.read(256)
    main = parse_main_header(main_raw)

    f.seek(256)
    sig_raw = f.read(main.n_header_bytes - 256)
    signals = parse_signal_headers(sig_raw, main.n_signals)

    f.seek(0)
    full_raw = f.read(main.n_header_bytes)

    return EdfHeader(main=main, signals=signals, raw_bytes=full_raw)


def record_bytes_per_signal(signals: list[SignalHeader]) -> list[int]:
    return [s.n_samples * 2 for s in signals]


def parse_recording_start_datetime(main: MainHeader) -> _dt.datetime | None:
    """
    Parses the EDF startdate ('dd.mm.yy') and starttime ('hh.mm.ss')
    fields into a real datetime. Two-digit year is ambiguous by spec;
    standard convention (also used by EDFlib etc.): year >= 85 -> 19xx,
    else 20xx. Returns None if the fields don't parse (e.g. already
    anonymized to 01.01.01, or malformed).
    """
    try:
        dd, mm, yy = (int(x) for x in main.startdate.split('.'))
        hh, mi, ss = (int(x) for x in main.starttime.split('.'))
        year = 1900 + yy if yy >= 85 else 2000 + yy
        return _dt.datetime(year, mm, dd, hh, mi, ss)
    except (ValueError, TypeError):
        return None


def onset_to_datetime(start: _dt.datetime, onset_sec: float) -> _dt.datetime:
    """Recording start datetime + an onset offset in seconds -> the
    actual clock datetime of that event."""
    return start + _dt.timedelta(seconds=onset_sec)


def find_annotation_channels(signals: list[SignalHeader]) -> list[int]:
    return [
        i for i, s in enumerate(signals)
        if 'EDF Annotations' in s.label or 'BDF Annotations' in s.label
    ]


# ── Anonymization ─────────────────────────────────────────────────────────

def anonymize_main_header_bytes(raw_header: bytes) -> bytes:
    """
    Anonymizes patient/recording identification per EDF+ spec section
    2.1.3, items 3-4: unknown/anonymized subfields become 'X', separated
    by spaces. Startdate kept but zeroed to 01.01.01; starttime untouched.

    Only touches the first 256 bytes. Per-signal free-text fields
    (transducer, prefiltering, reserved) are not anonymized here --
    see the QA header dump.
    """
    header = bytearray(raw_header)

    def write_field(pos, n, value):
        field_bytes = value.encode('ascii', errors='replace')[:n].ljust(n, b' ')
        header[pos:pos + n] = field_bytes

    write_field(8,   80, 'X X X X')
    write_field(88,  80, 'Startdate 01-JAN-2001 X X X')
    write_field(168, 8,  '01.01.01')
    # starttime (176, 8) intentionally untouched

    return bytes(header)


def validate_header_anonymized(main: MainHeader) -> tuple[bool, list[str]]:
    """Returns (ok, list_of_problem_strings)."""
    problems = []

    if main.patient != 'X X X X':
        problems.append(f"patient field not fully anonymized: {main.patient!r}")
    if main.recording != 'Startdate 01-JAN-2001 X X X':
        problems.append(f"recording field not fully anonymized: {main.recording!r}")
    if main.startdate != '01.01.01':
        problems.append(f"startdate not zeroed: {main.startdate!r}")

    return (len(problems) == 0, problems)

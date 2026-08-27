# EDF De-identification Pipeline

Strips annotation event text and anonymizes header PHI fields from an
EDF/EDF+ file, and produces a QA report for manual review before the
output is used or shared.

## Requirements

- Python 3.10+
- `numpy`

```
pip install numpy
```

## Usage

```
python3 main.py <input.edf> <output_dir>
```

The original file is never modified.

If the QA report's "UNRECOGNIZED ANNOTATION-LIKE CHANNELS" section flags a
real annotation channel that wasn't auto-detected, re-run with its channel
index (shown in the report's CHANNEL LIST) added:

```
python3 main.py <input.edf> <output_dir> --annotation-channels 3,7
```

This adds the specified channel(s) to whatever is auto-detected by label —
it doesn't replace auto-detection. The QA report's channel list marks each
annotation channel as `YES (auto)` or `YES (manual)` so it's clear which
channels were found by the label match versus manually specified.

## Output

```
<output_dir>/<patient_id>/
├── Deidentified/
│   └── <patient_id>_no_annotations.edf   <- de-identified EDF
├── Extracted/
│   └── <patient_id>_annotations.csv      <- annotation text removed from the EDF
└── QA/
    ├── <patient_id>_qa_report.txt        <- open this and review before use
    └── <patient_id>_run_log.txt          <- run log
```

## Reviewing the QA report

Open `<patient_id>_qa_report.txt` and check each section:

1. **Channel list** — confirm no unexpected or unrecognized channels.
2. **Header field dump** — original vs. anonymized values for every
   header field, including per-channel fields that are not
   automatically anonymized.
3. **Annotation extraction summary** — confirms annotation text was
   removed and the output file's annotation channel(s) are clean.
4. **Integrity check** — confirms nothing outside the annotation
   channels was altered.
5. **Unrecognized annotation-like channels** — flags any channel not
   already recognized as an annotation channel whose first several
   records structurally match the required annotation-record format —
   a backstop for vendor-specific or nonstandard event channels the
   label-based detection would otherwise miss.
6. **Overall** — PASS or NEEDS REVIEW.

A PASS result means no issues were automatically detected. It is not a
guarantee that zero identifying information remains — the report is a
tool to support manual review, not a replacement for it.

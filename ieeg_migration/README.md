# ieeg_migration

A small, `uv`-managed tool to load and interactively view iEEG **EDF** files
with [MNE-Python](https://mne.tools/).

## Requirements

- [uv](https://docs.astral.sh/uv/) (Python package/environment manager)
- Python 3.13+ (uv installs this automatically if needed)

## Setup

From inside this folder, build the environment from the lockfile:

```bash
uv sync
```

This creates a local `.venv/` with the pinned dependencies (MNE, matplotlib,
PyQt6, mne-qt-browser, Typer).

## Usage

Pass the path to any EDF file as an argument:

```bash
uv run readedf.py /path/to/recording.edf
```

An interactive Qt window opens showing the recording. Close the window to end
the program.

Paths that contain spaces must be quoted:

```bash
uv run readedf.py "../../data/some folder/recording.edf"
```

See the built-in help for all options:

```bash
uv run readedf.py --help
```

## Notes

- `readedf.py` reads the EDF header lazily and opens MNE's interactive viewer;
  it does not modify or export the file.
- Do not commit patient data or files whose names contain identifiers (PHI).
  The `data/` directory and `.venv/` are excluded from version control.

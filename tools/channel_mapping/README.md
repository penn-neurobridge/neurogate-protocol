# Automatic Channel Mapping

Generate channel mapping files from EDF channel labels for EEG recordings.

## Requirements

- Python 3.10+ (required by `edf-reader`)

## Install

```bash
pip install -r requirements.txt
```

## Usage

```bash
python auto_channel_mappings_edf.py -p /path/to/edf/folder
```

The script recursively scans the directory for `.edf` files, reads channel labels, and writes mapping files into the input directory:

- `channel_mapping.txt` — full channel index and label mapping
- `channel_mapping_final.txt` — trimmed mapping by removing blank channels
- `edf_channel_mapping_index.txt` — index linking each EDF file to its mapping files

When channel layouts differ across files, numbered variants (`channel_mapping_1.txt`, etc.) are created instead.

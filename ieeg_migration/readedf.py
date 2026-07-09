"""Load and interactively view an EDF file with MNE.

Usage:
    uv run readedf.py /path/to/recording.edf
"""

from pathlib import Path
from typing import Annotated

import matplotlib

# MNE's viewer needs a GUI backend. "qtagg" uses Qt, which is provided
# by the pyqt6 dependency. This must be set before other plotting imports.
matplotlib.use("qtagg")

import mne
import typer


def main(
    edf_path: Annotated[
        Path, typer.Argument(exists=True, help="Path to the .edf file to open")
    ],
) -> None:
    """Load an EDF file and open MNE's interactive viewer."""
    # read_raw_edf reads the header lazily; it does not load all samples yet.
    raw = mne.io.read_raw_edf(edf_path)

    # block=True keeps the interactive window open until you close it.
    raw.plot(block=True)


if __name__ == "__main__":
    # typer.run turns the main() function into a command-line program,
    # using its type hints to define the arguments.
    typer.run(main)

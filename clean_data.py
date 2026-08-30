"""
Standalone data-cleaning CLI: "clean deliberately" path, on demand.

Usage:
    python clean_data.py data/NAME

Thin wrapper around utils.data_cleaning.clean_dataset() — always runs it against the
given folder regardless of what the rubric finds per file (files with nothing flagged
are simply reported untouched by clean_dataset itself; this script doesn't special-case
anything). Cleaned output lands in data/NAME/cleaned/. Raw files under data/NAME are
never modified by this or any other caller of clean_dataset().
"""

import sys
from pathlib import Path

from utils.data_cleaning import clean_dataset


def main() -> None:
    if len(sys.argv) != 2:
        print(
            "Usage: python clean_data.py <folder_path>\n"
            "Example: python clean_data.py data/olist",
            file=sys.stderr,
        )
        sys.exit(1)

    folder = Path(sys.argv[1])
    if not folder.is_dir():
        print(f"ERROR: folder not found: {folder}", file=sys.stderr)
        sys.exit(1)

    result = clean_dataset(folder)
    print(result.summary())


if __name__ == "__main__":
    main()

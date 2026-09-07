"""Source inspection declaration for the headless operations boundary."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

HDF5_MAGIC = b"\x89HDF\r\n\x1a\n"


@dataclass(frozen=True, kw_only=True, slots=True)
class SourceInspection:
    """Metadata returned by the explicit source-inspection operation."""

    exists: bool
    size_bytes: int | None
    mtime: float | None
    format_guess: Literal["hdf5", "csv_enhanced"] | None
    resolved_uri: str | None = None
    device: int | None = None
    inode: int | None = None
    mtime_ns: int | None = None


def inspect_source(uri: str) -> SourceInspection:
    """Inspect shallow source metadata with strict regular-file and bounding guards."""
    try:
        st = os.stat(uri)
    except (FileNotFoundError, NotADirectoryError):
        return SourceInspection(
            exists=False,
            size_bytes=None,
            mtime=None,
            format_guess=None,
            resolved_uri=None,
            device=None,
            inode=None,
            mtime_ns=None,
        )

    mode = st.st_mode
    if stat.S_ISDIR(mode):
        raise IsADirectoryError(f"Source path is a directory: {uri}")
    non_reg = (
        stat.S_ISFIFO(mode)
        or stat.S_ISCHR(mode)
        or stat.S_ISBLK(mode)
        or stat.S_ISSOCK(mode)
    )
    if non_reg or not stat.S_ISREG(mode):
        raise ValueError(f"Source path is not a regular file: {uri}")

    path = Path(uri)
    suffix = path.suffix.lower()

    format_guess: Literal["hdf5", "csv_enhanced"] | None = None
    if suffix in (".h5", ".hdf5"):
        format_guess = "hdf5"
    elif suffix == ".csv":
        format_guess = "csv_enhanced"
    elif st.st_size >= 8:
        with open(uri, "rb") as f:
            header = f.read(8)
        if header == HDF5_MAGIC:
            format_guess = "hdf5"

    resolved_path = str(path.resolve())
    return SourceInspection(
        exists=True,
        size_bytes=st.st_size,
        mtime=st.st_mtime,
        format_guess=format_guess,
        resolved_uri=resolved_path,
        device=st.st_dev,
        inode=st.st_ino,
        mtime_ns=st.st_mtime_ns,
    )

"""Small real-reader fixtures for the narrowly approved trial I/O routes."""

from __future__ import annotations

import base64
import struct
from pathlib import Path


def write_minimal_diaggui_timeseries(path: Path) -> Path:
    """Write a valid single-channel DiagGUI TS product for real parser checks."""
    samples = (0.25, -1.5, 2.0, 0.75)
    encoded = base64.b64encode(struct.pack("<4f", *samples)).decode("ascii")
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<LIGO_LW Name="GWexpy Studio trial test" Type="Document">
  <LIGO_LW Name="Result[0]" Type="TimeSeries">
    <Time Name="t0" Type="GPS">1126259462</Time>
    <Param Name="Subtype" Type="int">0</Param>
    <Param Name="Channel" Type="string">H1:TEST-CHANNEL</Param>
    <Param Name="N" Type="int">4</Param>
    <Param Name="dt" Type="double">0.25</Param>
    <Array Name="TimeSeries" Type="float">
      <Dim Name="Time">4</Dim>
      <Stream Name="TimeSeries" Type="Local" Encoding="LittleEndian,base64">"""
        + encoded
        + """</Stream>
    </Array>
  </LIGO_LW>
</LIGO_LW>
""",
        encoding="utf-8",
    )
    return path

"""Curated operation specification interfaces."""

from .source import SourceInspection, inspect_source
from .spec import OperationSpec, ParamSpec
from .timeseries import REGISTRY

__all__ = [
    "OperationSpec",
    "ParamSpec",
    "REGISTRY",
    "SourceInspection",
    "inspect_source",
]

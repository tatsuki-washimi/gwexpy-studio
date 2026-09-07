"""Registry-facing native intake and extraction contracts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .io import IO_CLASSES, read_data
from .native_selection import native_extract
from .spec import OperationSpec, ParamSpec


def apply_data_read(inputs: Mapping[str, Any], params: Mapping[str, Any]) -> Any:
    """Read using the exact class, ordered sources, format, and native options."""
    del inputs
    return read_data(
        params["datatype"],
        params["source"],
        format=params.get("format"),
        args=params.get("args"),
        kwargs=params.get("kwargs"),
    )


def emit_data_read(context: Mapping[str, Any]) -> str:
    """Emit native intake with serialized arguments preserved in order."""
    params = context["params"]
    return (
        f"{context['variable']} = read_data({params['datatype']!r}, "
        f"{params['source']!r}, format={params.get('format')!r}, "
        f"args={params.get('args', [])!r}, kwargs={params.get('kwargs', {})!r})"
    )


def apply_data_extract(inputs: Mapping[str, Any], params: Mapping[str, Any]) -> Any:
    """Materialize one independent native leaf."""
    return native_extract(inputs["self"], params["selector"])


def emit_data_extract(context: Mapping[str, Any]) -> str:
    """Emit the same strict public metadata-based member selection."""
    return (
        f"{context['variable']} = native_extract("
        f"{context['inputs']['self']}, {context['params']['selector']!r})"
    )


IO_REGISTRY = {
    "data.read": OperationSpec(
        operation_id="data.read",
        schema_version=1,
        input_roles=(),
        result_kind="dynamic",
        params=(
            ParamSpec(name="datatype", kind="str", required=True),
            ParamSpec(name="source", kind="json", required=True),
            ParamSpec(name="format", kind="str"),
            ParamSpec(name="args", kind="json", default=[]),
            ParamSpec(name="kwargs", kind="json", default={}),
            ParamSpec(name="gwexpy_version", kind="str"),
        ),
        apply=apply_data_read,
        emit=emit_data_read,
    ),
    "data.extract": OperationSpec(
        operation_id="data.extract",
        schema_version=1,
        input_roles=("self",),
        result_kind="member",
        accepted_input_kinds={
            "self": tuple(
                kind for kind in IO_CLASSES if kind.endswith(("Dict", "List", "Matrix"))
            )
        },
        params=(ParamSpec(name="selector", kind="json", required=True),),
        apply=apply_data_extract,
        emit=emit_data_extract,
    ),
}

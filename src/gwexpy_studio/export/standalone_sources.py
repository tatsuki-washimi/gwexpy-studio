"""Deterministic AST projections of shared native helpers for Python export."""

from __future__ import annotations

import ast
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_NATIVE_SOURCE_ORDER = (
    "values",
    "io",
    "native_containers",
    "native_arithmetic",
    "native_results",
    "native_filters",
    "native_spectral",
    "native_selection",
)


def _standalone_nodes(path: Path) -> list[ast.stmt]:
    """Read shared definitions while removing only package-relative imports."""
    parsed = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node
        for node in parsed.body
        if not isinstance(node, ast.ImportFrom)
        or node.level == 0
        and node.module != "__future__"
    ]


def native_helper_source() -> str:
    """Embed the exact tested helper implementations in dependency order."""
    body = [
        node
        for name in _NATIVE_SOURCE_ORDER
        for node in _standalone_nodes(_PACKAGE_ROOT / "ops" / f"{name}.py")
    ]
    return ast.unparse(ast.Module(body=body, type_ignores=[]))


def plotting_helper_source() -> str:
    """Embed display transformations and the public rendering function only."""
    body = _standalone_nodes(_PACKAGE_ROOT / "plotting" / "complex_display.py")
    renderer = _standalone_nodes(_PACKAGE_ROOT / "plotting" / "renderer.py")
    body.extend(
        node
        for node in renderer
        if isinstance(node, ast.FunctionDef) and node.name == "render_plot"
    )
    imports = (
        "from types import SimpleNamespace\n"
        "from matplotlib.figure import Figure\n"
        "from matplotlib.backends.backend_agg import FigureCanvasAgg\n"
    )
    return imports + ast.unparse(ast.Module(body=body, type_ignores=[]))

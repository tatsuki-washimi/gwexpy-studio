"""Structural controller contract shared by the I/O and preview mixins."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from ..domain.model import DataObjectRef, MemberRef, PlotSpec
from ..domain.project import Project
from ..session import StudioSession
from .preview import PreviewData


class SignalHost(Protocol):
    """State and semantic actions required by the application mixins."""

    project: Project
    session: StudioSession

    def set_plot_spec(self, spec: PlotSpec) -> None:
        """Persist display settings independently of the scientific graph."""
        ...

    def _signal_request(
        self, kind: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]: ...
    def _object(self, object_id: str) -> DataObjectRef: ...
    def _execute_signal(
        self, name: str, inputs: Mapping[str, str], params: Mapping[str, Any]
    ) -> DataObjectRef: ...
    def _materialize_handle(self, handle: Mapping[str, Any]) -> str: ...
    def _detach_bundles(
        self, bundles: list[Mapping[str, Any]]
    ) -> tuple[PreviewData, ...]: ...
    def list_members(
        self, object_id: str, offset: int = 0, limit: int = 100
    ) -> tuple[MemberRef, ...]:
        """Describe a bounded page of members without materializing them."""
        ...

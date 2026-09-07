"""Reviewed, cancellable reconstruction in a fresh scientific worker."""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from ..domain.history import active_object_ids, active_targets, redo_history
from ..domain.model import ActivityRecord, Operation
from ..domain.project import Project
from ..errors import OperationError
from .alpha import AlphaController
from .workspace_document import WorkspaceDocument, detached, document_digest


def scientific_versions() -> dict[str, str]:
    """Read distribution versions without importing science libraries."""
    result = {}
    for name in ("gwexpy", "gwpy", "numpy", "scipy", "astropy"):
        try:
            result[name] = version(name)
        except PackageNotFoundError:
            result[name] = "not installed"
    return result


def read_paths(project: Project, operations: list[Operation]) -> list[str]:
    """Resolve recorded read arguments while preserving input order."""
    sources = {source.source_id: source.uri for source in project.sources}
    result: list[str] = []
    for op in operations:
        source = op.params.get("source")
        if isinstance(source, Mapping):
            source = sources.get(str(source.get("source_id")), source.get("source_id"))
        if isinstance(source, str):
            result.append(source)
        elif isinstance(source, (tuple, list)) and all(
            isinstance(path, str) for path in source
        ):
            result.extend(source)
        else:
            raise OperationError(
                f"Unresolvable source for {op.op_id}", code="source_not_found"
            )
    return result


def source_sets(
    project: Project,
) -> list[tuple[str, list[str], Mapping[str, Any] | None]]:
    """Associate full ordered read gestures with recorded or new evidence."""
    reads = {
        op.op_id: op
        for op in project.graph.operations
        if op.operation_id in {"timeseries.read", "data.read"}
    }
    needed = {
        op.op_id for op in project.graph.ancestors(active_targets(project))
    } & reads.keys()
    handled: set[str] = set()
    result: list[tuple[str, list[str], Mapping[str, Any] | None]] = []
    for group in project.history.groups:
        ids = [op_id for op_id in group.operation_ids if op_id in reads]
        if ids and needed.intersection(ids):
            anchor = ids[0]
            result.append(
                (
                    anchor,
                    read_paths(project, [reads[op_id] for op_id in ids]),
                    project.source_manifests.get(anchor),
                )
            )
            handled.update(ids)
    for op in project.graph.operations:
        if op.op_id in needed - handled:
            result.append(
                (
                    op.op_id,
                    read_paths(project, [op]),
                    project.source_manifests.get(op.op_id),
                )
            )
    return result


class WorkspaceRestore(WorkspaceDocument):
    """Require a reviewed snapshot before reading external sources."""

    def request_cancel(self) -> None:
        """Only signal cancellation and interrupt the owned worker process."""
        self._cancel_event.set()
        client = self._cancel_client
        if client is not None:
            client.cancel()

    def _check_cancel(self, event: threading.Event | None) -> None:
        if self._cancel_event.is_set() or (event is not None and event.is_set()):
            raise OperationError("Operation cancelled", code="operation_cancelled")

    def _verify_sources(
        self,
        project: Project,
        probe: AlphaController,
        *,
        cancel_event: threading.Event | None,
        progress: Callable[[dict[str, Any]], None] | None,
    ) -> list[dict[str, Any]]:
        groups = source_sets(project)
        checked = []
        for index, (anchor, paths, expected) in enumerate(groups):
            self._check_cancel(cancel_event)
            if progress:
                progress(
                    {
                        "completed": index,
                        "total": len(groups),
                        "label": "Checking source files",
                    }
                )
            request = {
                "paths": paths,
                "max_bytes": expected.get("max_bytes", 536870912)
                if expected
                else 536870912,
                "max_entries": expected.get("max_entries", 10000)
                if expected
                else 10000,
            }
            payload: dict[str, Any] = {"mode": "fingerprint", "request": request}
            if expected:
                payload["expected_manifest"] = dict(expected)
            evidence = probe._signal_request("inspect_io", payload)["source_manifest"]
            checked.append(
                {
                    "anchor": anchor,
                    "paths": paths,
                    "evidence": evidence,
                    "verified": expected is not None,
                }
            )
        self._check_cancel(cancel_event)
        return checked

    def review_restore(
        self,
        *,
        redo: bool = False,
        progress: Callable[[dict[str, Any]], None] | None = None,
        cancel_event: threading.Event | None = None,
        deadline_monotonic: float | None = None,
    ) -> dict[str, Any]:
        """Inspect the active inputs and issue a document-bound review token."""
        self._cancel_event.clear()
        candidate = detached(self.project)
        if redo:
            redo_history(candidate)
        probe = AlphaController(project=candidate)
        assert probe.session.client is not None
        self._cancel_client = probe.session.client
        try:
            scope = (
                probe.session.client.deadline_scope(deadline_monotonic)
                if deadline_monotonic
                else nullcontext()
            )
            with scope:
                sources = self._verify_sources(
                    candidate, probe, cancel_event=cancel_event, progress=progress
                )
        finally:
            with (
                probe.session.client.deadline_scope(deadline_monotonic)
                if deadline_monotonic is not None
                else nullcontext()
            ):
                probe.close()
            self._cancel_client = None
        current = scientific_versions()
        recorded = {
            key: val
            for rec in candidate.executions
            if rec.status == "succeeded"
            for key, val in rec.environment.items()
            if key in current
        }
        for key in current:
            if key not in recorded and key in candidate.compatibility:
                recorded[key] = candidate.compatibility[key]
        differences = {
            key: {"recorded": recorded.get(key), "current": val}
            for key, val in current.items()
            if recorded.get(key) != val
        }
        review = {
            "token": str(uuid.uuid4()),
            "revision": self._revision,
            "generation": self._generation,
            "document_digest": document_digest(self.project),
            "redo": redo,
            "targets": list(active_targets(candidate)),
            "sources": sources,
            "environment": current,
            "environment_differences": differences,
            "unverified_sources": any(not entry["verified"] for entry in sources),
        }
        self._restore_review = json.loads(json.dumps(review))
        return json.loads(json.dumps(review))

    def restore_workspace(
        self,
        review: Mapping[str, Any],
        *,
        confirmed: bool,
        progress: Callable[[dict[str, Any]], None] | None = None,
        cancel_event: threading.Event | None = None,
        deadline_monotonic: float | None = None,
    ) -> None:
        """Rebuild reviewed results in isolation, then publish them together."""
        if (
            not confirmed
            or self._restore_review is None
            or dict(review) != self._restore_review
            or review["revision"] != self._revision
            or review["generation"] != self._generation
            or review["document_digest"] != document_digest(self.project)
        ):
            raise OperationError(
                "Review this project before restoring data",
                code="restore_confirmation_required",
            )
        self._cancel_event.clear()
        self._check_cancel(cancel_event)
        candidate = detached(self.project)
        if review["redo"]:
            redo_history(candidate)
        targets = active_targets(candidate)
        session = self._fresh_session(candidate)
        assert session.client is not None
        probe = AlphaController(project=candidate, session=session)
        self._cancel_client = session.client
        self._intent("restore", {"targets": list(targets)})
        published = False
        try:
            scope = (
                session.client.deadline_scope(deadline_monotonic)
                if deadline_monotonic
                else nullcontext()
            )
            with scope:
                checked = self._verify_sources(
                    candidate, probe, cancel_event=cancel_event, progress=progress
                )
                if (
                    checked != review["sources"]
                    or scientific_versions() != review["environment"]
                ):
                    raise OperationError(
                        "Sources or environment changed after review",
                        code="source_changed",
                    )
                if targets:
                    session.start()
                    session.replay(
                        targets=targets,
                        refresh_metadata=True,
                        progress=progress,
                        check_cancel=lambda: self._check_cancel(cancel_event),
                    )
                self._check_cancel(cancel_event)
                resident = (
                    set(probe._signal_request("list_objects", {})["objects"])
                    if targets
                    else set()
                )
                missing = active_object_ids(candidate) - resident
                if missing:
                    raise OperationError(
                        "Worker restore missing data: " + ", ".join(sorted(missing)),
                        code="restore_incomplete",
                    )
                after = self._verify_sources(
                    candidate, probe, cancel_event=cancel_event, progress=None
                )
                if after != checked:
                    raise OperationError(
                        "Source changed while restoring", code="source_changed"
                    )
            candidate.source_manifests = {
                **candidate.source_manifests,
                **{
                    entry["anchor"]: entry["evidence"]
                    for entry in checked
                    if not entry["verified"]
                },
            }
            candidate.activities += (
                ActivityRecord(
                    activity_id=str(uuid.uuid4()),
                    action="restore_project",
                    object_id=None,
                    target=None,
                    started_at=datetime.now(UTC).isoformat(),
                    status="succeeded",
                    duration_s=0,
                    details={
                        "environment": review["environment"],
                        "environment_differences": review["environment_differences"],
                        "unverified_sources": review["unverified_sources"],
                        "generation": str(uuid.uuid4()),
                    },
                ),
            )
            candidate = detached(candidate)
            self.session.close()
            self.project = candidate
            self.session = session
            session.project = candidate
            session.graph = candidate.graph
            self._resident = resident
            self._needs_restore = False
            self._environment_changed = bool(review["environment_differences"])
            self._generation = str(uuid.uuid4())
            self._pending_intent = None
            self._changed()
            published = True
        except BaseException as exc:
            self._check_cancel(cancel_event)
            raise exc
        finally:
            if not published:
                with (
                    session.client.deadline_scope(deadline_monotonic)
                    if deadline_monotonic is not None
                    else nullcontext()
                ):
                    session.close()
                self._pending_intent = None
                self._checkpoint()
            self._cancel_client = None

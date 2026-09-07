"""Document ownership, explicit saves and independent recovery checkpoints."""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..domain.history import active_object_ids, can_redo, can_undo, selected_handles
from ..domain.project import Project
from ..errors import OperationError
from ..persistence.locking import ProjectLock
from ..persistence.project_io import load_project, save_project
from ..persistence.recovery import RecoveryStore
from ..session import StudioSession
from ..worker.client import WorkerClient
from .project_factory import create_alpha_project


def detached(project: Project) -> Project:
    """Validate and copy all nested state across an ownership boundary."""
    return Project.from_dict(json.loads(json.dumps(project.to_dict(), allow_nan=False)))


def document_digest(project: Project) -> str:
    """Compare persisted content without the timestamp assigned by Save."""
    value = project.to_dict()
    value.pop("modified", None)
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


class WorkspaceDocument:
    """Qt-free workspace lifecycle mixed into the scientific controller."""

    project: Project
    session: StudioSession
    _cancel_event: threading.Event
    _cancel_client: WorkerClient | None
    _group_depth: int

    def _init_document(self, recovery_root: Path | None, recovery: bool) -> None:
        self._revision = 0
        self._generation = str(uuid.uuid4())
        self._project_path: str | None = None
        self._project_lock: ProjectLock | None = None
        self._read_only = False
        self._saved_digest = document_digest(self.project)
        self._force_dirty = False
        self._resident: set[str] = set()
        self._needs_restore = bool(self.project.objects)
        self._recovery_root = recovery_root
        self._recovery_enabled = recovery
        self._recovery: RecoveryStore | None = None
        self._last_run_id: str | None = None
        self._pending_intent: dict[str, Any] | None = None
        self._recovery_error: str | None = None
        self._last_checkpoint_at: str | None = None
        self._restore_review: dict[str, Any] | None = None
        self._environment_changed = False
        self.resume_workspace()

    def resume_workspace(self) -> None:
        """Reserve a new recovery owner after a terminal bridge shutdown."""
        if not self._recovery_enabled or self._recovery is not None:
            return
        try:
            self._recovery = RecoveryStore(self._recovery_root)
            if self._last_run_id is not None:
                self._checkpoint()
                if self._recovery_error is None:
                    self._recovery.discard(self._last_run_id)
                    self._last_run_id = None
        except Exception as exc:
            self._recovery_error = f"Recovery protection unavailable: {exc}"

    def workspace_status(self) -> dict[str, Any]:
        """Return primitives; worker residency is separate from provenance."""
        return {
            "revision": self._revision,
            "generation": self._generation,
            "project_path": self._project_path,
            "read_only": self._read_only,
            "dirty": self._force_dirty
            or document_digest(self.project) != self._saved_digest,
            "needs_restore": self._needs_restore,
            "can_undo": can_undo(self.project),
            "can_redo": can_redo(self.project),
            "active_object_ids": sorted(active_object_ids(self.project)),
            "resident_object_ids": sorted(self._resident),
            "selected_handles": [
                {
                    "object_id": h["object_id"],
                    "selector": dict(h["selector"])
                    if h.get("selector") is not None
                    else None,
                }
                for h in selected_handles(self.project)
            ],
            "recovery_error": self._recovery_error,
            "last_checkpoint_at": self._last_checkpoint_at,
            "environment_changed": self._environment_changed,
        }

    def _checkpoint(self) -> None:
        if self._recovery is None:
            return
        try:
            snapshot = self._recovery.checkpoint(
                self.project,
                revision=self._revision,
                original_path=self._project_path,
                pending=self._pending_intent,
            )
            self._last_checkpoint_at = snapshot.created_at
            self._recovery_error = None
        except Exception as exc:
            self._recovery_error = f"Recovery save failed: {exc}"

    def _changed(self) -> None:
        self._revision += 1
        self._restore_review = None
        self._checkpoint()

    def _intent(self, kind: str, details: Mapping[str, Any]) -> None:
        self._pending_intent = {
            "kind": kind,
            "started_at": datetime.now(UTC).isoformat(),
            **dict(details),
        }
        self._checkpoint()

    def set_ui_state(self, ui_state: Mapping[str, Any]) -> None:
        """Validate and persist a detached set of current widget values."""
        candidate = detached(self.project)
        candidate.ui_state = json.loads(json.dumps(dict(ui_state), allow_nan=False))
        candidate = detached(candidate)
        if candidate.ui_state != self.project.ui_state:
            self.project.ui_state = candidate.ui_state
            self._changed()

    def save_workspace(
        self,
        path: str | Path | None = None,
        *,
        ui_state: Mapping[str, Any] | None = None,
    ) -> None:
        """Save atomically and mark clean only after replacing the file."""
        if ui_state is not None:
            self.set_ui_state(ui_state)
        if path is None:
            path = self._project_path
        if path is None:
            raise OperationError("Choose a project path", code="project_path_required")
        target = str(Path(path).expanduser().resolve())
        new_lock: ProjectLock | None = None
        if target != self._project_path:
            new_lock = ProjectLock(target)
            if not new_lock.acquire():
                new_lock.close()
                raise OperationError(
                    "Project is open in another process", code="project_read_only"
                )
        elif (
            self._read_only
            or self._project_lock is None
            or not self._project_lock.writable
        ):
            raise OperationError(
                "Use Save As for this read-only project", code="project_read_only"
            )
        self._intent("save_project", {"target": target})
        try:
            save_project(self.project, target)
        except BaseException:
            if new_lock is not None:
                new_lock.close()
            raise
        finally:
            self._pending_intent = None
            self._checkpoint()
        if new_lock is not None:
            if self._project_lock is not None:
                self._project_lock.close()
            self._project_lock = new_lock
        self._project_path = target
        self._read_only = False
        self._saved_digest = document_digest(self.project)
        self._force_dirty = False
        self._changed()

    def _adopt_document(
        self, project: Project, path: str | None, lock: ProjectLock | None
    ) -> None:
        candidate_digest = document_digest(project)
        fresh_session = self._fresh_session(project)
        old_recovery = self._recovery
        old_lock = self._project_lock
        try:
            # Keep the recovery owner locked until worker cleanup succeeds.
            self.session.close()
            if old_recovery is not None:
                old_recovery.close(clean=False)
        except BaseException:
            self._resident.clear()
            self._needs_restore = bool(active_object_ids(self.project))
            fresh_session.close()
            raise
        old_run = old_recovery.run_id if old_recovery is not None else None
        self._recovery = None
        self.project = project
        self.session = fresh_session
        self._project_path = path
        self._project_lock = lock
        self._read_only = lock is not None and not lock.writable
        self._resident = set()
        self._needs_restore = bool(active_object_ids(project))
        self._generation = str(uuid.uuid4())
        self._pending_intent = None
        self._last_run_id = None
        self._last_checkpoint_at = None
        self._saved_digest = candidate_digest
        self._force_dirty = False
        self._environment_changed = False
        self.resume_workspace()
        self._changed()
        if old_lock is not None and old_lock is not lock:
            old_lock.close()
        if old_run is not None and self._recovery is not None:
            try:
                self._recovery.discard(old_run)
            except Exception as exc:
                self._recovery_error = f"Previous recovery cleanup failed: {exc}"

    def _fresh_session(self, project: Project) -> StudioSession:
        from .alpha import AlphaController

        return AlphaController(project=project).session

    def new_workspace(self) -> None:
        """Start an empty document after the caller handles unsaved work."""
        self._adopt_document(create_alpha_project(), None, None)

    def open_workspace(self, path: str | Path) -> None:
        """Validate a project and show its metadata without reading sources."""
        target = str(Path(path).expanduser().resolve())
        candidate = load_project(target)
        if target == self._project_path and self._project_lock is not None:
            lock = self._project_lock
        else:
            lock = ProjectLock(target)
            lock.acquire()
        try:
            self._adopt_document(candidate, target, lock)
        except BaseException:
            if lock is not self._project_lock:
                lock.close()
            raise

    def list_recoveries(self) -> list[dict[str, Any]]:
        """List unlocked interrupted sessions without reading their sources."""
        if self._recovery is None:
            return []
        return [
            {
                "run_id": s.run_id,
                "revision": s.revision,
                "generation": s.generation,
                "created_at": s.created_at,
                "original_path": s.original_path,
                "project_id": s.project().project_id,
                "pending": s.pending,
            }
            for s in self._recovery.candidates()
        ]

    def recover_workspace(self, run_id: str) -> None:
        """Open an interrupted snapshot as an unsaved document."""
        if self._recovery is None:
            raise OperationError(
                "Recovery storage unavailable", code="recovery_unavailable"
            )
        snapshot = self._recovery.load(run_id)
        project = snapshot.project()
        self._adopt_document(project, None, None)
        self._force_dirty = True
        if snapshot.pending:
            self._append_interruption(snapshot.pending)
        self._changed()
        if self._recovery is not None and self._recovery_error is None:
            try:
                from ..persistence.atomic import cleanup_atomic_writes

                paths = {snapshot.original_path} if snapshot.original_path else set()
                if snapshot.pending and snapshot.pending.get("kind") == "save_project":
                    paths.add(str(snapshot.pending["target"]))
                for path in paths:
                    cleanup_atomic_writes(Path(path))
                self._recovery.discard(run_id)
            except Exception as exc:
                self._recovery_error = (
                    f"Recovered project; temporary file cleanup failed: {exc}"
                )

    def discard_recovery(self, run_id: str) -> None:
        """Discard one explicitly selected, currently unlocked candidate."""
        if self._recovery is not None:
            self._recovery.discard(run_id)

    def _append_interruption(self, pending: Mapping[str, Any]) -> None:
        from ..domain.model import ActivityRecord

        is_write = pending.get("kind") in {"write_data", "export"}
        self.project.activities += (
            ActivityRecord(
                activity_id=str(uuid.uuid4()),
                action="interrupted_command",
                object_id=None,
                target=pending.get("target"),
                started_at=str(pending.get("started_at", "")),
                duration_s=0,
                status="unknown" if is_write else "interrupted",
                details={"pending": dict(pending)},
                error={
                    "code": "output_unconfirmed"
                    if is_write
                    else "operation_interrupted",
                    "message": "Output result unconfirmed"
                    if is_write
                    else "Operation interrupted",
                },
            ),
        )

    def close(self) -> None:
        """Stop execution while retaining a document for explicit recovery."""
        self.session.close()
        self._resident.clear()
        self._needs_restore = bool(active_object_ids(self.project))
        if self._recovery is not None:
            self._checkpoint()
            self._last_run_id = self._recovery.run_id
            self._recovery.close(clean=False)
            self._recovery = None

    def finalize_workspace(self, *, clean: bool) -> None:
        """Release only this controller's locks after a user close decision."""
        self.close()
        if clean and self._last_run_id is not None and self._recovery_enabled:
            owner = RecoveryStore(self._recovery_root)
            try:
                owner.discard(self._last_run_id)
            finally:
                owner.close(clean=True)
            self._last_run_id = None
        if self._project_lock is not None:
            self._project_lock.close()
            self._project_lock = None

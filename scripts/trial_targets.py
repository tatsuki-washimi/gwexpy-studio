"""Closed definitions for supported cross-platform trial targets."""

from __future__ import annotations

from dataclasses import dataclass


class TrialTargetError(ValueError):
    """Raised when a trial target or architecture is outside the contract."""


@dataclass(frozen=True, slots=True)
class TrialTarget:
    """One immutable trial distribution target."""

    id: str
    host_os: str
    host_min_version: str
    os_id: str
    os_version: str
    guest_os: tuple[str, str] | None
    architectures: tuple[str, ...]
    mode: str
    backend: str
    allowed_backends: tuple[str, ...]
    hosted_backend: str
    constraints_filenames: tuple[tuple[str, str], ...]
    resolution_filenames: tuple[tuple[str, str], ...]
    documentation_directory: str
    publication_group: str

    def require_architecture(self, architecture: str) -> None:
        """Reject an architecture that is outside this target's contract."""
        if architecture not in self.architectures:
            raise TrialTargetError(
                f"architecture {architecture!r} is not supported by target {self.id!r}"
            )

    def constraints_filename(self, architecture: str) -> str:
        """Return the target-bound constraints filename for one architecture."""
        return self._filename(self.constraints_filenames, architecture, "constraints")

    def resolution_filename(self, architecture: str) -> str:
        """Return the target-bound resolution filename for one architecture."""
        return self._filename(self.resolution_filenames, architecture, "resolution")

    def _filename(
        self,
        filenames: tuple[tuple[str, str], ...],
        architecture: str,
        label: str,
    ) -> str:
        self.require_architecture(architecture)
        for candidate_architecture, filename in filenames:
            if candidate_architecture == architecture:
                return filename
        raise TrialTargetError(
            f"{label} filename is not defined for target {self.id!r}"
        )

    @property
    def execution_mode(self) -> str:
        """Return the immutable native/WSL execution mode."""
        return self.mode

    @property
    def qt_backend(self) -> str:
        """Return the Qt backend required by this target."""
        return self.backend

    def require_backend(self, backend: str) -> None:
        """Reject a runtime backend outside this target's native allowance."""
        if backend not in self.allowed_backends:
            raise TrialTargetError(
                f"backend {backend!r} is not supported by target {self.id!r}"
            )

    def quick_start_source(self, language: str) -> str:
        """Return a repository-relative platform Quick Start source path."""
        names = {"en": "Quick-Start.md", "ja": "Quick-Start.ja.md"}
        try:
            name = names[language]
        except KeyError as error:
            raise TrialTargetError(
                f"unsupported Quick Start language: {language!r}"
            ) from error
        return f"{self.documentation_directory}/{name}"

    def feedback_source(self) -> str:
        """Return the repository-relative Japanese feedback form source path."""
        return f"{self.documentation_directory}/Feedback.ja.md"

    def archive_root(self, build_id: str) -> str:
        """Return the target-qualified archive root directory name."""
        return f"gwexpy-studio-trial-{self.id}-{build_id}"

    def manifest_record(self) -> dict[str, object]:
        """Return the historical schema-4 target record without widening it."""
        guest = None
        if self.guest_os is not None:
            guest = {"id": self.guest_os[0], "version": self.guest_os[1]}
        return {
            "architectures": list(self.architectures),
            "guest_os": guest,
            "host_min_version": self.host_min_version,
            "host_os": self.host_os,
            "id": self.id,
        }

    def contract_record(self) -> dict[str, object]:
        """Return the complete new target contract outside legacy manifests."""
        record = self.manifest_record()
        record.update(
            {
                "allowed_backends": list(self.allowed_backends),
                "backend": self.backend,
                "constraints_filenames": dict(self.constraints_filenames),
                "documentation_directory": self.documentation_directory,
                "hosted_backend": self.hosted_backend,
                "mode": self.mode,
                "os_id": self.os_id,
                "os_version": self.os_version,
                "publication_group": self.publication_group,
                "resolution_filenames": dict(self.resolution_filenames),
            }
        )
        return record


_TARGETS = {
    "ubuntu24-x86_64": TrialTarget(
        id="ubuntu24-x86_64",
        host_os="ubuntu",
        host_min_version="24.04",
        os_id="ubuntu",
        os_version="24.04",
        guest_os=None,
        architectures=("x86_64",),
        mode="native",
        backend="wayland",
        allowed_backends=("wayland", "xcb"),
        hosted_backend="offscreen",
        constraints_filenames=(("x86_64", "constraints-ubuntu24-x86_64.txt"),),
        resolution_filenames=(("x86_64", "resolution-ubuntu24-x86_64.json"),),
        documentation_directory="docs/trial/ubuntu",
        publication_group="ubuntu",
    ),
    "debian13-x86_64": TrialTarget(
        id="debian13-x86_64",
        host_os="debian",
        host_min_version="13",
        os_id="debian",
        os_version="13",
        guest_os=None,
        architectures=("x86_64",),
        mode="native",
        backend="wayland",
        allowed_backends=("wayland", "xcb"),
        hosted_backend="offscreen",
        constraints_filenames=(("x86_64", "constraints-debian13-x86_64.txt"),),
        resolution_filenames=(("x86_64", "resolution-debian13-x86_64.json"),),
        documentation_directory="docs/trial/debian",
        publication_group="debian",
    ),
    "wsl2-ubuntu24": TrialTarget(
        id="wsl2-ubuntu24",
        host_os="windows",
        host_min_version="11",
        os_id="ubuntu",
        os_version="24.04",
        guest_os=("ubuntu", "24.04"),
        architectures=("x86_64", "aarch64"),
        mode="wsl2",
        backend="wayland",
        allowed_backends=("wayland", "xcb"),
        hosted_backend="offscreen",
        constraints_filenames=(
            ("x86_64", "constraints-ubuntu24-x86_64.txt"),
            ("aarch64", "constraints-ubuntu24-aarch64.txt"),
        ),
        resolution_filenames=(
            ("x86_64", "resolution-ubuntu24-x86_64.json"),
            ("aarch64", "resolution-ubuntu24-aarch64.json"),
        ),
        documentation_directory="docs/trial/wsl2",
        publication_group="wsl2-mac",
    ),
    "macos15-arm64": TrialTarget(
        id="macos15-arm64",
        host_os="macos",
        host_min_version="15",
        os_id="macos",
        os_version="15",
        guest_os=None,
        architectures=("arm64",),
        mode="native",
        backend="cocoa",
        allowed_backends=("cocoa",),
        hosted_backend="cocoa",
        constraints_filenames=(("arm64", "constraints-macos15-arm64.txt"),),
        resolution_filenames=(("arm64", "resolution-macos15-arm64.json"),),
        documentation_directory="docs/trial/macos",
        publication_group="wsl2-mac",
    ),
}


def trial_target(target_id: str) -> TrialTarget:
    """Resolve one supported target, failing closed for every other value."""
    try:
        return _TARGETS[target_id]
    except KeyError as error:
        raise TrialTargetError(f"unsupported trial target: {target_id!r}") from error


def target_ids() -> tuple[str, ...]:
    """Return target identifiers in canonical order."""
    return tuple(sorted(_TARGETS))

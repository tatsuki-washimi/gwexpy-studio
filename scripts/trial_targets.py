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
    guest_os: tuple[str, str] | None
    architectures: tuple[str, ...]
    documentation_directory: str

    def require_architecture(self, architecture: str) -> None:
        """Reject an architecture that is outside this target's contract."""
        if architecture not in self.architectures:
            raise TrialTargetError(
                f"architecture {architecture!r} is not supported by target {self.id!r}"
            )

    def constraints_filename(self, architecture: str) -> str:
        """Return the target-bound constraints filename for one architecture."""
        self.require_architecture(architecture)
        if self.id == "wsl2-ubuntu24":
            return f"constraints-ubuntu24-{architecture}.txt"
        return "constraints-macos15-arm64.txt"

    def resolution_filename(self, architecture: str) -> str:
        """Return the target-bound resolution filename for one architecture."""
        self.require_architecture(architecture)
        if self.id == "wsl2-ubuntu24":
            return f"resolution-ubuntu24-{architecture}.json"
        return "resolution-macos15-arm64.json"

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
        """Return the exact schema-4 target record."""
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


_TARGETS = {
    "wsl2-ubuntu24": TrialTarget(
        id="wsl2-ubuntu24",
        host_os="windows",
        host_min_version="11",
        guest_os=("ubuntu", "24.04"),
        architectures=("x86_64", "aarch64"),
        documentation_directory="docs/trial/wsl2",
    ),
    "macos15-arm64": TrialTarget(
        id="macos15-arm64",
        host_os="macos",
        host_min_version="15",
        guest_os=None,
        architectures=("arm64",),
        documentation_directory="docs/trial/macos",
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

"""Verify the exact four-file seed shared by platform build jobs."""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
from collections.abc import Sequence
from pathlib import Path

try:
    from .capture_trial_resolution import (
        ResolutionError,
        TrialArtifact,
        load_trial_artifact,
    )
except ImportError:  # pragma: no cover - direct CLI invocation.
    from capture_trial_resolution import (  # type: ignore[no-redef]
        ResolutionError,
        TrialArtifact,
        load_trial_artifact,
    )


class TrialSeedError(RuntimeError):
    """Raised when a downloaded build seed is incomplete or ambiguous."""


_SOURCE_SHA = re.compile(r"[0-9a-f]{40}")
_FIXED_NAMES = {
    "SOURCE-MANIFEST.json",
    "STAGING-MANIFEST.json",
    "TRIAL-MANIFEST.json",
}


def verify_trial_seed(seed_directory: Path, source_sha: str) -> TrialArtifact:
    """Require one wheel and its three mutually bound identity manifests."""
    if _SOURCE_SHA.fullmatch(source_sha) is None:
        raise TrialSeedError("expected source SHA is invalid")
    seed = Path(seed_directory)
    try:
        status = os.lstat(seed)
        names = os.listdir(seed)
    except OSError as exc:
        raise TrialSeedError("trial seed cannot be inspected") from exc
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise TrialSeedError("trial seed directory is unsafe")
    wheels = [name for name in names if name.endswith(".whl")]
    if len(wheels) != 1 or set(names) != _FIXED_NAMES | {wheels[0]}:
        raise TrialSeedError("trial seed asset set is invalid")
    try:
        artifact = load_trial_artifact(
            wheel=seed / wheels[0],
            trial_manifest=seed / "TRIAL-MANIFEST.json",
            source_manifest=seed / "SOURCE-MANIFEST.json",
            staging_manifest=seed / "STAGING-MANIFEST.json",
        )
    except (OSError, ResolutionError, ValueError) as exc:
        raise TrialSeedError("trial seed identity is invalid") from exc
    if artifact.source_sha != source_sha:
        raise TrialSeedError("trial seed source SHA does not match P")
    return artifact


def main(argv: Sequence[str] | None = None) -> int:
    """Verify one seed selected explicitly on the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", required=True, type=Path)
    parser.add_argument("--source-sha", required=True)
    arguments = parser.parse_args(argv)
    try:
        artifact = verify_trial_seed(arguments.seed, arguments.source_sha)
    except TrialSeedError as exc:
        print(f"verify_trial_seed: error: {exc}", file=sys.stderr)
        return 1
    print(artifact.wheel_filename)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

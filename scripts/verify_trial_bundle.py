"""Verify one complete, explicit M2 trial bundle without modifying it."""

from __future__ import annotations

import argparse
import os
import stat
import sys
from collections.abc import Sequence
from pathlib import Path

try:  # Support both ``python scripts/...`` and ``import scripts...``.
    from .assemble_trial_bundle import (
        TrialBundleError,
        _read_regular_bytes,
        verify_trial_bundle_bytes,
    )
except ImportError:  # pragma: no cover - exercised by direct CLI invocation.
    from assemble_trial_bundle import (  # type: ignore[no-redef]
        TrialBundleError,
        _read_regular_bytes,
        verify_trial_bundle_bytes,
    )


def verify_trial_bundle(bundle_directory: Path) -> dict[str, object]:
    """Read one bundle directory exactly as named; never select a latest asset."""
    return verify_trial_bundle_bytes(read_trial_bundle_bytes(bundle_directory))


def read_trial_bundle_bytes(bundle_directory: Path) -> dict[str, bytes]:
    """Capture every safe regular file in a bundle directory exactly once."""
    bundle = Path(bundle_directory)
    try:
        status = os.lstat(bundle)
    except OSError as exc:
        raise TrialBundleError("bundle directory cannot be inspected") from exc
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise TrialBundleError("bundle directory is unsafe")
    try:
        names = os.listdir(bundle)
    except OSError as exc:
        raise TrialBundleError("bundle directory cannot be listed") from exc
    return {name: _read_regular_bytes(bundle / name, "bundle asset") for name in names}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Verify the selected bundle and print its build ID on success."""
    arguments = _parser().parse_args(argv)
    try:
        manifest = verify_trial_bundle(arguments.bundle)
    except TrialBundleError as exc:
        print(f"verify_trial_bundle: error: {exc}", file=sys.stderr)
        return 1
    build = manifest.get("build")
    if not isinstance(build, dict) or not isinstance(build.get("id"), str):
        print(
            "verify_trial_bundle: error: verified manifest is invalid", file=sys.stderr
        )
        return 1
    print(build["id"])
    return 0


if __name__ == "__main__":  # pragma: no cover - direct CLI invocation.
    raise SystemExit(main())

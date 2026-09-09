"""Verify a trial release ZIP and its SHA256 sidecar without extraction."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

try:
    from .package_trial_release import TrialReleaseError, verify_trial_release
except ImportError:  # pragma: no cover
    from package_trial_release import (  # type: ignore
        TrialReleaseError,
        verify_trial_release,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the archive verification CLI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--sidecar", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        manifest = verify_trial_release(args.archive, args.sidecar)
    except (OSError, TrialReleaseError) as exc:
        print(f"verify_trial_release: error: {exc}", file=sys.stderr)
        return 1
    build = cast(dict[str, object], manifest["build"])
    print(cast(str, build["id"]))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

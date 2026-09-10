"""Build a non-release, one-command physical platform qualification kit."""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import stat
import sys
from collections.abc import Sequence
from pathlib import Path

try:
    from .package_trial_release import verify_trial_release_directory
    from .trial_targets import TrialTargetError, trial_target
except ImportError:  # pragma: no cover
    from package_trial_release import (  # type: ignore[no-redef]
        verify_trial_release_directory,
    )
    from trial_targets import TrialTargetError, trial_target  # type: ignore[no-redef]


class QualificationKitError(RuntimeError):
    """Raised when a qualification kit cannot preserve the candidate identity."""


_KIT_SCRIPTS = (
    "assemble_trial_bundle.py",
    "build_trial_wheel.py",
    "capture_trial_resolution.py",
    "export_release_source.py",
    "package_trial_release.py",
    "release_source_manifest.py",
    "run_platform_qualification.py",
    "run_trial_technical_gate.py",
    "trial_targets.py",
    "verify_platform_qualification.py",
    "verify_public_source.py",
    "verify_trial_bundle.py",
)


def _runner_script(target_id: str, archive_name: str, sidecar_name: str) -> str:
    target = shlex.quote(target_id)
    archive = shlex.quote(archive_name)
    sidecar = shlex.quote(sidecar_name)
    return f"""#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
command -v conda >/dev/null 2>&1 || {{ echo "conda is required" >&2; exit 1; }}
QUALIFICATION_TEMP="$(mktemp -d "${{TMPDIR:-/tmp}}/gwexpy-qualification.XXXXXXXX")"
cleanup() {{ rm -rf -- "$QUALIFICATION_TEMP"; }}
trap cleanup EXIT HUP INT TERM

conda create --prefix "$QUALIFICATION_TEMP/conda" --yes --quiet python=3.12 pip
conda run --no-capture-output --prefix "$QUALIFICATION_TEMP/conda" \\
  python "$SCRIPT_DIR/run_platform_qualification.py" \\
  --trial-target {target} \\
  --archive "$SCRIPT_DIR/{archive}" \\
  --sidecar "$SCRIPT_DIR/{sidecar}" \\
  --output "$SCRIPT_DIR/results" \\
  --work-root "$QUALIFICATION_TEMP/work"
"""


def build_qualification_kit(
    *,
    release_directory: Path,
    output_directory: Path,
    target_id: str,
    scripts_directory: Path | None = None,
) -> Path:
    """Bind one candidate release pair to a self-contained operator kit."""
    try:
        target = trial_target(target_id)
    except TrialTargetError as exc:
        raise QualificationKitError(str(exc)) from exc
    try:
        manifest = verify_trial_release_directory(release_directory)
    except Exception as exc:
        raise QualificationKitError("candidate release verification failed") from exc
    target_record = manifest.get("target")
    if not isinstance(target_record, dict) or target_record.get("id") != target.id:
        raise QualificationKitError("candidate release target does not match kit")
    output = Path(os.path.abspath(output_directory))
    if output.exists() or output.is_symlink():
        raise QualificationKitError("qualification kit output must be fresh")
    source_scripts = (
        Path(__file__).resolve().parent
        if scripts_directory is None
        else Path(scripts_directory).resolve(strict=True)
    )
    release_files = sorted(
        Path(release_directory).iterdir(), key=lambda path: path.name
    )
    archive = next(path for path in release_files if path.name.endswith(".zip"))
    sidecar = next(path for path in release_files if path.name.endswith(".zip.sha256"))
    try:
        output.mkdir(mode=0o700, parents=True)
        for source in (archive, sidecar):
            shutil.copyfile(source, output / source.name)
        for name in _KIT_SCRIPTS:
            source = source_scripts / name
            if source.is_symlink() or not source.is_file():
                raise QualificationKitError(f"qualification script is missing: {name}")
            shutil.copyfile(source, output / name)
        runner = output / "run-qualification.sh"
        runner.write_text(
            _runner_script(target.id, archive.name, sidecar.name), encoding="utf-8"
        )
        runner.chmod(
            stat.S_IRUSR
            | stat.S_IWUSR
            | stat.S_IXUSR
            | stat.S_IRGRP
            | stat.S_IXGRP
            | stat.S_IROTH
            | stat.S_IXOTH
        )
    except OSError as exc:
        raise QualificationKitError("qualification kit could not be written") from exc
    return output


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--trial-target", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Build a qualification kit from command-line arguments."""
    arguments = _parser().parse_args(argv)
    try:
        output = build_qualification_kit(
            release_directory=arguments.release,
            output_directory=arguments.output,
            target_id=arguments.trial_target,
        )
    except QualificationKitError as exc:
        print(f"build_qualification_kit: error: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

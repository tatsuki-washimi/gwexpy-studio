"""Create and verify the deterministic outer trial release archive."""

from __future__ import annotations

import argparse
import hashlib
import stat
import sys
import zipfile
from collections.abc import Sequence
from pathlib import Path

try:
    from . import verify_trial_bundle as _bundle_verifier
    from .assemble_trial_bundle import TrialBundleError, verify_trial_bundle_bytes
except ImportError:  # pragma: no cover
    import verify_trial_bundle as _bundle_verifier  # type: ignore
    from assemble_trial_bundle import TrialBundleError  # type: ignore


class TrialReleaseError(RuntimeError):
    """Raised when an outer release archive is not safe and complete."""


def package_trial_release(
    bundle_directory: Path, output_directory: Path
) -> tuple[Path, Path]:
    """Verify a flat bundle and write its deterministic archive and sidecar."""
    try:
        manifest = _bundle_verifier.verify_trial_bundle(Path(bundle_directory))
    except TrialBundleError as exc:
        raise TrialReleaseError("internal bundle verification failed") from exc
    build = manifest.get("build")
    if not isinstance(build, dict) or not isinstance(build.get("id"), str):
        raise TrialReleaseError("verified bundle has no build ID")
    build_id = build["id"]
    root = f"gwexpy-studio-trial-{build_id}"
    output = Path(output_directory)
    if output.exists():
        if not output.is_dir() or any(output.iterdir()):
            raise TrialReleaseError("output directory must be fresh and empty")
    else:
        output.mkdir(parents=True)
    files = {path.name: path.read_bytes() for path in Path(bundle_directory).iterdir()}
    archive_path = output / f"{root}.zip"
    sidecar_path = output / f"{archive_path.name}.sha256"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in sorted(files, key=lambda item: item.encode("utf-8")):
            info = zipfile.ZipInfo(
                f"{root}/{name}", date_time=(1980, 1, 1, 0, 0, 0)
            )
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_STORED
            info.extra = b""
            info.comment = b""
            archive.writestr(info, files[name])
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    sidecar_path.write_text(f"{digest}  {archive_path.name}\n", encoding="ascii")
    return archive_path, sidecar_path


def verify_trial_release(archive_path: Path, sidecar_path: Path) -> dict[str, object]:
    """Verify sidecar, outer archive safety, and delegated internal bytes."""
    archive = Path(archive_path)
    sidecar = Path(sidecar_path)
    if not archive.is_file() or not sidecar.is_file():
        raise TrialReleaseError("archive or sidecar is missing")
    expected_name = archive.name
    expected_line = (
        f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {expected_name}\n"
    )
    try:
        if sidecar.read_text(encoding="ascii") != expected_line:
            raise TrialReleaseError("sidecar digest or filename is incorrect")
    except (OSError, UnicodeDecodeError) as exc:
        raise TrialReleaseError("sidecar is invalid") from exc
    try:
        with zipfile.ZipFile(archive) as contents:
            infos = contents.infolist()
            if not infos:
                raise TrialReleaseError("archive is empty")
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise TrialReleaseError("archive contains duplicate entries")
            root = names[0].split("/", 1)[0]
            if not root or not root.startswith("gwexpy-studio-trial-"):
                raise TrialReleaseError("archive root is invalid")
            files: dict[str, bytes] = {}
            for info in infos:
                name = info.filename
                if not name or "\\" in name or name.startswith("/"):
                    raise TrialReleaseError("archive member path is unsafe")
                parts = name.split("/")
                if ".." in parts or len(parts) != 2 or parts[0] != root or not parts[1]:
                    raise TrialReleaseError("archive member path is unsafe")
                mode = (info.external_attr >> 16) & 0o777777
                if stat.S_IFMT(mode) != stat.S_IFREG or mode != (stat.S_IFREG | 0o644):
                    raise TrialReleaseError("archive member is not a regular 0644 file")
                if info.extra or info.comment or info.is_dir():
                    raise TrialReleaseError("archive member metadata is invalid")
                files[parts[1]] = contents.read(info)
    except (OSError, zipfile.BadZipFile) as exc:
        raise TrialReleaseError("archive cannot be read") from exc
    try:
        manifest = verify_trial_bundle_bytes(files)
    except TrialBundleError as exc:
        raise TrialReleaseError("internal bundle files are invalid") from exc
    build = manifest.get("build")
    if not isinstance(build, dict) or not isinstance(build.get("id"), str):
        raise TrialReleaseError("verified bundle has no build ID")
    expected_root = f"gwexpy-studio-trial-{build['id']}"
    if root != expected_root or archive.name != f"{expected_root}.zip":
        raise TrialReleaseError("archive root or filename does not match build ID")
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the packaging CLI."""
    args = _parser().parse_args(argv)
    try:
        archive, sidecar = package_trial_release(args.bundle, args.output)
    except (OSError, TrialReleaseError) as exc:
        print(f"package_trial_release: error: {exc}", file=sys.stderr)
        return 1
    print(archive)
    print(sidecar)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

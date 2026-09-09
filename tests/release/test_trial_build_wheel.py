"""Contracts for the staged M2 trial-wheel build boundary."""

from __future__ import annotations

import hashlib
import importlib
import json
import subprocess
import zipfile
from base64 import urlsafe_b64encode
from pathlib import Path

import pytest
from packaging.version import Version

from scripts.export_release_source import export_release_source
from scripts.release_source_manifest import (
    ManifestEntry,
    ReleaseSourceManifest,
    load_policy,
    manifest_digest,
    read_manifest,
)
from scripts.verify_public_source import scan_public_checkout

pytestmark = pytest.mark.unit

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_VERSION_PATH = Path("src/gwexpy_studio/_version.py")
_TRIAL_BUILD_PATH = Path("src/gwexpy_studio/assets/trial-build.json")
_CAPABILITY_PATH = Path("src/gwexpy_studio/assets/io-capabilities.json")


def _builder():
    return importlib.import_module("scripts.build_trial_wheel")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _canonical_json(document: object) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _rewrite_wheel_members(wheel: Path, members: dict[str, bytes]) -> None:
    """Rewrite a fixture wheel while regenerating its complete RECORD."""
    record_name = next(name for name in members if name.endswith(".dist-info/RECORD"))
    rows = []
    for name, content in members.items():
        if name == record_name:
            continue
        digest = urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=")
        rows.append(f"{name},sha256={digest.decode('ascii')},{len(content)}")
    members[record_name] = ("\n".join((*rows, f"{record_name},,")) + "\n").encode(
        "utf-8"
    )
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)


def _approved_wheel_metadata(builder: object, version: str) -> bytes:
    """Render the fixed policy in a form suitable for parser-only regressions."""
    runtime = getattr(builder, "_TRIAL_PROJECT_DEPENDENCIES")
    optional = getattr(builder, "_TRIAL_PROJECT_OPTIONAL_DEPENDENCIES")
    lines = [
        "Metadata-Version: 2.4",
        "Name: gwexpy-studio",
        f"Version: {version}",
        "Requires-Python: <3.13,>=3.12",
        "License-File: LICENSE",
        *(f"Requires-Dist: {value}" for value in runtime),
        "Provides-Extra: dev",
        *(
            f'Requires-Dist: {value}; extra == "dev"'
            for value in optional["dev"]
        ),
        "Dynamic: license-file",
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def _git(root: Path, *arguments: str) -> str:
    """Run one fixture-local Git command and return its standard output."""
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def _clone_checkout(source: Path, destination: Path) -> None:
    """Clone one committed fixture so a test can alter its pinned P tree."""
    completed = subprocess.run(
        ["git", "clone", "--quiet", str(source), str(destination)],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    _git(destination, "config", "user.email", "trial@example.invalid")
    _git(destination, "config", "user.name", "Trial Builder Test")


@pytest.fixture(scope="module")
def public_checkout(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, str]:
    """Create one committed, canonical public P fixture for build tests."""
    allowlist = REPOSITORY_ROOT / "packaging/release-source-allowlist.txt"
    root = tmp_path_factory.mktemp("public-p")
    checkout = root / "checkout"
    export_release_source(
        REPOSITORY_ROOT,
        checkout,
        load_policy(allowlist),
        root / "public-p-manifest.json",
    )
    _git(checkout, "init", "--quiet")
    _git(checkout, "config", "user.email", "trial@example.invalid")
    _git(checkout, "config", "user.name", "Trial Builder Test")
    _git(checkout, "add", "--all")
    _git(checkout, "commit", "--quiet", "-m", "Canonical public P")
    return checkout, _git(checkout, "rev-parse", "--verify", "HEAD")


def test_trial_identity_is_derived_and_rejects_unsafe_inputs() -> None:
    """Build identity comes only from validated CI inputs."""
    builder = _builder()
    source_sha = "a" * 40

    identity = builder.derive_trial_identity(
        source_sha=source_sha,
        utc_date="20260907",
        run=12,
        attempt=3,
    )

    assert identity.build_id == "P-aaaaaaa-20260907-r12-a3"
    assert identity.version == "0.1.0a1+trial.p.gaaaaaaa.20260907.r12.a3"
    assert identity.source_sha == source_sha

    for kwargs, label in (
        ({"source_sha": "A" * 40}, "source SHA"),
        ({"source_sha": "a" * 39}, "source SHA"),
        ({"utc_date": "20261301"}, "UTC date"),
        ({"utc_date": "2026097"}, "UTC date"),
        ({"run": 0}, "run"),
        ({"attempt": 0}, "attempt"),
    ):
        arguments = {
            "source_sha": source_sha,
            "utc_date": "20260907",
            "run": 1,
            "attempt": 1,
        }
        arguments.update(kwargs)
        with pytest.raises(builder.TrialBuildError, match=label):
            builder.derive_trial_identity(**arguments)


def test_trial_identity_preserves_an_all_numeric_short_sha() -> None:
    """PEP 440 normalization must not erase a leading zero from the source SHA."""
    builder = _builder()
    source_sha = "0854741" + "a" * 33

    identity = builder.derive_trial_identity(
        source_sha=source_sha,
        utc_date="20260907",
        run=1,
        attempt=1,
    )

    assert identity.build_id == "P-0854741-20260907-r1-a1"
    assert identity.version == "0.1.0a1+trial.p.g0854741.20260907.r1.a1"
    assert str(Version(identity.version)) == identity.version


def test_version_delta_rejects_a_nontrial_base_version() -> None:
    """A build cannot relabel an arbitrary source version as a trial wheel."""
    builder = _builder()
    identity = builder.derive_trial_identity(
        source_sha="a" * 40,
        utc_date="20260907",
        run=1,
        attempt=1,
    )

    with pytest.raises(builder.TrialBuildError, match="base version"):
        builder.make_version_delta(
            b'__version__ = "0.1.0a2"\n', identity.version
        )


@pytest.mark.parametrize(
    ("suffix", "message"),
    (
        (
            b"requires-dist: gwexpy>=0.2.0,<0.3.0\n",
            "Requires-Dist",
        ),
        (
            b"Requires-Dist: gwexpy>=0.2.0,<0.3.0\n injected\n",
            "folded",
        ),
        (
            b"Requires-Dist: injected-review-dependency; "
            b'extra == "dev" or os_name == "x"\n',
            "Requires-Dist",
        ),
    ),
)
def test_trial_wheel_metadata_rejects_ambiguous_dependency_headers(
    suffix: bytes,
    message: str,
) -> None:
    """Relevant Core Metadata headers cannot hide alternate resolver inputs."""
    builder = _builder()
    identity = builder.derive_trial_identity(
        source_sha="a" * 40,
        utc_date="20260907",
        run=1,
        attempt=1,
    )

    with pytest.raises(builder.TrialBuildError, match=message):
        builder._verify_trial_wheel_metadata(
            _approved_wheel_metadata(builder, identity.version) + suffix,
            identity,
        )


def test_source_policy_parses_one_captured_allowlist_read(
    public_checkout: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A policy swap cannot separate the scanned rules from the manifest hash."""
    builder = _builder()
    source, _ = public_checkout
    allowlist = source / "packaging/release-source-allowlist.txt"
    approved = allowlist.read_bytes()
    approved_policy = load_policy(allowlist)
    untrusted = approved + b"\ninclude unreviewed-private-input/**\n"
    original_load_policy = load_policy
    calls = 0

    def reread_as_untrusted(path: Path):
        nonlocal calls
        calls += 1
        assert path == allowlist
        allowlist.write_bytes(untrusted)
        try:
            return original_load_policy(path)
        finally:
            allowlist.write_bytes(approved)

    monkeypatch.setattr(builder, "load_policy", reread_as_untrusted, raising=False)

    policy, captured = builder._source_policy(source)

    assert calls == 0
    assert captured == approved
    assert policy == approved_policy
    assert _git(source, "status", "--porcelain=v1") == ""


def test_trial_build_uses_the_git_tree_not_a_transient_worktree_mutation(
    tmp_path: Path,
    public_checkout: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A temporary P edit cannot become a wheel labeled with P's HEAD SHA."""
    builder = _builder()
    source, source_sha = public_checkout
    version_path = source / _VERSION_PATH
    approved = version_path.read_bytes()
    transient = approved + b"# transient worktree mutation\n"
    output = tmp_path / "git-tree-output"
    original_identity_check = builder._verify_source_git_identity
    identity_checks = 0

    def mutate_between_identity_checks(root: Path, sha: str) -> None:
        nonlocal identity_checks
        identity_checks += 1
        if identity_checks == 2:
            version_path.write_bytes(approved)
        original_identity_check(root, sha)
        if identity_checks == 1:
            version_path.write_bytes(transient)

    monkeypatch.setattr(
        builder,
        "_verify_source_git_identity",
        mutate_between_identity_checks,
    )

    try:
        result = builder.build_trial_wheel(
            source_root=source,
            output_directory=output,
            source_sha=source_sha,
            utc_date="20260907",
            run=1,
            attempt=1,
        )
    finally:
        version_path.write_bytes(approved)

    with zipfile.ZipFile(output / result.wheel_filename) as archive:
        built_version = archive.read("gwexpy_studio/_version.py")

    assert b"transient worktree mutation" not in built_version
    assert _git(source, "status", "--porcelain=v1") == ""


def test_trial_build_uses_raw_git_blobs_despite_archive_attributes(
    tmp_path: Path,
    public_checkout: tuple[Path, str],
) -> None:
    """P's M must bind raw committed bytes, not a Git archive projection."""
    builder = _builder()
    fixture_source, _ = public_checkout
    source = tmp_path / "attribute-public-p"
    _clone_checkout(fixture_source, source)
    document = source / "docs/development.md"
    raw_document = b"raw $Format:%H$\n"
    document.write_bytes(raw_document)
    (source / ".gitattributes").write_text(
        "docs/development.md export-subst\n",
        encoding="utf-8",
    )
    allowlist = source / "packaging/release-source-allowlist.txt"
    allowlist.write_bytes(allowlist.read_bytes() + b"\ninclude .gitattributes\n")
    _git(source, "add", "--all")
    _git(source, "commit", "--quiet", "-m", "Archive attribute fixture")
    source_sha = _git(source, "rev-parse", "--verify", "HEAD")
    output = tmp_path / "raw-blob-output"

    builder.build_trial_wheel(
        source_root=source,
        output_directory=output,
        source_sha=source_sha,
        utc_date="20260907",
        run=1,
        attempt=1,
    )

    source_manifest = read_manifest(output / "SOURCE-MANIFEST.json")
    document_entry = next(
        entry
        for entry in source_manifest.entries
        if entry.path == "docs/development.md"
    )

    assert document_entry.sha256 == _sha256(raw_document)
    assert _git(source, "status", "--porcelain=v1") == ""


def test_trial_build_ignores_untracked_git_archive_attributes(
    tmp_path: Path,
    public_checkout: tuple[Path, str],
) -> None:
    """Untracked Git attributes must not alter the source identity of P."""
    builder = _builder()
    fixture_source, _ = public_checkout
    source = tmp_path / "local-attribute-public-p"
    _clone_checkout(fixture_source, source)
    source_sha = _git(source, "rev-parse", "--verify", "HEAD")
    raw_document = (source / "docs/development.md").read_bytes()
    (source / ".git/info/attributes").write_text(
        "docs/development.md export-ignore\n",
        encoding="utf-8",
    )
    output = tmp_path / "local-attribute-output"

    builder.build_trial_wheel(
        source_root=source,
        output_directory=output,
        source_sha=source_sha,
        utc_date="20260907",
        run=1,
        attempt=1,
    )

    source_manifest = read_manifest(output / "SOURCE-MANIFEST.json")
    document_entry = next(
        entry
        for entry in source_manifest.entries
        if entry.path == "docs/development.md"
    )

    assert document_entry.sha256 == _sha256(raw_document)
    assert _git(source, "status", "--porcelain=v1") == ""


def test_staged_trial_build_preserves_p_and_binds_the_wheel(
    tmp_path: Path,
    public_checkout: tuple[Path, str],
) -> None:
    """Only the reviewed build-time delta separates a trial wheel from P."""
    builder = _builder()
    source, source_sha = public_checkout
    short_sha = source_sha[:7]
    build_id = f"P-{short_sha}-20260907-r2-a3"
    version = f"0.1.0a1+trial.p.g{short_sha}.20260907.r2.a3"
    output = tmp_path / "trial-output"
    allowlist = source / "packaging/release-source-allowlist.txt"
    source_version = source / _VERSION_PATH
    source_policy = source / "packaging/trial-io-capabilities.json"
    source_before = source_version.read_bytes()

    assert not (source / _TRIAL_BUILD_PATH).exists()
    assert not (source / _CAPABILITY_PATH).exists()

    result = builder.build_trial_wheel(
        source_root=source,
        output_directory=output,
        source_sha=source_sha,
        utc_date="20260907",
        run=2,
        attempt=3,
    )

    wheel = output / result.wheel_filename
    source_manifest_path = output / "SOURCE-MANIFEST.json"
    staging_manifest_path = output / "STAGING-MANIFEST.json"
    trial_manifest_path = output / "TRIAL-MANIFEST.json"
    assert {path.name for path in output.iterdir()} == {
        wheel.name,
        source_manifest_path.name,
        staging_manifest_path.name,
        trial_manifest_path.name,
    }
    assert wheel.name.endswith("-py3-none-any.whl")

    source_manifest = read_manifest(source_manifest_path)
    staging_manifest = read_manifest(staging_manifest_path)
    trial_manifest = json.loads(trial_manifest_path.read_text(encoding="utf-8"))
    policy_bytes = source_policy.read_bytes()
    expected_trial_build = _canonical_json(
        {
            "build_id": build_id,
            "schema": 1,
            "source_manifest_sha256": manifest_digest(source_manifest),
            "source_sha": source_sha,
            "version": version,
        }
    )

    assert source_version.read_bytes() == source_before
    assert not (source / _TRIAL_BUILD_PATH).exists()
    assert not (source / _CAPABILITY_PATH).exists()
    assert source_manifest == scan_public_checkout(source, load_policy(allowlist))
    assert "packaging/release-source-allowlist.txt" in {
        entry.path for entry in source_manifest.entries
    }
    allowlist_entry = next(
        entry
        for entry in source_manifest.entries
        if entry.path == "packaging/release-source-allowlist.txt"
    )
    assert allowlist_entry.file_type == "file"
    assert allowlist_entry.sha256 == _sha256(allowlist.read_bytes())
    assert trial_manifest == {
        "build_id": build_id,
        "generated_files": [
            {
                "path": str(_VERSION_PATH),
                "sha256": _sha256(
                    b'"""Package version for GWexpy Studio."""\n\n'
                    + f'__version__ = "{version}"\n'.encode("ascii")
                ),
            },
            {
                "path": str(_TRIAL_BUILD_PATH),
                "sha256": _sha256(expected_trial_build),
            },
            {"path": str(_CAPABILITY_PATH), "sha256": _sha256(policy_bytes)},
        ],
        "schema": 1,
        "source_manifest_sha256": manifest_digest(source_manifest),
        "source_sha": source_sha,
        "staging_manifest_sha256": manifest_digest(staging_manifest),
        "version": version,
        "wheel_filename": wheel.name,
        "wheel_sha256": _sha256(wheel.read_bytes()),
    }
    assert {
        entry.path for entry in staging_manifest.entries
    }.issuperset({str(_VERSION_PATH), str(_TRIAL_BUILD_PATH), str(_CAPABILITY_PATH)})

    serialized_trial = trial_manifest_path.read_text(encoding="utf-8")
    assert str(source) not in serialized_trial
    assert str(tmp_path) not in serialized_trial

    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        version_bytes = archive.read("gwexpy_studio/_version.py")
        embedded_trial = archive.read("gwexpy_studio/assets/trial-build.json")
        embedded_capabilities = archive.read(
            "gwexpy_studio/assets/io-capabilities.json"
        )
        wheel_metadata = archive.read(
            next(name for name in names if name.endswith(".dist-info/WHEEL"))
        ).decode("utf-8")

    assert version_bytes == (
        b'"""Package version for GWexpy Studio."""\n\n'
        + f'__version__ = "{version}"\n'.encode("ascii")
    )
    assert embedded_trial == expected_trial_build
    assert embedded_capabilities == policy_bytes
    assert "Root-Is-Purelib: true" in wheel_metadata
    assert "Tag: py3-none-any" in wheel_metadata


def test_trial_build_rejects_a_mismatched_git_head(
    tmp_path: Path,
    public_checkout: tuple[Path, str],
) -> None:
    """A caller cannot assign a source SHA that differs from P's Git HEAD."""
    builder = _builder()
    source, source_sha = public_checkout
    wrong_sha = "0" * 40 if source_sha != "0" * 40 else "1" * 40
    policy = load_policy(source / "packaging/release-source-allowlist.txt")
    source_before = scan_public_checkout(source, policy)

    with pytest.raises(
        builder.TrialBuildError,
        match="source SHA does not match Git HEAD",
    ):
        builder.build_trial_wheel(
            source_root=source,
            output_directory=tmp_path / "mismatched-head-output",
            source_sha=wrong_sha,
            utc_date="20260907",
            run=1,
            attempt=1,
        )

    assert scan_public_checkout(
        source, load_policy(source / "packaging/release-source-allowlist.txt")
    ) == source_before
    assert not (tmp_path / "mismatched-head-output").exists()


def test_trial_build_requires_a_git_checkout(tmp_path: Path) -> None:
    """A source tree without a committed P cannot acquire a trial identity."""
    builder = _builder()
    source = tmp_path / "not-a-checkout"
    source.mkdir()

    with pytest.raises(builder.TrialBuildError, match="Git checkout"):
        builder.build_trial_wheel(
            source_root=source,
            output_directory=tmp_path / "not-a-checkout-output",
            source_sha="a" * 40,
            utc_date="20260907",
            run=1,
            attempt=1,
        )


def test_trial_build_rejects_a_git_subdirectory(
    tmp_path: Path,
    public_checkout: tuple[Path, str],
) -> None:
    """P must be the checkout root rather than a directory within it."""
    builder = _builder()
    source, source_sha = public_checkout
    output = tmp_path / "subdirectory-output"

    with pytest.raises(builder.TrialBuildError, match="Git checkout root"):
        builder.build_trial_wheel(
            source_root=source / "src",
            output_directory=output,
            source_sha=source_sha,
            utc_date="20260907",
            run=1,
            attempt=1,
        )

    assert not output.exists()


def test_source_identity_ignores_ambient_git_overrides(
    tmp_path: Path,
    public_checkout: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ambient Git repository overrides cannot redirect the identity lookup."""
    builder = _builder()
    source, source_sha = public_checkout
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "untrusted-git-dir"))

    builder._verify_source_git_identity(source, source_sha)


def test_trial_build_rejects_a_dirty_checkout(
    tmp_path: Path,
    public_checkout: tuple[Path, str],
) -> None:
    """P must not contain tracked or untracked changes before a trial build."""
    builder = _builder()
    source, source_sha = public_checkout
    readme = source / "README.md"
    source_before = readme.read_bytes()
    output = tmp_path / "dirty-checkout-output"
    readme.write_bytes(source_before + b"\n")

    try:
        with pytest.raises(builder.TrialBuildError, match="Git checkout is not clean"):
            builder.build_trial_wheel(
                source_root=source,
                output_directory=output,
                source_sha=source_sha,
                utc_date="20260907",
                run=1,
                attempt=1,
            )
    finally:
        readme.write_bytes(source_before)

    assert not output.exists()
    assert _git(source, "status", "--porcelain=v1") == ""


def test_trial_wheel_cli_rejects_an_external_allowlist(tmp_path: Path) -> None:
    """The command accepts only the reviewed allowlist stored in P."""
    builder = _builder()

    with pytest.raises(SystemExit) as error:
        builder.main(
            [
                "--source",
                str(tmp_path / "source"),
                "--output",
                str(tmp_path / "output"),
                "--source-sha",
                "a" * 40,
                "--utc-date",
                "20260907",
                "--run",
                "1",
                "--attempt",
                "1",
                "--allowlist",
                str(tmp_path / "untrusted-policy.txt"),
            ]
        )

    assert error.value.code == 2


def test_trial_build_rejects_an_ancestor_symlink_output_alias_before_staging(
    tmp_path: Path,
    public_checkout: tuple[Path, str],
) -> None:
    """A lexical external output cannot resolve physically inside P."""
    builder = _builder()
    source, source_sha = public_checkout
    alias = tmp_path / "source-alias"
    alias.symlink_to(source, target_is_directory=True)
    output = alias / "src" / "trial-output"
    policy = load_policy(source / "packaging/release-source-allowlist.txt")
    source_before = scan_public_checkout(source, policy)

    with pytest.raises(
        builder.TrialBuildError,
        match="output directory must be outside source P",
    ):
        builder.build_trial_wheel(
            source_root=source,
            output_directory=output,
            source_sha=source_sha,
            utc_date="20260907",
            run=1,
            attempt=1,
        )

    assert not output.exists()
    assert not tuple((source / "src").glob(".trial-wheel-*"))
    assert scan_public_checkout(source, policy) == source_before


def test_trial_build_rejects_postbuild_staging_mutation(
    tmp_path: Path,
    public_checkout: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backend mutation after assembly cannot change the bound staging tree."""
    builder = _builder()
    source, source_sha = public_checkout
    output = tmp_path / "postbuild-mutation-output"
    policy = load_policy(source / "packaging/release-source-allowlist.txt")
    source_before = scan_public_checkout(source, policy)
    original_build = builder._build_one_wheel

    def mutate_after_build(staging: Path, artifacts: Path, identity: object) -> Path:
        wheel = original_build(staging, artifacts, identity)
        (staging / _TRIAL_BUILD_PATH).write_bytes(b'{"tampered":true}\n')
        return wheel

    monkeypatch.setattr(builder, "_build_one_wheel", mutate_after_build)

    with pytest.raises(builder.TrialBuildError, match="unexpected staging delta"):
        builder.build_trial_wheel(
            source_root=source,
            output_directory=output,
            source_sha=source_sha,
            utc_date="20260907",
            run=1,
            attempt=1,
        )

    assert not output.exists()
    assert scan_public_checkout(source, policy) == source_before


def test_trial_build_rejects_project_runtime_dependency_drift(
    tmp_path: Path,
    public_checkout: tuple[Path, str],
) -> None:
    """A new source M cannot silently expand the reviewed resolver inputs."""
    builder = _builder()
    source, _source_sha = public_checkout
    altered = tmp_path / "project-metadata-drift"
    _clone_checkout(source, altered)
    project = altered / "pyproject.toml"
    project.write_text(
        project.read_text(encoding="utf-8").replace(
            '    "matplotlib>=3.10.0,<4.0.0",\n',
            '    "matplotlib>=3.10.0,<4.0.0",\n'
            '    "injected-review-dependency",\n',
        ),
        encoding="utf-8",
    )
    _git(altered, "add", "pyproject.toml")
    _git(altered, "commit", "--quiet", "-m", "inject dependency")
    output = tmp_path / "project-metadata-drift-output"

    with pytest.raises(builder.TrialBuildError, match="runtime dependencies"):
        builder.build_trial_wheel(
            source_root=altered,
            output_directory=output,
            source_sha=_git(altered, "rev-parse", "HEAD"),
            utc_date="20260907",
            run=1,
            attempt=1,
        )

    assert not output.exists()


def test_trial_build_rejects_backend_injected_package_member(
    tmp_path: Path,
    public_checkout: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wheel backend cannot add unreviewed code after the staging scan."""
    builder = _builder()
    source, source_sha = public_checkout
    output = tmp_path / "injected-package-output"
    policy = load_policy(source / "packaging/release-source-allowlist.txt")
    source_before = scan_public_checkout(source, policy)
    original_build = builder._build_one_wheel

    def inject_after_build(staging: Path, artifacts: Path, identity: object) -> Path:
        wheel = original_build(staging, artifacts, identity)
        with zipfile.ZipFile(wheel) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        members["gwexpy_studio/backdoor.py"] = b"raise RuntimeError\n"
        _rewrite_wheel_members(wheel, members)
        return wheel

    monkeypatch.setattr(builder, "_build_one_wheel", inject_after_build)

    with pytest.raises(builder.TrialBuildError, match="payload"):
        builder.build_trial_wheel(
            source_root=source,
            output_directory=output,
            source_sha=source_sha,
            utc_date="20260907",
            run=1,
            attempt=1,
        )

    assert not output.exists()
    assert scan_public_checkout(source, policy) == source_before


def test_trial_build_rejects_backend_injected_metadata_dependency(
    tmp_path: Path,
    public_checkout: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backend cannot add a resolver input after source-policy validation."""
    builder = _builder()
    source, source_sha = public_checkout
    output = tmp_path / "injected-metadata-output"
    policy = load_policy(source / "packaging/release-source-allowlist.txt")
    source_before = scan_public_checkout(source, policy)
    original_build = builder._build_one_wheel

    def inject_after_build(staging: Path, artifacts: Path, identity: object) -> Path:
        wheel = original_build(staging, artifacts, identity)
        with zipfile.ZipFile(wheel) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        metadata = next(
            name for name in members if name.endswith(".dist-info/METADATA")
        )
        members[metadata] += b"Requires-Dist: injected-review-dependency\n"
        _rewrite_wheel_members(wheel, members)
        return wheel

    monkeypatch.setattr(builder, "_build_one_wheel", inject_after_build)

    with pytest.raises(builder.TrialBuildError, match="metadata"):
        builder.build_trial_wheel(
            source_root=source,
            output_directory=output,
            source_sha=source_sha,
            utc_date="20260907",
            run=1,
            attempt=1,
        )

    assert not output.exists()
    assert scan_public_checkout(source, policy) == source_before


def test_trial_build_rejects_wheel_mutated_after_verification(
    tmp_path: Path,
    public_checkout: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Publication must not use a wheel changed after its validator accepted it."""
    builder = _builder()
    source, source_sha = public_checkout
    output = tmp_path / "postverify-wheel-mutation-output"
    original_verify_wheel = builder._verify_wheel

    def mutate_after_verification(
        wheel: Path,
        identity: object,
        generated_files: object,
        staging_manifest: object,
    ) -> object:
        verified = original_verify_wheel(
            wheel,
            identity,
            generated_files,
            staging_manifest,
        )
        wheel.write_bytes(b"post-verification wheel mutation\n")
        return verified

    monkeypatch.setattr(builder, "_verify_wheel", mutate_after_verification)

    with pytest.raises(
        builder.TrialBuildError,
        match="wheel changed after verification",
    ):
        builder.build_trial_wheel(
            source_root=source,
            output_directory=output,
            source_sha=source_sha,
            utc_date="20260907",
            run=1,
            attempt=1,
        )

    assert not output.exists()


def test_trial_build_does_not_replace_an_output_that_appears_before_publish(
    tmp_path: Path,
    public_checkout: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No-replace publication preserves an output created during the build."""
    builder = _builder()
    source, source_sha = public_checkout
    output = tmp_path / "appearing-output"
    original_rename = builder._rename_no_replace

    def create_output_before_rename(
        source_parent: int,
        source_name: str,
        destination_parent: int,
        destination_name: str,
    ) -> None:
        output.mkdir()
        original_rename(
            source_parent,
            source_name,
            destination_parent,
            destination_name,
        )

    monkeypatch.setattr(
        builder,
        "_rename_no_replace",
        create_output_before_rename,
    )

    with pytest.raises(FileExistsError, match="output directory"):
        builder.build_trial_wheel(
            source_root=source,
            output_directory=output,
            source_sha=source_sha,
            utc_date="20260907",
            run=1,
            attempt=1,
        )

    assert output.is_dir()
    assert not tuple(output.iterdir())
    assert not tuple(tmp_path.glob(".appearing-output.stage-*"))


def test_trial_build_rejects_output_parent_replaced_during_publication(
    tmp_path: Path,
    public_checkout: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retained parent must still be reachable by the requested pathname."""
    builder = _builder()
    source, source_sha = public_checkout
    parent = tmp_path / "publication-parent"
    moved_parent = tmp_path / "moved-publication-parent"
    output = parent / "trial-output"
    parent.mkdir()
    original_rename = builder._rename_no_replace

    def replace_parent_before_publish(
        source_parent: int,
        source_name: str,
        destination_parent: int,
        destination_name: str,
    ) -> None:
        parent.rename(moved_parent)
        parent.symlink_to(source, target_is_directory=True)
        original_rename(
            source_parent,
            source_name,
            destination_parent,
            destination_name,
        )

    monkeypatch.setattr(
        builder,
        "_rename_no_replace",
        replace_parent_before_publish,
    )

    with pytest.raises(
        builder.TrialBuildError,
        match="output directory parent changed",
    ):
        builder.build_trial_wheel(
            source_root=source,
            output_directory=output,
            source_sha=source_sha,
            utc_date="20260907",
            run=1,
            attempt=1,
        )

    assert parent.is_symlink()
    assert not (source / "trial-output").exists()
    assert (moved_parent / "trial-output").is_dir()
    assert not tuple(moved_parent.glob(".trial-output.stage-*"))


def test_wheel_verification_rejects_a_generated_member_mismatch(tmp_path: Path) -> None:
    """A wheel cannot substitute bytes for any reviewed generated file."""
    builder = _builder()
    identity = builder.derive_trial_identity(
        source_sha="a" * 40,
        utc_date="20260907",
        run=1,
        attempt=1,
    )
    generated = {
        str(_VERSION_PATH): f'__version__ = "{identity.version}"\n'.encode("ascii"),
        str(_TRIAL_BUILD_PATH): b'{"schema":1}\n',
        str(_CAPABILITY_PATH): b'{"entries":[],"schema_version":1}\n',
    }
    wheel = tmp_path / f"gwexpy_studio-{identity.version}-py3-none-any.whl"
    dist_info = f"gwexpy_studio-{identity.version}.dist-info"
    with zipfile.ZipFile(wheel, "w") as archive:
        for path, content in generated.items():
            wheel_path = Path(path).relative_to("src").as_posix()
            archive.writestr(
                wheel_path,
                b'{"tampered":true}\n'
                if wheel_path.endswith("trial-build.json")
                else content,
            )
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(
            f"{dist_info}/METADATA",
            (
                "Metadata-Version: 2.3\n"
                "Name: gwexpy-studio\n"
                f"Version: {identity.version}\n"
            ),
        )

    with pytest.raises(builder.TrialBuildError, match="generated file"):
        builder._verify_wheel(wheel, identity, generated, ReleaseSourceManifest(()))


def test_wheel_verification_rejects_split_dist_info_metadata(tmp_path: Path) -> None:
    """Wheel metadata must come from one expected dist-info directory."""
    builder = _builder()
    identity = builder.derive_trial_identity(
        source_sha="a" * 40,
        utc_date="20260907",
        run=1,
        attempt=1,
    )
    generated = {
        str(_VERSION_PATH): f'__version__ = "{identity.version}"\n'.encode("ascii"),
        str(_TRIAL_BUILD_PATH): b'{"schema":1}\n',
        str(_CAPABILITY_PATH): b'{"entries":[],"schema_version":1}\n',
    }
    wheel = tmp_path / "split-metadata.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for path, content in generated.items():
            archive.writestr(Path(path).relative_to("src").as_posix(), content)
        archive.writestr(
            "first.dist-info/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(
            "second.dist-info/METADATA",
            (
                "Metadata-Version: 2.3\n"
                "Name: gwexpy-studio\n"
                f"Version: {identity.version}\n"
            ),
        )

    with pytest.raises(builder.TrialBuildError, match="dist-info"):
        builder._verify_wheel(wheel, identity, generated, ReleaseSourceManifest(()))


def test_wheel_verification_rejects_duplicate_critical_header(tmp_path: Path) -> None:
    """The validator cannot silently accept a second trial-wheel tag claim."""
    builder = _builder()
    identity = builder.derive_trial_identity(
        source_sha="a" * 40,
        utc_date="20260907",
        run=1,
        attempt=1,
    )
    generated = {
        str(_VERSION_PATH): f'__version__ = "{identity.version}"\n'.encode("ascii"),
        str(_TRIAL_BUILD_PATH): b'{"schema":1}\n',
        str(_CAPABILITY_PATH): b'{"entries":[],"schema_version":1}\n',
    }
    wheel = tmp_path / "duplicate-header.whl"
    dist_info = f"gwexpy_studio-{identity.version}.dist-info"
    with zipfile.ZipFile(wheel, "w") as archive:
        for path, content in generated.items():
            archive.writestr(Path(path).relative_to("src").as_posix(), content)
        archive.writestr(
            f"{dist_info}/WHEEL",
            (
                "Wheel-Version: 1.0\n"
                "Root-Is-Purelib: true\n"
                "Tag: py3-none-any\n"
                "Tag: py3-none-any\n"
            ),
        )
        archive.writestr(
            f"{dist_info}/METADATA",
            (
                "Metadata-Version: 2.3\n"
                "Name: gwexpy-studio\n"
                f"Version: {identity.version}\n"
            ),
        )

    with pytest.raises(builder.TrialBuildError, match="duplicate"):
        builder._verify_wheel(wheel, identity, generated, ReleaseSourceManifest(()))


def test_failed_wheel_build_preserves_p_and_publishes_nothing(
    tmp_path: Path,
    public_checkout: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed backend cannot mutate selected P content or publish an artifact."""
    builder = _builder()
    source, source_sha = public_checkout
    output = tmp_path / "failed-build-output"
    policy = load_policy(source / "packaging/release-source-allowlist.txt")
    source_before = scan_public_checkout(source, policy)

    def fail_wheel_build(*_arguments: object, **_kwargs: object) -> Path:
        raise builder.TrialBuildError("simulated wheel build failure")

    monkeypatch.setattr(builder, "_build_one_wheel", fail_wheel_build)

    with pytest.raises(builder.TrialBuildError, match="simulated wheel build failure"):
        builder.build_trial_wheel(
            source_root=source,
            output_directory=output,
            source_sha=source_sha,
            utc_date="20260907",
            run=1,
            attempt=1,
        )

    assert not output.exists()
    assert scan_public_checkout(source, policy) == source_before


def test_staging_delta_rejects_an_unapproved_generated_file() -> None:
    """Staging cannot accumulate an unreviewed file before wheel assembly."""
    builder = _builder()
    base_version = b'__version__ = "0.1.0a1"\n'
    generated = {
        str(_VERSION_PATH): (
            b'__version__ = "0.1.0a1+trial.p.gaaaaaaa.20260907.r1.a1"\n'
        ),
        str(_TRIAL_BUILD_PATH): b'{"schema":1}\n',
        str(_CAPABILITY_PATH): b'{"entries":[],"schema_version":1}\n',
    }
    source_manifest = ReleaseSourceManifest(
        (
            ManifestEntry(
                path=str(_VERSION_PATH),
                file_type="file",
                mode="0644",
                sha256=_sha256(base_version),
            ),
        )
    )
    staging_entries = [
        ManifestEntry(
            path=path,
            file_type="file",
            mode="0644",
            sha256=_sha256(data),
        )
        for path, data in generated.items()
    ]
    staging_entries.append(
        ManifestEntry(
            path="src/gwexpy_studio/assets/unreviewed.json",
            file_type="file",
            mode="0644",
            sha256=_sha256(b"{}\n"),
        )
    )
    staging_manifest = ReleaseSourceManifest(
        tuple(sorted(staging_entries, key=lambda item: item.path))
    )

    with pytest.raises(builder.TrialBuildError, match="unexpected staging delta"):
        builder.verify_staging_delta(
            source_manifest=source_manifest,
            staging_manifest=staging_manifest,
            generated_files=generated,
        )

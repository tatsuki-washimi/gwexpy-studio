"""Trial-launcher capability policy inheritance across real worker spawns."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from gwexpy_studio.worker.client import WorkerClient

pytestmark = pytest.mark.integration

_TRIAL_VERSION = "0.1.0a1+trial.p.g0854741.20260907.r1.a1"


def _write_trial_assets(root: Path) -> Path:
    """Create only the package-local assets emitted by a trial wheel build."""
    assets = root / "gwexpy_studio" / "assets"
    assets.mkdir(parents=True)
    (assets / "trial-build.json").write_text(
        json.dumps(
            {
                "build_id": "P-0854741-20260907-r1-a1",
                "schema": 1,
                "source_manifest_sha256": "b" * 64,
                "source_sha": "0854741" + "a" * 33,
                "version": _TRIAL_VERSION,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    policy = assets / "io-capabilities.json"
    from tests.support.trial_io_policy import trial_io_policy_document

    policy.write_text(
        json.dumps(trial_io_policy_document(), indent=2) + "\n", encoding="utf-8"
    )
    return root / "gwexpy_studio"


def _assert_trial_capabilities(snapshot: object) -> None:
    from gwexpy_studio.ops.io_capabilities import EffectiveCapabilitySnapshot
    from tests.support.trial_io_policy import TRIAL_IO_POLICY_PAIRS

    assert isinstance(snapshot, EffectiveCapabilitySnapshot)
    for (datatype, format_name), (tier, reason) in TRIAL_IO_POLICY_PAIRS.items():
        entry = snapshot.capability(datatype, format_name, "read")
        assert entry is not None, (datatype, format_name)
        assert entry.tier == tier, entry.document()
        if tier == "C":
            assert entry.status == "unavailable", entry.document()
            assert entry.available is False
            assert entry.reason == reason
        else:
            assert entry.status == "verified", entry.document()
            assert entry.available is True


@pytest.mark.contract("TRT-IO-004")
def test_trial_launcher_policy_reaches_initial_and_restarted_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trial's embedded policy survives both initial and restarted worker spawns."""
    import gwexpy_studio.ui.app as app_module
    from gwexpy_studio.ops.io_capabilities import CAPABILITY_ENVIRONMENT

    package = _write_trial_assets(tmp_path)
    monkeypatch.setattr(app_module.resources, "files", lambda _package: package)
    monkeypatch.setattr(
        app_module,
        "_installed_package_version",
        lambda: _TRIAL_VERSION,
    )
    monkeypatch.setenv(CAPABILITY_ENVIRONMENT, "/outside/unreviewed-policy.json")
    client = WorkerClient(startup_timeout_s=30.0, join_timeout_s=10.0)

    try:
        with app_module._trial_capability_environment():
            embedded = package / "assets" / "io-capabilities.json"
            assert os.environ[CAPABILITY_ENVIRONMENT] == str(embedded)

            client.start()
            _assert_trial_capabilities(client.capability_snapshot)

            client.restart()
            _assert_trial_capabilities(client.capability_snapshot)
    finally:
        client.shutdown()

    assert os.environ[CAPABILITY_ENVIRONMENT] == "/outside/unreviewed-policy.json"

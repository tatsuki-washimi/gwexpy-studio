"""Helpers for constructing isolated contract-auditor test suites."""

from __future__ import annotations

import json
from pathlib import Path


def write_contract_shards(
    root: Path,
    *,
    requirements: list[dict[str, str]],
    catalog: list[dict[str, str]],
    status: dict[str, dict[str, str]],
    contract_root: Path | None = None,
) -> None:
    """Write the three TOML shard families used by meta tests."""
    contracts_root = contract_root or root / "tests" / "contracts"
    requirements_dir = contracts_root / "requirements"
    catalog_dir = contracts_root / "catalog"
    status_dir = contracts_root / "status"
    for directory in (requirements_dir, catalog_dir, status_dir):
        directory.mkdir(parents=True, exist_ok=True)

    requirements_lines: list[str] = []
    for record in requirements:
        requirements_lines.extend(
            [
                "[[requirement]]",
                f"id = {json.dumps(record['id'])}",
                f"source = {json.dumps(record['source'])}",
                f"text = {json.dumps(record['text'])}",
                "",
            ]
        )
    (requirements_dir / "requirements.toml").write_text(
        "\n".join(requirements_lines), encoding="utf-8"
    )

    catalog_lines: list[str] = []
    for record in catalog:
        catalog_lines.extend(
            [
                "[[contract]]",
                f"id = {json.dumps(record['id'])}",
                f"requirement = {json.dumps(record['requirement'])}",
                f"nodeid = {json.dumps(record['nodeid'])}",
                f"kind = {json.dumps(record['kind'])}",
                f"target_owner = {json.dumps(record['target_owner'])}",
                f"coverage = {json.dumps(record['coverage'])}",
                "",
            ]
        )
    (catalog_dir / "catalog.toml").write_text(
        "\n".join(catalog_lines), encoding="utf-8"
    )

    status_lines: list[str] = []
    for contract_id, record in status.items():
        status_lines.extend(
            [
                f"[contract.{json.dumps(contract_id)}]",
                f"state = {json.dumps(record['state'])}",
            ]
        )
        for key in ("expected_code", "expected_owner"):
            if key in record:
                status_lines.append(f"{key} = {json.dumps(record[key])}")
        status_lines.append("")
    (status_dir / "status.toml").write_text("\n".join(status_lines), encoding="utf-8")

    (root / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n    contract(id): contract item\n"
        "    long: long-running contract\n",
        encoding="utf-8",
    )


def write_test_module(
    root: Path, source: str, *, name: str = "test_contracts.py"
) -> None:
    """Write a test module in the isolated suite."""
    tests_dir = root / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / name).write_text(source, encoding="utf-8")


def contract_record(
    contract_id: str,
    nodeid: str,
    *,
    requirement: str = "REQ-001",
    kind: str = "unit",
    target_owner: str = "owner.target",
    coverage: str = "full",
) -> dict[str, str]:
    """Return a catalog record with stable defaults."""
    return {
        "id": contract_id,
        "requirement": requirement,
        "nodeid": nodeid,
        "kind": kind,
        "target_owner": target_owner,
        "coverage": coverage,
    }


def green_status() -> dict[str, str]:
    """Return the status record for a passing implementation contract."""
    return {"state": "green"}


def scaffold_status(
    *,
    code: str = "STUDIO-FOUNDATION-NOT-IMPLEMENTED",
    owner: str = "owner.target",
) -> dict[str, str]:
    """Return the status for a pass-after-sentinel scaffold contract."""
    return {
        "state": "scaffold_green",
        "expected_code": code,
        "expected_owner": owner,
    }


def red_status(
    state: str = "direct_red",
    *,
    code: str = "STUDIO-TEST-NOT-IMPLEMENTED",
    owner: str = "owner.target",
) -> dict[str, str]:
    """Return a status record for an expected sentinel failure."""
    return {"state": state, "expected_code": code, "expected_owner": owner}


def single_requirement() -> list[dict[str, str]]:
    """Return the smallest valid requirements shard."""
    return [{"id": "REQ-001", "source": "test", "text": "A test requirement."}]

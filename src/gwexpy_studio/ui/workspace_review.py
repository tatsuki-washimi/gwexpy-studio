"""Readable restoration evidence without internal authorization identifiers."""

from collections.abc import Mapping
from typing import Any


def restoration_summary(review: Mapping[str, Any]) -> str:
    """Show inputs, missing provenance, and explicit old-to-new versions."""
    lines = [f"Restore {len(review.get('targets', []))} active output(s)."]
    for source in review.get("sources", []):
        state = (
            "verified"
            if source.get("verified")
            else "unverified — no saved source evidence"
        )
        lines.extend(f"{path} ({state})" for path in source.get("paths", []))
    differences = review.get("environment_differences", {})
    if differences:
        lines.append("Environment changed; new results will be recorded:")
        lines.extend(
            f"{name}: {versions.get('recorded') or 'not recorded'} "
            f"→ {versions['current']}"
            for name, versions in differences.items()
        )
    else:
        lines.append("The recorded scientific environment matches.")
    return "\n".join(lines)


def public_review_details(value: Any) -> Any:
    """Omit transport authorization and digest fields from diagnostic text."""
    if isinstance(value, Mapping):
        return {
            key: public_review_details(item)
            for key, item in value.items()
            if "token" not in key and "digest" not in key
        }
    if isinstance(value, list):
        return [public_review_details(item) for item in value]
    return value

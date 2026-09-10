# WSL2 and macOS technical qualification

Platform prereleases must not be published until all three physical
configurations have produced passing evidence from the same source commit:

- Windows 11 x86_64, WSL2 Ubuntu 24.04 x86_64, and WSLg
- Windows 11 ARM64, WSL2 Ubuntu 24.04 aarch64, and WSLg
- Apple Silicon arm64 with macOS 15 or later

The build workflow creates a non-release qualification kit for each target.
Give the matching kit to the machine owner. The owner extracts it and runs one
command without reading the participant Quick Start or manually operating Studio:

```bash
bash run-qualification.sh
```

The kit creates a temporary conda Python 3.12 prefix, verifies and installs the
candidate with `--only-binary=:all:`, drives the UI, removes temporary state,
and leaves one result under `results/`. A failed check returns a nonzero status.

Collect these three canonical files through a private channel:

```text
qualification-wsl2-ubuntu24-x86_64-<build-id>.json
qualification-wsl2-ubuntu24-aarch64-<build-id>.json
qualification-macos15-arm64-<build-id>.json
```

Generate the campaign summary and its sidecar:

```bash
python scripts/verify_platform_qualification.py \
  qualification-wsl2-ubuntu24-x86_64-*.json \
  qualification-wsl2-ubuntu24-aarch64-*.json \
  qualification-macos15-arm64-*.json \
  --output QUALIFICATION-SUMMARY.json
```

The verifier rejects a failed check, incomplete or duplicate architecture,
mixed source commits, wrong Build ID, wrong wheel digest, and noncanonical
evidence. The result and summary intentionally contain no path, username, or
hostname. Supply the SHA-256 of `QUALIFICATION-SUMMARY.json` to both publish
workflow runs. The common digest is recorded in both Release bodies.

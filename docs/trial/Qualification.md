# Physical qualification for the four-target trial

This document is for release owners and kit owners.
It is not a participant Quick Start.

## Required configurations

| Target | Required physical configuration |
| --- | --- |
| `ubuntu24-x86_64` | native Ubuntu 24.04, x86_64, graphical desktop |
| `debian13-x86_64` | native Debian 13, x86_64, graphical desktop |
| `wsl2-ubuntu24` | Windows 11 x86_64 + WSL2 Ubuntu 24.04 x86_64 + WSLg; Windows 11 ARM64 + WSL2 Ubuntu 24.04 aarch64 + WSLg |
| `macos15-arm64` | Apple Silicon with macOS 15 or later |

All five configurations are required.
The Ubuntu, Debian, and joint WSL2 + Mac publication groups are evaluated separately as described in the release contract.

## Build and handoff

The selected Build workflow’s original kit artifact is the formal handoff artifact and must be preserved for later trusted summary verification. The following Build/operator interface describes how the release owner creates a source-bound kit from the selected target Release directory; a locally created kit does not replace the original Build artifact. This command is run from the `P_trial` source checkout by the release owner; a kit owner does not need Git, a source checkout, or any source operation.

```bash
python3 scripts/build_qualification_kit.py \
  --release /path/to/target-release \
  --output /path/to/qualification-kit-ubuntu24-x86_64-BUILD_ID \
  --trial-target ubuntu24-x86_64
```

Use the matching target and a new, unused output directory for each locally created kit. The builder verifies and binds the Release ZIP, sidecar, source manifest, wheel, Build ID, and helper scripts into the kit. It refuses to replace an existing kit. Preserve the selected Build workflow’s original kit artifact; keep owner results in separate working-kit copies.

The owner runs the supplied `run-qualification.sh` from a Bash-capable native terminal on the assigned physical target. Start from an untouched copy of the handed-off kit, change the current directory to that fresh copy containing `run-qualification.sh`, and select the conda executable explicitly in that terminal. This invocation also works when artifact extraction has removed the launcher’s executable bit:

```bash
set -eu
conda_base="$(conda info --base)"
test -x "$conda_base/bin/conda"
CONDA_EXE="$conda_base/bin/conda" bash ./run-qualification.sh
```

For macOS, use the macOS terminal. For WSL2, use a Bash terminal inside the matching WSL2 guest and select the conda executable installed in that guest. The launcher reads `CONDA_EXE`, invokes `conda run -n base`, and creates the temporary Python 3.12 environments itself. It does not use the owner’s system Python or accept launcher arguments for changing the result location. A normal run writes the final result under `results/` in the working kit copy.

## Publication and audit summary operator procedure

The release operator creates the publication or audit summary after independently verifying the physical results and the selected Build artifacts. This is a source-checkout operation for the release operator; kit owners remain Git-free. The verifier accepts a private descriptor and writes a canonical summary plus its `.sha256` sidecar:

```bash
GROUPS_PYTHON=/path/to/accepted-python3.12
env -u PYTHONPATH PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
  "$GROUPS_PYTHON" scripts/verify_publication_groups.py \
  --input /private/trial-proof/ubuntu-descriptor.json \
  --output /private/trial-proof/ubuntu-summary.json \
  --publication-group ubuntu
```

The output summary and sidecar paths must be fresh. The verifier refuses to overwrite either file. Use `--publication-group debian` for the Debian group, `--publication-group wsl2-mac` for the two WSL2 configurations plus macOS, or `--audit` for the separate all-five-configuration audit. The all-five audit is not a publication group and cannot be used as one.

The descriptor has exactly these top-level fields:

| Field | Content |
| --- | --- |
| `repository` | Trusted repository full name from the Build run metadata |
| `default_branch` | The trusted default branch name, normally `main` |
| `builds` | A target-keyed map; each value has `run` (the exact successful Build run JSON object), `artifacts` (the actual artifact metadata list or API page list), `release_wrapper` (downloaded release artifact wrapper path), and `kit_wrapper` (downloaded original qualification-kit artifact wrapper path) |
| `qualification_results` | A target-keyed map whose architecture keys point to canonical schema-2 result files |

The example values above are field shapes and private local paths, not evidence. The `run` object must be the exact successful `workflow_dispatch` Build run response for the selected default branch. `artifacts` must be the actual metadata list or paginated API responses fetched for that exact run. The release and kit wrapper files must be the downloaded artifact wrappers whose digests appear in that metadata; the kit wrapper must be the original kit artifact from the selected Build workflow. The operator must fetch these objects independently from the GitHub Build run and artifact APIs. Neither an owner nor a qualification result can supply them, and the operator must not reconstruct them from result fields.

Use the following pattern with saved metadata, downloaded wrappers, and canonical result files. It generates the Ubuntu descriptor without inventing a Build ID, hash, or pass claim:

```bash
export TRUSTED_REPOSITORY="${TRUSTED_REPOSITORY:?set to the repository full name from the fetched Build run}"
export BUILD_RUN_JSON=/private/trial-proof/ubuntu-build-run.json
export BUILD_ARTIFACTS_JSON=/private/trial-proof/ubuntu-build-artifacts-pages.json
export RELEASE_WRAPPER=/private/trial-proof/ubuntu-release-wrapper.zip
export KIT_WRAPPER=/private/trial-proof/ubuntu-kit-wrapper.zip
export QUALIFICATION_RESULT=/private/trial-proof/qualification-ubuntu24-x86_64.json
export DESCRIPTOR=/private/trial-proof/ubuntu-descriptor.json
export GROUPS_PYTHON=/path/to/accepted-python3.12

env -u PYTHONPATH PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 "$GROUPS_PYTHON" - <<'PY'
import json
import os
from pathlib import Path

descriptor = {
    "repository": os.environ["TRUSTED_REPOSITORY"],
    "default_branch": "main",
    "builds": {
        "ubuntu24-x86_64": {
            "run": json.loads(Path(os.environ["BUILD_RUN_JSON"]).read_text()),
            "artifacts": json.loads(Path(os.environ["BUILD_ARTIFACTS_JSON"]).read_text()),
            "release_wrapper": os.environ["RELEASE_WRAPPER"],
            "kit_wrapper": os.environ["KIT_WRAPPER"],
        }
    },
    "qualification_results": {
        "ubuntu24-x86_64": {
            "x86_64": os.environ["QUALIFICATION_RESULT"],
        }
    },
}
Path(os.environ["DESCRIPTOR"]).write_text(
    json.dumps(descriptor, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
)
PY

env -u PYTHONPATH PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
  "$GROUPS_PYTHON" scripts/verify_publication_groups.py \
  --input "$DESCRIPTOR" \
  --output /private/trial-proof/ubuntu-summary.json \
  --publication-group ubuntu
```

For Debian, replace the descriptor mapping with `debian13-x86_64` and its `x86_64` result, and run `--publication-group debian`. For the joint group, include exactly `wsl2-ubuntu24` with both `x86_64` and `aarch64` result paths plus `macos15-arm64` with its `arm64` result path, then run `--publication-group wsl2-mac`. For the audit, include all four target mappings with all five configuration results and run `--audit`; retain that summary separately from the three publication-group summaries.

The descriptor is private input and its local paths do not appear in the canonical summary. Before approval, reverify the exact wrapper bytes, artifact metadata, Build run identity, and every qualification result, then retain the resulting summary and sidecar bytes and hashes. `read_publication_summary` only checks the structure of an already-produced summary; it does not replace this evidence verification.

The Publish workflow receives the existing `build_run_id` and summary SHA-256 format input only. It does not fetch or validate the summary body. Parent and F perform the byte and evidence verification and record concrete results before human approval and manual dispatch. After one approved `wsl2-mac` summary, publish WSL2 and macOS serially. The all-five audit remains an audit record and does not authorize publication.

## Kit owner procedure

Use the exact kit assigned to the target and Build.
Do not substitute a wheel, constraints file, source checkout, or another target.

Follow the entry procedure supplied with the kit.
The kit is the source of truth for the procedure and its accepted inputs.

The run order is fixed:

```text
checksum and preconditions
→ temporary conda Python 3.12 environment
→ automatic checks
→ owner checks in the same environment
→ cleanup
→ final result save
```

The owner check uses a native file dialog with a Japanese path containing spaces.
Check the normal display scale and one other available scale.
Record only boolean pass or fail in the canonical result.
Record numeric scale settings only in private work notes.

The automatic checks cover the target identity, Qt and display path, clipboard,
sample workflow, Save → Close → Open, recovery path, worker exit, cleanup,
Python export, I/O policy, and About Build ID as implemented by the kit.

The native dialog prompt supplies a Japanese project path containing a space. Select that prepared path in the real native dialog, then answer each prompt in order with `y`, `n`, or `c`: native dialog selected the path; normal-scale display; normal-scale operation; available alternate-scale display; available alternate-scale operation. The numeric scale values belong in the owner’s private work memo. They do not belong in canonical JSON.

An unanswered item, EOF, interruption, failed cleanup, missing result, result-save failure, or other incomplete run is not a pass. The runner does not overwrite a result or reuse an existing work root. Preserve the untouched original Build kit artifact, retain the failed working copy and its evidence, then use a fresh working copy for a rerun. Do not add results to or overwrite the original Build artifact.
Do not include paths, usernames, hostnames, owner contact information, or freeform logs in canonical evidence.

## Evidence and group review

Qualification evidence uses schema 2 and binds the source SHA, target, CPU, Build ID, wheel hash, ZIP hash, and kit manifest hash.
The five-configuration audit summary is distinct from each publication group summary.

The release owner verifies that every result comes from the same `P_trial`, the actual Build, and the matching two assets.
Missing, duplicate, extra, or mixed-candidate results fail review.

Do not record a public URL, publication success, or physical pass until the parent release record contains the concrete Build run, hash summary, and verified assets.

# GWexpy Studio macOS trial quick start

This guide applies only to the `macos15-arm64` prerelease on an Apple Silicon Mac running macOS 15 or later.
Do not use it on an Intel Mac, WSL2, or Linux.

Git, source-code operations, and a GitHub account are not required.
The trial uses a dedicated conda environment and does not use the OS Python or the user site.
Confirm the available distribution and verified macOS version from the trial coordinator.

## Prerequisites

Use an Apple Silicon Mac running macOS 15 or later with a normal desktop session.
Have conda available in the original shell, a network for conda and pip, `unzip`, `shasum`, and bash.

## Download and verify

Download the ZIP and its matching `.zip.sha256` sidecar from the supplied prerelease link into an empty directory.
macOS's standard zsh does not run bash array syntax, so run the checks and installation in bash.

First, in the original shell where conda is available, obtain the conda paths and then start bash.
Do not type or replace a conda base path manually.

```zsh
if ! CONDA_BASE="$(conda info --base)"; then
  echo "conda info --base failed; stop before starting bash" >&2
  exit 1
fi
export CONDA_BASE
export CONDA_EXE="$CONDA_BASE/bin/conda"
export CONDA_SH="$CONDA_BASE/etc/profile.d/conda.sh"
if ! test -x "$CONDA_EXE"; then
  echo "conda executable was not found; stop before starting bash" >&2
  exit 1
fi
if ! test -r "$CONDA_SH"; then
  echo "conda initialization script was not found; stop before starting bash" >&2
  exit 1
fi
/bin/bash --noprofile --norc
```

In that bash, run:

```bash
set -e
if ! source "$CONDA_SH"; then
  echo "conda initialization failed" >&2
  exit 1
fi
set -u
set -o pipefail
shopt -s nullglob

test "$(uname -m)" = "arm64"
macos_major="$(sw_vers -productVersion | cut -d. -f1)"
test "$macos_major" -ge 15
sw_vers

zips=(gwexpy-studio-trial-macos15-arm64-*.zip)
sidecars=(gwexpy-studio-trial-macos15-arm64-*.zip.sha256)
test "${#zips[@]}" -eq 1
test "${#sidecars[@]}" -eq 1
archive="${zips[0]}"
sidecar="${sidecars[0]}"
test "${sidecar%.sha256}" = "$archive"
shasum -a 256 -c "$sidecar"
unzip "$archive"
bundle="${archive%.zip}"
test -d "$bundle"
cd "$bundle"
shasum -a 256 -c SHA256SUMS
```

Every ZIP, sidecar, and internal `SHA256SUMS` check must report `OK`.
If selection is not unique or any check fails, stop and contact the trial coordinator.

## Install into an isolated conda environment

The commands below do not overwrite an existing environment with the same name.
If it already exists, do not delete or change it; contact the trial coordinator.

```bash
env_name=gwexpy-studio-macos
if ! env_list="$(conda env list)"; then
  echo "conda env list failed; stop" >&2
  exit 1
fi
if printf '%s\n' "$env_list" | awk -v name="$env_name" '$1 == name { found=1 } END { exit found ? 0 : 1 }'; then
  echo "conda environment already exists: $env_name; do not overwrite it" >&2
  exit 2
fi

conda create -n "$env_name" python=3.12 pip
conda activate "$env_name"
export PYTHONNOUSERSITE=1
unset PYTHONPATH
python - <<'PY'
import platform
import sys

assert sys.version_info[:2] == (3, 12)
assert platform.machine() == "arm64"
PY
python --version
conda --version
python -m pip --version

constraints_path=constraints-macos15-arm64.txt
wheel=(gwexpy_studio-*.whl)
test -f "$constraints_path"
test "${#wheel[@]}" -eq 1
wheel_path="${wheel[0]}"
test -f "$wheel_path"
python -m pip install --only-binary=:all: \
  -c "$constraints_path" \
  "$wheel_path"
```

The wheel is accepted only when the array contains exactly one candidate.
Keep this activated conda environment in the bash used for the trial.
If a new terminal is needed, confirm conda in its original shell, start bash, and run this short block:

```bash
if ! source "$CONDA_SH"; then exit 1; fi
if ! conda activate gwexpy-studio-macos; then exit 1; fi
export PYTHONNOUSERSITE=1
unset PYTHONPATH
```

If installation or launch fails, do not change the instructions; contact the trial coordinator.

## Run the workflow

```bash
gwexpy-studio
```

When the application is visible, run:

`Try Sample → Load → Crop → ASD → Save → Close → Open`

Use the file selection window for Save and Open, including a save location containing Japanese characters and spaces.
Record display-scaling problems if they appear.
This distribution checks reading of time-series CSV files.
Unsupported types are shown as unavailable in the application.
Record Review or Restore only if it is shown, including whether continuation worked.
Open About and record the Build ID.

Record the result of the normal Save → Close → Open sequence.
If launch, reopen, or recovery cannot continue, contact the trial coordinator with the error shown and the operation immediately before it.
Before sending, redact usernames, hostnames, personal save locations, credentials, and confidential measurement data.

## Feedback

Complete `Feedback.ja.md` with observed facts and reply to the supplied email or chat.
No GitHub account is required.

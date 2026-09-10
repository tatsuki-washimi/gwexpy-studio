# GWexpy Studio macOS trial quick start

This guide applies only to the `macos15-arm64` prerelease on an Apple Silicon
Mac running macOS 15 or later. Do not use it on an Intel Mac, WSL2, or Linux.

This human trial must be your first manual use of GWexpy Studio. Install conda
before starting. Git, a source checkout, and an editable install are not needed.

Record environment setup and installation time separately from the workflow
time. Start the workflow timer when the Welcome screen appears.

## Download and verify

Download the ZIP and matching `.zip.sha256` file from the single prerelease link
into an empty directory. Then run:

```bash
set -euo pipefail
sidecars=(gwexpy-studio-trial-macos15-arm64-*.zip.sha256)
test "${#sidecars[@]}" -eq 1
shasum -a 256 -c "${sidecars[0]}"
archive="${sidecars[0]%.sha256}"
unzip "$archive"
bundle="${archive%.zip}"
cd "$bundle"
shasum -a 256 -c SHA256SUMS
```

Every checksum must report `OK`. Stop without installing if a check fails.

## Confirm the Mac

```bash
set -euo pipefail
test "$(uname -m)" = "arm64"
test "$(sw_vers -productVersion | cut -d. -f1)" -ge 15
sw_vers
```

Stop and report the output if a command fails.

## Create an isolated environment and install

```bash
conda create -n gwexpy-studio-macos python=3.12
conda activate gwexpy-studio-macos
export PYTHONNOUSERSITE=1
unset PYTHONPATH
python --version
conda --version
pip --version
pip install --only-binary=:all: \
  -c constraints-macos15-arm64.txt \
  ./gwexpy_studio-*.whl
```

Keep the environment active and repeat the two environment settings in every
new terminal used for the trial. Do not change constraints, build from source,
or try a workaround if installation or launch fails.

## Run the workflow

```bash
gwexpy-studio
```

From the Welcome screen, complete **Try Sample → Load → Crop → ASD → Save →
Close → Open**. Exercise the native file picker when saving and reopening. If
Review or Restore appears, record whether you can continue from it. Open About
and record its Build ID.

If launch, project reopen, or recovery cannot continue, do not try a workaround.
Record the step, screen, and terminal output, then stop.

Complete `Feedback.ja.md` and reply to the email or chat that supplied the
prerelease link. Do not send confidential measurement data, credentials,
usernames, hostnames, paths, or unrelated logs.

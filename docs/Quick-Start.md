# GWexpy Studio trial quick start

This initial participant trial supports Ubuntu 24.04 x86_64 only.

Install Miniforge or another conda distribution before starting.

Git, an editable install, and a source checkout are not required.

## Download and verify the release

Open the GitHub prerelease link shared by the trial coordinator.

In an empty directory, download exactly the ZIP and its checksum sidecar.

Both filenames start with `gwexpy-studio-trial-` and the sidecar ends in
`.zip.sha256`.

Run the following commands from that download directory.

```bash
set -euo pipefail
shopt -s nullglob
sidecars=(gwexpy-studio-trial-*.zip.sha256)
test "${#sidecars[@]}" -eq 1
sha256sum -c "${sidecars[0]}"
archive="${sidecars[0]%.sha256}"
unzip "$archive"
bundle="${archive%.zip}"
cd "$bundle"
sha256sum -c SHA256SUMS
```

The outer checksum and every internal checksum must report `OK`.

Stop without installing if either check fails.

## Confirm the trial machine

```bash
test "$(uname -m)" = "x86_64"
grep '^VERSION_ID="24.04"$' /etc/os-release
```

Stop and report the output if either command fails.

Do not substitute the bundled aarch64 constraints in this initial trial.

## Create the environment

```bash
conda create -n gwexpy-studio python=3.12
conda activate gwexpy-studio
export PYTHONNOUSERSITE=1
unset PYTHONPATH
python --version
conda --version
pip --version
```

Keep these two environment settings in the terminal used for the trial. They
prevent packages from another Python 3.12 environment or source checkout from
being imported. If you open another terminal, activate the conda environment
and repeat the two environment-setting commands before starting Studio.

## Check the Qt GL runtime

Before installing the wheel, check that Ubuntu can load both Qt GL runtime
libraries.

```bash
python - <<'PY'
import ctypes

for library in ("libEGL.so.1", "libGL.so.1"):
    ctypes.CDLL(library)

print("Qt GL runtime: OK")
PY
```

If this command fails, run the following commands and repeat the preflight.

```bash
sudo apt-get update
sudo apt-get install --no-install-recommends -y libegl1 libgl1
```

If `sudo` is unavailable, ask the Ubuntu administrator to install `libegl1`
and `libgl1`.

Do not continue until the preflight reports `OK`.

## Install the trial wheel

```bash
pip install --only-binary=:all: -c constraints-ubuntu24-x86_64.txt ./gwexpy_studio-*.whl
```

`--only-binary=:all:` prevents pip from building native dependencies on the
trial machine.

If installation stops, save the terminal output and stop the trial.

Do not change constraints or attempt a source build.

## Start and exercise Studio

```bash
gwexpy-studio
```

Complete these steps:

1. Choose **Try Sample**.
2. Crop the sample.
3. Calculate ASD.
4. Save the project and close Studio.
5. Start Studio again and reopen the saved project.

Open the About dialog and compare its Build ID with this command:

```bash
python - <<'PY'
import json

with open("TRIAL-MANIFEST.json", encoding="utf-8") as stream:
    print(json.load(stream)["build"]["id"])
PY
```

## Send feedback

Record the result in `Feedback.ja.md` and reply to the email or chat that
contained the prerelease link.

Do not send confidential measurement data, credentials, or unrelated logs.

If installation or startup stops, keep the terminal output and the Build ID
if it was visible, then end the trial at that point.

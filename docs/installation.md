# Installation

This page takes you from installation to the first Studio workflow.
You do not need to move between platform pages during the normal setup.

## 1. Choose a route

| Goal | Route | You need |
| --- | --- | --- |
| Use a validated prerelease | ZIP from the matching GitHub Release | Matching ZIP, checksum, and conda |
| Run or modify the source | python -m pip install . | Source checkout and Python 3.12 |

The trial route and the source-checkout route are different.
The trial route uses a prebuilt wheel, pinned constraints, and platform qualification.
The source route resolves the package dependencies for development or local evaluation.

## 2. Install the trial build

### Download the two Release assets

From the [GWexpy Studio GitHub Releases](https://github.com/tatsuki-washimi/gwexpy-studio/releases) page, download these two files with the same Build ID into an empty directory:

~~~text
gwexpy-studio-trial-<build-id>.zip
gwexpy-studio-trial-<build-id>.zip.sha256
~~~

Verify the outer checksum, unpack the archive, and then verify the checksums inside the bundle.

On Ubuntu or WSL2:

~~~bash
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
~~~

On macOS, replace sha256sum with shasum -a 256.
If either checksum fails, stop without installing.

### Check the host and install

Open only the section that matches the host.
A failed preflight is a stop condition, not a reason to switch to a source install or different constraints.

<details open>
<summary>Windows 11 WSL2 / Ubuntu 24.04 / WSLg</summary>

Use this route only when the same Release contains a wsl2-ubuntu24 asset.
Confirm whether the guest uses x86_64 or aarch64 and use the matching constraints file.

~~~bash
set -euo pipefail
grep -qi microsoft /proc/sys/kernel/osrelease
test -d /mnt/wslg
grep '^ID=ubuntu$' /etc/os-release
grep '^VERSION_ID="24.04"$' /etc/os-release
architecture="$(uname -m)"
case "$architecture" in x86_64|aarch64) ;; *) exit 1 ;; esac
powershell.exe -NoLogo -NoProfile -NonInteractive -Command \
  '$env:PROCESSOR_ARCHITECTURE; [Environment]::OSVersion.Version'
~~~

Stop here if /mnt/wslg, powershell.exe, Ubuntu 24.04, or a matching architecture cannot be confirmed.

~~~bash
conda create -n gwexpy-studio-wsl2 python=3.12
conda activate gwexpy-studio-wsl2
export PYTHONNOUSERSITE=1
unset PYTHONPATH

python - <<'PY'
import ctypes

for library in ("libEGL.so.1", "libGL.so.1"):
    ctypes.CDLL(library)
print("Qt GL runtime: OK")
PY

architecture="$(uname -m)"
pip install --only-binary=:all: \
  -c "constraints-ubuntu24-${architecture}.txt" \
  ./gwexpy_studio-*.whl
gwexpy-studio
~~~

If the Qt GL check fails, install the Ubuntu runtime libraries with administrator access and run the check again:

~~~bash
sudo apt-get update
sudo apt-get install --no-install-recommends -y libegl1 libgl1
~~~

</details>

<details>
<summary>Native Ubuntu 24.04</summary>

Use this route only when the matching Ubuntu asset is included in the same Release.

~~~bash
set -euo pipefail
grep '^ID=ubuntu$' /etc/os-release
grep '^VERSION_ID="24.04"$' /etc/os-release
architecture="$(uname -m)"
case "$architecture" in x86_64|aarch64) ;; *) exit 1 ;; esac
~~~

~~~bash
conda create -n gwexpy-studio python=3.12
conda activate gwexpy-studio
export PYTHONNOUSERSITE=1
unset PYTHONPATH

python - <<'PY'
import ctypes

for library in ("libEGL.so.1", "libGL.so.1"):
    ctypes.CDLL(library)
print("Qt GL runtime: OK")
PY

architecture="$(uname -m)"
pip install --only-binary=:all: \
  -c "constraints-ubuntu24-${architecture}.txt" \
  ./gwexpy_studio-*.whl
gwexpy-studio
~~~

If libEGL.so.1 or libGL.so.1 is missing, install libegl1 and libgl1 before retrying.

</details>

<details>
<summary>macOS 15 or newer / Apple Silicon</summary>

Use this route only when the same Release contains a macos15-arm64 asset.
Intel Macs are not supported by this route.

~~~bash
set -euo pipefail
test "$(uname -m)" = "arm64"
test "$(sw_vers -productVersion | cut -d. -f1)" -ge 15
sw_vers
~~~

~~~bash
conda create -n gwexpy-studio-macos python=3.12
conda activate gwexpy-studio-macos
export PYTHONNOUSERSITE=1
unset PYTHONPATH
pip install --only-binary=:all: \
  -c constraints-macos15-arm64.txt \
  ./gwexpy_studio-*.whl
gwexpy-studio
~~~

</details>

## 3. Install from a source checkout

Use this route when trial qualification is not required.
The supported Python version is 3.12.

~~~bash
python3.12 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python -m pip check
.venv/bin/gwexpy-studio
~~~

python -m pip install . installs the runtime dependencies, including PySide6, and creates the gwexpy-studio launcher inside .venv.
Pass one .gwxproj path to the launcher to open a saved project.

To add test and lint tools, install the dev extra in the same environment:

~~~bash
.venv/bin/python -m pip install '.[dev]'
~~~

When actively editing the checkout, an editable install is also available:

~~~bash
.venv/bin/python -m pip install -e '.[dev]'
~~~

Editable installation is for development and is not the trial-participant route.

## 4. The four steps after launch

The following images show the workflow shared by the trial and source routes.
You can follow these steps on this page without moving to a platform-specific page.

### Press Try Sample on the Welcome screen

![GWexpy Studio Welcome screen after startup](images/installation/01-welcome.png)

After startup preparation completes, **Try Sample** becomes available on the Welcome screen.
Choose **Open Data** instead when using your own data.

### Load the sample

![TimeSeries loaded through Try Sample](images/installation/02-try-sample.png)

Review the sample and press **Load** to display the TimeSeries in the central view.
The Sources panel shows the selected object, and Metadata shows its basic properties.

### Crop the TimeSeries and run ASD

![Crop and ASD result](images/installation/03-asd.png)

With the TimeSeries selected, run **Operations → Crop**, then run **Operations → ASD**.
History records timeseries.crop and timeseries.asd.

### Save and reopen the project

![Saved project reopened in GWexpy Studio](images/installation/04-reopen-project.png)

Use **File → Save Project** to save a .gwxproj, close Studio, and launch it again.
If **Review / Restore Data** is required after reopening, follow the displayed review step.
The project stores source references, operations, active state, view state, and the Undo/Redo position.

## 5. Minimum checks

Run these commands in the environment that should launch Studio:

~~~bash
python -m pip check
python -c "import gwexpy, PySide6; print('runtime imports: OK')"
test -x "$(command -v gwexpy-studio)" && echo "launcher: OK"
~~~

For a trial build, also confirm the Build ID in the Release asset and its bundled manifest.
Do not replace the trial wheel install with python -m pip install ., change its constraints, or build a dependency from source.

## If Studio does not start

First save the output from pip check, the architecture check, the Python version check, and the Qt GL check.
On WSL2, confirm /mnt/wslg and powershell.exe again.

Use the following only when you need detailed qualification records, diagnostics, or feedback formats:

- [Ubuntu Quick Start](Quick-Start.md)
- [WSL2 Quick Start](trial/wsl2/Quick-Start.md)
- [macOS Quick Start](trial/macos/Quick-Start.md)

If a WSL2 preflight fails, do not continue from native Ubuntu or use a source build as a workaround.
The source route does not carry a trial Build ID, release checksum, qualification record, or pinned trial resolution.

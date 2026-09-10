# GWexpy Studio

[日本語](README.ja.md)

GWexpy Studio is a desktop GUI for inspecting scientific data, running GWexpy
analysis, saving a project, and exporting ordinary Python.

It is intended for researchers and students who use Python tools but do not
need to learn Git or begin by writing a notebook.

## Trial distribution

Invited participants receive one explicitly shared GitHub prerelease link.

Download the two release assets with the same base name:

- `gwexpy-studio-trial-<build-id>.zip`
- `gwexpy-studio-trial-<build-id>.zip.sha256`

Verify the outer checksum, unpack the ZIP, and follow the bundled Quick Start.

The currently published prerelease is limited to native Ubuntu 24.04 x86_64
with a conda Python 3.12 environment. It is not qualified for WSL2 or macOS and
must not be distributed to users of those platforms.

After entering the unpacked directory, the installation path is:

```bash
conda create -n gwexpy-studio python=3.12
conda activate gwexpy-studio
export PYTHONNOUSERSITE=1
unset PYTHONPATH
pip install --only-binary=:all: \
  -c constraints-ubuntu24-x86_64.txt \
  ./gwexpy_studio-*.whl
gwexpy-studio
```

The two environment settings keep packages from another Python environment or
source checkout out of the trial process.

The ZIP contains the wheel, architecture qualification records, checksums,
build identity, Quick Starts, and the Japanese feedback form.

Git, a source checkout, an editable install, and PyPI are not part of the
participant path.

Separate `wsl2-ubuntu24` and `macos15-arm64` prereleases will be published only
after both WSL2 architectures and Apple Silicon macOS have passed physical
technical qualification from the same source commit. Their participant guides
are under [docs/trial](docs/trial), but those guides do not make an unpublished
target available.

## First five minutes

The trial workflow is deliberately small:

1. Launch Studio.
2. Select **Try Sample**.
3. Crop the time series.
4. Run **ASD**.
5. Save the project, close Studio, reopen it, and review recovery if offered.

Projects use the `.gwxproj` extension.

They record source references, operations, active state, view state, and
scientific Undo/Redo position; source data and computed arrays stay outside the
project file.

## I/O availability

Registered GWexpy formats are not automatically available in a trial build.

The UI will present each data type, format, and direction as one of:

- **Verified**: reviewed policy and a worker-process runtime probe both pass.
- **Experimental**: the runtime probe passes, with a stated caveat.
- **Unavailable**: the policy, registry, or runtime dependency does not allow it.

The effective capability snapshot is included in path-free diagnostics.

## Roadmap and source development

[ROADMAP.md](ROADMAP.md) defines the public-source, Ubuntu reference, WSL2,
macOS, and cross-platform human-trial milestones.

The source is [MIT licensed](LICENSE).

Contributors who intentionally want a development environment should read
[docs/development.md](docs/development.md).

The public trial contract is in
[docs/release/0.1.0a1-trial-readiness.md](docs/release/0.1.0a1-trial-readiness.md).

# GWexpy Studio

[日本語](README.ja.md)

GWexpy Studio is a desktop GUI for inspecting scientific data, running GWexpy
analysis, saving a project, and exporting ordinary Python.

It is intended for researchers and students who use Python tools but do not
need to learn Git or begin by writing a notebook.

## Trial status

The first installable trial wheel is being prepared.

There is no public trial wheel or PyPI release yet, so this repository is not
an end-user installation path.

The planned first trial target is Ubuntu 24.04 with a conda Python 3.12
environment.

Its user path will be:

```bash
conda create -n gwexpy-studio python=3.12
conda activate gwexpy-studio
pip install --only-binary=:all: \
  -c constraints-ubuntu24-x86_64.txt \
  ./gwexpy_studio-0.1.0a1+trial.<build-id>-py3-none-any.whl
gwexpy-studio
```

The trial wheel, architecture-specific constraints, checksums, build identity,
and Quick Start will be attached to one GitHub prerelease.

The ARM64 Quick Start uses the matching `constraints-ubuntu24-aarch64.txt`.

Git, a source checkout, and an editable install are not part of that path.

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

[ROADMAP.md](ROADMAP.md) defines the public-source, wheel, Ubuntu, and WSL2
milestones.

The source is [MIT licensed](LICENSE).

Contributors who intentionally want a development environment should read
[docs/development.md](docs/development.md).

The public trial contract is in
[docs/release/0.1.0a1-trial-readiness.md](docs/release/0.1.0a1-trial-readiness.md).

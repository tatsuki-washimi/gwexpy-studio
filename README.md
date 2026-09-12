# GWexpy Studio

[日本語](README.ja.md)

GWexpy Studio is a desktop GUI for inspecting scientific data, running GWexpy
analysis, saving a project, and exporting ordinary Python.

It is intended for researchers and students who use Python tools but do not
need to learn Git or begin by writing a notebook.

## Trial distribution

The trial is distributed as a target-specific ZIP and a matching outer checksum
sidecar.

The candidate documents cover `ubuntu24-x86_64`, `debian13-x86_64`,
`wsl2-ubuntu24`, and `macos15-arm64`.

The presence of a guide does not mean that its target is published or qualified.
Use only the verified Release explicitly named by the sender.

Participants need conda with Python 3.12, but do not need Git, a source
checkout, an editable install, or a GitHub account.

Use the Quick Start for the target named by the sender.
It checks the exact ZIP, wheel, and constraints files supplied in that bundle,
creates a dedicated conda environment, and keeps the operating system Python
unchanged.

The target guides are under [docs/trial](docs/trial).
After publication, use the external distribution list to confirm the current
Release and the OS version actually tested.

The ZIP contains the wheel, constraints, checksums, build identity, target
Quick Start, and Japanese feedback form.

Do not use a bundle on another OS, CPU architecture, native or WSL mode, or
desktop environment.

The old Ubuntu reference artifact remains separate from this four-target trial.
Its schema 3 tag and assets are read-only compatibility material and do not
represent a newly qualified target.

## Basic workflow

The trial workflow is deliberately small:

1. Launch Studio.
2. Select **Try Sample**.
3. Crop the time series.
4. Run **ASD**.
5. Save the project, close Studio, and reopen it.

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

Feedback instructions are included in each bundle.

# GWexpy Studio

[日本語](README.ja.md)

GWexpy Studio is a desktop GUI for inspecting scientific data, running GWexpy analysis, saving a project, and exporting ordinary Python.

It is intended for researchers and students who use Python tools but do not need to learn Git or begin by writing a notebook.

![GWexpy Studio after startup](docs/images/installation/01-welcome.png)

## Start with the installation guide

The [installation guide](docs/installation.md) takes you from either a validated trial build or a source checkout to the first workflow on one page.
Use a platform Quick Start only when you need the detailed qualification or diagnostic record bundled with a release.

- For a validated trial build, download the matching ZIP and checksum, then choose one platform section in the installation guide.
- For this source checkout, create a Python 3.12 virtual environment and run `python -m pip install .`.

The trial route uses a prebuilt wheel and qualified constraints.
The source route resolves the package dependencies for development or local evaluation.

## Trial distribution

The trial is distributed as a target-specific ZIP and a matching outer checksum
sidecar.

Candidate guides cover `ubuntu24-x86_64`, `debian13-x86_64`,
`wsl2-ubuntu24`, and `macos15-arm64`. Candidate guides are documentation only;
published assets and their live status are listed on [GitHub Releases](https://github.com/tatsuki-washimi/gwexpy-studio/releases).

The presence of a guide does not mean that its target is published or qualified.
Use only the verified Release explicitly named by the sender.

Download the ZIP and checksum sidecar with the same Build ID.
Verify both the outer and inner checksums, then follow the matching platform section in the [installation guide](docs/installation.md).

Participants need conda with Python 3.12, but do not need Git, a source
checkout, an editable install, or a GitHub account.

The target guides are under [docs/trial](docs/trial).
Use the matching published Release and confirm its tested OS version before installing.

The ZIP contains the wheel, constraints, checksums, build identity, target
Quick Start, and Japanese feedback form.

Do not use a bundle on another OS, CPU architecture, native or WSL mode, or
desktop environment.

Git, a source checkout, an editable install, and PyPI are not part of the trial route.
Do not replace the trial wheel install with `python -m pip install .`, change its constraints, or build a dependency from source.

The old Ubuntu reference artifact remains separate from this four-target trial.
Its schema 3 tag and assets are read-only compatibility material and do not
represent a newly qualified target.

## Basic workflow

After installation, follow this sequence:

1. Launch Studio.
2. Select **Try Sample**.
3. Load and crop the TimeSeries.
4. Run **ASD**.
5. Save the project, close Studio, and reopen it.

The installation guide shows the same flow with four real application screenshots.

<details>
<summary>View the four workflow screens</summary>

![TimeSeries loaded through Try Sample](docs/images/installation/02-try-sample.png)

![Crop and ASD result](docs/images/installation/03-asd.png)

![Saved project reopened in GWexpy Studio](docs/images/installation/04-reopen-project.png)

</details>

Projects use the .gwxproj extension.

They record source references, operations, active state, view state, and scientific Undo/Redo position.
Source data and computed arrays stay outside the project file.

## I/O availability

Registered GWexpy formats are not automatically available in a trial build.

The trial enables these read routes:

| Data type | Format and reader |
| --- | --- |
| `TimeSeries` | CSV |
| `TimeSeries`, `TimeSeriesDict` | GWF: `gwf.lalframe` (recommended); `gwf` (also selectable) |
| `TimeSeries`, `TimeSeriesDict`, `TimeSeriesMatrix` | DiagGUI XML: `xml.diaggui`, product `TS` |
| `TimeSeries`, `TimeSeriesDict` | NDScope HDF5: `hdf.ndscope` |

The trial reads source files through these routes and does not write back to them. In **Open Data**, choose the data type and format, press **Inspect / Review**, review the result, and then explicitly press **Read Data**.
Dropping a file only fills the generic **Open Data** form; it does not identify the data type or format, or start a read.

On GWexpy 0.2.0, DiagGUI XML reads into `FrequencySeries`, `FrequencySeriesDict`, and `FrequencySeriesMatrix` are currently unavailable and fail closed.

The UI will present each data type, format, and direction as one of:

- **Verified**: reviewed policy and a worker-process runtime probe both pass.
- **Experimental**: the runtime probe passes, with a stated caveat.
- **Unavailable**: the policy, registry, or runtime dependency does not allow it.

The effective capability snapshot is included in path-free diagnostics.

## Roadmap and source development

[ROADMAP.md](ROADMAP.md) defines the public-source, Ubuntu reference, WSL2, macOS, and cross-platform human-trial milestones.

The source is [MIT licensed](LICENSE).

Contributors who intentionally want a development environment should read the [installation guide](docs/installation.md) and [development guide](docs/development.md).

The public trial contract is in [docs/release/0.1.0a1-trial-readiness.md](docs/release/0.1.0a1-trial-readiness.md).

Feedback instructions are included in each bundle.

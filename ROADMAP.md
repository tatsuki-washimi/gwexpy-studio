# GWexpy Studio Roadmap

## Product goals

GWexpy Studio is intended for two primary use cases:

1. **Help people who are not yet comfortable with Python, Jupyter, GWpy, or GWexpy learn the analysis workflow through a GUI.**
2. **Provide a fast way to inspect and analyze newly acquired data without writing a program for every quick-look task.**

The primary users are expected to be physics researchers and students working with experimental hardware and data analysis. Many of them may be comfortable with `conda` or `pip`, but not with Git or software-development workflows. Therefore, **`git clone` is not a normal end-user installation path**.

The near-term distribution strategy is:

> Make the application installable without Git using a wheel and `conda`/`pip`, validate it with a small number of real users, then move to PyPI and finally to native desktop artifacts such as AppImage or DMG.

---

## Current stage

The private development line already contains the main scientific-workbench functionality, including:

- GUI data browsing and plotting
- GWexpy-native I/O and container handling
- arithmetic operations and filtering
- PSD, CSD, coherence, transfer-function analysis, and Bode display
- project save/reopen
- scientific Undo/Redo and independent View Undo/Redo
- crash/recovery handling
- provenance and reconstruction checks
- Python export independent of Studio at runtime
- initial distribution/readiness infrastructure

Before publishing `0.1.0a1` to PyPI, the next priority is **real human trial**, not additional signal-processing features.

---

# Phase 1 — Public source migration

## Goal

Move the normal product-development and trial-preparation path from the private development repository to the public `gwexpy-studio` repository.

## Scope

- Integrate the public-ready portions of PR4, PR5, PR6, and the distribution/readiness work.
- Generate a curated public source snapshot.
- Exclude private audit evidence, internal notes, private workflow data, secrets, and machine-specific material.
- Include the public product source, tests, fixtures, schemas, packaging/build scripts, documentation, and CI required to reproduce the public build and checks.
- Verify that the public commit matches the canonical release-source manifest.
- Update the public README and user-facing installation/trial documentation.

After migration:

| Repository | Role |
| --- | --- |
| `gwexpy-studio` | Product source, normal PRs, Issues, CI, trial builds, releases |
| `gwexpy-studio-dev` | Private investigations, internal audit material, pre-disclosure security work, or other non-public development material |

Public source migration does **not** by itself mean that a binary release or PyPI release is ready.

---

# Phase 2 — Installable trial wheel

## Goal

Provide a trial build that can be installed by researchers without Git, before publishing to PyPI.

The initial trial artifact will be a wheel built from a fixed source commit.

Example installation path:

```bash
conda create -n gwexpy-studio python=3.12
conda activate gwexpy-studio
pip install ./gwexpy_studio-0.1.0a1+trial.<build-id>-py3-none-any.whl
gwexpy-studio
```

## Requirements

- `gwexpy-studio` GUI entry point is available after installation.
- Clean installation works outside the source tree.
- No Git checkout or editable install is required.
- Welcome / Try Sample works.
- Quick Start is available to trial users.
- About/diagnostics identifies the product version, trial build ID, and source commit.
- Trial builds are distinguishable from the final `0.1.0a1` publication build.

This phase intentionally does **not** require AppImage, `.deb`, DMG, Windows installer, or PyPI publication.

---

# Phase 3 — Human Trial 1: Ubuntu 24.04

## Goal

Validate installation and first-use workflow with a small number of real users, including people who do not normally use Git or develop software.

## Initial environment

- Ubuntu 24.04
- conda environment with Python 3.12
- trial wheel installation
- no source checkout

## Five-minute trial

```text
Install
  ↓
Launch
  ↓
Try Sample
  ↓
Crop
  ↓
ASD
  ↓
Save Project
  ↓
Close Studio
  ↓
Reopen
  ↓
Restore
```

The observer should avoid teaching the UI step by step. The goal is to see whether the user can complete the workflow from the Quick Start and the application itself.

## Evaluate

- Can the user create the conda environment and install the wheel?
- Does `gwexpy-studio` start without development knowledge?
- Is the Welcome screen understandable?
- Are Sources, Metadata, History, Save Project, and Export Python distinguishable?
- Can the user perform Crop and ASD without assistance?
- Can the user reopen and restore the analysis?
- Which messages, parameters, or concepts are confusing?

---

# Phase 4 — Human Trial 2: Windows 11 + WSL2

## Goal

Validate the same workflow in an environment expected to be common among target users.

## Initial target

- Windows 11
- WSL2
- Ubuntu 24.04 under WSL
- WSLg
- conda Python 3.12
- the same trial wheel

## Additional checks

- WSLg GUI rendering and display scaling
- worker spawn/exit and shared-memory cleanup
- files inside the WSL filesystem
- files under `/mnt/c/...`
- spaces and non-ASCII paths
- project save/reopen
- source-file change detection
- crash/recovery behavior
- clipboard and Python export

The result of this trial will determine whether WSL is sufficient for early Windows users or whether native Windows packaging should be prioritized.

---

# Phase 5 — Trial feedback and stabilization

Issues found during Ubuntu and WSL trials will be grouped into three categories.

## Installation

Examples:

- Python version constraints
- conda environment creation
- dependency resolution
- PySide6 installation
- optional I/O backends

## User experience

Examples:

- unclear data/object selection
- confusing History or recovery behavior
- Save Project vs Export Python
- difficult parameter entry
- unclear error messages

## Scientific workflow

Examples:

- filter parameter semantics
- PSD vs ASD choice
- transfer-function direction
- container/member selection
- Bode interpretation

No major new analysis feature is required during this phase unless the trial exposes a blocker.

A specific question to evaluate is whether the learning-oriented goal needs a stronger GUI-to-Python bridge, for example:

```text
History item
  ├─ Parameters
  ├─ Result
  └─ Show Python
```

The priority of such a feature should be determined from actual user feedback rather than assumed in advance.

---

# Phase 6 — Wider platform trial

After the Ubuntu/WSL path is stable, expand the wheel-based trial.

## Debian 13

- Do not depend on Debian's system Python version.
- Use a dedicated conda Python 3.12 environment initially.
- Validate installation, GUI, worker behavior, I/O, project persistence, recovery, and export.
- If the conda/pip path remains a significant usability barrier, prioritize a `.deb` or other native Linux distribution path.

## macOS

- Begin with wheel + conda Python 3.12 technical trials.
- Validate Apple Silicon first if that matches the available user hardware.
- Check PySide6, worker/process behavior, shared memory, project paths, file dialogs, shortcuts, recovery, and export.
- Native `.app`/DMG packaging and signing/notarization are later distribution work, not prerequisites for the first technical trial.

---

# Phase 7 — PyPI `0.1.0a1`

PyPI publication should happen **after** the first real-user trials.

Minimum publication criteria:

- Ubuntu 24.04 human trial completed
- Windows 11 + WSL2 human trial completed
- clean wheel installation verified
- primary GUI workflow verified
- project save/reopen/recovery verified
- no known major installation or usability blocker
- Quick Start validated by actual users
- build identity and source provenance are clear

Expected user installation path:

```bash
conda create -n gwexpy-studio python=3.12
conda activate gwexpy-studio
pip install gwexpy-studio
gwexpy-studio
```

Git is not required for normal users.

---

# Phase 8 — Native desktop distribution

After the wheel/PyPI trial path is stable, reduce the installation barrier further.

## Linux

Candidates:

- AppImage
- standalone archive
- `.deb`

Long-term goal:

```text
Download
  ↓
Launch
```

## macOS

- `.app`
- DMG
- Developer ID signing
- notarization

## Windows

Use WSL trial feedback to decide priority. If native Windows is needed, investigate:

- native standalone build
- installer
- Start-menu integration
- file association

The native packaging tracks do not need to complete simultaneously. A platform may enter trial distribution as soon as its own gate is satisfied.

---

# Phase 9 — Product maturity

Once installation and distribution are no longer the main blockers, continue improving the scientific workbench itself.

| Area | Candidate work |
| --- | --- |
| Learning | Show Python; visible mapping from GUI operations to GWexpy APIs |
| Comparison | before/after overlay; branch comparison; linked views |
| Interaction | Preview/Apply; mouse-based Crop |
| Data onboarding | generic CSV import; HDF5 browser; Raw/Logical views |
| Analysis | further filter/resample/PSD workflows; multi-channel analysis; fitting |
| Plotting | styling and publication-oriented export |
| Notebook interoperability | Marimo export |
| Projects | source relink; cache; more portable project handling |

---

# Milestones

| Milestone | Exit criterion |
| --- | --- |
| **M1 Public Source** | Public repository alone contains enough source/tests/build information to work on and verify the product |
| **M2 Trial Wheel** | A user can install a fixed trial wheel without Git |
| **M3 Ubuntu Trial** | Several users complete the five-minute workflow on Ubuntu 24.04 |
| **M4 WSL2 Trial** | Primary workflows work on Windows 11 + WSL2/WSLg |
| **M5 Feedback Fix** | Major installation/usability blockers from the first trials are resolved |
| **M6 Wider Trial** | Debian 13 and macOS trials begin |
| **M7 PyPI Alpha** | `pip install gwexpy-studio` is the supported alpha installation path |
| **M8 Native Distribution** | At least one platform supports a Download → Launch style installation |

---

## Immediate priority

The immediate sequence is:

```text
1. Migrate the public-ready source to this repository
2. Build a trial wheel
3. Run small human trials on Ubuntu 24.04
4. Run small human trials on Windows 11 + WSL2
5. Fix the installation and UX problems found there
6. Expand to Debian 13 and macOS
7. Publish the PyPI alpha
8. Continue native desktop packaging
```

The first user-facing success criterion is:

> **A physics researcher or student who does not use Git can use conda + pip to start GWexpy Studio, inspect data, perform a basic analysis, and save the work within a short first-use session.**

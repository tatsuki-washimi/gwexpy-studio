# Development from source

This document is for contributors who intentionally work from a source checkout.
For the short installation decision tree, see [the installation guide](installation.md).

It is not the installation method for trial participants.

Trial participants must use the release ZIP, checksum, constraints, and
platform-specific Quick Start supplied with the matching prerelease.

## Install the runtime

Use Python 3.12 from the repository root.

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python -m pip check
.venv/bin/gwexpy-studio
```

`python -m pip install .` installs the runtime dependencies, including the
PySide6 GUI dependency, and creates the launcher inside `.venv`.

## Add development tools

The `dev` extra supplies test, lint, and type-checking tools.

```bash
.venv/bin/python -m pip install '.[dev]'
```

When actively editing the checkout, use an editable install instead:

```bash
.venv/bin/python -m pip install -e '.[dev]'
```

The editable form is for development and is not part of the participant path.

To open an explicit saved project during development, pass one `.gwxproj` path.

```bash
.venv/bin/gwexpy-studio analysis.gwxproj
```

## Tests

Run the public source suite with the isolated environment.

```bash
.venv/bin/python -m pytest -q
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -c pytest-gui.ini -q gui_tests
```

The public-source migration contract is in
[release/0.1.0a1-trial-readiness.md](release/0.1.0a1-trial-readiness.md).

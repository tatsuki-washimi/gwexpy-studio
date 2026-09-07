# Development from source

This document is for contributors.

It is not the installation method for trial participants.

The first participant-facing trial will install a wheel into a conda Python
3.12 environment without Git or an editable install.

## Python environment

Use Python 3.12 from the repository root.

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -c constraints/signal-linux-py312.txt -e '.[dev]'
.venv/bin/python -m pip check
gwexpy-studio
```

Qt is a base runtime dependency of the trial wheel.

The `dev` extra supplies development tools.

To open an explicit saved project during development, pass one `.gwxproj` path.

```bash
gwexpy-studio analysis.gwxproj
```

## Tests

Run the public source suite with the isolated environment.

```bash
.venv/bin/python -m pytest -q
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -c pytest-gui.ini -q gui_tests
```

The public-source migration contract is in
[release/0.1.0a1-trial-readiness.md](release/0.1.0a1-trial-readiness.md).

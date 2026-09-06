# gwexpy-studio

GWexpy Studio is a desktop scientific workbench for GWexpy.

The project is being developed to make GWexpy-based analysis accessible to researchers and students who may not be comfortable with Python, Jupyter, GWpy, GWexpy, Git, or software-development workflows, while also providing a fast GUI for quick-look analysis of newly acquired data.

The current public roadmap prioritizes:

1. migrating the public-ready source and tests into this repository,
2. producing a Git-free trial wheel,
3. running human trials on Ubuntu 24.04 and Windows 11 + WSL2,
4. fixing installation and usability blockers,
5. expanding trials to Debian 13 and macOS,
6. publishing the PyPI alpha,
7. then continuing native desktop packaging such as AppImage and DMG.

See [ROADMAP.md](ROADMAP.md) for the full development and distribution roadmap.

Normal end users are not expected to use `git clone`; the near-term installation target is a dedicated conda environment plus `pip`, followed later by native desktop artifacts.

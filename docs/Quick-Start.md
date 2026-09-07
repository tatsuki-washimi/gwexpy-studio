# GWexpy Studio trial quick start

This trial is for Ubuntu 24.04 Linux, including Ubuntu 24.04 running in WSL2.

Install Miniforge or another conda distribution before starting.

Download and unpack the trial bundle, then open a terminal in that directory.

The bundle already contains the wheel and the matching dependency constraints.

## Verify the download

Run this command before installing.

```bash
sha256sum -c SHA256SUMS
```

Every listed file must report `OK`.

Do not install if a checksum fails.

## Create the environment

```bash
conda create -n gwexpy-studio python=3.12
conda activate gwexpy-studio
```

## Install the trial wheel

Check the Linux architecture.

```bash
uname -m
```

For `x86_64`, run:

```bash
pip install --only-binary=:all: -c constraints-ubuntu24-x86_64.txt ./gwexpy_studio-*.whl
```

For `aarch64`, run:

```bash
pip install --only-binary=:all: -c constraints-ubuntu24-aarch64.txt ./gwexpy_studio-*.whl
```

`--only-binary=:all:` prevents pip from building native dependencies on the trial machine.

## Start Studio

```bash
gwexpy-studio
```

Choose **Try Sample**, crop the sample, calculate ASD, and save a project.

Close Studio, start it again with `gwexpy-studio`, and reopen the saved project.

Git, an editable install, and a source checkout are not required for this trial.

## If installation stops

Keep the terminal output, the trial bundle, and the Build ID shown in Studio's About dialog.

Do not substitute a different constraints file or rerun the install without `--only-binary=:all:`.

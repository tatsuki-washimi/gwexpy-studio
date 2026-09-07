"""Durable workspace generations and independent process ownership."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from gwexpy_studio.domain.project import Project
from gwexpy_studio.errors import ProjectFormatError
from gwexpy_studio.persistence import project_io


def _project(**kwargs):
    return Project(compatibility={"studio": "0.1.0", "gwexpy": "0.2.0"}, **kwargs)


@pytest.mark.contract("WSP-0045")
def test_project_save_syncs_file_and_parent(tmp_path, monkeypatch):
    calls = []
    original = os.fsync

    def fsync(fd):
        calls.append(os.fstat(fd).st_mode)
        original(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    project_io.save_project(_project(project_id="durable"), tmp_path / "作業.gwxproj")
    assert len(calls) >= 2


@pytest.mark.contract("WSP-0046")
def test_recovery_snapshot_is_independent_and_ignores_live_run(tmp_path):
    from gwexpy_studio.persistence.recovery import RecoveryStore

    owner = RecoveryStore(tmp_path)
    observer = RecoveryStore(tmp_path)
    project = _project(project_id="original", ui_state={"label": "日本語"})
    pending = {"kind": "read", "command_id": "command-1"}
    snapshot = owner.checkpoint(
        project, revision=2, original_path="/元/作業.gwxproj", pending=pending
    )
    project.ui_state = {"label": "changed"}
    pending["kind"] = "changed"
    assert snapshot.project().ui_state == {"label": "日本語"}
    assert snapshot.pending["kind"] == "read"
    assert observer.candidates() == []
    with pytest.raises(BlockingIOError):
        observer.discard(owner.run_id)
    owner.close()
    candidate = observer.candidates()[0]
    assert candidate.revision == 2
    assert candidate.original_path == "/元/作業.gwxproj"
    observer.discard(candidate.run_id)
    assert observer.candidates() == []
    observer.close(clean=True)


@pytest.mark.contract("WSP-0047")
def test_corrupt_current_falls_back_and_failed_write_preserves_previous(
    tmp_path, monkeypatch
):
    from gwexpy_studio.persistence.recovery import RecoveryStore

    owner = RecoveryStore(tmp_path)
    owner.checkpoint(_project(project_id="first"), revision=1)
    owner.checkpoint(_project(project_id="second"), revision=2)
    directory = tmp_path / owner.run_id
    (directory / "current.json").write_text("broken")
    original_replace = os.replace

    def fail_current(source, target):
        if Path(target).name == "current.json":
            raise OSError("disk failure")
        original_replace(source, target)

    monkeypatch.setattr(os, "replace", fail_current)
    with pytest.raises(OSError, match="disk failure"):
        owner.checkpoint(_project(project_id="third"), revision=3)
    owner.close()
    observer = RecoveryStore(tmp_path)
    restored = observer.load(owner.run_id)
    assert restored.project().project_id == "first"
    assert restored.generation == 1
    observer.close(clean=True)


@pytest.mark.parametrize(
    "number",
    [
        pytest.param(
            "1e999", marks=pytest.mark.contract("WSP-0048"), id="nonfinite-number"
        ),
        pytest.param(
            '"\\ud800"', marks=pytest.mark.contract("WSP-0049"), id="lone-surrogate"
        ),
    ],
)
def test_invalid_json_value_falls_back_without_breaking_candidate_discovery(
    tmp_path, number
):
    from gwexpy_studio.persistence.recovery import RecoveryStore

    owner = RecoveryStore(tmp_path)
    owner.checkpoint(_project(project_id="valid"), revision=1)
    owner.checkpoint(_project(project_id="corrupt"), revision=2)
    path = owner.directory / "current.json"
    raw = path.read_text().replace(
        '"pending":null', '"pending":{"number":' + number + "}"
    )
    path.write_text(raw)
    owner.close()
    observer = RecoveryStore(tmp_path)
    assert observer.candidates()[0].project().project_id == "valid"
    observer.close(clean=True)


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param("hash", marks=pytest.mark.contract("WSP-0050"), id="hash"),
        pytest.param("schema", marks=pytest.mark.contract("WSP-0051"), id="schema"),
        pytest.param("bool", marks=pytest.mark.contract("WSP-0052"), id="bool"),
        pytest.param("depth", marks=pytest.mark.contract("WSP-0053"), id="depth"),
        pytest.param(
            "duplicate", marks=pytest.mark.contract("WSP-0054"), id="duplicate"
        ),
    ],
)
def test_recovery_rejects_invalid_envelopes(tmp_path, mutation):
    from gwexpy_studio.persistence.recovery import RecoveryStore

    owner = RecoveryStore(tmp_path)
    owner.checkpoint(_project(project_id="original"), revision=1)
    path = tmp_path / owner.run_id / "current.json"
    document = json.loads(path.read_text())
    if mutation == "hash":
        document["project"]["project_id"] = "tampered"
    elif mutation == "schema":
        document["schema_version"] = 99
    elif mutation == "bool":
        document["revision"] = True
    elif mutation == "depth":
        value = []
        for _ in range(70):
            value = [value]
        document["pending"] = {"deep": value}
    if mutation == "duplicate":
        path.write_text('{"revision":1,"revision":2}')
    else:
        path.write_text(json.dumps(document))
    owner.close()
    observer = RecoveryStore(tmp_path)
    with pytest.raises(ProjectFormatError):
        observer.load(owner.run_id)
    assert observer.candidates() == []
    observer.close(clean=True)


@pytest.mark.contract("WSP-0055")
def test_project_lock_survives_replace_and_fork_owner_exit(tmp_path):
    from gwexpy_studio.persistence.locking import ProjectLock

    target = tmp_path / "共同作業.gwxproj"
    locks = tmp_path / "locks"
    owner = ProjectLock(target, root=locks)
    contender = ProjectLock(target, root=locks)
    assert owner.acquire()
    project_io.save_project(_project(project_id="same-target"), target)
    assert not contender.acquire()
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "from gwexpy_studio.persistence.locking import ProjectLock; "
            "import sys; l=ProjectLock(sys.argv[1],root=sys.argv[2]); "
            "print(l.acquire()); l.close()",
            str(target),
            str(locks),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert child.stdout.strip() == "False"
    owner.close()
    assert contender.acquire()
    contender.close()


@pytest.mark.contract("WSP-0056")
def test_lock_does_not_live_in_forked_worker(tmp_path):
    from gwexpy_studio.persistence.locking import ProjectLock

    owner = ProjectLock(tmp_path / "project.gwxproj", root=tmp_path / "locks")
    assert owner.acquire()
    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(ready_read)
        os.close(release_write)
        os.write(ready_write, b"1")
        os.read(release_read, 1)
        os._exit(0)
    try:
        os.close(ready_write)
        os.close(release_read)
        assert os.read(ready_read, 1) == b"1"
        owner.close()
        replacement = ProjectLock(tmp_path / "project.gwxproj", root=tmp_path / "locks")
        assert replacement.acquire()
        replacement.close()
    finally:
        os.write(release_write, b"1")
        os.close(release_write)
        os.close(ready_read)
        os.waitpid(child, 0)


@pytest.mark.contract("WSP-0057")
def test_project_directory_sync_failure_restores_original(tmp_path, monkeypatch):
    import stat

    path = tmp_path / "project.gwxproj"
    project_io.save_project(_project(project_id="before"), path)
    before = path.read_bytes()
    project = _project(project_id="after", modified="original-time")
    original = os.fsync

    def fail_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("directory fsync failed")
        original(fd)

    monkeypatch.setattr(os, "fsync", fail_directory)
    with pytest.raises(OSError, match="directory fsync"):
        project_io.save_project(project, path)
    assert path.read_bytes() == before
    assert project.modified == "original-time"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.contract("WSP-0058")
def test_new_project_parent_directories_are_durable(tmp_path, monkeypatch):
    from gwexpy_studio.persistence import atomic

    synced = []
    original = atomic.sync_directory

    def sync(directory):
        synced.append(directory)
        original(directory)

    monkeypatch.setattr(atomic, "sync_directory", sync)
    directory = tmp_path / "新規" / "保存先"
    project_io.save_project(
        _project(project_id="nested"), directory / "project.gwxproj"
    )
    assert {tmp_path, directory.parent, directory} <= set(synced)


@pytest.mark.contract("WSP-0059")
def test_atomic_cleanup_removes_only_abandoned_exact_target_transactions(tmp_path):
    import uuid

    from gwexpy_studio.persistence import atomic
    from gwexpy_studio.persistence.locking import FileLock

    assert callable(getattr(atomic, "cleanup_atomic_writes", None))
    target = tmp_path / "project.gwxproj"
    other = tmp_path / "other.gwxproj"
    entries = []
    for selected in (target, target, other):
        identity = uuid.uuid4().hex
        owner = tmp_path / f".gwx-{identity}.owner"
        owner.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "gwexpy-studio-atomic",
                    "target": str(selected),
                }
            )
        )
        temporary = tmp_path / f".gwx-{identity}.tmp"
        temporary.write_bytes(b"temporary")
        backup = tmp_path / f".gwx-{identity}.old"
        backup.write_bytes(b"previous")
        entries.append((owner, temporary, backup))
    live = FileLock(entries[0][0])
    assert live.acquire()
    atomic.cleanup_atomic_writes(target)
    assert all(path.exists() for path in entries[0])
    assert not any(path.exists() for path in entries[1])
    assert all(path.exists() for path in entries[2])
    live.close()
    atomic.cleanup_atomic_writes(target)
    assert not any(path.exists() for path in entries[0])
    assert all(path.exists() for path in entries[2])


@pytest.mark.contract("WSP-0060")
def test_recovery_size_limit_and_unserializable_intent_preserve_valid(tmp_path):
    from gwexpy_studio.persistence.recovery import RecoveryStore

    owner = RecoveryStore(tmp_path)
    owner.checkpoint(_project(project_id="valid"), revision=1)
    for pending in ({1: "invalid JSON key"}, {"token": object()}):
        with pytest.raises(ProjectFormatError):
            owner.checkpoint(_project(project_id="new"), revision=2, pending=pending)
    with pytest.raises(ProjectFormatError, match="size"):
        owner.checkpoint(
            _project(project_id="huge", ui_state={"text": "x" * (16 * 1024 * 1024)}),
            revision=2,
        )
    owner.close()
    observer = RecoveryStore(tmp_path)
    assert observer.load(owner.run_id).project().project_id == "valid"
    observer.close(clean=True)


@pytest.mark.contract("WSP-0061")
def test_project_load_invalid_utf8_raises_format_error(tmp_path):
    path = tmp_path / "invalid.gwxproj"
    path.write_bytes(b"\xff\xfe\xff")
    with pytest.raises(ProjectFormatError):
        project_io.load_project(path)


@pytest.mark.contract("WSP-0062")
def test_project_load_bounds_actual_read_when_file_grows(tmp_path, monkeypatch):
    import io

    path = tmp_path / "growing.gwxproj"
    project_io.save_project(_project(project_id="growing"), path)
    document = _project(
        project_id="growing", ui_state={"large": "x" * (16 * 1024 * 1024)}
    ).to_dict()
    raw = json.dumps(document)
    original_open = Path.open

    def growing_open(self, mode="r", **kwargs):
        if self == path:
            return io.BytesIO(raw.encode()) if "b" in mode else io.StringIO(raw)
        return original_open(self, mode, **kwargs)

    monkeypatch.setattr(Path, "open", growing_open)
    with pytest.raises(ProjectFormatError, match="size"):
        project_io.load_project(path)


@pytest.mark.contract("WSP-0063")
def test_recovery_generation_and_clean_exit(tmp_path):
    from gwexpy_studio.persistence.recovery import RecoveryStore

    store = RecoveryStore(tmp_path)
    one = store.checkpoint(_project(project_id="one"), revision=1)
    two = store.checkpoint(_project(project_id="two"), revision=2)
    assert (one.generation, two.generation) == (1, 2)
    with pytest.raises(BlockingIOError):
        RecoveryStore(tmp_path, run_id=store.run_id)
    store.close()
    resumed = RecoveryStore(tmp_path, run_id=store.run_id)
    three = resumed.checkpoint(_project(project_id="three"), revision=3)
    assert three.generation == 3
    assert three.to_dict()["revision"] == 3
    resumed.close(clean=True)
    resumed.close(clean=True)
    with pytest.raises(RuntimeError, match="closed"):
        resumed.checkpoint(_project(project_id="four"), revision=4)
    assert not list(store.directory.glob("*.json"))


@pytest.mark.parametrize(
    "version",
    [
        pytest.param(1, marks=pytest.mark.contract("WSP-0064"), id="v1"),
        pytest.param(2, marks=pytest.mark.contract("WSP-0065"), id="v2"),
    ],
)
def test_recovery_checkpoint_upgrades_legacy_project_document(tmp_path, version):
    from gwexpy_studio.persistence.recovery import RecoveryStore

    store = RecoveryStore(tmp_path)
    legacy = _project(project_id="legacy", schema_version=version)
    store.checkpoint(legacy, revision=1)
    document = json.loads((store.directory / "current.json").read_text())
    assert document["project"]["schema_version"] == 3
    assert legacy.schema_version == version
    store.close(clean=True)


@pytest.mark.contract("WSP-0066")
def test_recovery_uses_xdg_and_rejects_run_symlinks(tmp_path, monkeypatch):
    import uuid

    from gwexpy_studio.persistence.locking import state_directory
    from gwexpy_studio.persistence.recovery import RecoveryStore

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    store = RecoveryStore()
    assert store.root == tmp_path / "gwexpy-studio" / "recovery"
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    fake_id = str(uuid.uuid4())
    (store.root / fake_id).symlink_to(foreign, target_is_directory=True)
    assert store.candidates() == []
    with pytest.raises(ProjectFormatError, match="directory"):
        store.discard(fake_id)
    with pytest.raises(ProjectFormatError, match="UUID4"):
        store.load("../../foreign")
    store.close(clean=True)
    monkeypatch.setenv("XDG_STATE_HOME", "relative")
    assert state_directory() == Path.home() / ".local/state/gwexpy-studio"


@pytest.mark.contract("WSP-0067")
def test_recovery_load_in_fresh_process_imports_no_science_or_runtime(tmp_path):
    from gwexpy_studio.persistence.recovery import RecoveryStore

    store = RecoveryStore(tmp_path)
    store.checkpoint(_project(project_id="safe"), revision=1)
    store.close()
    script = """
import sys
from gwexpy_studio.persistence.recovery import RecoveryStore
store = RecoveryStore(sys.argv[1])
assert store.load(sys.argv[2]).project().project_id == 'safe'
assert not any(name in sys.modules for name in ('numpy','gwpy','gwexpy','scipy'))
assert not any(name.startswith(('gwexpy_studio.runtime', 'gwexpy_studio.worker'))
               for name in sys.modules)
store.close(clean=True)
"""
    subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), store.run_id], check=True
    )


@pytest.mark.parametrize(
    "malformed",
    [
        pytest.param("fifo", marks=pytest.mark.contract("WSP-0109"), id="fifo"),
        pytest.param("deep", marks=pytest.mark.contract("WSP-0110"), id="deep"),
    ],
)
def test_atomic_cleanup_bounds_malformed_owners(tmp_path, malformed):
    target = tmp_path / "project.gwxproj"
    broken = tmp_path / (".gwx-" + "1" * 32 + ".owner")
    if malformed == "fifo":
        os.mkfifo(broken)
    else:
        broken.write_text("[" * 2000 + "0" + "]" * 2000)
    identity = "2" * 32
    valid = tmp_path / f".gwx-{identity}.owner"
    valid.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "gwexpy-studio-atomic",
                "target": str(target),
            }
        )
    )
    temporary = tmp_path / f".gwx-{identity}.tmp"
    temporary.write_bytes(b"interrupted write")
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from gwexpy_studio.persistence.atomic import cleanup_atomic_writes; "
            "import sys; cleanup_atomic_writes(sys.argv[1])",
            str(target),
        ],
        check=True,
        timeout=5,
    )
    assert broken.exists()
    assert not valid.exists()
    assert not temporary.exists()


@pytest.mark.contract("WSP-0111")
def test_recovery_discard_removes_only_owned_generation_write_journals(tmp_path):
    from gwexpy_studio.persistence.recovery import RecoveryStore

    owner = RecoveryStore(tmp_path)
    owner.checkpoint(_project(project_id="committed"), revision=1)
    observer = RecoveryStore(tmp_path)
    for number, name in enumerate(("current.json", "previous.json")):
        identity = str(number) * 32
        journal = owner.directory / f".gwx-{identity}.owner"
        journal.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "gwexpy-studio-atomic",
                    "target": str(owner.directory / name),
                }
            )
        )
        for suffix in ("tmp", "old"):
            (owner.directory / f".gwx-{identity}.{suffix}").write_bytes(b"owned")
    with pytest.raises(BlockingIOError):
        observer.discard(owner.run_id)
    assert len(list(owner.directory.glob(".gwx-*"))) == 6
    owner.close()
    observer.discard(owner.run_id)
    assert not list(owner.directory.glob(".gwx-*"))
    observer.close(clean=True)

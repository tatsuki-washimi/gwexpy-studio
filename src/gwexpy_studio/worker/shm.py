"""Shared-memory array transport interface."""

from __future__ import annotations

import math
import os
import re
import secrets
import sys
import uuid
from dataclasses import dataclass
from multiprocessing import resource_tracker, shared_memory
from typing import Any, Literal

from ..errors import SharedMemoryError

SHM_SIZE_LIMIT_BYTES: int = 1 * 1024**3
_SHM_PREFIX_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,80}")
_DARWIN_SHM_PREFIX_MAX_BYTES = 14
_DARWIN_SHM_NAME_MAX_BYTES = 30
_tracker_owner_pid = os.getpid()

try:
    resource_tracker.ensure_running()
except Exception:
    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class SharedMemoryDescriptor:
    """Serializable descriptor for a worker-held shared memory block."""

    name: str
    dtype: str
    shape: tuple[int, ...]
    nbytes: int
    order: Literal["C"] = "C"
    unit: str


@dataclass(frozen=True, slots=True)
class SharedMemoryBlock:
    """A worker-owned shared-memory buffer holding a contiguous array."""

    descriptor: SharedMemoryDescriptor
    shm: shared_memory.SharedMemory

    @property
    def name(self) -> str:
        """Return the POSIX shared-memory segment identifier."""
        return self.descriptor.name


@dataclass(frozen=True, slots=True)
class SharedMemoryPreview:
    """Client-attached view with display-friendly copy-out."""

    values: Any
    coordinates: Any
    handle: Any


@dataclass(frozen=True, slots=True)
class FetchedArray:
    """A descriptor-attached array, retaining enough to release and re-verify it.

    ``SharedMemoryPreview`` alone carries only the copied-out values and their
    coordinates, so a consumer holding one cannot name the block it came from
    (needed to request ``release_shm``), cannot re-attach that exact descriptor
    after release (needed to prove release fails closed), and cannot see the
    transfer unit. Keeping the descriptor and unit alongside the preview makes
    the full fetch/verify/release cycle expressible from the consumer side.
    """

    descriptor: SharedMemoryDescriptor
    preview: SharedMemoryPreview
    unit: str


def _platform_is_darwin() -> bool:
    """Return whether explicit POSIX names use Darwin's 31-byte kernel cap."""
    return sys.platform == "darwin"


def _owned_shm_name() -> str | None:
    prefix = os.environ.get("GWEXPY_STUDIO_SHM_PREFIX")
    if prefix is None:
        return None
    if _SHM_PREFIX_PATTERN.fullmatch(prefix) is None:
        raise SharedMemoryError(
            "GWEXPY_STUDIO_SHM_PREFIX contains unsafe characters",
            code="invalid_payload",
        )
    if _platform_is_darwin() and len(prefix.encode("ascii")) > (
        _DARWIN_SHM_PREFIX_MAX_BYTES
    ):
        raise SharedMemoryError(
            "GWEXPY_STUDIO_SHM_PREFIX exceeds Darwin's 14-byte limit",
            code="invalid_payload",
        )
    suffix = secrets.token_hex(8) if _platform_is_darwin() else uuid.uuid4().hex
    name = f"{prefix}{suffix}"
    if _platform_is_darwin() and len(name.encode("ascii")) > _DARWIN_SHM_NAME_MAX_BYTES:
        raise SharedMemoryError(
            "Shared-memory name exceeds Darwin's 30-byte limit",
            code="invalid_payload",
        )
    return name


def create_block(array: Any) -> SharedMemoryBlock:
    """Create a worker-owned shared-memory block from a NumPy array."""
    global _tracker_owner_pid
    import numpy as np

    is_obj = (
        not isinstance(array, np.ndarray)
        or array.dtype == object
        or array.dtype.kind == "O"
    )
    if is_obj:
        raise SharedMemoryError(
            "Object dtype arrays cannot cross shared-memory boundary",
            code="invalid_payload",
        )

    if array.size == 0 or array.nbytes == 0:
        raise SharedMemoryError(
            "Zero-length arrays cannot create a shared-memory block",
            code="invalid_payload",
        )

    if array.nbytes > SHM_SIZE_LIMIT_BYTES:
        raise SharedMemoryError(
            f"Array size ({array.nbytes} bytes) exceeds limit "
            f"({SHM_SIZE_LIMIT_BYTES} bytes)",
            code="payload_too_large",
        )

    unit = getattr(array, "unit", None)
    unit_str = str(unit) if unit is not None else ""

    # GH / POSIX isolation: If this process inherited a resource tracker from a parent
    # without owning the tracker pid, detach from the parent tracker so worker death
    # unlinks worker-created blocks immediately.
    tracker = getattr(resource_tracker, "_resource_tracker", None)
    if tracker is not None and getattr(tracker, "_fd", None) is not None:
        if getattr(tracker, "_pid", None) is None or _tracker_owner_pid != os.getpid():
            try:
                os.close(tracker._fd)
            except OSError:
                pass
            tracker._fd = None
            tracker._pid = None
            _tracker_owner_pid = os.getpid()

    shm = shared_memory.SharedMemory(
        name=_owned_shm_name(),
        create=True,
        size=int(array.nbytes),
    )
    try:
        target = np.ndarray(array.shape, dtype=array.dtype, buffer=shm.buf, order="C")
        target[...] = np.ascontiguousarray(array)
    except BaseException:
        shm.close()
        shm.unlink()
        raise

    dtype_name = np.dtype(array.dtype).name
    shape_tuple = tuple(array.shape)

    descriptor = SharedMemoryDescriptor(
        name=shm.name,
        dtype=dtype_name,
        shape=shape_tuple,
        nbytes=int(array.nbytes),
        order="C",
        unit=unit_str,
    )
    return SharedMemoryBlock(descriptor=descriptor, shm=shm)


def attach_block(
    descriptor: SharedMemoryDescriptor,
    *,
    preview_stride: int | None = None,
) -> SharedMemoryPreview:
    """Attach a client copy-out block using the descriptor as source of truth.

    ``dtype``/``shape``/``nbytes`` are taken strictly from ``descriptor`` — no
    sidecar file and no float64/1-D fallback. Two independent mismatches are
    both hard failures (fail closed) rather than silent misinterpretation:

    1. **Internal self-consistency**: ``shape`` and ``dtype`` must imply
       exactly ``nbytes`` (``prod(shape) * dtype.itemsize == nbytes``). If a
       JSON round trip across the IPC boundary desynchronizes these fields,
       this is caught before any shared-memory block is even opened.
    2. **Descriptor vs. reality**: the declared ``nbytes`` must match the
       actual shared-memory block size once opened (checked below).

    Without check 1, a descriptor with an ``nbytes`` larger than what
    ``shape``/``dtype`` require would silently succeed and copy out only the
    smaller, truncated view instead of raising — the same class of defect as
    the sidecar-removal fix (G1) closed for the create-side path.
    """
    import numpy as np

    if descriptor.nbytes > SHM_SIZE_LIMIT_BYTES:
        raise SharedMemoryError(
            f"Requested size ({descriptor.nbytes} bytes) exceeds limit "
            f"({SHM_SIZE_LIMIT_BYTES} bytes)",
            code="payload_too_large",
        )

    expected_nbytes = int(np.dtype(descriptor.dtype).itemsize) * math.prod(
        descriptor.shape
    )
    if expected_nbytes != descriptor.nbytes:
        raise SharedMemoryError(
            "Shared memory descriptor is internally inconsistent: shape "
            f"{descriptor.shape} and dtype {descriptor.dtype!r} imply "
            f"{expected_nbytes} bytes, but descriptor declares "
            f"{descriptor.nbytes} bytes",
            code="shm_invalid_descriptor",
        )

    try:
        shm = shared_memory.SharedMemory(name=descriptor.name)
        try:
            shm_name = getattr(shm, "_name", shm.name)
            resource_tracker.unregister(shm_name, "shared_memory")
        except Exception:
            pass
    except FileNotFoundError as exc:
        raise SharedMemoryError(
            f"Shared memory block not found: {descriptor.name}",
            code="shm_not_found",
        ) from exc

    if shm.size != descriptor.nbytes:
        shm.close()
        raise SharedMemoryError(
            "Shared memory size mismatch: descriptor declares "
            f"{descriptor.nbytes} bytes, actual block is {shm.size} bytes",
            code="shm_invalid_descriptor",
        )

    dtype = np.dtype(descriptor.dtype)
    raw_array = np.ndarray(
        descriptor.shape, dtype=dtype, buffer=shm.buf, order=descriptor.order
    )

    if raw_array.ndim == 0:
        values = raw_array.copy()
        coordinates = np.array(0, dtype=np.int64)
    else:
        dim0_size = raw_array.shape[0]
        if preview_stride is not None and preview_stride > 0:
            values = raw_array[::preview_stride, ...].copy()
            coordinates = np.arange(dim0_size, dtype=np.int64)[::preview_stride].copy()
        else:
            values = raw_array.copy()
            coordinates = np.arange(dim0_size, dtype=np.int64).copy()

    return SharedMemoryPreview(
        values=values,
        coordinates=coordinates,
        handle=shm,
    )


def release_block(name: str, *, acknowledged: bool = True) -> None:
    """Close and unlink a shared-memory block after transfer completion."""
    if not acknowledged:
        raise SharedMemoryError(
            "release_block requires acknowledged=True to prevent premature unlinking",
            code="operation_failed",
        )

    try:
        shm = shared_memory.SharedMemory(name=name)
        shm.close()
        shm.unlink()
    except FileNotFoundError:
        pass

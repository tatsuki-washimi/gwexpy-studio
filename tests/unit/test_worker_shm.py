"""Contract tests for shared-memory descriptors and ownership boundaries."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import FrozenInstanceError, fields, is_dataclass
from multiprocessing import shared_memory
from typing import Any, cast

import numpy as np
import pytest

from gwexpy_studio.errors import PrototypeNotImplementedError
from gwexpy_studio.worker.protocol import SHM_SIZE_LIMIT_BYTES
from gwexpy_studio.worker.shm import (
    SharedMemoryDescriptor,
    attach_block,
    create_block,
    release_block,
)

pytestmark = pytest.mark.unit

_FOUNDATION_CODE = "STUDIO-FOUNDATION-NOT-IMPLEMENTED"
_CREATE_OWNER = "gwexpy_studio.worker.shm.create_block"
_ATTACH_OWNER = "gwexpy_studio.worker.shm.attach_block"
_RELEASE_OWNER = "gwexpy_studio.worker.shm.release_block"


class _UnitArray(np.ndarray):
    """A real ndarray carrying the unit metadata required by the boundary."""

    unit: str

    def __new__(cls, values: Any, unit: str = "m") -> _UnitArray:
        result = np.asarray(values).view(cls)
        result.unit = unit
        return result

    def __array_finalize__(self, source: Any) -> None:
        if source is not None:
            self.unit = getattr(source, "unit", "m")


def _assert_sentinel(error: PrototypeNotImplementedError, owner: str) -> None:
    assert type(error) is PrototypeNotImplementedError
    assert error.code == _FOUNDATION_CODE
    assert error.owner == owner


def _invoke(owner: str, callback: Callable[[], Any]) -> Any:
    """Call the API while preserving prototype sentinel identity."""
    try:
        return callback()
    except PrototypeNotImplementedError as error:
        _assert_sentinel(error, owner)
        raise


def _require_shm_error(error: Exception, *, code: str) -> None:
    """Require a declared coded shared-memory exception."""
    from gwexpy_studio import errors as error_module

    error_type = getattr(error_module, "SharedMemoryError", None)
    if error_type is None:
        pytest.fail(
            "gwexpy_studio.errors.SharedMemoryError(StudioError) must declare a .code "
            f"field for shared-memory failures such as {code}."
        )
    assert isinstance(error, error_type)
    if not hasattr(error, "code"):
        pytest.fail(
            "Add a non-empty .code attribute to "
            "gwexpy_studio.errors.SharedMemoryError."
        )
    assert error.code == code


def _expect_shm_error(
    owner: str,
    callback: Callable[[], Any],
    *,
    code: str,
) -> None:
    try:
        callback()
    except PrototypeNotImplementedError as error:
        _assert_sentinel(error, owner)
        raise
    except Exception as error:
        _require_shm_error(error, code=code)
    else:
        pytest.fail(f"shared-memory operation silently accepted invalid input: {code}")


def _array_for_case(case: str) -> _UnitArray:
    if case == "scalar":
        return _UnitArray(np.array(3.25, dtype=np.float64))
    if case == "one-mib":
        return _UnitArray(np.arange(1024**2 // 8, dtype=np.float64))
    if case == "sixteen-mib":
        return _UnitArray(np.arange(16 * 1024**2 // 8, dtype=np.float64))
    if case == "one-hundred-mib-long":
        return _UnitArray(np.arange(100 * 1024**2 // 8, dtype=np.float64))
    raise AssertionError(f"unknown array case: {case}")


def _unpack_created(value: Any) -> tuple[SharedMemoryDescriptor, Any]:
    """Require create_block to expose both metadata and an inspectable buffer."""
    assert value is not None, "create_block returned None"
    if isinstance(value, tuple) and len(value) == 2:
        descriptor, block = value
    else:
        descriptor = getattr(value, "descriptor", None)
        block = getattr(value, "shm", None)
        if block is None:
            block = getattr(value, "block", None)
    assert isinstance(descriptor, SharedMemoryDescriptor)
    if not isinstance(block, shared_memory.SharedMemory):
        pytest.fail(
            "create_block must return or expose "
            "an actual multiprocessing.shared_memory.SharedMemory owner "
            "alongside SharedMemoryDescriptor."
        )
    return descriptor, block


def _copy_from_block(
    block: Any, descriptor: SharedMemoryDescriptor
) -> np.ndarray[Any, Any]:
    assert isinstance(block, shared_memory.SharedMemory)
    buffer = block.buf
    view = np.ndarray(
        descriptor.shape,
        dtype=np.dtype(descriptor.dtype),
        buffer=buffer,
        order=descriptor.order,
    )
    return view.copy()


def _close_and_unlink(block: Any) -> None:
    close = getattr(block, "close", None)
    if callable(close):
        close()
    unlink = getattr(block, "unlink", None)
    if callable(unlink):
        try:
            unlink()
        except FileNotFoundError:
            pass


def _unpack_preview(
    attached: Any,
) -> tuple[np.ndarray[Any, Any], np.ndarray[Any, Any], Any]:
    """Require raw preview arrays and the attachment handle that owns them."""
    values: Any = None
    coordinates: Any = None
    handle: Any = None
    if isinstance(attached, dict):
        values = attached.get("values", attached.get("array"))
        coordinates = attached.get("coordinates")
        handle = attached.get("handle", attached.get("shm"))
    elif isinstance(attached, tuple) and len(attached) == 3:
        values, coordinates, handle = attached
    else:
        if isinstance(attached, np.ndarray):
            values = attached
        else:
            values = getattr(attached, "values", None)
        coordinates = getattr(attached, "coordinates", None)
        handle = getattr(attached, "handle", getattr(attached, "shm", None))
    if values is None or coordinates is None:
        pytest.fail(
            "Preview copy-out must expose "
            "raw values and coordinates, for example "
            "(values, coordinates, handle) or a mapping with all three fields."
        )
    if not isinstance(values, np.ndarray) or not isinstance(coordinates, np.ndarray):
        pytest.fail(
            "Preview copy-out values and "
            "coordinates must be NumPy arrays before ownership assertions."
        )
    if handle is None or not callable(getattr(handle, "close", None)):
        pytest.fail(
            "Preview copy-out must expose an "
            "attachment handle with close() so ownership can be asserted."
        )
    return values, coordinates, handle


@pytest.mark.contract("C-SHM-001")
def test_shared_memory_descriptor_has_exact_metadata_fields() -> None:
    """A descriptor names one C-order block and preserves dtype, shape, and unit."""
    descriptor = SharedMemoryDescriptor(
        name="studio-shm-1",
        dtype="float64",
        shape=(2, 3),
        nbytes=48,
        unit="m",
    )

    assert tuple(field.name for field in fields(descriptor)) == (
        "name",
        "dtype",
        "shape",
        "nbytes",
        "order",
        "unit",
    )
    assert descriptor.name == "studio-shm-1"
    assert descriptor.dtype == "float64"
    assert descriptor.shape == (2, 3)
    assert descriptor.nbytes == 48
    assert descriptor.order == "C"
    assert descriptor.unit == "m"


@pytest.mark.contract("C-SHM-002")
def test_shared_memory_descriptor_is_frozen_slotted_and_keyword_only() -> None:
    """Descriptor construction is immutable, slotted, and keyword-only."""
    assert is_dataclass(cast(Any, SharedMemoryDescriptor))
    assert getattr(SharedMemoryDescriptor, "__slots__") == (
        "name",
        "dtype",
        "shape",
        "nbytes",
        "order",
        "unit",
    )
    descriptor_type = cast(Any, SharedMemoryDescriptor)
    descriptor = descriptor_type(
        name="studio-shm-immutable",
        dtype="float64",
        shape=(1,),
        nbytes=8,
        unit="m",
    )
    with pytest.raises(FrozenInstanceError):
        setattr(descriptor, "name", "changed")
    signature = inspect.signature(cast(Any, SharedMemoryDescriptor))
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in signature.parameters.values()
    )


@pytest.mark.contract("C-SHM-003")
def test_shared_memory_default_limit_is_one_gibibyte() -> None:
    """The shared-memory allocator has one explicit one-GiB ceiling, defined once."""
    from gwexpy_studio.worker import protocol as protocol_module
    from gwexpy_studio.worker import shm as shm_module

    assert SHM_SIZE_LIMIT_BYTES == 1 * 1024**3
    assert shm_module.SHM_SIZE_LIMIT_BYTES is protocol_module.SHM_SIZE_LIMIT_BYTES


@pytest.mark.contract("C-SHM-004")
def test_create_block_rejects_object_dtype() -> None:
    """Object arrays cannot cross the raw shared-memory data-plane boundary."""
    array = np.array(["not raw bytes"], dtype=object)
    _expect_shm_error(
        _CREATE_OWNER,
        lambda: create_block(array),
        code="invalid_payload",
    )


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            "scalar",
            id="scalar",
            marks=pytest.mark.contract("C-SHM-005"),
        ),
        pytest.param(
            "one-mib",
            id="one-mib",
            marks=pytest.mark.contract("C-SHM-007"),
        ),
        pytest.param(
            "sixteen-mib",
            id="sixteen-mib",
            marks=pytest.mark.contract("C-SHM-008"),
        ),
        pytest.param(
            "one-hundred-mib-long",
            id="one-hundred-mib-long",
            marks=[pytest.mark.contract("C-SHM-009"), pytest.mark.long],
        ),
    ],
)
def test_create_block_copies_real_boundary_arrays(case: str) -> None:
    """Real NumPy arrays produce exact descriptors and independent copied values."""
    array = _array_for_case(case)
    created = _invoke(_CREATE_OWNER, lambda: create_block(array))
    descriptor, block = _unpack_created(created)
    try:
        assert descriptor.dtype == np.dtype(array.dtype).name
        assert descriptor.shape == array.shape
        assert descriptor.nbytes == array.nbytes
        assert descriptor.order == "C"
        assert descriptor.unit == array.unit
        copied = _copy_from_block(block, descriptor)
        assert np.array_equal(copied, np.asarray(array))
        assert not np.shares_memory(copied, array)
    finally:
        _close_and_unlink(block)


@pytest.mark.contract("C-SHM-006")
def test_create_block_rejects_zero_length_array() -> None:
    """A zero-length array cannot create a meaningful shared-memory block."""
    array = _UnitArray(np.empty((0,), dtype=np.float64))
    _expect_shm_error(
        _CREATE_OWNER,
        lambda: create_block(array),
        code="invalid_payload",
    )


@pytest.mark.contract("C-SHM-010")
def test_create_block_rejects_size_overflow_without_huge_allocation() -> None:
    """A real strided NumPy view can exercise the one-GiB check without allocation."""
    backing = np.zeros(1, dtype=np.float64)
    overflowing = np.lib.stride_tricks.as_strided(
        backing,
        shape=(SHM_SIZE_LIMIT_BYTES // backing.itemsize + 1,),
        strides=(0,),
    )
    assert overflowing.nbytes > SHM_SIZE_LIMIT_BYTES
    _expect_shm_error(
        _CREATE_OWNER,
        lambda: create_block(_UnitArray(overflowing)),
        code="payload_too_large",
    )


@pytest.mark.contract("C-SHM-017")
def test_create_block_defaults_unit_to_empty_string_without_a_unit_attribute() -> None:
    """A plain ndarray with no ``.unit`` attribute gets descriptor.unit == "".

    The data-plane transport stays unit-agnostic instead of inventing a
    physical unit. A bare ``np.ndarray`` is required to exercise this default
    because the other cases use ``_UnitArray`` with an explicit unit.
    """
    array = np.arange(4, dtype=np.float64)
    assert not hasattr(array, "unit")
    created = _invoke(_CREATE_OWNER, lambda: create_block(array))
    descriptor, block = _unpack_created(created)
    try:
        assert descriptor.unit == ""
    finally:
        _close_and_unlink(block)


@pytest.mark.contract("C-SHM-011")
def test_attach_block_rejects_an_actual_block_size_mismatch() -> None:
    """Attachment validates the descriptor's nbytes against a real block."""
    block = shared_memory.SharedMemory(create=True, size=16)
    try:
        descriptor = SharedMemoryDescriptor(
            name=block.name,
            dtype="uint8",
            shape=(8,),
            nbytes=8,
            unit="",
        )
        _expect_shm_error(
            _ATTACH_OWNER,
            lambda: attach_block(descriptor),
            code="shm_invalid_descriptor",
        )
    finally:
        _close_and_unlink(block)


@pytest.mark.parametrize(
    ("shape", "declared_nbytes"),
    [
        pytest.param(
            (10,),
            800,
            id="oversized-nbytes",
            marks=pytest.mark.contract("C-SHM-018"),
        ),
        pytest.param(
            (10,),
            40,
            id="undersized-nbytes",
            marks=pytest.mark.contract("C-SHM-019"),
        ),
    ],
)
def test_attach_block_rejects_shape_dtype_nbytes_self_inconsistency(
    shape: tuple[int, ...], declared_nbytes: int
) -> None:
    """A descriptor whose ``nbytes`` disagrees with ``shape`` x ``dtype.itemsize``.

    A real shared-memory block is sized to exactly ``declared_nbytes`` so the
    actual-size check cannot catch this mismatch; only descriptor
    self-consistency can. The two directions matter separately:

    - ``oversized-nbytes`` (``shape=(10,) float64`` => 80 bytes expected,
      ``nbytes=800`` declared): before the fix, this was **silently
      accepted** -- ``numpy.ndarray`` happily carved an 80-byte view out of
      the larger 800-byte buffer, with no exception at all.
    - ``undersized-nbytes`` (``shape=(10,) float64`` => 80 bytes expected,
      ``nbytes=40`` declared): before the fix, numpy itself raised an
      unguarded ``TypeError`` when constructing the ``ndarray`` view, which
      propagated raw instead of becoming a coded ``SharedMemoryError``.

    Both must now fail closed with the same ``shm_invalid_descriptor`` code
    used by the sibling actual-size check, before any shared-memory block is
    opened for the view.
    """
    block = shared_memory.SharedMemory(create=True, size=declared_nbytes)
    try:
        descriptor = SharedMemoryDescriptor(
            name=block.name,
            dtype="float64",
            shape=shape,
            nbytes=declared_nbytes,
            unit="",
        )
        _expect_shm_error(
            _ATTACH_OWNER,
            lambda: attach_block(descriptor),
            code="shm_invalid_descriptor",
        )
    finally:
        _close_and_unlink(block)


@pytest.mark.contract("C-SHM-020")
def test_create_and_attach_block_round_trip_preserves_two_dimensional_shape() -> None:
    """A 2-D, non-float64 array survives ``create_block`` -> ``attach_block`` exactly.

    Scalar and one-dimensional cases do not prove shape preservation for a
    multidimensional buffer. This test exercises the shared-memory boundary
    directly without depending on scientific type registration.
    """
    array = _UnitArray(
        (np.arange(24, dtype=np.float32).reshape(4, 6)) * np.float32(0.5),
        unit="Pa",
    )
    created = _invoke(_CREATE_OWNER, lambda: create_block(array))
    descriptor, block = _unpack_created(created)
    try:
        assert descriptor.shape == (4, 6)
        assert descriptor.dtype == "float32"
        assert descriptor.nbytes == array.nbytes

        attached = _invoke(_ATTACH_OWNER, lambda: attach_block(descriptor))
        values, _coordinates, handle = _unpack_preview(attached)
        try:
            assert values.shape == (4, 6)
            assert values.dtype == np.dtype("float32")
            assert np.array_equal(values, np.asarray(array))
        finally:
            handle.close()
    finally:
        _close_and_unlink(block)


@pytest.mark.contract("C-SHM-012")
def test_release_block_reports_copy_ack_failure() -> None:
    """Copy-out acknowledgement failure is explicit rather than silent cleanup."""
    parameters = inspect.signature(release_block).parameters
    ack_name = next(
        (name for name in ("acknowledged", "ack", "copy_ack") if name in parameters),
        None,
    )
    if ack_name is None:
        try:
            release_block("studio-shm-copy-failed")
        except PrototypeNotImplementedError as error:
            _assert_sentinel(error, _RELEASE_OWNER)
            raise
        pytest.fail(
            "release_block(name, *, acknowledged: bool) or an equivalent "
            "acknowledgement seam so copy/ack failure can be asserted."
        )
    _expect_shm_error(
        _RELEASE_OWNER,
        lambda: release_block("studio-shm-copy-failed", **{ack_name: False}),
        code="operation_failed",
    )


@pytest.mark.contract("C-SHM-013")
def test_release_block_is_idempotent_and_unlinks_a_real_block() -> None:
    """Double release is safe and a released block cannot be reattached."""
    block = shared_memory.SharedMemory(create=True, size=8)
    name = block.name
    try:
        _invoke(_RELEASE_OWNER, lambda: release_block(name))
        _invoke(_RELEASE_OWNER, lambda: release_block(name))
        block.close()
        with pytest.raises(FileNotFoundError):
            shared_memory.SharedMemory(name=name)
    finally:
        _close_and_unlink(block)


@pytest.mark.contract("C-SHM-014")
def test_attach_block_rejects_the_one_gibibyte_limit_overflow() -> None:
    """Attachment rejects an over-limit claim without allocating one GiB."""
    descriptor = SharedMemoryDescriptor(
        name="studio-shm-too-large",
        dtype="uint8",
        shape=(SHM_SIZE_LIMIT_BYTES + 1,),
        nbytes=SHM_SIZE_LIMIT_BYTES + 1,
        unit="",
    )
    _expect_shm_error(
        _ATTACH_OWNER,
        lambda: attach_block(descriptor),
        code="payload_too_large",
    )


@pytest.mark.contract("C-SHM-016")
def test_attach_block_accepts_the_exact_one_gibibyte_limit() -> None:
    """The size gate passes at exactly the limit; failure is lookup, not size."""
    descriptor = SharedMemoryDescriptor(
        name="studio-shm-at-limit",
        dtype="uint8",
        shape=(SHM_SIZE_LIMIT_BYTES,),
        nbytes=SHM_SIZE_LIMIT_BYTES,
        unit="",
    )
    _expect_shm_error(
        _ATTACH_OWNER,
        lambda: attach_block(descriptor),
        code="shm_not_found",
    )


@pytest.mark.contract("C-SHM-015")
def test_attach_copy_out_preserves_source_and_projects_preview_stride() -> None:
    """A real copy-out supports display ``[::n]`` values, coordinates, and count."""
    sources = (
        _UnitArray(np.arange(12, dtype=np.float64)),
        _UnitArray(np.arange(12, dtype=np.float64) * -7.0 + 101.0),
    )
    sources_before = tuple(source.copy() for source in sources)
    coordinates = np.arange(sources[0].size, dtype=np.int64)
    block = shared_memory.SharedMemory(create=True, size=sources[0].nbytes)
    try:
        signature = inspect.signature(attach_block)
        if "preview_stride" not in signature.parameters:
            descriptor = SharedMemoryDescriptor(
                name=block.name,
                dtype=np.dtype(sources[0].dtype).name,
                shape=sources[0].shape,
                nbytes=sources[0].nbytes,
                unit=str(sources[0].unit),
            )
            _invoke(
                _ATTACH_OWNER,
                lambda: attach_block(descriptor),
            )
            pytest.fail(
                "attach_block(descriptor, *, preview_stride) must exist, or move the "
                "display projection to a public client result API."
            )
        for source, source_before in zip(sources, sources_before, strict=True):
            target = np.ndarray(source.shape, dtype=source.dtype, buffer=block.buf)
            target[...] = source
            block_view = np.ndarray(
                source.shape,
                dtype=source.dtype,
                buffer=block.buf,
            )
            expected_values = source[::3].copy()
            expected_coordinates = coordinates[::3].copy()
            source_descriptor = SharedMemoryDescriptor(
                name=block.name,
                dtype=np.dtype(source.dtype).name,
                shape=source.shape,
                nbytes=source.nbytes,
                unit=str(source.unit),
            )
            attached = _invoke(
                _ATTACH_OWNER,
                lambda: attach_block(
                    source_descriptor,
                    preview_stride=3,
                ),
            )
            display_values, display_coordinates, handle = _unpack_preview(attached)
            assert not np.shares_memory(display_values, block_view)
            assert not np.shares_memory(display_coordinates, block_view)
            assert not np.shares_memory(display_values, source)
            assert not np.shares_memory(display_coordinates, coordinates)
            assert np.array_equal(source, source_before)
            handle.close()
            assert display_values.shape == expected_values.shape
            assert display_coordinates.shape == expected_coordinates.shape
            assert np.array_equal(display_values, expected_values)
            assert np.array_equal(display_coordinates, expected_coordinates)
            assert display_values.size == 4
            assert np.array_equal(source, source_before)
    finally:
        _close_and_unlink(block)

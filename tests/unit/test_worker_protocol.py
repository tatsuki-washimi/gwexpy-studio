"""Contract tests for the versioned worker control-plane boundary."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any, NoReturn, cast, get_args, get_type_hints

import pytest

from gwexpy_studio.errors import PrototypeNotImplementedError
from gwexpy_studio.worker.protocol import (
    CONTROL_MESSAGE_LIMIT_BYTES,
    ERROR_CODES,
    MESSAGE_TYPES,
    PROJECT_SIZE_LIMIT_BYTES,
    PROTOCOL_VERSION,
    REQUEST_TYPES,
    RESPONSE_TYPES,
    SHM_SIZE_LIMIT_BYTES,
    UUID4,
    ErrorCode,
    ErrorEnvelope,
    GetArrayRequestPayload,
    GetArrayResultPayload,
    MessageType,
    RequestEnvelope,
    RequestType,
    ResultEnvelope,
    decode_message,
    encode_message,
)
from gwexpy_studio.worker.shm import SharedMemoryDescriptor

pytestmark = pytest.mark.unit

_FOUNDATION_CODE = "STUDIO-FOUNDATION-NOT-IMPLEMENTED"
_REQUEST_ID = "00000000-0000-4000-8000-000000000001"
_DECODE_OWNER = "gwexpy_studio.worker.protocol.decode_message"
_ENCODE_OWNER = "gwexpy_studio.worker.protocol.encode_message"


def _request(
    message_type: RequestType = "ping",
    payload: dict[str, Any] | None = None,
) -> RequestEnvelope:
    return {
        "protocol": PROTOCOL_VERSION,
        "request_id": UUID4(_REQUEST_ID),
        "type": message_type,
        "payload": {} if payload is None else payload,
    }


def _result() -> dict[str, Any]:
    return {
        "protocol": PROTOCOL_VERSION,
        "request_id": _REQUEST_ID,
        "type": "result",
        "payload": {"worker": "ready"},
    }


def _error() -> dict[str, Any]:
    return {
        "protocol": PROTOCOL_VERSION,
        "request_id": _REQUEST_ID,
        "type": "error",
        "code": "invalid_payload",
        "message": "bad payload",
    }


def _json_message(value: Any) -> bytes:
    """Build deliberately permissive input for negative decoder cases."""
    return json.dumps(value, separators=(",", ":"), allow_nan=True).encode("utf-8")


def _strict_json(data: bytes) -> dict[str, Any]:
    """Parse an encoded frame with strict UTF-8 and no JSON constants."""

    def reject_constant(value: str) -> NoReturn:
        raise AssertionError(f"encoder emitted non-finite JSON constant: {value}")

    assert isinstance(data, bytes)
    assert len(data) <= CONTROL_MESSAGE_LIMIT_BYTES
    return json.loads(
        data.decode("utf-8"),
        parse_constant=reject_constant,
    )


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


def _assert_validation_error(error: Exception, code: str) -> None:
    """Require a declared Studio-owned protocol validation error."""
    from gwexpy_studio import errors as error_module

    error_type = getattr(error_module, "ProtocolValidationError", None)
    if error_type is None:
        pytest.fail(
            "gwexpy_studio.errors.ProtocolValidationError(StudioError) must exist "
            "with a non-empty .code field for protocol validation failures."
        )
    assert isinstance(error, error_type)
    assert getattr(error, "code", None) == code


def _expect_rejection(
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
        _assert_validation_error(error, code)
    else:
        pytest.fail(f"invalid protocol input was silently accepted: expected {code}")


def _assert_uuid4(value: Any) -> None:
    parsed = uuid.UUID(str(value))
    assert parsed.version == 4
    assert parsed.variant == uuid.RFC_4122


def _assert_request_wire(value: dict[str, Any], expected: RequestEnvelope) -> None:
    assert set(value) == {"protocol", "request_id", "type", "payload"}
    assert value == dict(expected)
    assert value["protocol"] == 2
    assert value["request_id"] == _REQUEST_ID
    _assert_uuid4(value["request_id"])
    assert value["type"] in REQUEST_TYPES
    assert isinstance(value["payload"], dict)


def _assert_result_wire(value: dict[str, Any], expected: dict[str, Any]) -> None:
    assert set(value) == {"protocol", "request_id", "type", "payload"}
    assert value == expected
    assert value["protocol"] == 2
    assert value["request_id"] == _REQUEST_ID
    _assert_uuid4(value["request_id"])
    assert value["type"] == "result"
    assert isinstance(value["payload"], dict)


def _assert_error_wire(value: dict[str, Any], expected: dict[str, Any]) -> None:
    assert set(value) == {"protocol", "request_id", "type", "code", "message"}
    assert value == expected
    assert value["protocol"] == 2
    assert value["request_id"] == _REQUEST_ID
    _assert_uuid4(value["request_id"])
    assert value["type"] == "error"
    assert isinstance(value["code"], str) and value["code"]
    assert isinstance(value["message"], str) and value["message"]


@pytest.mark.contract("C-PROT-001")
def test_protocol_v2_limits_are_exact() -> None:
    """The public control, project, graph, and shared-memory limits are fixed."""
    from gwexpy_studio.worker.protocol import MAX_GRAPH_DEPTH

    assert PROTOCOL_VERSION == 2
    assert CONTROL_MESSAGE_LIMIT_BYTES == 1 * 1024**2
    assert PROJECT_SIZE_LIMIT_BYTES == 16 * 1024**2
    assert MAX_GRAPH_DEPTH == 64
    assert SHM_SIZE_LIMIT_BYTES == 1 * 1024**3


@pytest.mark.contract("C-PROT-002")
def test_request_response_message_type_sets_are_exact() -> None:
    """The wire discriminator vocabulary has no spurious operation type."""
    assert REQUEST_TYPES == {
        "ping",
        "execute",
        "inspect_source",
        "get_array",
        "release_shm",
        "delete_object",
        "list_objects",
        "shutdown",
        "catalog_io",
        "inspect_io",
        "list_members",
        "preview_data",
        "filter_preview",
        "write_data",
    }
    assert RESPONSE_TYPES == {"result", "error"}
    assert MESSAGE_TYPES == REQUEST_TYPES | RESPONSE_TYPES
    assert "operation" not in MESSAGE_TYPES


@pytest.mark.contract("C-PROT-003")
def test_error_vocabulary_is_exact() -> None:
    """All protocol errors use the approved stable machine-code vocabulary."""
    assert ERROR_CODES == {
        "protocol_mismatch",
        "malformed_message",
        "unknown_message_type",
        "request_id_mismatch",
        "payload_too_large",
        "invalid_payload",
        "object_not_found",
        "operation_failed",
        "io_capability_unavailable",
        "shm_not_found",
        "shm_invalid_descriptor",
        "worker_busy",
        "worker_timeout",
        "worker_crashed",
        "runtime_dependency_missing",
        "probe_failed",
        "registry_missing",
        "unreviewed_registry_entry",
    }
    assert set(get_args(ErrorCode)) == ERROR_CODES


@pytest.mark.contract("C-PROT-004")
def test_envelope_shapes_are_strict_and_correlated() -> None:
    """Every envelope requires v2, a UUID4 request ID, type, and object payload."""
    assert RequestEnvelope.__required_keys__ == {
        "protocol",
        "request_id",
        "type",
        "payload",
    }
    assert ResultEnvelope.__required_keys__ == {
        "protocol",
        "request_id",
        "type",
        "payload",
    }
    assert ErrorEnvelope.__required_keys__ == {
        "protocol",
        "request_id",
        "type",
        "code",
        "message",
    }
    assert get_type_hints(RequestEnvelope)["request_id"] is UUID4
    assert get_type_hints(RequestEnvelope)["type"] == RequestType
    assert (
        get_args(MessageType) == tuple(sorted(MESSAGE_TYPES))
        or set(get_args(MessageType)) == MESSAGE_TYPES
    )


@pytest.mark.contract("C-PROT-005")
def test_get_array_payload_declares_preview_controls_and_descriptor_result() -> None:
    """Array requests expose display controls and return a descriptor boundary."""
    request_hints = get_type_hints(GetArrayRequestPayload)
    assert set(request_hints) == {"object_id", "preview", "preview_stride"}
    assert request_hints["object_id"] is str
    assert request_hints["preview"] is bool
    assert request_hints["preview_stride"] == int | None

    result_hints = get_type_hints(GetArrayResultPayload)
    assert set(result_hints) == {"descriptor", "unit"}
    assert result_hints["descriptor"] is SharedMemoryDescriptor
    assert result_hints["unit"] is str


@pytest.mark.contract("C-PROT-006")
def test_encode_accepts_all_v2_request_types_with_strict_round_trip_wire() -> None:
    """Every request type encodes as exact UTF-8 JSON with retained correlation."""
    payloads: dict[RequestType, dict[str, Any]] = {
        "ping": {},
        "execute": {"operation": "timeseries.detrend", "inputs": ["obj-1"]},
        "inspect_source": {"uri": "file:///tmp/source.hdf5"},
        "get_array": {
            "object_id": "obj-1",
            "preview": True,
            "preview_stride": 4,
        },
        "release_shm": {"name": "shm-1"},
        "delete_object": {"object_id": "obj-1"},
        "list_objects": {},
        "shutdown": {},
        "catalog_io": {},
        "inspect_io": {},
        "list_members": {},
        "preview_data": {},
        "filter_preview": {},
        "write_data": {},
    }

    for message_type, payload in payloads.items():
        message = _request(message_type, payload)
        wire = _invoke(
            _ENCODE_OWNER,
            cast(Callable[[], Any], lambda message=message: encode_message(message)),
        )
        parsed = _strict_json(wire)
        _assert_request_wire(parsed, message)
        assert parsed["request_id"] == _REQUEST_ID
        assert parsed["type"] == message_type


@pytest.mark.contract("C-PROT-020")
def test_encode_accepts_a_v1_result_with_strict_envelope_wire() -> None:
    """A valid result is bounded UTF-8 JSON with no extra fields."""
    message = _result()
    wire = _invoke(_ENCODE_OWNER, lambda: encode_message(message))
    parsed = _strict_json(wire)
    _assert_result_wire(parsed, message)


@pytest.mark.contract("C-PROT-021")
def test_encode_accepts_a_v1_error_with_strict_envelope_wire() -> None:
    """A valid error preserves code, message, UUID4, and exact envelope fields."""
    message = _error()
    wire = _invoke(_ENCODE_OWNER, lambda: encode_message(message))
    parsed = _strict_json(wire)
    _assert_error_wire(parsed, message)


@pytest.mark.parametrize(
    "message_type,payload",
    [
        pytest.param(
            "ping",
            {},
            id="ping",
            marks=pytest.mark.contract("C-PROT-028"),
        ),
        pytest.param(
            "execute",
            {"operation": "timeseries.detrend", "inputs": ["obj-1"]},
            id="execute",
            marks=pytest.mark.contract("C-PROT-029"),
        ),
        pytest.param(
            "inspect_source",
            {"uri": "file:///tmp/source.hdf5"},
            id="inspect-source",
            marks=pytest.mark.contract("C-PROT-030"),
        ),
        pytest.param(
            "get_array",
            {"object_id": "obj-1", "preview": True, "preview_stride": 4},
            id="get-array",
            marks=pytest.mark.contract("C-PROT-031"),
        ),
        pytest.param(
            "release_shm",
            {"name": "shm-1"},
            id="release-shm",
            marks=pytest.mark.contract("C-PROT-032"),
        ),
        pytest.param(
            "delete_object",
            {"object_id": "obj-1"},
            id="delete-object",
            marks=pytest.mark.contract("C-PROT-033"),
        ),
        pytest.param(
            "list_objects",
            {},
            id="list-objects",
            marks=pytest.mark.contract("C-PROT-034"),
        ),
        pytest.param(
            "shutdown",
            {},
            id="shutdown",
            marks=pytest.mark.contract("C-PROT-035"),
        ),
    ],
)
def test_decode_round_trips_each_v1_request_type(
    message_type: RequestType,
    payload: dict[str, Any],
) -> None:
    """Each encoded request frame decodes exactly, including payload and UUID4."""
    message = dict(_request(message_type, payload))
    wire = _json_message(message)
    assert isinstance(wire, bytes)
    decoded = _invoke(
        _DECODE_OWNER,
        lambda: decode_message(wire),
    )
    assert decoded == message
    assert set(decoded) == {"protocol", "request_id", "type", "payload"}
    assert decoded["protocol"] == PROTOCOL_VERSION
    assert decoded["request_id"] == _REQUEST_ID
    _assert_uuid4(decoded["request_id"])
    assert decoded["type"] == message_type
    assert decoded["payload"] == payload

    for companion in (_result(), _error()):
        companion_decoded = _invoke(
            _DECODE_OWNER,
            cast(
                Callable[[], Any],
                lambda companion=companion: decode_message(_json_message(companion)),
            ),
        )
        assert companion_decoded == companion
        if companion["type"] == "result":
            _assert_result_wire(companion_decoded, companion)
        else:
            _assert_error_wire(companion_decoded, companion)


@pytest.mark.contract("C-PROT-007")
def test_decode_rejects_extra_request_fields() -> None:
    """Strict request decoding rejects fields outside the v1 envelope."""
    _expect_rejection(
        _DECODE_OWNER,
        lambda: decode_message(
            _json_message(
                {
                    "protocol": 2,
                    "request_id": _REQUEST_ID,
                    "type": "ping",
                    "payload": {},
                    "extra": True,
                }
            )
        ),
        code="invalid_payload",
    )


@pytest.mark.contract("C-PROT-008")
def test_decode_rejects_extra_result_fields() -> None:
    """Strict result decoding rejects fields outside the response envelope."""
    _expect_rejection(
        _DECODE_OWNER,
        lambda: decode_message(
            _json_message(
                {
                    "protocol": 2,
                    "request_id": _REQUEST_ID,
                    "type": "result",
                    "payload": {},
                    "extra": True,
                }
            )
        ),
        code="invalid_payload",
    )


@pytest.mark.contract("C-PROT-009")
def test_decode_rejects_extra_error_fields() -> None:
    """Strict error decoding rejects fields outside the error envelope."""
    _expect_rejection(
        _DECODE_OWNER,
        lambda: decode_message(
            _json_message(
                {
                    "protocol": 2,
                    "request_id": _REQUEST_ID,
                    "type": "error",
                    "code": "invalid_payload",
                    "message": "bad payload",
                    "extra": True,
                }
            )
        ),
        code="invalid_payload",
    )


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(
            b"\xff\xfe",
            id="invalid-utf8",
            marks=pytest.mark.contract("C-PROT-010"),
        ),
        pytest.param(
            b'{"protocol":2,',
            id="invalid-json",
            marks=pytest.mark.contract("C-PROT-011"),
        ),
    ],
)
def test_decode_rejects_malformed_utf8_or_json(data: bytes) -> None:
    """Malformed UTF-8 and JSON return the declared malformed-message code."""
    _expect_rejection(
        _DECODE_OWNER,
        lambda: decode_message(data),
        code="malformed_message",
    )


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(
            _json_message(
                {
                    "protocol": 2,
                    "request_id": _REQUEST_ID,
                    "type": "ping",
                    "payload": {"value": float("nan")},
                }
            ),
            id="nan",
            marks=pytest.mark.contract("C-PROT-012"),
        ),
        pytest.param(
            _json_message(
                {
                    "protocol": 2,
                    "request_id": _REQUEST_ID,
                    "type": "ping",
                    "payload": {"value": float("inf")},
                }
            ),
            id="positive-inf",
            marks=pytest.mark.contract("C-PROT-013"),
        ),
        pytest.param(
            _json_message(
                {
                    "protocol": 2,
                    "request_id": _REQUEST_ID,
                    "type": "ping",
                    "payload": {"value": float("-inf")},
                }
            ),
            id="negative-inf",
            marks=pytest.mark.contract("C-PROT-014"),
        ),
    ],
)
def test_decode_rejects_nonfinite_json_numbers(data: bytes) -> None:
    """NaN and infinities are invalid object payload values."""
    _expect_rejection(
        _DECODE_OWNER,
        lambda: decode_message(data),
        code="invalid_payload",
    )


@pytest.mark.contract("C-PROT-015")
def test_decode_rejects_a_control_frame_over_one_mib() -> None:
    """Control frames larger than one MiB are rejected before dispatch."""
    oversized = {
        "protocol": 2,
        "request_id": _REQUEST_ID,
        "type": "ping",
        "payload": {"blob": "x" * CONTROL_MESSAGE_LIMIT_BYTES},
    }
    _expect_rejection(
        _DECODE_OWNER,
        lambda: decode_message(_json_message(oversized)),
        code="payload_too_large",
    )


@pytest.mark.contract("C-PROT-016")
def test_decode_rejects_a_protocol_version_mismatch() -> None:
    """A message from another protocol version returns protocol_mismatch."""
    _expect_rejection(
        _DECODE_OWNER,
        lambda: decode_message(
            _json_message(
                {
                    "protocol": 1,
                    "request_id": _REQUEST_ID,
                    "type": "ping",
                    "payload": {},
                }
            )
        ),
        code="protocol_mismatch",
    )


@pytest.mark.contract("C-PROT-017")
def test_decode_rejects_an_unknown_request_type() -> None:
    """The historical operation discriminator returns unknown_message_type."""
    _expect_rejection(
        _DECODE_OWNER,
        lambda: decode_message(
            _json_message(
                {
                    "protocol": 2,
                    "request_id": _REQUEST_ID,
                    "type": "operation",
                    "payload": {},
                }
            )
        ),
        code="unknown_message_type",
    )


@pytest.mark.parametrize(
    "request_id",
    [
        pytest.param(
            "not-a-uuid",
            id="malformed-text",
            marks=pytest.mark.contract("C-PROT-018"),
        ),
        pytest.param(
            "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
            id="uuid1",
            marks=pytest.mark.contract("C-PROT-036"),
        ),
        pytest.param(
            "886313e1-3b8a-5372-9b90-0c9aee199e5d",
            id="uuid5",
            marks=pytest.mark.contract("C-PROT-037"),
        ),
    ],
)
def test_decode_rejects_a_non_uuid4_request_id(request_id: str) -> None:
    """Malformed, UUID1, and UUID5 IDs are rejected at the wire boundary."""
    if request_id != "not-a-uuid":
        assert uuid.UUID(request_id).version in {1, 5}
    _expect_rejection(
        _DECODE_OWNER,
        lambda: decode_message(
            _json_message(
                {
                    "protocol": 2,
                    "request_id": request_id,
                    "type": "ping",
                    "payload": {},
                }
            )
        ),
        code="invalid_payload",
    )


@pytest.mark.contract("C-PROT-019")
def test_decode_rejects_an_empty_error_code_or_message() -> None:
    """Error envelopes require non-empty machine code and human message fields."""
    invalid_errors = (
        {"code": "", "message": ""},
        {"code": None, "message": "bad payload"},
        {"code": "invalid_payload", "message": 7},
    )
    for fields in invalid_errors:
        _expect_rejection(
            _DECODE_OWNER,
            cast(
                Callable[[], Any],
                lambda fields=fields: decode_message(
                    _json_message(
                        {
                            "protocol": 2,
                            "request_id": _REQUEST_ID,
                            "type": "error",
                            **fields,
                        }
                    )
                ),
            ),
            code="invalid_payload",
        )


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(
            _json_message(
                {
                    "protocol": 2,
                    "request_id": _REQUEST_ID,
                    "type": "ping",
                }
            ),
            id="request-missing-payload",
            marks=pytest.mark.contract("C-PROT-022"),
        ),
        pytest.param(
            _json_message(
                {
                    "protocol": 2,
                    "request_id": _REQUEST_ID,
                    "type": "result",
                    "payload": [],
                }
            ),
            id="result-payload-not-object",
            marks=pytest.mark.contract("C-PROT-023"),
        ),
        pytest.param(
            _json_message(
                {
                    "protocol": 2,
                    "request_id": _REQUEST_ID,
                    "type": "error",
                    "code": "invalid_payload",
                }
            ),
            id="error-missing-message",
            marks=pytest.mark.contract("C-PROT-024"),
        ),
        pytest.param(
            _json_message(
                {
                    "protocol": 2,
                    "request_id": _REQUEST_ID,
                    "type": "ping",
                    "payload": "not-an-object",
                }
            ),
            id="request-payload-not-object",
            marks=pytest.mark.contract("C-PROT-025"),
        ),
    ],
)
def test_decode_rejects_missing_or_wrong_typed_envelope_fields(data: bytes) -> None:
    """Request, result, and error fields remain present and correctly typed."""
    _expect_rejection(
        _DECODE_OWNER,
        lambda: decode_message(data),
        code="invalid_payload",
    )


@pytest.mark.contract("C-PROT-026")
def test_decode_rejects_an_unknown_error_code() -> None:
    """Error responses cannot invent codes outside the fixed vocabulary."""
    value = _error()
    value["code"] = "made_up_error"
    _expect_rejection(
        _DECODE_OWNER,
        lambda: decode_message(_json_message(value)),
        code="invalid_payload",
    )


@pytest.mark.contract("C-PROT-027")
def test_encode_rejects_a_control_frame_over_one_mib() -> None:
    """The one-MiB control limit applies to outgoing encoded frames too."""
    request = _request()
    request["payload"] = {"blob": "x" * CONTROL_MESSAGE_LIMIT_BYTES}
    _expect_rejection(
        _ENCODE_OWNER,
        lambda: encode_message(request),
        code="payload_too_large",
    )
    nonfinite = _request()
    nonfinite["payload"] = {"value": float("nan")}
    _expect_rejection(
        _ENCODE_OWNER,
        lambda: encode_message(nonfinite),
        code="invalid_payload",
    )


def _json_depth(value: Any) -> int:
    """Return the structural JSON nesting depth of one data-only value.

    Same depth definition as ``tests/unit/test_project_io.py::_json_depth``
    (duplicated locally rather than imported, matching this test suite's
    existing no-cross-test-import convention), but written with an explicit
    stack instead of recursion: this test file also exercises documents at
    depth 512, and a naive recursive helper would itself hit Python's
    RecursionError well before that (measured — each Python-level recursive
    call costs more stack than the C json.loads scanner's per-level cost),
    which would make the *test* the thing overflowing instead of the
    production code under test.
    """
    if not isinstance(value, (dict, list)):
        return 0

    max_depth = 1
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        node, level = stack.pop()
        children = node.values() if isinstance(node, dict) else node
        for child in children:
            if isinstance(child, (dict, list)):
                child_level = level + 1
                max_depth = max(max_depth, child_level)
                stack.append((child, child_level))
    return max_depth


def _envelope_at_depth(depth: int) -> dict[str, Any]:
    """Build an otherwise-valid ping envelope with an exact structural depth.

    A bare envelope (``payload={"nested": "leaf"}``) already measures depth 2
    (envelope dict -> payload dict), so only ``depth - 2`` extra
    ``{"level": ...}`` wraps are needed to reach the requested depth —
    verified against ``_json_depth`` by the callers of this helper rather
    than assumed.
    """
    nested: Any = "leaf"
    for _ in range(depth - 2):
        nested = {"level": nested}
    return {
        "protocol": PROTOCOL_VERSION,
        "request_id": _REQUEST_ID,
        "type": "ping",
        "payload": {"nested": nested},
    }


@pytest.mark.parametrize(
    "depth",
    [
        pytest.param(64, id="depth-64", marks=pytest.mark.contract("C-PROT-038")),
        pytest.param(65, id="depth-65", marks=pytest.mark.contract("C-PROT-039")),
    ],
)
def test_decode_enforces_control_message_depth_boundary(depth: int) -> None:
    """The exact structural depth limit is accepted once and rejected above it."""
    document = _envelope_at_depth(depth)
    assert _json_depth(document) == depth

    if depth == 64:
        raw = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        decoded = _invoke(_DECODE_OWNER, lambda: decode_message(raw))
        assert decoded["type"] == "ping"
    else:
        _expect_rejection(
            _DECODE_OWNER,
            lambda: decode_message(
                json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
            ),
            code="invalid_payload",
        )


def _raw_deep_ping_message(depth: int) -> bytes:
    """Serialize an otherwise-valid ping envelope at ``depth`` without recursion.

    ``json.dumps`` recurses once per nesting level internally in its own
    encoder and — like ``json.loads`` — can itself raise ``RecursionError``
    on a sufficiently deep document (measured: on this envelope shape,
    ``json.dumps`` fails around the same depth ``json.loads`` does, roughly
    9998-9999 levels). Building the JSON text directly via string
    concatenation costs no Python call-stack depth regardless of how many
    levels are chained, so this helper can reach depths ``json.dumps``
    cannot, without the *test* becoming the thing that overflows.
    """
    wraps = depth - 2
    nested = ('{"level":' * wraps) + '"leaf"' + ("}" * wraps)
    text = (
        '{"protocol":' + str(PROTOCOL_VERSION) + ","
        f'"request_id":"{_REQUEST_ID}",'
        '"type":"ping",'
        '"payload":{"nested":' + nested + "}}"
    )
    return text.encode("utf-8")


@pytest.mark.contract("C-PROT-040")
def test_decode_converts_far_over_limit_nesting_into_protocol_error() -> None:
    """A document far beyond the depth limit fails cleanly, never with RecursionError.

    Exercises both places a pathologically deep document can reach
    ``decode_message``, with depths chosen to distinguish the parser boundary
    from the protocol validator boundary:

    - depth 4000: comfortably parseable by ``json.loads`` (measured ceiling
      ~9998 on this envelope shape) but far beyond ``MAX_GRAPH_DEPTH``, so
      this must be caught by ``_validate_envelope``'s own iterative depth
      walk (``_bounded_json_depth``). 4000 is deliberately *not* 512: 512
      only exceeds Python's default recursion limit (1000) for a
      two-frames-per-level recursive walk, so a regression to a naive
      one-frame-per-level recursive implementation would silently pass at
      512 (measured) while still being unbounded recursion. 4000 exceeds
      the recursion limit under either accounting, so it keeps failing if
      ``_bounded_json_depth`` regresses to any recursive form.
    - depth 20000: past the point where ``json.loads`` itself raises
      ``RecursionError`` (measured ceiling ~9998-9999 on this envelope
      shape) while its serialized size stays far under
      ``CONTROL_MESSAGE_LIMIT_BYTES`` (asserted below), so the size gate
      cannot intercept it before depth does. This exercises
      ``decode_message``'s own ``except (..., RecursionError)`` handling
      around ``json.loads`` directly.
    """
    document = _envelope_at_depth(4000)
    assert _json_depth(document) == 4000
    raw_moderate = json.dumps(
        document, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    _expect_rejection(
        _DECODE_OWNER,
        lambda: decode_message(raw_moderate),
        code="invalid_payload",
    )

    raw_far = _raw_deep_ping_message(20000)
    assert len(raw_far) < CONTROL_MESSAGE_LIMIT_BYTES
    _expect_rejection(
        _DECODE_OWNER,
        lambda: decode_message(raw_far),
        code="malformed_message",
    )

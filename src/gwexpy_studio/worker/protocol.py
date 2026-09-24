"""Typed JSON control-plane declarations for protocol version two."""

from __future__ import annotations

import json
import math
import uuid
from typing import Any, Literal, NewType, TypedDict, cast

from ..errors import ProtocolValidationError
from .shm import SHM_SIZE_LIMIT_BYTES, SharedMemoryDescriptor

PROTOCOL_VERSION = 2
CONTROL_MESSAGE_LIMIT_BYTES = 1 * 1024 * 1024
PROJECT_SIZE_LIMIT_BYTES = 16 * 1024 * 1024
MAX_GRAPH_DEPTH = 64

# Descriptive aliases keep the security limits discoverable at every boundary.
MAX_CONTROL_MESSAGE_BYTES = CONTROL_MESSAGE_LIMIT_BYTES
MAX_PROJECT_BYTES = PROJECT_SIZE_LIMIT_BYTES
MAX_SHARED_MEMORY_BYTES = SHM_SIZE_LIMIT_BYTES

UUID4 = NewType("UUID4", str)

RequestType = Literal[
    "catalog_io",
    "inspect_io",
    "list_members",
    "preview_data",
    "filter_preview",
    "write_data",
    "ping",
    "execute",
    "inspect_source",
    "get_array",
    "release_shm",
    "delete_object",
    "list_objects",
    "shutdown",
]
ResponseType = Literal["result", "error"]
MessageType = Literal[
    "catalog_io",
    "inspect_io",
    "list_members",
    "preview_data",
    "filter_preview",
    "write_data",
    "ping",
    "execute",
    "inspect_source",
    "get_array",
    "release_shm",
    "delete_object",
    "list_objects",
    "shutdown",
    "result",
    "error",
]

REQUEST_TYPES = frozenset(
    {
        "catalog_io",
        "inspect_io",
        "list_members",
        "preview_data",
        "filter_preview",
        "write_data",
        "ping",
        "execute",
        "inspect_source",
        "get_array",
        "release_shm",
        "delete_object",
        "list_objects",
        "shutdown",
    }
)
RESPONSE_TYPES = frozenset({"result", "error"})
MESSAGE_TYPES = REQUEST_TYPES | RESPONSE_TYPES

ErrorCode = Literal[
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
]
ERROR_CODES = frozenset(
    {
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
)

type JSONPrimitive = str | int | float | bool | None
type JSONValue = JSONPrimitive | list["JSONValue"] | dict[str, "JSONValue"]
type ObjectPayload = dict[str, JSONValue]


class RequestEnvelope(TypedDict):
    """Operation request with a v2 protocol discriminator."""

    protocol: Literal[2]
    request_id: UUID4
    type: RequestType
    payload: ObjectPayload


class ResultEnvelope(TypedDict):
    """Successful operation result correlated to one request."""

    protocol: Literal[2]
    request_id: UUID4
    type: Literal["result"]
    payload: ObjectPayload


class ErrorEnvelope(TypedDict):
    """Typed protocol error with a nonempty machine code and message."""

    protocol: Literal[2]
    request_id: UUID4
    type: Literal["error"]
    code: ErrorCode
    message: str


type Envelope = RequestEnvelope | ResultEnvelope | ErrorEnvelope
type ResponseEnvelope = ResultEnvelope | ErrorEnvelope


class GetArrayRequestPayload(TypedDict):
    """Typed payload declaration for a future get-array request."""

    object_id: str
    preview: bool
    preview_stride: int | None


class GetArrayResultPayload(TypedDict):
    """Typed payload declaration for a future shared-memory response."""

    descriptor: SharedMemoryDescriptor
    unit: str


def _bounded_json_depth(value: Any, *, limit: int) -> int:
    """Return the structural JSON depth of value using an explicit stack.

    Mirrors the recursive depth definition used across this codebase (every
    nested dict/list adds one level, a leaf contributes zero) without ever
    recursing, so a document far beyond ``limit`` cannot raise
    ``RecursionError`` before the depth limit is enforced. The walk stops
    as soon as the running depth exceeds ``limit`` — callers only need to
    distinguish "at or under the limit" from "over it", not the exact depth
    of a pathologically deep document.
    """
    if not isinstance(value, (dict, list, tuple)):
        return 0

    max_depth = 1
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        node, level = stack.pop()
        children = node.values() if isinstance(node, dict) else node
        for child in children:
            if isinstance(child, (dict, list, tuple)):
                child_level = level + 1
                if child_level > max_depth:
                    max_depth = child_level
                    if max_depth > limit:
                        return max_depth
                stack.append((child, child_level))
    return max_depth


def _check_finite_recursive(value: Any) -> None:
    """Ensure no float NaN or infinite values exist in data structures."""
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProtocolValidationError(
                f"Non-finite float value encountered: {value}",
                code="invalid_payload",
            )
    elif isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise ProtocolValidationError(
                    f"Dictionary key must be string: {k!r}",
                    code="invalid_payload",
                )
            _check_finite_recursive(v)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_finite_recursive(item)


def _validate_uuid4(request_id: Any) -> None:
    """Validate that request_id is a valid UUID4 string."""
    if not isinstance(request_id, str):
        raise ProtocolValidationError(
            "request_id must be a string",
            code="invalid_payload",
        )
    try:
        parsed = uuid.UUID(request_id)
        if parsed.version != 4 or parsed.variant != uuid.RFC_4122:
            raise ProtocolValidationError(
                f"request_id must be UUID4 (seen version={parsed.version})",
                code="invalid_payload",
            )
    except (ValueError, AttributeError) as exc:
        raise ProtocolValidationError(
            f"Invalid UUID4 string: {request_id!r}",
            code="invalid_payload",
        ) from exc


def _validate_envelope(message: dict[str, Any]) -> None:
    """Validate structural envelope correctness according to protocol v2."""
    if not isinstance(message, dict):
        raise ProtocolValidationError(
            "Control message must be a dictionary",
            code="invalid_payload",
        )

    # 0. Check structural depth before any recursive walk of the message
    # (in particular before _check_finite_recursive below, which is
    # itself unbounded recursion) so a pathologically deep document is
    # rejected here instead of overflowing the call stack later.
    depth = _bounded_json_depth(message, limit=MAX_GRAPH_DEPTH)
    if depth > MAX_GRAPH_DEPTH:
        raise ProtocolValidationError(
            f"Control message depth {depth} exceeds limit {MAX_GRAPH_DEPTH}",
            code="invalid_payload",
        )

    # 1. Check protocol version
    if "protocol" not in message:
        raise ProtocolValidationError(
            "Missing 'protocol' in envelope",
            code="invalid_payload",
        )
    proto = message["protocol"]
    if type(proto) is not int or proto != PROTOCOL_VERSION:
        raise ProtocolValidationError(
            f"Protocol version mismatch: expected {PROTOCOL_VERSION}, got {proto!r}",
            code="protocol_mismatch",
        )

    # 2. Check request_id
    if "request_id" not in message:
        raise ProtocolValidationError(
            "Missing 'request_id' in envelope",
            code="invalid_payload",
        )
    _validate_uuid4(message["request_id"])

    # 3. Check type
    if "type" not in message or not isinstance(message["type"], str):
        raise ProtocolValidationError(
            "Missing or invalid 'type' in envelope",
            code="invalid_payload",
        )
    msg_type = message["type"]
    if msg_type not in MESSAGE_TYPES:
        raise ProtocolValidationError(
            f"Unknown message type: {msg_type!r}",
            code="unknown_message_type",
        )

    # 4. Check envelope fields per message type
    if msg_type in REQUEST_TYPES or msg_type == "result":
        expected_keys = frozenset({"protocol", "request_id", "type", "payload"})
        actual_keys = frozenset(message.keys())
        if actual_keys != expected_keys:
            raise ProtocolValidationError(
                f"Invalid envelope keys for {msg_type}: {sorted(actual_keys)}",
                code="invalid_payload",
            )
        payload = message["payload"]
        if not isinstance(payload, dict):
            raise ProtocolValidationError(
                f"Payload for {msg_type} must be a dictionary",
                code="invalid_payload",
            )
        _check_finite_recursive(payload)
    elif msg_type == "error":
        expected_keys = frozenset({"protocol", "request_id", "type", "code", "message"})
        actual_keys = frozenset(message.keys())
        if actual_keys != expected_keys:
            raise ProtocolValidationError(
                f"Invalid envelope keys for error: {sorted(actual_keys)}",
                code="invalid_payload",
            )
        code = message["code"]
        if not isinstance(code, str) or code not in ERROR_CODES:
            raise ProtocolValidationError(
                f"Invalid error code: {code!r}",
                code="invalid_payload",
            )
        err_msg = message["message"]
        if not isinstance(err_msg, str) or not err_msg:
            raise ProtocolValidationError(
                "Error message must be a non-empty string",
                code="invalid_payload",
            )


def encode_message(message: Envelope) -> bytes:
    """Encode one bounded JSON control message with strict validation."""
    _validate_envelope(cast(dict[str, Any], message))
    raw = json.dumps(
        message, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    if len(raw) > CONTROL_MESSAGE_LIMIT_BYTES:
        raise ProtocolValidationError(
            f"Encoded message size ({len(raw)} bytes) exceeds limit "
            f"({CONTROL_MESSAGE_LIMIT_BYTES} bytes)",
            code="payload_too_large",
        )
    return raw


def decode_message(data: bytes) -> Envelope:
    """Decode one bounded JSON control message with strict validation."""
    if not isinstance(data, (bytes, bytearray)):
        raise ProtocolValidationError(
            "Encoded message data must be bytes",
            code="malformed_message",
        )

    if len(data) > CONTROL_MESSAGE_LIMIT_BYTES:
        raise ProtocolValidationError(
            f"Control message size ({len(data)} bytes) exceeds limit "
            f"({CONTROL_MESSAGE_LIMIT_BYTES} bytes)",
            code="payload_too_large",
        )

    try:
        text = data.decode("utf-8")
    except UnicodeError as exc:
        raise ProtocolValidationError(
            f"Invalid UTF-8 control message: {exc}",
            code="malformed_message",
        ) from exc

    def reject_constant(constant: str) -> None:
        raise ProtocolValidationError(
            f"Non-finite JSON constant encountered: {constant!r}",
            code="invalid_payload",
        )

    try:
        doc = json.loads(text, parse_constant=reject_constant)
    except json.JSONDecodeError as exc:
        raise ProtocolValidationError(
            f"Malformed JSON control message: {exc}",
            code="malformed_message",
        ) from exc
    except RecursionError as exc:
        # CPython's C JSON scanner still recurses once per nesting level
        # internally (measured: RecursionError first appears around depth
        # ~9998-9999 on this envelope shape, well under
        # CONTROL_MESSAGE_LIMIT_BYTES), so a pathologically deep document
        # can blow the call stack before ``_validate_envelope``'s own
        # iterative depth check ever runs. Treat that exactly like a
        # malformed document instead of letting RecursionError escape:
        # the message is intentionally static (no f-string over ``exc``)
        # because the stack is already near its limit here, and any
        # further formatting work risks a second RecursionError on the
        # way out.
        raise ProtocolValidationError(
            "Control message nesting is too deep to parse as JSON",
            code="malformed_message",
        ) from exc

    _validate_envelope(doc)
    return cast(Envelope, doc)

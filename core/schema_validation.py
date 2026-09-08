"""Schema-aware validation and normalisation of model-supplied tool arguments.

WHY THIS MODULE EXISTS.

A tool call has two representations and they used to be allowed to disagree.
There is what the PROVIDER serialised -- ``{"delta": 10}``, ``{"delta": 10.0}``,
``{"delta": "10"}`` -- and there is what the narrow handler actually executes,
which for all three of those is the integer ``10``, because
``core.pc_control.audio.parse_delta`` coerces through ``float()``.

The per-turn execution ledger in ``core.brain`` keys on the first
representation. Provider failover happens INSIDE a turn, so a Gemini round that
really changed the volume, followed by a Groq round that re-issues the same
request spelled ``10.0`` instead of ``10``, produced two different ledger keys
for one effect -- and the volume moved twice. That is the whole defect:
identity was computed on serialisation, execution on meaning.

So the arguments are normalised ONCE, centrally, against the tool's registered
schema, and everything downstream -- the ledger key, the policy engine, the
permission target and the handler itself -- sees that same normalised object.
One effect, one identity.

WHAT THIS IS NOT.

It is not a JSON Schema implementation. The registered schemas were inventoried
and use exactly this much of the vocabulary:

    type: object / string / integer / number / boolean / array
    properties, required, items, enum, default
    description, minimum, maximum, maxLength

``minimum``, ``maximum`` and ``maxLength`` are deliberately NOT enforced here.
They are range questions, not representation questions -- they cannot make two
spellings of one effect look different -- and the narrow handlers already
enforce them with a domain message a person can act on ("O volume tem de estar
entre 0 e 100"). Re-checking them here would replace that sentence with a
generic one and buy nothing.

``additionalProperties`` appears in no registered schema, so the JSON Schema
default applies: an undeclared property is allowed. It is passed through
untouched and it counts towards the identity, because the handler can see it.

THE RULE THAT BOUNDS EVERY DECISION BELOW: normalising may never let the model
do something it could not do before. Every coercion here is one a handler
already performed, so the set of accepted calls is unchanged and only its
SPELLING is collapsed. Where a handler was accidentally lenient --
``bool("false")`` is ``True`` -- this module is strict instead, which narrows,
never widens.
"""
from __future__ import annotations

import math
from typing import Any


class SchemaValidationError(ValueError):
    """An argument does not satisfy the tool's registered schema."""


#: Written by ToolExecutor as the authoritative permission target and read back
#: by PermissionManager. Whatever the MODEL supplies under this name is dropped
#: before anything reads it, so a crafted value cannot rebind a grant -- and,
#: because it is dropped, it must not reach the ledger key either. Two calls
#: differing only in a discarded key are one call.
RESERVED_KEYS = ("_pc_target",)


def _fail(field: str, expected: str, value: Any) -> SchemaValidationError:
    kind = type(value).__name__
    return SchemaValidationError(
        f"O argumento '{field}' tem de ser {expected} (recebido {kind}).")


def _finite(number: float, field: str) -> float:
    if math.isnan(number) or math.isinf(number):
        # A NaN volume is not a volume. Never coerced to a bound: that would
        # turn a malformed argument into an arbitrary real change.
        raise SchemaValidationError(
            f"O argumento '{field}' nao e um numero valido.")
    return number


def _as_number(value: Any, field: str, expected: str) -> float:
    """The one place a scalar becomes a number, and the one place bools do not.

    ``isinstance(True, int)`` is True in Python, so without the explicit bool
    check a ``true`` would sail through every numeric field as ``1``. It is
    rejected here rather than clamped, exactly as the handlers already do.
    """
    if isinstance(value, bool):
        raise _fail(field, expected, value)
    if isinstance(value, int):
        return float(value)
    if isinstance(value, float):
        return _finite(value, field)
    if isinstance(value, str):
        # Numeric strings are accepted because every numeric handler already
        # accepts them (they all coerce through `float()`), and a provider that
        # quotes a number must not turn a working command into a failure. This
        # widens nothing: it gives the values the handlers already took a single
        # canonical spelling, which is precisely what the ledger needs.
        text = value.strip()
        if not text:
            raise _fail(field, expected, value)
        try:
            return _finite(float(text), field)
        except (TypeError, ValueError):
            raise _fail(field, expected, value) from None
    raise _fail(field, expected, value)


def _normalize_scalar(value: Any, schema: dict, field: str) -> Any:
    declared = schema.get("type")

    if declared == "integer":
        number = _as_number(value, field, "um numero inteiro")
        if not float(number).is_integer():
            raise SchemaValidationError(
                f"O argumento '{field}' tem de ser um numero inteiro "
                f"(recebido {number:g}).")
        return int(number)

    if declared == "number":
        number = _as_number(value, field, "um numero")
        # An integral number canonicalises to int so that 10 and 10.0 are one
        # identity. Nothing downstream can tell the difference: Python treats
        # them identically in arithmetic and in formatting.
        return int(number) if number.is_integer() else number

    if declared == "boolean":
        # Strict. `bool("false")` is True, so a lenient handler would have
        # turned the string "false" into an ENABLE. Refusing is the narrow
        # reading and the safe one.
        if not isinstance(value, bool):
            raise _fail(field, "true ou false", value)
        return value

    if declared == "string":
        if not isinstance(value, str):
            raise _fail(field, "texto", value)
        return value

    # No declared type: the schema states no contract, so neither does this.
    return value


def _normalize_enum(value: Any, members: list, field: str) -> Any:
    """Strict membership, with the case-insensitivity the handlers already have.

    Every enum consumer in `core/pc_control` lowercases before matching
    (`geometry`, `keyboard`, `power`, `screen`, `settings`, `web`), so "Left"
    and "left" are already one call. Matching case-insensitively and returning
    the schema's own spelling keeps that true and gives it one identity; an
    exact-only match would instead REJECT a call that works today.
    """
    if value in members:
        return value
    if isinstance(value, str):
        folded = value.strip().casefold()
        for member in members:
            if isinstance(member, str) and member.casefold() == folded:
                return member
    allowed = ", ".join(str(m) for m in members)
    raise SchemaValidationError(
        f"O argumento '{field}' tem de ser um de: {allowed}.")


def _normalize_value(value: Any, schema: Any, field: str) -> Any:
    if not isinstance(schema, dict):
        return value

    declared = schema.get("type")

    if declared == "array":
        if isinstance(value, tuple):
            value = list(value)
        if not isinstance(value, list):
            raise _fail(field, "uma lista", value)
        items = schema.get("items")
        # Order is preserved, never sorted: for `pc_file_search.roots` the
        # order is the search order, so reordering would change the result.
        return [_normalize_value(item, items, f"{field}[{index}]")
                for index, item in enumerate(value)]

    if declared == "object" or "properties" in schema:
        if not isinstance(value, dict):
            raise _fail(field, "um objecto", value)
        return _normalize_object(value, schema, prefix=f"{field}.")

    value = _normalize_scalar(value, schema, field)
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        value = _normalize_enum(value, enum, field)
    return value


def _normalize_object(arguments: dict, schema: dict, *, prefix: str = "") -> dict:
    properties = schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    required = schema.get("required")
    required = list(required) if isinstance(required, list) else []

    normalized: dict[str, Any] = {}
    for key, value in arguments.items():
        field = f"{prefix}{key}"
        declared = properties.get(key)
        if value is None:
            # An explicit null and an absent argument reach the handler
            # identically, so they must not be two different calls. A REQUIRED
            # property is a different matter and is caught below.
            if key in required:
                raise SchemaValidationError(
                    f"O argumento '{field}' e obrigatorio.")
            continue
        if declared is None:
            # Undeclared, and no schema forbids it: passed through untouched.
            normalized[key] = value
            continue
        normalized[key] = _normalize_value(value, declared, field)

    for key, declared in properties.items():
        if key in normalized or not isinstance(declared, dict):
            continue
        if "default" in declared:
            # Every schema default in this repository is the same value the
            # handler's own signature already defaults to, so writing it here
            # changes no behaviour -- it only makes "omitted" and "sent
            # explicitly" one identity instead of two.
            normalized[key] = _normalize_value(
                declared["default"], declared, f"{prefix}{key}")

    for key in required:
        if key not in normalized:
            raise SchemaValidationError(
                f"O argumento '{prefix}{key}' e obrigatorio.")

    return normalized


def normalize_arguments(schema: Any, arguments: dict | None) -> dict:
    """Validate ``arguments`` against ``schema`` and return the effective call.

    The returned object is THE request: the ledger identity is computed from it
    and the handler executes it, so the two can no longer disagree. Raises
    ``SchemaValidationError`` before anything has been touched when the call
    does not satisfy the schema -- validation fails closed, and the caller must
    not execute.

    Idempotent by construction: normalising an already-normalised object
    returns an equal object, which is what lets ``Brain`` normalise for the
    ledger and ``ToolExecutor`` re-normalise as the authority without the two
    results drifting apart.
    """
    prepared = dict(arguments or {})
    for reserved in RESERVED_KEYS:
        prepared.pop(reserved, None)
    if not isinstance(schema, dict):
        return prepared
    if schema.get("type") not in (None, "object") or "properties" not in schema:
        # Not an object schema for a tool call; nothing here claims to know
        # what it means, so nothing is rewritten.
        return prepared
    return _normalize_object(prepared, schema)


__all__ = ["SchemaValidationError", "RESERVED_KEYS", "normalize_arguments"]

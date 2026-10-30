"""JSON schema emission and argument validation for the tool seam.

FR-6. Every tool declares its parameters as a JSON Schema object, the same object that reaches
the model in ``ChatRequest.tools``, and the same object every incoming call is checked against
before a handler ever runs. One declaration, two uses, so a tool cannot advertise one shape and
accept another.

The supported vocabulary is deliberately the subset both dialects render faithfully: ``type``,
``properties``, ``required`` and ``additionalProperties``. That is a decision, not an omission -
the architecture rejects a validation framework, and a tool that needs more expressive checking
does the extra checking in its own body where the error message can say something useful.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from harnessed.errors import ToolArgumentError, ToolError

__all__ = ["JSON_TYPES", "validate"]

#: The declared type words this validator understands, and what each accepts in Python.
#: ``bool`` is excluded from the numeric entries because ``True`` is an ``int`` here and is not
#: an integer in JSON, and a model that sends ``true`` for a count should be told so.
JSON_TYPES: Final[dict[str, tuple[type, ...]]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
    "null": (type(None),),
}


def _type_matches(declared: str, value: object) -> bool:
    """Whether ``value`` satisfies the declared JSON type word."""
    try:
        accepted = JSON_TYPES[declared]
    except KeyError as exc:
        raise ToolError(
            f"unknown JSON schema type {declared!r}. Known types: {', '.join(sorted(JSON_TYPES))}."
        ) from exc
    if isinstance(value, bool) and declared in {"integer", "number"}:
        return False
    return isinstance(value, accepted)


def validate(name: str, parameters: Mapping[str, Any], arguments: Mapping[str, Any]) -> None:
    """Check ``arguments`` against ``name``'s declared schema, or raise ``ToolArgumentError``.

    The message names the tool and the offending property, because it is handed straight back to
    the model as the content of an error :class:`~harnessed.providers.types.ToolResult` and a
    weak model can only self-correct from a sentence that says which argument was wrong.
    """
    if not isinstance(arguments, Mapping):
        raise ToolArgumentError(
            f"tool {name!r} takes a JSON object of arguments, got {type(arguments).__name__}"
        )

    properties: Mapping[str, Any] = parameters.get("properties", {})
    for required in parameters.get("required", ()):
        if required not in arguments:
            raise ToolArgumentError(
                f"tool {name!r} needs the {required!r} argument, which is absent"
            )

    if parameters.get("additionalProperties", True) is False:
        for supplied in arguments:
            if supplied not in properties:
                raise ToolArgumentError(
                    f"tool {name!r} takes no argument named {supplied!r}. "
                    f"Its arguments are: {', '.join(sorted(properties)) or 'none'}."
                )

    for key, declared in properties.items():
        if key not in arguments:
            continue
        expected = declared.get("type")
        if expected is None:
            continue
        if not _type_matches(expected, arguments[key]):
            raise ToolArgumentError(
                f"tool {name!r} needs {key!r} to be a JSON {expected}, got "
                f"{type(arguments[key]).__name__}"
            )

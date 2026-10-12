"""Decides whether a rule matches a video, with three outcomes: MATCH, NO_MATCH and UNKNOWN.

A rule keeps its condition as a JsonLogic tree of ``and``, ``or`` and ``!`` groups. JsonLogic has no regular
expression, so the custom operator ``regex`` adds one.
"""

import functools
import itertools
import json
import logging
import re
from collections.abc import Iterator, Mapping
from enum import StrEnum
from typing import Any

import datalogic_py

LOG = logging.getLogger(__name__)


class Verdict(StrEnum):
    MATCH = "match"
    NO_MATCH = "no_match"
    UNKNOWN = "unknown"


class RuleError(ValueError):
    """The rule tree is not valid, or it failed at evaluation."""


# A rule knows a channel only by its `UC...` id. A channel can change its name and its handle, and another channel
# can take them later, so no rule reads the channel name.
VIDEO_FIELDS = frozenset(
    {
        "video_id",
        "title",
        "channel_id",
        "duration_s",
        "upload_date",
        "live_status",
        "is_short",
    }
)
ALLOWED_OPERATORS = frozenset({"and", "or", "!", "==", "!=", "<", "<=", ">", ">=", "in", "var", "regex"})
MAX_UNKNOWN_LEAVES = 8

_GROUP_OPERATORS = frozenset({"and", "or", "!"})
_REGEX_FLAGS = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}

type _Path = tuple[int, ...]


@functools.lru_cache(maxsize=512)
def _compile(pattern: str, flags: str) -> re.Pattern[str]:
    combined = re.NOFLAG
    for flag in flags:
        combined |= _REGEX_FLAGS[flag]
    return re.compile(pattern, combined)


def _regex_operator(args_json: str) -> str:
    """``{"regex": [value, pattern]}`` or ``[value, pattern, flags]``: true when ``re.search`` finds the pattern."""
    value, pattern, *rest = json.loads(args_json)
    if not isinstance(value, str):
        return "false"
    flags = rest[0] if rest else ""
    return "true" if _compile(pattern, flags).search(value) else "false"


# The engine is thread-safe, so all threads share one instance.
_ENGINE = datalogic_py.Engine(custom_operators={"regex": _regex_operator})


def validate(tree: Any) -> None:
    """Raise RuleError unless ``tree`` is a JsonLogic object with only the allowed operators and video fields."""
    if not isinstance(tree, dict):
        raise RuleError(f"The root of a rule must be an object, not {type(tree).__name__}")
    _validate_node(tree)


def _validate_node(node: Any) -> None:
    if isinstance(node, list):
        for item in node:
            _validate_node(item)
        return
    if not isinstance(node, dict):
        return
    operator, args = _operator_and_args(node)
    if operator not in ALLOWED_OPERATORS:
        raise RuleError(f"The operator {operator!r} is not allowed")
    if operator == "var":
        name = _var_name(args)
        if not isinstance(name, str) or name not in VIDEO_FIELDS:
            raise RuleError(f"The field {name!r} is not a video field")
    elif operator == "regex":
        _validate_regex(args)
    _validate_node(args)


def _validate_regex(args: Any) -> None:
    if not isinstance(args, list) or len(args) not in (2, 3):
        raise RuleError(f"regex takes [value, pattern] or [value, pattern, flags], not {args!r}")
    pattern = args[1]
    flags = args[2] if len(args) == 3 else ""
    if not isinstance(pattern, str):
        raise RuleError(f"The regex pattern must be a string, not {pattern!r}")
    if not isinstance(flags, str) or not set(flags).issubset(_REGEX_FLAGS):
        raise RuleError(f"The regex flags must be a subset of 'ims', not {flags!r}")
    try:
        _compile(pattern, flags)
    except re.error as e:
        raise RuleError(f"The regex pattern {pattern!r} does not compile: {e}") from e


def _operator_and_args(node: dict) -> tuple[str, Any]:
    if len(node) != 1:
        raise RuleError(f"An operator object must have exactly one key, not {sorted(node)}")
    return next(iter(node.items()))


def _var_name(args: Any) -> Any:
    """The field name of a ``var``: ``"name"``, ``["name"]`` or ``["name", default]``."""
    if isinstance(args, list):
        return args[0] if args else None
    return args


def _is_group(node: Any) -> bool:
    return isinstance(node, dict) and next(iter(node)) in _GROUP_OPERATORS


def _group_items(args: Any) -> list:
    return args if isinstance(args, list) else [args]


def _leaves(node: Any, path: _Path = ()) -> Iterator[tuple[_Path, Any]]:
    """Each node outside the ``and``, ``or`` and ``!`` groups, with its path of argument indexes."""
    if not _is_group(node):
        yield path, node
        return
    _, args = _operator_and_args(node)
    for index, item in enumerate(_group_items(args)):
        yield from _leaves(item, (*path, index))


def _fields_read(node: Any) -> Iterator[str]:
    if isinstance(node, list):
        for item in node:
            yield from _fields_read(item)
    elif isinstance(node, dict):
        operator, args = _operator_and_args(node)
        if operator == "var":
            yield _var_name(args)
        yield from _fields_read(args)


def _substitute(node: Any, values: Mapping[_Path, bool], path: _Path = ()) -> Any:
    """A copy of ``node`` with the leaf at each path of ``values`` replaced by its boolean."""
    if path in values:
        return values[path]
    if not _is_group(node):
        return node
    operator, args = _operator_and_args(node)
    items = [_substitute(item, values, (*path, index)) for index, item in enumerate(_group_items(args))]
    return {operator: items if isinstance(args, list) else items[0]}


def _is_true(tree: Any, data_json: str) -> bool:
    try:
        result_json = _ENGINE.eval_str(json.dumps(tree), data_json)
    except datalogic_py.DataLogicError as e:
        raise RuleError(f"The rule failed at evaluation: {e}") from e
    # For each JSON value, the truth of Python is the truth of the engine: false, null, 0, "", [] and {} are false.
    return bool(json.loads(result_json))


def evaluate(tree: Any, video: Mapping[str, Any]) -> Verdict:
    """Decide whether the rule ``tree`` matches ``video``.

    A leaf is a node outside the ``and``, ``or`` and ``!`` groups, usually an operator node. A leaf is unknown when it
    reads a field that has no value in ``video`` (None or missing). The evaluator replaces the unknown leaves with each combination
    of true and false. The result is MATCH when every combination is true, NO_MATCH when every combination is false,
    and UNKNOWN otherwise. With more than MAX_UNKNOWN_LEAVES unknown leaves, the result is UNKNOWN without evaluation.

    Two leaves that read the same field count as independent. So "duration_s < 120 or duration_s >= 120" gives
    UNKNOWN for a video without a duration, although one of the two leaves must be true. This is safe, because
    UNKNOWN only means that the service checks the video again in a later run.

    Raise RuleError when ``validate`` refuses the tree, or when the tree fails at evaluation.
    """
    validate(tree)
    unknown = [path for path, leaf in _leaves(tree) if any(video.get(name) is None for name in _fields_read(leaf))]
    data_json = json.dumps({name: video.get(name) for name in VIDEO_FIELDS})
    if not unknown:
        return Verdict.MATCH if _is_true(tree, data_json) else Verdict.NO_MATCH
    if len(unknown) > MAX_UNKNOWN_LEAVES:
        LOG.debug("The rule has %d unknown leaves, more than %d: UNKNOWN", len(unknown), MAX_UNKNOWN_LEAVES)
        return Verdict.UNKNOWN
    outcomes = set()
    for combination in itertools.product((False, True), repeat=len(unknown)):
        outcomes.add(_is_true(_substitute(tree, dict(zip(unknown, combination, strict=True))), data_json))
        if len(outcomes) > 1:
            return Verdict.UNKNOWN
    return Verdict.MATCH if outcomes == {True} else Verdict.NO_MATCH

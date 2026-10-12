"""The model of the condition editor, and its conversion from and to the JsonLogic tree of a rule.

The visual editor shows a condition as groups and leaves. The root is always a group. A group is "all of" (``and``),
"any of" (``or``) or "none of" (``{"!": [{"or": [...]}]}``), and groups can nest. A leaf compares one video field
with a value, always in the form ``{operator: [{"var": field}, value]}``. A pattern leaf (``regex``) also has the flag
``i``, unless it is case-sensitive.

``from_tree`` returns None for each tree that the editor cannot show, and the page shows such a tree as JSON text. A
tree that ``from_tree`` accepts comes back unchanged from ``to_tree``. A condition without leaves is ``MATCH_ALL``,
and the editor shows it as an empty root group. This module imports no NiceGUI, so the conversion has tests of its
own.
"""

import copy
import dataclasses
import datetime
import json
import re
from collections.abc import Iterator, Mapping
from enum import StrEnum
from typing import Any

from youtube_automanager.engine.rules import MATCH_ALL, RuleError, validate
from youtube_automanager.i18n import N_, _


class GroupKind(StrEnum):
    ALL = "all"
    ANY = "any"
    NONE = "none"


class ValueKind(StrEnum):
    """The kind of value that a leaf takes. It decides the input of the editor for the value."""

    TEXT = "text"
    CHANNEL = "channel"
    CHANNELS = "channels"
    SECONDS = "seconds"
    DATE = "date"
    LIVE_STATUS = "live_status"
    SHORT = "short"


GROUP_KINDS = {
    GroupKind.ALL: N_("All of these conditions"),
    GroupKind.ANY: N_("Any of these conditions"),
    GroupKind.NONE: N_("None of these conditions"),
}

# The video fields in the order of the editor. A rule knows a channel only by its `UC...` id.
FIELDS = {
    "title": N_("Title"),
    "channel_id": N_("Channel"),
    "duration_s": N_("Duration in seconds"),
    "upload_date": N_("Upload date"),
    "live_status": N_("Live status"),
    "is_short": N_("Shorts"),
    "video_id": N_("Video id"),
}

_TEXT_OPERATORS = {"regex": N_("matches the pattern"), "==": N_("is"), "!=": N_("is not")}

# The operators of each field with their labels, in the order of the editor. A new leaf takes the first one.
OPERATORS = {
    "title": _TEXT_OPERATORS,
    "channel_id": {"in": N_("is one of"), "==": N_("is"), "!=": N_("is not"), "regex": N_("matches the pattern")},
    "duration_s": {
        ">=": N_("is at least"),
        ">": N_("is more than"),
        "<=": N_("is at most"),
        "<": N_("is less than"),
        "==": N_("is"),
        "!=": N_("is not"),
    },
    "upload_date": {
        ">=": N_("is on or after"),
        ">": N_("is after"),
        "<=": N_("is on or before"),
        "<": N_("is before"),
        "==": N_("is on"),
    },
    "live_status": {"==": N_("is"), "!=": N_("is not")},
    "is_short": {"==": N_("is")},
    "video_id": _TEXT_OPERATORS,
}

# The values of ``live_status`` that yt-dlp gives.
LIVE_STATUSES = {
    "not_live": N_("not live"),
    "is_live": N_("live now"),
    "is_upcoming": N_("upcoming"),
    "was_live": N_("a past live stream"),
    "post_live": N_("a live stream that just ended"),
}

SHORT_CHOICES = {True: N_("is a Short"), False: N_("is not a Short")}

# Above this number of channels, the description of a leaf gives the number and not the names.
MAX_NAMED_CHANNELS = 3


@dataclasses.dataclass
class Leaf:
    field: str = "title"
    operator: str = "regex"
    value: Any = ""
    # Only for the operator ``regex``: without this, the pattern ignores the case.
    case_sensitive: bool = False


@dataclasses.dataclass
class Group:
    kind: GroupKind = GroupKind.ALL
    items: list[Group | Leaf] = dataclasses.field(default_factory=list)


def value_kind(field: str, operator: str) -> ValueKind:
    if field == "channel_id":
        return {"in": ValueKind.CHANNELS, "regex": ValueKind.TEXT}.get(operator, ValueKind.CHANNEL)
    return {
        "duration_s": ValueKind.SECONDS,
        "upload_date": ValueKind.DATE,
        "live_status": ValueKind.LIVE_STATUS,
        "is_short": ValueKind.SHORT,
    }.get(field, ValueKind.TEXT)


def default_value(kind: ValueKind) -> Any:
    """The value of a new leaf: an empty text or list, True for the Shorts, and None for no choice yet."""
    return {ValueKind.TEXT: "", ValueKind.CHANNELS: [], ValueKind.SHORT: True}.get(kind)


def new_leaf(field: str = "title") -> Leaf:
    operator = next(iter(OPERATORS[field]))
    return Leaf(field, operator, default_value(value_kind(field, operator)))


def change_field(leaf: Leaf, field: str) -> None:
    """Give ``leaf`` the field ``field``, with its first operator and an empty value."""
    fresh = new_leaf(field)
    leaf.field, leaf.operator, leaf.value, leaf.case_sensitive = fresh.field, fresh.operator, fresh.value, False


def change_operator(leaf: Leaf, operator: str) -> None:
    """Give ``leaf`` the operator ``operator``. The value stays when it fits the new operator.

    One channel becomes a list of one channel, and the first channel of a list becomes the one channel.
    """
    old = value_kind(leaf.field, leaf.operator)
    new = value_kind(leaf.field, operator)
    leaf.operator = operator
    if old is new:
        return
    if old is ValueKind.CHANNEL and new is ValueKind.CHANNELS:
        leaf.value = [leaf.value] if leaf.value else []
    elif old is ValueKind.CHANNELS and new is ValueKind.CHANNEL:
        leaf.value = leaf.value[0] if leaf.value else None
    else:
        leaf.value = default_value(new)


def _is_date(value: Any) -> bool:
    """True for a date in the form 2026-10-01, the form of the field ``upload_date``."""
    if not isinstance(value, str):
        return False
    try:
        return datetime.date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def leaf_error(leaf: Leaf) -> str | None:
    """Return the message for the user when ``leaf`` is not complete or not valid, else None."""
    if leaf.operator not in OPERATORS.get(leaf.field, {}):
        return _("Choose a field and a comparison.")
    value = leaf.value
    match value_kind(leaf.field, leaf.operator):
        case ValueKind.TEXT if leaf.operator == "regex":
            if not isinstance(value, str) or not value:
                return _("Enter a pattern.")
            try:
                re.compile(value, re.NOFLAG if leaf.case_sensitive else re.IGNORECASE)
            except re.error as error:
                return _("The pattern does not compile: {error}").format(error=error)
        case ValueKind.TEXT:
            if not isinstance(value, str) or not value:
                return _("Enter a text.")
        case ValueKind.CHANNEL:
            if not isinstance(value, str) or not value:
                return _("Choose a channel.")
        case ValueKind.CHANNELS:
            if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
                return _("Choose at least one channel.")
        case ValueKind.SECONDS:
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                return _("Enter a whole number of seconds, 0 or more.")
        case ValueKind.DATE:
            if not _is_date(value):
                return _("Enter a date.")
        case ValueKind.LIVE_STATUS:
            if not isinstance(value, str) or value not in LIVE_STATUSES:
                return _("Choose a live status.")
        case ValueKind.SHORT:
            if not isinstance(value, bool):
                return _("Choose whether the video is a Short.")
    return None


def leaves(group: Group) -> Iterator[Leaf]:
    """Each leaf of ``group`` and of its nested groups, in the order of the editor."""
    for item in group.items:
        if isinstance(item, Group):
            yield from leaves(item)
        else:
            yield item


# From the model to the tree.


def to_tree(root: Group) -> Any:
    """Return the JsonLogic tree of the model. A group without leaves gives no condition, and so does the root."""
    tree = _group_tree(root)
    return copy.deepcopy(MATCH_ALL) if tree is None else tree


def _group_tree(group: Group) -> dict | None:
    items = [tree for tree in map(_item_tree, group.items) if tree is not None]
    if not items:
        return None
    match group.kind:
        case GroupKind.ALL:
            return {"and": items}
        case GroupKind.ANY:
            return {"or": items}
        case GroupKind.NONE:
            return {"!": [{"or": items}]}


def _item_tree(item: Group | Leaf) -> dict | None:
    return _group_tree(item) if isinstance(item, Group) else _leaf_tree(item)


def _leaf_tree(leaf: Leaf) -> dict:
    variable = {"var": leaf.field}
    value = list(leaf.value) if isinstance(leaf.value, list) else leaf.value
    if leaf.operator == "regex" and not leaf.case_sensitive:
        return {"regex": [variable, value, "i"]}
    return {leaf.operator: [variable, value]}


# From the tree to the model.


class _NotShown(Exception):
    """The editor cannot show this part of the tree."""


def from_tree(tree: Any) -> Group | None:
    """Return the model of ``tree``, or None when the visual editor cannot show the tree."""
    if tree == MATCH_ALL:
        return Group()
    try:
        group = _parse_group(copy.deepcopy(tree))
    except _NotShown:
        return None
    # A tree in another form with the same model, for example with a value of another type that compares equal in
    # the model, would come back changed. Such a tree stays in the JSON text.
    return group if to_tree(group) == tree else None


def _single(node: Any) -> tuple[str, Any]:
    if not isinstance(node, dict) or len(node) != 1:
        raise _NotShown
    return next(iter(node.items()))


def _parse_group(node: Any) -> Group:
    operator, args = _single(node)
    if operator == "and":
        return Group(GroupKind.ALL, _parse_items(args))
    if operator == "or":
        return Group(GroupKind.ANY, _parse_items(args))
    if operator == "!" and isinstance(args, list) and len(args) == 1:
        inner_operator, inner_args = _single(args[0])
        if inner_operator == "or":
            return Group(GroupKind.NONE, _parse_items(inner_args))
    raise _NotShown


def _parse_items(args: Any) -> list[Group | Leaf]:
    # An empty group matches no video, but the editor shows an empty group as no condition, so it stays in JSON.
    if not isinstance(args, list) or not args:
        raise _NotShown
    return [_parse_group(item) if _single(item)[0] in ("and", "or", "!") else _parse_leaf(item) for item in args]


def _parse_leaf(node: Any) -> Leaf:
    operator, args = _single(node)
    if not isinstance(args, list) or len(args) < 2:
        raise _NotShown
    variable, value, *rest = args
    field = variable.get("var") if isinstance(variable, dict) and len(variable) == 1 else None
    if not isinstance(field, str) or operator not in OPERATORS.get(field, {}):
        raise _NotShown
    if operator == "regex" and rest in ([], ["i"]):
        leaf = Leaf(field, operator, value, case_sensitive=not rest)
    elif not rest:
        leaf = Leaf(field, operator, value)
    else:
        raise _NotShown
    if leaf_error(leaf) is not None:
        raise _NotShown
    return leaf


# The condition for a save.


def condition_of(root: Group) -> Any:
    """Return the tree of the editor model for a save. Raise RuleError with a message for the user when a leaf is not
    complete or the tree is not valid."""
    for leaf in leaves(root):
        if (error := leaf_error(leaf)) is not None:
            # Translators: the error of one condition. {field} is the name of a video field, for example "Title".
            message = _("{field}: {error}").format(field=_(FIELDS.get(leaf.field, leaf.field)), error=error)
            raise RuleError(message)
    tree = to_tree(root)
    validate(tree)
    return tree


def condition_from_json(text: str | None) -> Any:
    """Return the tree of the JSON text for a save. Raise RuleError when the text is no valid rule tree."""
    try:
        tree = json.loads(text or "")
    except json.JSONDecodeError as error:
        raise RuleError(_("The condition is not valid JSON: {error}").format(error=error)) from None
    validate(tree)
    return tree


def json_text(tree: Any) -> str:
    return json.dumps(tree, indent=2, ensure_ascii=False)


# The short text of a condition for the table of the rules.


def describe(tree: Any, channel_titles: Mapping[str, str]) -> str:
    """Return a short text of the condition ``tree``. ``channel_titles`` gives the title of each known channel id."""
    root = from_tree(tree)
    if root is None:
        return _("Custom JSON condition")
    return _describe_group(root, channel_titles, nested=False) or _("Each video")


def _describe_group(group: Group, channel_titles: Mapping[str, str], *, nested: bool) -> str:
    parts = [
        _describe_group(item, channel_titles, nested=True)
        if isinstance(item, Group)
        else _describe_leaf(item, channel_titles)
        for item in group.items
    ]
    if not parts:
        return ""
    # Translators: the word between two conditions that must both match.
    and_word = _("and")
    # Translators: the word between two conditions of which one must match.
    or_word = _("or")
    if group.kind is GroupKind.NONE:
        # Translators: conditions that no video of the rule may match. "or" joins the conditions.
        text = _("not ({conditions})").format(conditions=f" {or_word} ".join(parts))
        return text
    text = f" {and_word if group.kind is GroupKind.ALL else or_word} ".join(parts)
    return f"({text})" if nested and len(parts) > 1 else text


def _describe_leaf(leaf: Leaf, channel_titles: Mapping[str, str]) -> str:
    if leaf.field == "is_short":
        return _("the video is a Short") if leaf.value else _("the video is not a Short")
    # Translators: one condition of a rule, for example: Title matches the pattern "Levels of". {field} is the name
    # of a video field, and {operator} is the comparison, for example "is at least".
    text = _("{field} {operator} {value}").format(
        field=_(FIELDS[leaf.field]),
        operator=_(OPERATORS[leaf.field][leaf.operator]),
        value=_describe_value(leaf, channel_titles),
    )
    if leaf.operator == "regex" and leaf.case_sensitive:
        # Translators: a pattern condition in which the case of the letters counts.
        text = _("{condition} (case-sensitive)").format(condition=text)
    return text


def _describe_value(leaf: Leaf, channel_titles: Mapping[str, str]) -> str:
    match value_kind(leaf.field, leaf.operator):
        case ValueKind.TEXT:
            return f'"{leaf.value}"'
        case ValueKind.CHANNEL:
            return channel_titles.get(leaf.value, leaf.value)
        case ValueKind.CHANNELS:
            if len(leaf.value) > MAX_NAMED_CHANNELS:
                # Translators: the channels of the condition "Channel is one of", when they are too many to name.
                text = _("{count} channels").format(count=len(leaf.value))
                return text
            return ", ".join(channel_titles.get(channel_id, channel_id) for channel_id in leaf.value)
        case ValueKind.LIVE_STATUS:
            return _(LIVE_STATUSES[leaf.value])
    # The number of seconds, or the date.
    return str(leaf.value)

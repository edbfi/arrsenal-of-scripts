"""Typed interactive prompts (questionary underneath).

questionary answers come back untyped. Choices are passed by index and the
answer is narrowed here, once, so callers get exactly the type they offered.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import questionary
from questionary import Choice, Separator, Style

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

STYLE = Style(
    [
        ("qmark", "fg:#22d3ee bold"),
        ("question", "bold"),
        ("answer", "fg:#a78bfa bold"),
        ("pointer", "fg:#f472b6 bold"),
        ("highlighted", "fg:#f472b6 bold"),
        ("selected", "fg:#4ade80"),
        ("separator", "fg:#6b7280"),
        ("instruction", "fg:#6b7280 italic"),
        ("text", ""),
        ("disabled", "fg:#6b7280 italic"),
        ("hint", "fg:#6b7280"),
    ]
)
QMARK = "›"
POINTER = "❯"


class Cancelled(Exception):
    """The user pressed Ctrl-C or Esc at a prompt."""


@dataclass(frozen=True, slots=True)
class Option[T]:
    label: str
    value: T
    hint: str = ""
    disabled: str | None = None  # shown instead of allowing selection
    checked: bool = False


@dataclass(frozen=True, slots=True)
class Divider:
    label: str = ""


type Entry[T] = Option[T] | Divider


def _choices[T](entries: Sequence[Entry[T]]) -> list[Choice | Separator]:
    out: list[Choice | Separator] = []
    for index, entry in enumerate(entries):
        if isinstance(entry, Divider):
            out.append(Separator(f"── {entry.label} " if entry.label else "──"))
            continue
        title: list[tuple[str, str]] = [("class:text", entry.label)]
        if entry.hint:
            title.append(("class:hint", f"  {entry.hint}"))
        out.append(Choice(title=title, value=index, disabled=entry.disabled, checked=entry.checked))
    return out


def _answer(raw: object) -> object:
    if raw is None:  # questionary's way of reporting Ctrl-C / Esc
        raise Cancelled
    return raw


def _option[T](entries: Sequence[Entry[T]], index: object) -> T:
    if not isinstance(index, int):
        raise TypeError(f"unexpected prompt answer {index!r}")
    entry = entries[index]
    if isinstance(entry, Divider):
        raise TypeError("a divider was selected")
    return entry.value


def select[T](message: str, entries: Sequence[Entry[T]], *, default: T | None = None) -> T:
    choices = _choices(entries)
    start = next(
        (
            c
            for c, e in zip(choices, entries, strict=True)
            if isinstance(e, Option) and default is not None and e.value == default
        ),
        None,
    )
    question = questionary.select(
        message,
        choices=choices,
        default=start,
        style=STYLE,
        qmark=QMARK,
        pointer=POINTER,
        instruction="(↑↓, enter)",
        use_shortcuts=False,
    )
    return _option(entries, _answer(cast("object", question.ask())))


def checkbox[T](message: str, entries: Sequence[Entry[T]]) -> list[T]:
    question = questionary.checkbox(
        message,
        choices=_choices(entries),
        style=STYLE,
        qmark=QMARK,
        pointer=POINTER,
        instruction="(space toggles, a all, enter confirms)",
    )
    raw = _answer(cast("object", question.ask()))
    if not isinstance(raw, list):
        raise TypeError(f"unexpected prompt answer {raw!r}")
    return [_option(entries, i) for i in cast("list[object]", raw)]


def confirm(message: str, *, default: bool = True) -> bool:
    question = questionary.confirm(message, default=default, style=STYLE, qmark=QMARK)
    raw = _answer(cast("object", question.ask()))
    if not isinstance(raw, bool):
        raise TypeError(f"unexpected prompt answer {raw!r}")
    return raw


def text(
    message: str,
    *,
    default: str = "",
    validate: Callable[[str], str | None] | None = None,
) -> str:
    """Free text; `validate` returns an error message or None."""

    def check(value: str) -> bool | str:
        if validate is None:
            return True
        error = validate(value)
        return True if error is None else error

    question = questionary.text(message, default=default, validate=check, style=STYLE, qmark=QMARK)
    raw = _answer(cast("object", question.ask()))
    if not isinstance(raw, str):
        raise TypeError(f"unexpected prompt answer {raw!r}")
    return raw


def pause(message: str = "Press enter to continue") -> None:
    _ = cast("object", questionary.press_any_key_to_continue(message, style=STYLE).ask())

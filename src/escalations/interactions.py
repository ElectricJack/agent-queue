"""§5.3's buttons: what an escalation post offers, and what a press *names*.

An escalation that carries ``choices`` is answerable without opening a browser
(spec §5.3): its one post grows a button per choice plus ``Reply…``, and a press
from an allow-listed user becomes a verified human reply exactly as a thread
reply does.  This module is the whole of that, as a pure function of the
incident, so the interesting rules are table tests rather than gateway
observations:

* how many choices become buttons (§5.3 says at most five) and where the sixth
  one goes -- nowhere: the thread opener and the escalation page still carry
  every option;
* the ``custom_id`` grammar, which is the only thing Discord hands back on an
  interaction, and therefore the only thing an untrusted client controls.

That last point is the reason a press carries an **index** and never a label.
The button a human taps is not evidence of what they chose: anyone can send
Discord the payload for any ``custom_id``.  :func:`parse_custom_id` therefore
returns the incident and the option's position, and the caller reads the option
*text* back out of the durable ``escalations`` row before recording anything.
A payload naming an index the row does not have is refused, not guessed at.

Nothing here imports ``discord``: the transport turns a :class:`ButtonSpec` into
components, and :mod:`src.discord.escalation_buttons` turns a press back into a
:class:`ButtonPress`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from src.discord.render_budget import cut

#: Namespace of every custom id this feature owns.  A press whose id does not
#: start with it is not ours, and :func:`parse_custom_id` refuses it rather than
#: guessing: Discord buttons are addressed by an opaque string and another
#: feature's ids must never be read as escalation answers.
CUSTOM_ID_PREFIX = "aqesc"

#: §5.3's "one button per choice (≤5) plus ``Reply…``".  Discord lays five
#: buttons across one action row and a sixth would wrap into a second row, which
#: on a phone is where a decision stops being readable.
MAX_CHOICE_BUTTONS = 5

#: Discord's own button-label ceiling.  A longer option is cut here rather than
#: refused: the option is still on the escalation page and in the thread.
MAX_BUTTON_LABEL_CHARS = 80

#: The affordance that says "you may also just write".  It records nothing: a
#: press cannot carry text, so the real reply still arrives through the thread
#: (:mod:`src.discord.escalation_intake`) or the escalation page.
REPLY_BUTTON_LABEL = "Reply…"

#: Button kinds.  ``choice`` selects an option; ``reply`` only points at the
#: thread.  Both are named in the custom id so the press needs no extra context.
KIND_CHOICE = "choice"
KIND_REPLY = "reply"

#: What an unrecognised or foreign custom id is refused with.
PARSE_FAILED = "not an escalation button"

#: ``aqesc:<escalation id>:<kind>[:<index>]``.  The id may not contain the
#: separator, so the grammar is unambiguous without escaping, and the whole id
#: stays far below Discord's 100-character custom-id limit even for a UUID.
_CUSTOM_ID = re.compile(
    rf"^{CUSTOM_ID_PREFIX}:(?P<escalation_id>[^:\s]{{1,64}}):"
    rf"(?P<kind>{KIND_CHOICE}|{KIND_REPLY})"
    r"(?::(?P<index>\d{1,3}))?$"
)


@dataclass(frozen=True, slots=True)
class ButtonSpec:
    """One button to render on an escalation post."""

    custom_id: str
    kind: str
    label: str
    escalation_id: str
    #: Position of the option in the incident's stored ``choices``, or ``None``
    #: for the ``Reply…`` affordance.
    choice_index: int | None = None

    @property
    def is_reply(self) -> bool:
        return self.kind == KIND_REPLY


@dataclass(frozen=True, slots=True)
class ButtonPress:
    """A parsed press: *which* incident and *which* option, and nothing more.

    There is deliberately no label here.  Discord does not send one, and a
    caller that wanted the text would have to read it from the incident's own
    row -- which is the point.
    """

    escalation_id: str
    kind: str
    choice_index: int | None = None

    @property
    def is_reply(self) -> bool:
        return self.kind == KIND_REPLY


def custom_id_for(escalation_id: str, kind: str, choice_index: int | None = None) -> str:
    """The custom id for one button, or :data:`PARSE_FAILED` when it cannot be built.

    An incident id that would not survive the grammar is refused at *render*
    time rather than posted as a button nobody can press.
    """
    suffix = "" if choice_index is None else f":{int(choice_index)}"
    candidate = f"{CUSTOM_ID_PREFIX}:{escalation_id}:{kind}{suffix}"
    return candidate if _CUSTOM_ID.fullmatch(candidate) else PARSE_FAILED


def parse_custom_id(custom_id: object) -> ButtonPress | None:
    """The press a custom id names, or ``None`` when it names nothing of ours.

    A ``choice`` press without an index, and a ``Reply…`` press *with* one, are
    both refused: each would let a payload pick a different button than the one
    rendered.
    """
    if not isinstance(custom_id, str):
        return None
    match = _CUSTOM_ID.fullmatch(custom_id.strip())
    if match is None:
        return None
    kind = match["kind"]
    raw_index = match["index"]
    if (kind == KIND_CHOICE) != (raw_index is not None):
        return None
    return ButtonPress(
        escalation_id=match["escalation_id"],
        kind=kind,
        choice_index=(int(raw_index) if raw_index is not None else None),
    )


def button_label(choice: str) -> str:
    """One choice as a button label: one line, within Discord's 80 characters."""
    return cut(" ".join(str(choice or "").split()), MAX_BUTTON_LABEL_CHARS)


def choice_buttons(escalation_id: str, choices: Sequence[str]) -> tuple[ButtonSpec, ...]:
    """§5.3's buttons for an incident: one per choice (≤5), then ``Reply…``.

    No choices means no buttons at all: §5.3 offers them to an incident that
    offers options, and a lone ``Reply…`` on a post with nothing to choose
    between is a decoration.  Options past the fifth are not silently dropped --
    they remain on the escalation page and in the thread, which is what §3.2's
    overflow rule already sends the reader to.
    """
    options = [choice for choice in (choices or ()) if str(choice or "").strip()]
    if not options:
        return ()
    specs: list[ButtonSpec] = []
    for index, choice in enumerate(options[:MAX_CHOICE_BUTTONS]):
        custom_id = custom_id_for(escalation_id, KIND_CHOICE, index)
        if custom_id == PARSE_FAILED:
            return ()
        specs.append(
            ButtonSpec(
                custom_id=custom_id,
                kind=KIND_CHOICE,
                label=button_label(choice),
                escalation_id=escalation_id,
                choice_index=index,
            )
        )
    reply_id = custom_id_for(escalation_id, KIND_REPLY)
    if reply_id == PARSE_FAILED:
        return ()
    specs.append(
        ButtonSpec(
            custom_id=reply_id,
            kind=KIND_REPLY,
            label=REPLY_BUTTON_LABEL,
            escalation_id=escalation_id,
        )
    )
    return tuple(specs)


def choice_text(facts_choices: Sequence[str], press: ButtonPress) -> str | None:
    """The stored option a press names, or ``None`` when the row has no such one.

    This is the only place a press turns into text, and it reads the incident's
    durable ``choices`` rather than anything the client sent: a payload with an
    out-of-range index resolves to ``None`` and the caller records nothing.
    """
    if press.choice_index is None:
        return None
    options = [choice for choice in (facts_choices or ()) if str(choice or "").strip()]
    if not 0 <= press.choice_index < len(options):
        return None
    return options[press.choice_index].strip()


__all__ = [
    "CUSTOM_ID_PREFIX",
    "KIND_CHOICE",
    "KIND_REPLY",
    "MAX_BUTTON_LABEL_CHARS",
    "MAX_CHOICE_BUTTONS",
    "PARSE_FAILED",
    "REPLY_BUTTON_LABEL",
    "ButtonPress",
    "ButtonSpec",
    "button_label",
    "choice_buttons",
    "choice_text",
    "custom_id_for",
    "parse_custom_id",
]
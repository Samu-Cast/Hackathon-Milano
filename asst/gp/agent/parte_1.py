"""Care-protocol example. ``city init`` does not copy this file.

The scaffold brain is ``templates/agent/agent/agent.py``. This one is the full care
protocol (prescribe, option, hold, commit, check-in, report), one inbound message per
step. The role is ``role`` in ``card.yaml``. Ledger writes are chat to ``city:ledger``;
citizens read the act word and ``key=value`` tokens (ADR-110).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from city_sdk.city import LEDGER, CapError, City, Message, Need, OutOfScope, clinical, understood

ROLES = ("booking_centre", "gp", "hospital", "specialist_care", "family_counseling")
_MAX_PAGE = 8
# Four agenda hours is about half a minute of real time at 480x, so a slot is still
# ahead when the citizen decides. One hour was already past by then.
_LEAD = timedelta(hours=4)
# Doctor key (the last segment of doctor_id) -> languages, gender, days. ``*`` is the ASST's
# rare language. The Ledger's slot line names the doctor; it does not repeat this table.
_DOCTORS: dict[str, tuple[frozenset[str], str, frozenset[str]]] = {
    "h1": (frozenset({"it", "en"}), "f", frozenset({"mon", "tue", "wed"})),
    "h2": (frozenset({"it"}), "m", frozenset({"wed", "thu", "fri"})),
    "h3": (frozenset({"it", "*"}), "m", frozenset({"tue", "thu"})),
    "s1": (frozenset({"it", "en"}), "m", frozenset({"mon", "wed", "fri"})),
    "s2": (frozenset({"it"}), "f", frozenset({"tue", "thu"})),
    "c1": (frozenset({"it", "en"}), "f", frozenset({"mon", "tue", "wed", "thu", "fri"})),
}
_PROTOCOLS: dict[str, tuple[str, str]] = {
    "SYM-BACK": ("VIS-FIS", "B"),
    "SYM-KNEE": ("VIS-FIS", "D"),
    "SYM-PALPITATIONS": ("VIS-CARD", "B"),
    "SYM-FOLLOWUP": ("VIS-CARD", "P"),
}
_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "SYM-BACK",
        (
            "mal di schiena",
            "back pain",
            "dolore alla schiena",
            "dolori alla schiena",
            "male alla schiena",
            "schiena mi fa male",
            "mi fa male la schiena",
            "lombalgia",
            "backache",
            "my back hurts",
            "lower back",
            "schiena a pezzi",
            "male schiena",
            "dolore schiena",
            "schiena fa male",
            "fa male schiena",
            "schiena bloccata",
            "colpo della strega",
            "lombalgia",
            "dolore lombare",
            "mal di reni",
            "back is killing me",
        ),
    ),
    ("SYM-KNEE", ("ginocchio", "ginocchia", "knee")),
    (
        "SYM-PALPITATIONS",
        (
            "cuore che batte",
            "heart racing",
            "palpitazion",
            "batticuore",
            "cuore batte",
            "cuore accelerato",
            "heart is racing",
            "heart pounding",
            "heart is pounding",
            "palpitation",
            "heart beating fast",
            "cuore mi batte",
            "cuore che mi batte",
            "batte fortissimo",
            "batte all'impazzata",
            "tachicardia",
            "cuore in gola",
            "cuore impazzito",
            "heart is beating",
            "heart beats fast",
        ),
    ),
    (
        "SYM-FOLLOWUP",
        (
            "controllo del cuore",
            "routine heart check",
            "controllo cardiologico",
            "controllo di routine al cuore",
            "check-up del cuore",
            "heart check-up",
            "heart checkup",
            "controllo al cuore",
            "controllo per il cuore",
            "controllo cardiaco",
            "controllo periodico del cuore",
            "visita di controllo al cuore",
            "check-up cardiologico",
            "heart check",
            "cardiology check-up",
            "cardiac check-up",
        ),
    ),
)
_RED_FLAGS = (
    "perdita di forza alle gambe",
    "loss of strength in the legs",
    "dolore al petto",
    "chest pain",
)
_DEADLINE_DAYS = {"B": 2, "D": 5}
_RX = re.compile(r"^prescription_number-[A-Z0-9-]{4,40}$")
_LOG = logging.getLogger("care")
# The Relay accepts a ledger reply and then the agent rejects the push, so the reply sits in
# the case history and never opens a turn. Wait briefly and read it back from history.
_PULL_TRIES = 24
_PULL_PAUSE = 0.25
# Needs whose sent offers this process remembers; the oldest is forgotten first.
_SENT_NEEDS = 4096


def role_of() -> str:
    """The role this process serves, from ``CITY_ROLE`` or the card next to the brain."""

    forced = os.environ.get("CITY_ROLE", "").strip()
    if forced in ROLES:
        return forced
    path = os.environ.get("CITY_CARD", "")
    if not path:
        return ""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""
    return _card_role(text)


async def main(need: Need, city: City) -> None:
    message = city.inbound
    if message is None:
        return
    text = message.text or need.summary
    role = role_of()
    # Medico di base: prima emergenza e protocollo, poi il rifiuto clinico (dentro _gp).
    if clinical(text) and role != "gp":
        raise OutOfScope()
    # Un solo turno alla volta per need: due messaggi che arrivano insieme (o un messaggio e il
    # suo reinvio) non producono due ricette. La storia si rilegge DENTRO il lucchetto, così il
    # secondo turno vede la ricetta già emessa dal primo.
    lock = _NEED_LOCKS.setdefault(message.need_id or message.msg_id, asyncio.Lock())
    async with lock:
        case = await city.history(need_id=message.need_id)
        await handle(role, city, message, case)


_NEED_LOCKS: dict[str, asyncio.Lock] = {}


def _asks_address(message: Message) -> bool:
    """True when this turn is a ``data.request`` that names the field ``address``."""

    text = message.text
    if text:
        named = False
        asked = False
        for token in text.split():
            bare = token.strip(".,;:!?()\"'")
            if bare == "data.request":
                named = True
            key, eq, value = bare.partition("=")
            if eq and key == "fields":
                names = {part.strip() for part in value.split(",") if part.strip()}
                asked = asked or "address" in names
        return named and asked
    if message.type != "data.request":
        return False
    raw = message.body.get("fields")
    if isinstance(raw, list):
        return "address" in raw
    if isinstance(raw, str):
        return "address" in {part.strip() for part in raw.replace(",", " ").split() if part.strip()}
    return False


def _own_street(city: City) -> str:
    """This agent's door: the city value, else the street shipped beside the brain."""

    street = getattr(city, "address", "")
    if isinstance(street, str) and street.strip():
        return street.strip()
    env = os.environ.get("CITY_ADDRESS", "").strip()
    if env:
        return env
    path = Path(__file__).with_name("door.txt")
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


async def handle(role: str, city: City, message: Message, chat: list[Message]) -> None:
    street = _own_street(city)
    if street and _asks_address(message):
        await city.say(message.sender, f"address={street}")
        return
    if role == "gp":
        await _gp(city, message, chat)
    elif role == "booking_centre":
        await _desk(city, message, chat, hold_self=False, fixed=None)
    elif role == "hospital":
        await _facility(city, message, chat, reports=False, hold_self=False)
    elif role == "specialist_care":
        await _facility(city, message, chat, reports=True, hold_self=False)
    elif role == "family_counseling":
        await _family(city, message, chat)


# --- shared ------------------------------------------------------------------------------------


def _card_role(text: str) -> str:
    role = ""
    types: list[str] = []
    in_types = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line[0] in " \t":
            if in_types:
                item = line.strip().removeprefix("-").strip().strip("'\"")
                if item:
                    types.append(item)
            continue
        in_types = False
        key, _, value = line.partition(":")
        value = value.strip().strip("'\"")
        if key.strip() == "role":
            role = value
        elif (
            key.strip() in {"capabilities", "service_types"}
            and value.startswith("[")
            and value.endswith("]")
        ):
            types = [part.strip().strip("'\"") for part in value[1:-1].split(",") if part.strip()]
        elif key.strip() in {"capabilities", "service_types"}:
            in_types = True
    if role in ROLES:
        return role
    for family in ("specialist_care", "family_counseling", "hospital", "booking_centre", "gp"):
        if any(item == family or item.startswith(family + ".") for item in types):
            return family
    return "hospital" if role == "facility" else ""


def words(text: str) -> set[str]:
    return {token.strip(".,;:!?()\"'").lower() for token in text.split() if token.strip()}


def first_word(text: str) -> str:
    parts = text.split()
    return parts[0].strip(".,;:!?()\"'").lower() if parts else ""


def fields_of(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for token in text.replace(",", " ").split():
        key, eq, value = token.partition("=")
        if not eq or not key or not value:
            continue
        value = value.strip(".,;:!?")
        if key not in found and value:
            found[key] = value
    return found


def _when(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _citizen(chat: list[Message]) -> str | None:
    for row in chat:
        if row.sender.startswith("citizen:"):
            return row.sender
    for row in chat:
        fields = fields_of(row.text)
        for key in ("patient", "citizen"):
            party = fields.get(key, "")
            if party.startswith("citizen:"):
                return party
    return None


def _we(chat: list[Message], agent: str, token: str, *, to: str, word: str) -> bool:
    for row in chat:
        if row.sender != agent or row.recipient != to:
            continue
        if token in row.text.split() and word in words(row.text):
            return True
    return False


def _exact(chat: list[Message], agent: str, text: str) -> bool:
    return any(row.sender == agent and row.text.strip() == text for row in chat)


async def _say(city: City, to: str, text: str) -> bool:
    try:
        await city.say(to, text)
    except CapError:
        return False
    return True


def _latest(chat: list[Message], head: str, token: str) -> Message | None:
    found: Message | None = None
    for row in chat:
        if row.sender == LEDGER and first_word(row.text) == head and token in row.text.split():
            found = row
    return found


async def _until(
    city: City,
    need_id: str | None,
    chat: list[Message],
    ready: Callable[[list[Message]], bool],
) -> list[Message]:
    """Re-read the case until ``ready`` or the short wait ends. The push may never arrive."""

    fresh = chat
    for attempt in range(_PULL_TRIES):
        try:
            fresh = await city.history(need_id=need_id)
        except CapError:
            fresh = chat
        if ready(fresh):
            return fresh
        if attempt + 1 < _PULL_TRIES:
            await asyncio.sleep(_PULL_PAUSE)
    return fresh


async def _await_line(
    city: City,
    need_id: str | None,
    chat: list[Message],
    head: str,
    token: str,
    *,
    op: str,
) -> tuple[Message, list[Message]] | None:
    """The ledger line for ``token``, or a refusal of ``op`` that was not already in ``chat``."""

    known = {row.msg_id for row in chat}
    fresh = chat
    for attempt in range(_PULL_TRIES):
        try:
            fresh = await city.history(need_id=need_id)
        except CapError:
            fresh = chat
        hit = _latest(fresh, head, token)
        if hit is not None:
            return hit, fresh
        for row in fresh:
            if row.msg_id in known or row.sender != LEDGER:
                continue
            if first_word(row.text) == "refused" and op in words(row.text):
                return row, fresh
        if attempt + 1 < _PULL_TRIES:
            await asyncio.sleep(_PULL_PAUSE)
    return None


def _has_agenda(chat: list[Message]) -> bool:
    agenda = _ledger(chat, "agenda")
    return _when("" if agenda is None else agenda.get("agenda_now", "")) is not None


def _has_procedure(chat: list[Message]) -> bool:
    reply = _ledger(chat, "prescription")
    return reply is not None and bool(reply.get("procedure"))


def _rx(need_id: str | None, msg_id: str) -> str:
    raw = (need_id or msg_id or "CASE").upper()
    suffix = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in raw).strip("-")
    if len(suffix) < 4:
        suffix = (suffix + "0000")[:4]
    number = "prescription_number-" + suffix[:40]
    return number if _RX.fullmatch(number) else "prescription_number-CASE"


def _speaks(doctor_id: str, lang: str) -> bool:
    spec = _DOCTORS.get(doctor_id.rsplit("-", 1)[-1])
    if spec is None:
        return False
    langs = spec[0]
    if lang in langs:
        return True
    return "*" in langs and lang not in {"it", "en"}


def fitting(
    lines: list[str],
    *,
    now: datetime,
    priority: str | None,
    lang: str,
    gender: str | None = None,
    facility: str | None = None,
    procedure: str | None = None,
) -> list[dict[str, str]]:
    """Slots a citizen can accept: future, free, in the priority window, spoken language."""

    if priority == "U":
        horizon: datetime | None = now.replace(hour=23, minute=59, second=59, microsecond=0)
    elif priority == "P":
        horizon = None
    else:
        horizon = now + timedelta(days=_DEADLINE_DAYS.get(priority or "", 14))
    earliest = now + _LEAD
    chosen: list[tuple[datetime, dict[str, str]]] = []
    for line in lines:
        slot = fields_of(line)
        slot_id = slot.get("slot_id")
        doctor = slot.get("doctor_id")
        at = _when(slot.get("at", ""))
        if not slot_id or not doctor or at is None:
            continue
        if procedure and slot.get("procedure") not in (None, procedure):
            continue
        if facility and slot.get("facility") != facility:
            continue
        free = slot.get("free")
        if free is not None and free.isdigit() and int(free) <= 0:
            continue
        reserve = slot.get("urgent_reserve", "").lower() == "true"
        if reserve and priority != "U":
            continue
        if not earliest < at or (horizon is not None and at >= horizon):
            continue
        if not _speaks(doctor, lang):
            continue
        spec = _DOCTORS.get(doctor.rsplit("-", 1)[-1])
        if spec is None:
            continue
        day = slot.get("day", "")
        if day and day not in spec[2]:
            continue
        if gender and spec[1] != gender:
            continue
        chosen.append((at, slot))
    chosen.sort(key=lambda item: (item[0], item[1].get("slot_id", "")))
    return [slot for _, slot in chosen]


# --- medico di base ----------------------------------------------------------------------------


# Le due red flag della tabella di protocollo (dolore al petto, perdita di forza alle gambe)
# dette con parole diverse. Una frase negata subito prima ("non ho", "senza") non conta.
_RED_FLAG_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"\b(?:dolor[ei]|male|fitt[ae]|oppressione|stretta|peso)\s+(?:\w+\s+)?"
        r"(?:al|nel|sul|in)\s+(?:petto|torace)\b",
        r"\b(?:petto|torace)\s+(?:che\s+)?mi\s+fa\s+male\b",
        r"\bmi\s+fa\s+male\s+(?:il|al)\s+(?:petto|torace)\b",
        r"\bdolore\s+toracic[oa]\b",
        r"\bchest\s+(?:pain|pains|hurts|hurting|pressure|tightness)\b",
        r"\bpains?\s+in\s+(?:my|the)\s+chest\b",
        r"\bmy\s+chest\s+(?:hurts|is\s+hurting|aches)\b",
        r"\b(?:perdita|perdo|perso|perdendo)\s+(?:di\s+|la\s+)?forza\s+"
        r"(?:alle|nelle|delle|alla|nella)\s+gamb[ae]\b",
        r"\bnon\s+ho\s+(?:più\s+)?forza\s+(?:alle|nelle|nella|alla)\s+gamb[ae]\b",
        r"\bdebolezza\s+(?:alle|nelle)\s+gambe\b",
        r"\bgambe\s+(?:che\s+)?(?:non\s+mi\s+reggono|mi\s+cedono|cedono|senza\s+forza)\b",
        r"\bnon\s+(?:sento|muovo)\s+(?:più\s+)?le\s+gambe\b",
        r"\bnon\s+riesco\s+(?:più\s+)?a\s+(?:muovere|sentire|camminare|reggermi|stare\s+in\s+piedi)\b"
        r"(?:\s+\w+){0,3}?\s*(?:le\s+|sulle\s+)?gamb[ae]\b",
        r"\bnon\s+mi\s+reggo\s+(?:più\s+)?(?:in\s+piedi|sulle\s+gambe)\b",
        r"\bgambe\s+(?:molto\s+|tutte\s+)?(?:deboli|molli)\b",
        # Sicurezza del cittadino: respiro e svenimento non sono nel protocollo, ma sono
        # emergenze vere. Meglio il 112 che una ricetta.
        r"\b(?:non\s+riesco\s+(?:più\s+)?a|fatico\s+a|faccio\s+fatica\s+a)\s+respirare\b",
        r"\bfame\s+d'aria\b",
        r"\bmanca\s+(?:il\s+)?(?:respiro|fiato)\b",
        r"\bcan(?:'|’|no)?t\s+breathe\b|\bcannot\s+breathe\b|\bshort(?:ness)?\s+of\s+breath\b",
        r"\bsvenut[oa]\b|\bsvenimento\b|\bho\s+perso\s+i\s+sensi\b",
        r"\bfainted\b|\bpassed\s+out\b",
        r"\b(?:loss|lost|losing)\s+(?:of\s+)?(?:the\s+)?strength\s+in\s+(?:my\s+|the\s+|both\s+)?legs?\b",
        r"\bweak(?:ness)?\s+in\s+(?:my|the|both)\s+legs?\b",
        r"\blegs?\s+(?:feel\s+|are\s+|is\s+)?(?:so\s+|very\s+)?(?:weak|giving\s+(?:way|out)|gave\s+(?:way|out))\b",
        r"\bcan(?:'|’|no)?t\s+(?:move|feel)\s+my\s+legs\b",
    )
)
_NEGATIONS = frozenset(
    {
        "non",
        "senza",
        "nessun",
        "nessuna",
        "niente",
        "alcun",
        "alcuna",
        "nemmeno",
        "neanche",
        "né",
        "no",
        "not",
        "without",
        "never",
        "nor",
        "any",
        "don't",
        "dont",
        "don’t",
        "doesn't",
        "didn't",
    }
)


def _negated(lowered: str, start: int) -> bool:
    """True when one of the two words right before ``start`` negates the phrase."""

    before = lowered[:start].split()[-2:]
    words_before: list[str] = []
    for token in before:
        if token[-1:] in ",.;:!?":
            words_before = []
            continue
        words_before.append(token.strip("\"'()"))
    return any(token in _NEGATIONS for token in words_before)


def _red_flag(text: str) -> bool:
    if _alarm_text(text):
        return True
    lowered = " ".join(text.lower().split())
    for flag in _RED_FLAGS:
        at = lowered.find(flag)
        while at >= 0:
            if not _negated(lowered, at):
                return True
            at = lowered.find(flag, at + 1)
    for pattern in _RED_FLAG_PATTERNS:
        for found in pattern.finditer(lowered):
            if not _negated(lowered, found.start()):
                return True
    return False


def _emergency(message: Message) -> bool:
    if message.body.get("is_emergency") is True:
        return True
    if understood(message).body.get("is_emergency") is True:
        return True
    return _red_flag(message.text)


def _symptom(message: Message) -> str | None:
    code = understood(message).body.get("symptom_code")
    if isinstance(code, str) and code in _PROTOCOLS:
        return code
    lowered = message.text.lower()
    for name, phrases in _KEYWORDS:
        if any(phrase in lowered for phrase in phrases):
            return name
    return None


def _our_rx(chat: list[Message], agent: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for row in chat:
        if row.sender == agent and "prescribe" in words(row.text):
            found = fields_of(row.text)
    return found


async def _gp(city: City, message: Message, chat: list[Message]) -> None:
    agent = city.agent_id
    if message.sender == LEDGER:
        await _announce(city, message, chat, agent)
        return
    if not message.sender.startswith("citizen:"):
        return
    # Dopo un'escalation non si manda altro su quel need (e19-rules, emergenza).
    if _we(chat, agent, "escalate.emergency", to=message.sender, word="escalate.emergency"):
        return
    if _emergency(message):
        lang_text = _lang_text(chat, message)
        await _say(
            city, message.sender, "escalate.emergency reason=red_flag" + _emergency_note(lang_text)
        )
        return
    old = _renewal_of(message)
    if old:
        await _renew(city, message, chat, agent, old)
        return
    code = _symptom(message)
    # Frasi che le parole chiave non coprono, o petto e gambe nominati senza una red flag
    # chiara: il modello della città riscrive la richiesta come domanda e la classifica.
    already = bool(_our_rx(chat, agent))
    if (code is None and not already) or (code is not None and _body_doubt(message.text)):
        reading = await _ask_model(city, message)
        if reading is not None:
            model_code, model_flag = reading
            if model_flag:
                await _say(
                    city,
                    message.sender,
                    "escalate.emergency reason=red_flag"
                    + _emergency_note(_lang_text(chat, message)),
                )
                return
            if code is None:
                code = model_code
    if code is None:
        if clinical(message.text):
            raise OutOfScope()
        await _no_symptom(city, message, chat, agent)
        return
    procedure, priority = _PROTOCOLS[code]
    number = _rx(message.need_id, message.msg_id)
    token = f"prescription_number={number}"
    # The citizen's reply window closes before a ledger round trip comes back, so the
    # prescription is said in this same turn. The ledger write is what the judge reads.
    if not _we(chat, agent, token, to=LEDGER, word="prescribe"):
        if not await _say(
            city,
            LEDGER,
            (
                f"prescribe patient={message.sender} procedure={procedure} priority={priority} "
                f"symptom_code={code} {token}"
            ),
        ):
            return
    if _we(chat, agent, token, to=message.sender, word="prescription"):
        return
    lang_text = _lang_text(chat, message)
    note = _rx_note(lang_text, procedure, priority, code=code)
    if clinical(message.text):
        note += _no_advice_note(lang_text)
    await _say(
        city,
        message.sender,
        f"prescription {token} procedure={procedure} priority={priority}{note}",
    )


async def _announce(city: City, message: Message, chat: list[Message], agent: str) -> None:
    head = first_word(message.text)
    if head == "refused" and "DUPLICATE" not in message.text:
        return
    if head not in {"prescribed", "refused"}:
        return
    fields = fields_of(message.text)
    ours = _our_rx(chat, agent)
    number = fields.get("prescription_number") or ours.get("prescription_number")
    citizen = fields.get("patient") or _citizen(chat)
    if not number or not citizen:
        return
    if _we(chat, agent, f"prescription_number={number}", to=citizen, word="prescription"):
        return
    parts = ["prescription", f"prescription_number={number}"]
    procedure = fields.get("procedure") or ours.get("procedure")
    priority = fields.get("priority") or ours.get("priority")
    if procedure:
        parts.append(f"procedure={procedure}")
    if priority:
        parts.append(f"priority={priority}")
    note = _rx_note(_lang_text(chat, None), procedure or "", priority or "")
    await _say(city, citizen, " ".join(parts) + note)


# --- medico di base: frasi per il cittadino e rinnovo (squadra epica) ---------------------------
# Le frasi stanno dopo i token: cittadino e giudice leggono la parola dell'atto e i token.
# Nessun consiglio clinico: solo cosa succede adesso e dove andare.

_EN_HINTS = frozenset(
    {
        "i", "i'm", "my", "me", "the", "have", "has", "and", "is", "am", "for", "since",
        "pain", "doctor", "hello", "hi", "please", "need", "with", "weeks", "days",
        "heart", "back", "knee", "chest", "check", "racing", "renew", "thank", "thanks",
        "would", "like", "it", "a", "of", "to", "been",
    }
)
_IT_HINTS = frozenset(
    {
        "il", "lo", "la", "di", "che", "ho", "mal", "dottore", "buongiorno", "salve",
        "sono", "un", "una", "mi", "per", "da", "cuore", "schiena", "ginocchio",
        "petto", "controllo", "vorrei", "ricetta", "grazie", "scusi", "batte", "dolore",
        "settimane", "giorni", "ciao", "io", "non", "e", "al", "del", "è",
    }
)
_VISIT = {
    "VIS-FIS": ("una visita fisiatrica", "a physiatry visit"),
    "VIS-CARD": ("una visita cardiologica", "a cardiology visit"),
    "CTRL-CARD": ("un controllo cardiologico", "a cardiology check-up"),
}
_WHEN = {
    "U": ("da fare oggi stesso", "to be done today"),
    "B": ("da fare entro 2 giorni", "to be done within 2 days"),
    "D": ("da fare entro 5 giorni", "to be done within 5 days"),
    "P": ("senza urgenza", "with no urgency"),
}
_NO_SYMPTOM = (
    "Grazie per avermi scritto. Dal messaggio non riesco a capire il disturbo principale:",
    "Thank you for writing. I could not tell the main problem from your message:",
)
_AFTER_RX = ("Sono qui. La sua ricetta è pronta:", "I am here. Your referral is ready:")


def _lang_text(chat: list[Message], message: Message | None) -> str:
    parts = [row.text for row in chat if row.sender.startswith("citizen:")]
    if message is not None and all(row.msg_id != message.msg_id for row in chat):
        parts.append(message.text)
    return " ".join(parts)


def _english(text: str) -> bool:
    lang = fields_of(text).get("lang", "").lower()
    if lang:
        return lang.startswith("en")
    found = words(text)
    return len(found & _EN_HINTS) > len(found & _IT_HINTS)


# Registro di chi scrive. I cittadini scrivono come Gen Z, boomer, in modo formale, ansioso,
# sbrigativo, polemico, remissivo o in un italiano semplice da non madrelingua: la risposta
# usa lo stesso registro. Contenuto identico in ogni registro: domanda, ricetta, passo dopo.
_SIMPLE_IT = re.compile(r"\bio\s+(?:avere|volere|essere|andare|stare|sentire|potere|avuto)\b")
_INFORMAL = re.compile(
    r"\b(?:raga|ciao|lowkey|fr|boh|tipo|bro|zio|cmq|xke|xché|nn|tvb|ahah\w*|lol|ok\s+ok)\b"
)
_FORMAL = re.compile(
    r"\b(?:gentile\s+dottor\w*|egregio|distinti\s+saluti|cordiali\s+saluti|la\s+prego|"
    r"la\s+informo|le\s+porgo|in\s+attesa\s+di\s+un\s+suo|resto\s+in\s+attesa)\b"
)
_POLEMIC = re.compile(
    r"(?:speriamo\s+che\s+(?:stavolta|questa\s+volta)|comuni\s+mortali|glaciazion|"
    r"come\s+al\s+solito|solita\s+attesa|mesi\s+di\s+attesa|ennesim|vergogn|assurdo\s+che)"
)
_ANXIOUS = re.compile(
    r"(?:preoccupat|preoccupa|ansia|ansios|paura|spaventat|agitat|tranquill|rassicur|"
    r"worried|anxious|scared|afraid|nervous)"
)
_EMOJI = re.compile("[\U0001F300-\U0001FAFF]")


def _style(text: str) -> str:
    lowered = " ".join(text.lower().split())
    if _SIMPLE_IT.search(lowered):
        return "simple"
    if _INFORMAL.search(lowered) or _EMOJI.search(text):
        return "informal"
    if _FORMAL.search(lowered):
        return "formal"
    if _POLEMIC.search(lowered):
        return "polemic"
    if len(lowered.split()) <= 12:
        return "brief"
    return "plain"


_SUBJECT = {
    "SYM-BACK": ("il mal di schiena", "your back pain", "la schiena"),
    "SYM-KNEE": ("il dolore al ginocchio", "your knee pain", "il ginocchio"),
    "SYM-PALPITATIONS": ("il cuore che batte forte", "your racing heart", "il cuore"),
}
_SIMPLE_VISIT = {
    "SYM-BACK": "una visita per la schiena",
    "SYM-KNEE": "una visita per il ginocchio",
    "SYM-PALPITATIONS": "una visita per il cuore",
    "SYM-FOLLOWUP": "un controllo del cuore",
}
# Priorità P: lista d'attesa, detto in una frase a parte.
_WAITING = (" Il CUP la inserirà in lista d'attesa.", " The booking centre will put you on the waiting list.")
_SIMPLE_WHEN = {
    "U": "La visita è da fare oggi.",
    "B": "La visita è da fare entro 2 giorni.",
    "D": "La visita è da fare entro 5 giorni.",
    "P": "La visita non ha urgenza: il CUP la mette in lista d'attesa.",
}


def _question(code: str, style: str, english: bool) -> str:
    """La richiesta del cittadino, riscritta come domanda."""

    if english:
        if code == "SYM-FOLLOWUP":
            return "You are asking for a heart check-up."
        subject = _SUBJECT.get(code)
        return f"You are asking which visit you need for {subject[1]}." if subject else ""
    if code == "SYM-FOLLOWUP":
        return {"informal": "Mi chiedi un controllo del cuore.",
                "simple": "Lei chiede un controllo del cuore. Ecco la ricetta."}.get(
            style, "Mi chiede un controllo del cuore."
        )
    subject = _SUBJECT.get(code)
    if not subject:
        return ""
    if style == "informal":
        return f"Mi chiedi quale visita fare per {subject[0]}."
    if style == "simple":
        return f"Lei chiede una visita per {subject[2]}. Ecco la ricetta."
    return f"Mi chiede quale visita fare per {subject[0]}."




# --- livello 8: segnali d'allarme (stessa lista in ogni agente che li legge) --------------------
# Una negazione vicina ("non ho dolore al petto") non conta. Una frase d'emergenza conta anche
# se il resto del messaggio dice altro: niente "NOT_FLAG" sull'intero messaggio.
_ALARM = re.compile(
    r"\b(?:dolor[ei]|male|fitt[ae]|stretta|oppressione|peso|pesantezza)\s+(?:\w+\s+){0,2}?"
    r"(?:al|nel|sul|in|di)\s+(?:petto|torace)\b"
    r"|\b(?:petto|torace)\s+(?:che\s+)?mi\s+fa\s+male\b|\bmi\s+fa\s+male\s+(?:il|al)\s+(?:petto|torace)\b"
    r"|\bdolore\s+toracic[oa]\b|\bchest\s+(?:pain|pains|pressure|tightness|hurts|hurting)\b"
    r"|\bpains?\s+in\s+(?:my|the)\s+chest\b|\bmy\s+chest\s+(?:hurts|is\s+hurting|aches)\b"
    r"|\b(?:non\s+riesco|fatico|faccio\s+fatica)\s+(?:più\s+)?a\s+respirare\b"
    r"|\b(?:can'?t|cannot|can\s+not)\s+breathe\b|\bshort(?:ness)?\s+of\s+breath\b|\bfame\s+d'aria\b"
    r"|\bmanca\s+(?:il\s+)?(?:respiro|fiato)\b"
    r"|\bnon\s+riesco\s+(?:più\s+)?a\s+(?:muovere|sentire)\s+(?:le\s+|sulle\s+)?gamb[ae]\b"
    r"|\b(?:perdita|perdo|perso|perdendo)\s+(?:di\s+|la\s+)?forza\s+(?:alle|nelle|delle|alla|nella)\s+gamb[ae]\b"
    r"|\b(?:loss|lost|losing)\s+(?:of\s+)?(?:the\s+)?strength\s+in\s+(?:my\s+|the\s+|both\s+)?legs?\b"
    r"|\bcan(?:'|’)?t\s+(?:move|feel)\s+my\s+legs\b"
    r"|\bsvenut[oa]\b|\bsvenimento\b|\bperso\s+i\s+sensi\b|\bfainted\b|\bpassed\s+out\b"
    r"|\bconvulsion\w*|\bseizure\b|\bcrisi\s+epilettica\b"
    r"|\bemorragi\w*|\bheavy\s+bleeding\b|\bbleeding\s+heavily\b|\bsevere\s+bleeding\b"
    r"|\bsanguinament\w*\s+(?:molto|forte|abbondante|intenso)\b|\bsanguino\s+molto\b"
    r"|\b(?:perdo|perdendo|perso|perde)\s+(?:molto\s+|tanto\s+)?sangue\b"
    r"|\bsangue\s+dalla\s+bocca\b|\bvomito\s+sangue\b"
    r"|\bdolore\s+(?:fortissimo|insopportabile)\b|\bsevere\s+pain\b|\bunbearable\s+pain\b"
    r"|\bfar(?:mi)?\s+del\s+male\b|\btogliermi\s+la\s+vita\b|\bfarla\s+finita\b|\bsuicid\w*"
    r"|\bhurt\s+myself\b|\bkill\s+myself\b"
    r"|\bbocca\s+storta\b|\bparlo\s+male\s+all'improvviso\b|\bnon\s+riesco\s+a\s+parlare\b"
    r"|\bdolor[ea]?\s+(?:en\s+el\s+pecho|de\s+pecho)\b|\bno\s+puedo\s+respirar\b"
    r"|\bdouleur\s+(?:à\s+la\s+poitrine|thoracique)\b|\bje\s+n'arrive\s+pas\s+à\s+respirer\b"
    r"|\bbrustschmerz\w*|\bschmerzen\s+in\s+der\s+brust\b|\batemnot\b"
    r"|\bdurere\s+în\s+piept\b|\bnu\s+pot\s+respira\b|\bdor\s+no\s+peito\b|\bnão\s+consigo\s+respirar\b"
)
_ALARM_FOREIGN = (
    "ألم في الصدر", "لا أستطيع التنفس", "胸痛", "呼吸困难", "喘不过气", "біль у грудях", "не можу дихати",
)


def _alarm_text(text: str, extra: tuple[str, ...] = ()) -> bool:
    """extra: le frasi storte dell'agente (_RED_FLAGS o _FACILITY_FLAGS), con la stessa negazione."""

    lowered = " ".join(text.lower().replace("’", "'").split())
    for found in _ALARM.finditer(lowered):
        if not _negated(lowered, found.start()):
            return True
    for flag in extra:
        at = lowered.find(flag)
        while at >= 0:
            if not _negated(lowered, at):
                return True
            at = lowered.find(flag, at + 1)
    return any(flag in lowered for flag in _ALARM_FOREIGN)

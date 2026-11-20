"""Consultorio: accesso diretto, prenotazione e check-in tramite il Ledger."""

from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime, timedelta

from city_sdk.city import LEDGER, CapError, City, Message, Need, clinical, understood

_MAX_PAGE = 16  # richieste di pagina per turno: ricerca binaria (~8) più qualche pagina in ordine


_LEAD = timedelta(hours=4)


_DOCTORS: dict[str, tuple[frozenset[str], str, frozenset[str]]] = {
    "h1": (frozenset({"it", "en"}), "f", frozenset({"mon", "tue", "wed"})),
    "h2": (frozenset({"it"}), "m", frozenset({"wed", "thu", "fri"})),
    "h3": (frozenset({"it", "*"}), "m", frozenset({"tue", "thu"})),
    "s1": (frozenset({"it", "en"}), "m", frozenset({"mon", "wed", "fri"})),
    "s2": (frozenset({"it"}), "f", frozenset({"tue", "thu"})),
    "c1": (frozenset({"it", "en"}), "f", frozenset({"mon", "tue", "wed", "thu", "fri"})),
}


_DEADLINE_DAYS = {"B": 2, "D": 5}


_LOG = logging.getLogger("care")


_PULL_TRIES = 24


_PULL_PAUSE = 0.25


_SENT_NEEDS = 4096


async def main(need: Need, city: City) -> None:
    message = city.inbound
    if message is None or not message.need_id:
        return
    key = message.need_id
    gate = _GATES.setdefault(key, _Gate())
    gate.turns += 1
    try:
        async with gate.lock:
            await _handle_turn(need, city, message)
    finally:
        gate.turns -= 1
        if not gate.turns:
            del _GATES[key]


async def _handle_turn(need: Need, city: City, message: Message) -> None:
    sender = message.sender
    citizen = sender.startswith("citizen:")
    if (citizen or sender.startswith("agent:")) and _asks_address(message):
        if city.address:
            await city.say(sender, f"address={city.address}")
        return
    if sender.startswith("agent:"):
        # Un CUP (di un'altra ASST) cerca posti o conferma uno slot che gli abbiamo offerto.
        case = await city.history(need_id=message.need_id)
        if first_word(message.text) == "need.request":
            await _serve_partner(city, message, case)
        elif "confirm" in words(message.text):
            await _partner_confirm(city, message, case)
        return
    if not citizen and sender != LEDGER:
        return
    case = await city.history(need_id=message.need_id)
    act = first_word(message.text)
    if citizen:
        interpreted = understood(message)
        tone = _tone(case)
        if (
            getattr(need, "is_emergency", False) is True
            or interpreted.body.get("is_emergency") is True
            or _red_flag(message.text)
        ):
            if not _we(case, city.agent_id, "reason=red_flag", to=sender, word="escalate.emergency"):
                await _say(city, sender, _escalation_text(tone))
            return
        if act == "checkin" or message.type == "checkin" or (
            _checkin_intent(message.text) and _booking_in(message.text, case)
        ):
            await _on_checkin(city, message, case, reports=False)
            return
        fields = dict(interpreted.body) | fields_of(message.text)
        offers = _our_offers(case, city.agent_id)
        chosen = fields.get("offer_id") if fields.get("offer_id") in offers else None
        if not chosen and offers and not _awaiting_consent(case, city.agent_id, sender):
            # Livello 3: "prendo quello di martedì", "la seconda", "alle 10".
            chosen = _natural_choice(message.text, offers)
            if not chosen and _choice_like(message.text):
                await _on_choice(city, message, case, None, None, offers, tone)  # chiede quale
                return
        consent = fields.get("consent_ref") or _bare_consent(message.text)
        if not chosen and (
            act == "consent" or consent or _awaiting_consent(case, city.agent_id, sender)
        ):
            # Risposta alla nostra richiesta di consenso (un sì con consent_ref, o un no a parole).
            chosen = _pending_choice(case, city.agent_id, sender, offers)
        if chosen or act == "confirm" or message.type == "confirm":
            await _on_choice(city, message, case, chosen, consent, offers, tone)
            return
        # Una parola clinica non blocca la prenotazione: rispondo con garbo e prenoto.
        note = ""
        if clinical(message.text or need.summary):
            note = (
                "For clinical questions the right person is a healthcare professional; here I "
                "take care of your appointment."
                if tone == "en"
                else "Per le domande cliniche la persona giusta è un professionista sanitario; "
                "qui mi occupo del suo appuntamento."
            )
        # The incoming need already identifies the consultorio journey. Its prose
        # may be a statement, so no question mark or booking keyword is required.
        if interpreted.type == "need.request" or not any(
            row.sender.startswith("citizen:") and row.msg_id != message.msg_id for row in case
        ):
            await _advance(city, message, case, fixed="CONS-GIN", replacing=False, note=note)
        return
    if act == "fault.cancel" or message.type == "fault.cancel":
        await _on_refused(city, message, case, hold_self=True)
    elif act in {"agenda", "world", "slots"}:
        await _advance(city, message, case, fixed="CONS-GIN", replacing=False)
    elif act in {"held", "committed", "checked_in", "refused"}:
        await _facility(city, message, case, reports=False, hold_self=True)


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


def words(text: str) -> set[str]:
    return {token.strip(".,;:!?()\"'").lower() for token in text.split() if token.strip()}


def _valid_ref(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", value) is not None


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
    days: frozenset[str] = frozenset(),
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
        if (day and day not in spec[2]) or (days and day not in days):
            continue
        if gender and spec[1] != gender:
            continue
        chosen.append((at, slot))
    chosen.sort(key=lambda item: (item[0], item[1].get("slot_id", "")))
    return [slot for _, slot in chosen]


def _ledger(chat: list[Message], head: str) -> dict[str, str] | None:
    found: dict[str, str] | None = None
    for row in chat:
        if row.sender == LEDGER and first_word(row.text) == head:
            found = fields_of(row.text)
    return found


def _latest_slots(chat: list[Message]) -> str | None:
    found: str | None = None
    for row in chat:
        if row.sender == LEDGER and first_word(row.text) == "slots":
            found = row.text
    return found


def _page(text: str) -> tuple[int, int]:
    raw = fields_of(text.splitlines()[0] if text else "").get("page", "")
    left, _, right = raw.partition("/")
    if left.isdigit() and right.isdigit():
        return int(left), int(right)
    return 1, 1


def _page_spans(chat: list[Message]) -> tuple[dict[int, tuple[datetime | None, datetime | None]], int]:
    """Per ogni pagina del mondo letta: primo e ultimo orario. Più il numero di pagine."""

    spans: dict[int, tuple[datetime | None, datetime | None]] = {}
    total = 0
    for row in chat:
        if row.sender != LEDGER or first_word(row.text) not in {"slots", "world"}:
            continue
        head = row.text.splitlines()[0] if row.text else ""
        left, _, right = fields_of(head).get("page", "1/1").partition("/")
        page = int(left) if left.isdigit() else 1
        total = max(total, int(right) if right.isdigit() else 1)
        times = []
        for line in row.text.splitlines():
            fields = fields_of(line)
            if "slot_id" in fields:
                at = _when(fields.get("at") or fields.get("agenda_date", ""))
                if at is not None:
                    times.append(at)
        spans[page] = (min(times), max(times)) if times else (None, None)
    return spans, total


def _all_slot_lines(chat: list[Message]) -> list[str]:
    """Le righe di slot di tutte le pagine lette (senza doppioni)."""

    seen: set[str] = set()
    lines: list[str] = []
    for row in chat:
        if row.sender != LEDGER or first_word(row.text) not in {"slots", "world"}:
            continue
        for line in row.text.splitlines():
            slot = fields_of(line).get("slot_id")
            if slot and slot not in seen:
                seen.add(slot)
                if "at" not in fields_of(line) and "agenda_date=" in line:
                    line += " at=" + fields_of(line)["agenda_date"]
                if "day" not in fields_of(line) and "agenda_day=" in line:
                    line += " day=" + fields_of(line)["agenda_day"]
                lines.append(line)
    return lines


def _searching(chat: list[Message], earliest: datetime) -> bool:
    """True finché la ricerca binaria non ha trovato la PRIMA pagina con orari futuri."""

    spans, _ = _page_spans(chat)
    past = [p for p, (_, last) in spans.items() if last is not None and last < earliest]
    future = sorted(p for p, (_, last) in spans.items() if last is not None and last >= earliest)
    return bool(future) and future[0] > max(past, default=0) + 1


def _next_page(chat: list[Message], earliest: datetime, limit: datetime | None) -> int | None:
    """Quale pagina chiedere: ricerca binaria fino alla prima pagina con orari da ``earliest``
    in poi (gli slot sono in ordine di data), poi in ordine finché supero ``limit``."""

    spans, total = _page_spans(chat)
    if not spans:
        return 1
    past = [p for p, (_, last) in spans.items() if last is not None and last < earliest]
    future = sorted(p for p, (_, last) in spans.items() if last is not None and last >= earliest)
    low = max(past, default=0)
    if not future or future[0] > low + 1:
        high = future[0] if future else total + 1
        wanted = max((low + high) // 2, low + 1)
        while wanted in spans and wanted < high:
            wanted += 1
        return wanted if wanted <= total and wanted not in spans else None
    page = future[0]
    while page in spans:
        _, last = spans[page]
        if limit is not None and last is not None and last >= limit:
            return None
        page += 1
    return page if page <= total else None


def _asked(chat: list[Message], agent: str, procedure: str, page: int) -> bool:
    token = f"page={page}"
    proc = f"procedure={procedure}"
    for row in chat:
        if row.sender == agent and token in row.text.split() and proc in row.text.split():
            return True
    return False


def _offer_lines(chat: list[Message], agent: str, to: str | None = None) -> list[dict[str, str]]:
    """Ogni riga ``offer ...`` che abbiamo mandato (un messaggio può averne fino a tre)."""

    found: list[dict[str, str]] = []
    for row in chat:
        if row.sender != agent or (to is not None and row.recipient != to):
            continue
        for line in row.text.splitlines():
            if first_word(line) == "offer":
                found.append(fields_of(line))
    return found


def _offered(chat: list[Message], agent: str) -> set[str]:
    return {line["slot_id"] for line in _offer_lines(chat, agent) if line.get("slot_id")}


def _withdrawn(chat: list[Message], agent: str) -> set[str]:
    """Slots we cancelled. The citizen drops the rest, so those are offered again."""

    by_offer: dict[str, str] = {}
    for fields in _offer_lines(chat, agent):
        offer_id = fields.get("offer_id")
        slot = fields.get("slot_id")
        if offer_id and slot:
            by_offer[offer_id] = slot
    dropped: set[str] = set()
    for row in chat:
        if "cancel" not in words(row.text):
            continue
        slot = by_offer.get(fields_of(row.text).get("offer_id", ""))
        if slot:
            dropped.add(slot)
    return dropped


def _lang(chat: list[Message]) -> str:
    """Il token lang= vince sempre; senza, la lingua si riconosce dalle parole (livello 3)."""

    for row in chat:
        if row.sender.startswith("citizen:"):
            lang = fields_of(row.text).get("lang", "")
            if lang:
                return lang
    texts = " ".join(row.text for row in chat if row.sender.startswith("citizen:"))
    return _detect_lang(texts) or "it"


def _gender(chat: list[Message]) -> str | None:
    for row in chat:
        if not row.sender.startswith("citizen:"):
            continue
        raw = fields_of(row.text).get("gender_preference") or fields_of(row.text).get("gender")
        if raw in {"m", "f"}:
            return raw
    for row in chat:
        if row.sender.startswith("citizen:"):
            said = _natural_prefs(row.text)[1]
            if said:
                return said
    return None


def _preferred_days(chat: list[Message]) -> frozenset[str]:
    for row in reversed(chat):
        if not row.sender.startswith("citizen:"):
            continue
        raw = fields_of(row.text).get("days") or understood(row).body.get("days")
        if isinstance(raw, str):
            # Keep commas in the day list, unlike the general field parser.
            match = re.search(r"(?:^|\s)days=([^\s]+)", row.text)
            raw = re.split(r"[,|/]", match[1] if match else raw)
        if isinstance(raw, (list, tuple)):
            days = frozenset(day.lower()[:3] for day in raw if isinstance(day, str))
            return days & {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
    for row in reversed(chat):
        if row.sender.startswith("citizen:"):
            said = _natural_prefs(row.text)[0]
            if said:
                return frozenset(said)
    return frozenset()


# --- livello 3: richieste in linguaggio naturale -------------------------------------------------

_DAY_NAMES = (
    ("mon", ("lunedi", "lunedì", "monday")), ("tue", ("martedi", "martedì", "tuesday")),
    ("wed", ("mercoledi", "mercoledì", "wednesday")), ("thu", ("giovedi", "giovedì", "thursday")),
    ("fri", ("venerdi", "venerdì", "friday")),
)
_ORDINALS = (
    (0, r"prim[ao]|first|1st|opzione\s*1|option\s*1"),
    (1, r"second[ao]|second|2nd|opzione\s*2|option\s*2"),
    (2, r"terz[ao]|third|3rd|opzione\s*3|option\s*3"),
    (-1, r"ultim[ao]|the\s+last(?:\s+one)?"),
)
_CHOICE = re.compile(
    r"\b(scelgo|prendo|confermo|accetto|va bene|ok|d'accordo|perfetto|quell[ao]|opzione|"
    r"i choose|i'll take|i accept|that one|the one|yes|sì|si)\b"
)
_MORE = re.compile(
    r"\b(nessuna|non (mi )?va(nno)? bene|altre date|altri giorni|altro giorno|altra data|"
    r"none of (them|these)|other dates|another (day|date))\b"
)
_CHECKIN_RE = re.compile(
    r"\bcheck[\s-]?in\b|\bsono (?:arrivat[oa]|qui|in sala|allo sportello)\b|\beccomi\b|"
    r"\bi'?m here\b|\bi am here\b|\bi(?: have|'ve) arrived\b|\bje suis (?:là|arrivée?)\b|"
    r"\bestoy aquí\b|\bich bin da\b"
)
_LANG_WORDS = {
    "en": {"the", "i", "i'm", "i'd", "my", "is", "and", "you", "hello", "hi", "please",
           "appointment", "would", "like", "need", "doctor", "thank", "thanks", "want", "have",
           "with", "for", "am", "gave", "me"},
    "fr": {"je", "bonjour", "merci", "rendez-vous", "voudrais", "mon", "ma", "médecin", "avec",
           "pour", "suis", "j'ai", "est", "s'il", "vous", "besoin"},
    "es": {"hola", "quiero", "cita", "gracias", "médico", "necesito", "tengo", "buenos", "días",
           "quisiera", "estoy", "favor", "para"},
    "de": {"ich", "hallo", "termin", "bitte", "danke", "arzt", "ärztin", "möchte", "mein",
           "meine", "habe", "guten", "tag", "und", "einen"},
    "pt": {"olá", "obrigado", "obrigada", "consulta", "gostaria", "preciso", "meu", "minha", "bom"},
    "ro": {"bună", "mulțumesc", "programare", "vreau", "aș", "nevoie", "medic", "ziua"},
    "it": {"il", "lo", "la", "di", "che", "ho", "vorrei", "buongiorno", "grazie", "sono",
           "una", "mi", "ricetta", "salve", "visita", "medico", "della", "del", "non", "è",
           "un", "le", "ci"},
}
_PREF_MARK = re.compile(
    r"\b(preferisc\w*|preferirei|vorrei|se possibile|meglio|magari|ideally|prefer\w*|would like|"
    r"i'd like|rather|if possible|solo (?:il|di)|ginecologa|dottoressa|female doctor|male doctor)\b"
)
_SINCE_DAY = re.compile(
    r"\b(?:da|dal|since|from|scorso|scorsa|last)\s+(?:\w+\s+)?"
    r"(?:luned\w*|marted\w*|mercoled\w*|gioved\w*|venerd\w*|monday|tuesday|wednesday|thursday|friday)"
)


def _low(text: str) -> str:
    return " ".join(text.lower().replace("’", "'").split())


def _detect_lang(text: str) -> str:
    """La lingua più probabile del testo; "" se non si capisce (resta l'italiano)."""

    tokens = [token.strip(".,;:!?()\"'«»") for token in text.lower().split()]
    scores = {lang: sum(1 for token in tokens if token in vocab) for lang, vocab in _LANG_WORDS.items()}
    best = max(scores, key=lambda lang: scores[lang])
    if best == "it" or scores[best] < 2 or scores[best] <= scores["it"]:
        return ""
    return best


def _natural_prefs(text: str) -> tuple[set[str] | None, str | None]:
    """Giorni e genere detti a parole come preferenza ("se possibile di martedì")."""

    lowered = _low(text)
    if not _PREF_MARK.search(lowered):
        return None, None
    cleaned = _SINCE_DAY.sub(" ", lowered)
    days = {code for code, names in _DAY_NAMES if any(name in cleaned for name in names)}
    gender = None
    if re.search(r"\b(ginecologa|dottoressa|una donna|medico donna|female doctor|a woman)\b", lowered):
        gender = "f"
    elif re.search(r"\b(un uomo|medico uomo|male doctor|a man)\b", lowered):
        gender = "m"
    return days or None, gender


def _choice_like(text: str) -> bool:
    lowered = _low(text)
    return bool(_CHOICE.search(lowered)) and not _MORE.search(lowered)


def _natural_choice(text: str, offers: dict[str, dict[str, str]]) -> str | None:
    """L'offerta indicata a parole (ordinale, giorno, ora, data). Solo se è una sola."""

    lowered = _low(text)
    if _MORE.search(lowered):
        return None
    slots = [oid for oid, offer in offers.items() if offer.get("slot_id")]
    waiting = [oid for oid, offer in offers.items() if offer.get("waiting_list")]
    if not slots:
        return waiting[-1] if waiting and _CHOICE.search(lowered) else None
    latest = slots[-3:]
    picked, named, ordinal = set(slots), False, False
    for index, pattern in _ORDINALS:
        if re.search(rf"\b(?:{pattern})\b", lowered):
            picked &= {latest[index]} if -len(latest) <= index < len(latest) else set()
            named = ordinal = True
            break
    days = {code for code, names in _DAY_NAMES if any(name in lowered for name in names)}
    if days:
        picked &= {oid for oid in slots if offers[oid].get("agenda_day") in days}
        named = True
    hour = re.search(r"\b(?:alle|ore|at)\s*(\d{1,2})(?:[:.](\d{2}))?\b|\b(\d{1,2})[:.](\d{2})\b", lowered)
    if hour:
        wanted = (int(hour.group(1) or hour.group(3)), int(hour.group(2) or hour.group(4) or 0))
        picked &= {
            oid for oid in slots
            if (at := _when(offers[oid].get("agenda_date", ""))) is not None and (at.hour, at.minute) == wanted
        }
        named = True
    if not named or len(picked) != 1:
        return None
    if not (ordinal or _CHOICE.search(lowered)):
        return None
    return picked.pop()


def _checkin_intent(text: str) -> bool:
    return bool(_CHECKIN_RE.search(_low(text)))


def _booking_in(text: str, chat: list[Message]) -> str:
    """Il booking_id del check-in detto a parole: il token, un codice noto scritto nel testo,
    oppure l'unica prenotazione di questo caso registrata sul Ledger."""

    token = fields_of(text).get("booking_id", "")
    if _valid_ref(token):
        return token
    known: list[str] = []
    for row in chat:
        if row.sender == LEDGER and first_word(row.text) == "committed":
            booking = fields_of(row.text).get("booking_id", "")
            if booking and booking not in known:
                known.append(booking)
    said = words(text)
    for booking in known:
        if booking.lower() in said or booking in text:
            return booking
    return known[0] if len(known) == 1 else ""


def _offer_line(slot: dict[str, str], number: str | None, priority: str | None) -> str:
    parts = [
        "offer",
        f"offer_id=of-{slot['slot_id']}",
        f"slot_id={slot['slot_id']}",
        f"doctor_id={slot['doctor_id']}",
        f"facility={slot['facility']}",
        f"procedure={slot['procedure']}",
        f"agenda_date={slot['at']}",
        f"agenda_day={slot['day']}",
        f"expiry={slot['at']}",
    ]
    if priority:
        parts.append(f"priority={priority}")
    if number:
        parts.append(f"prescription_number={number}")
    return " ".join(parts)


def _our_offer(chat: list[Message], agent: str, offer_id: str) -> dict[str, str]:
    for fields in _offer_lines(chat, agent):
        if fields.get("offer_id") == offer_id:
            return fields
    return {}


# --- squadra epica: tono, allarmi, scelta, consenso, ASST partner ------------------------------

_DAY_IT = {"mon": "lunedì", "tue": "martedì", "wed": "mercoledì", "thu": "giovedì", "fri": "venerdì"}
_DAY_EN = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday", "fri": "Friday"}
_MONTH_IT = ("gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto",
             "settembre", "ottobre", "novembre", "dicembre")
_DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Segnali d'allarme: non è una diagnosi, è il motivo per fermarsi e mandare al 112.
_RED_FLAGS = (
    "emorragi", "perdo molto sangue", "perdendo molto sangue", "sanguino molto",
    "sanguinamento abbondante", "sanguinamento forte", "dolore fortissimo", "dolore insopportabile",
    "dolore al petto", "dolore toracico", "non riesco a respirare", "svenut", "perso i sensi",
    "convulsion", "voglio farla finita", "farmi del male", "togliermi la vita", "suicid",
    "heavy bleeding", "bleeding heavily", "severe pain", "unbearable pain", "chest pain",
    "can't breathe", "cannot breathe", "fainted", "passed out", "seizure",
)
_CONSENT_BARE = re.compile(r"\b(cons-[A-Za-z0-9][A-Za-z0-9_-]{2,80})")


def _tone(chat: list[Message]) -> str:
    """Italiano per chi scrive in italiano, inglese per tutti gli altri."""

    return "it" if _lang(chat).lower() in {"", "it", "ita"} else "en"


def _red_flag(text: str) -> bool:
    lowered = " ".join(text.lower().replace("’", "'").split())
    return any(flag in lowered for flag in _RED_FLAGS)


def _escalation_text(tone: str) -> str:
    if tone == "en":
        return (
            "escalate.emergency reason=red_flag\n"
            "What you describe needs to be looked at straight away, before any appointment. "
            "Please call 112 now or go to the nearest emergency department. If someone is with "
            "you, ask them to stay close. The emergency team is the right help right now."
        )
    return (
        "escalate.emergency reason=red_flag\n"
        "Quello che mi descrive va valutato subito, prima di qualsiasi appuntamento. Chiami ora "
        "il 112 oppure si rechi al pronto soccorso più vicino. Se c'è qualcuno con lei, gli "
        "chieda di restarle accanto. I soccorritori sono l'aiuto giusto in questo momento."
    )


def _when_text(value: str, tone: str) -> str:
    at = _when(value)
    if at is None:
        return value
    day = _DAY_KEYS[at.weekday()]
    if tone == "en":
        return f"{_DAY_EN.get(day, day)} {at.day} {at.strftime('%B')} at {at:%H:%M}"
    return f"{_DAY_IT.get(day, day)} {at.day} {_MONTH_IT[at.month - 1]} alle ore {at:%H:%M}"


def _bare_consent(text: str) -> str | None:
    hit = _CONSENT_BARE.search(text)
    return hit.group(1) if hit else None


def _our_offers(chat: list[Message], agent: str) -> dict[str, dict[str, str]]:
    citizen = _citizen(chat)
    return {
        line["offer_id"]: line
        for line in _offer_lines(chat, agent, citizen)
        if line.get("offer_id")
    }


def _pending_choice(
    chat: list[Message], agent: str, citizen: str, offers: dict[str, dict[str, str]]
) -> str | None:
    """L'offerta per cui abbiamo chiesto il consenso, o l'ultima nominata dal cittadino."""

    chosen: str | None = None
    for row in chat:
        if row.sender == agent and row.recipient == citizen and first_word(row.text) == "consent":
            offer_id = fields_of(row.text).get("offer_id")
            if offer_id in offers:
                chosen = offer_id
        elif row.sender == citizen:
            offer_id = fields_of(row.text).get("offer_id")
            if offer_id in offers:
                chosen = offer_id
    return chosen


def _awaiting_consent(chat: list[Message], agent: str, citizen: str) -> bool:
    """Abbiamo chiesto il consenso e non abbiamo ancora mandato l'hold."""

    asked = None
    for index, row in enumerate(chat):
        if row.sender == agent and row.recipient == citizen and first_word(row.text) == "consent":
            asked = index
    if asked is None:
        return False
    return not any(
        row.sender == agent and row.recipient == LEDGER and first_word(row.text) == "hold"
        for row in chat[asked:]
    )


def _requester(chat: list[Message], agent: str, slot: str) -> str:
    """Il CUP (agente) che ci ha chiesto di prenotare questo slot, se c'è."""

    token = f"slot_id={slot}"
    for row in chat:
        if (
            row.sender.startswith("agent:")
            and row.sender != agent
            and "confirm" in words(row.text)
            and token in row.text.split()
        ):
            return row.sender
    return ""


async def _on_choice(
    city: City,
    message: Message,
    case: list[Message],
    chosen: str | None,
    consent: str | None,
    offers: dict[str, dict[str, str]],
    tone: str,
) -> None:
    agent = city.agent_id
    citizen = message.sender
    if _citizen(case) != citizen:
        return
    if not chosen:
        if offers:
            listed = [
                f"- {_when_text(offer.get('agenda_date', ''), tone)}: offer_id={offer_id}"
                for offer_id, offer in offers.items()
                if offer.get("slot_id")
            ]
            intro = (
                "Thank you! To book the right time I need the offer_id of the option you prefer:"
                if tone == "en"
                else "Grazie! Per prenotare proprio l'orario giusto mi serve l'offer_id "
                "dell'opzione che preferisce:"
            )
            await _say(city, citizen, "\n".join([intro, *listed]))
        return
    offer = offers[chosen]
    if offer.get("waiting_list", "").lower() == "true":
        done = (
            "Done: you are on the waiting list of the family counselling centre. You do not "
            "need a prescription; we will propose a time as soon as one becomes free."
            if tone == "en"
            else "Fatto: è in lista d'attesa al consultorio. Non serve la ricetta; le proporremo "
            "un orario appena se ne libera uno."
        )
        if not _exact(case, agent, done):
            await _say(city, citizen, done)
        return
    if _we(case, agent, f"offer_id={chosen}", to=citizen, word="cancel"):
        return
    if not consent:
        if not _we(case, agent, f"offer_id={chosen}", to=citizen, word="consent"):
            prose = (
                "Thank you for your choice! To book it I need your consent to use your booking "
                "data (name and appointment) at the centre. No prescription is needed."
                if tone == "en"
                else "Grazie per la scelta! Per prenotarla mi serve il suo consenso a usare i dati "
                "della prenotazione (nominativo e appuntamento) al consultorio. Non serve la ricetta."
            )
            await _say(city, citizen, f"consent purpose=booking offer_id={chosen}\n" + prose)
        else:
            prose = (
                "No problem: without your consent I will not book anything. If you change your "
                "mind, just write to me and we will pick up from here."
                if tone == "en"
                else "Nessun problema: senza il suo consenso non prenoto nulla. Se cambia idea mi "
                "scriva pure, e ripartiamo da qui."
            )
            if not _exact(case, agent, prose):
                await _say(city, citizen, prose)
        return
    if not _valid_ref(consent):
        return
    slot = offer["slot_id"]
    if not _we(case, agent, f"slot_id={slot}", to=LEDGER, word="hold"):
        if not await _say(city, LEDGER, f"hold slot_id={slot} citizen={citizen} consent_ref={consent}"):
            await _say(
                city, citizen,
                f"cancel offer_id={chosen} reason=hold_refused\n"
                + (
                    "I am sorry, I could not reserve this time. I am looking for another one."
                    if tone == "en"
                    else "Mi dispiace, non sono riuscita a bloccare questo orario. Cerco subito "
                    "un'alternativa."
                ),
            )
            return
    await _finish_booking(city, message, case, slot, hold_self=True)


async def _serve_partner(city: City, message: Message, case: list[Message]) -> None:
    """Il CUP di un'altra ASST cerca posti: offro i miei slot liberi (solo criteri)."""

    agent = city.agent_id
    fields = fields_of(message.text)
    procedure = fields.get("procedure") or "CONS-GIN"
    request_id = fields.get("request_id") or message.need_id or message.msg_id
    tag = f"request_id={request_id}"
    if _we(case, agent, tag, to=message.sender, word="offer") or _we(
        case, agent, tag, to=message.sender, word="cancel"
    ):
        return
    if not _has_agenda(case) and not _exact(case, agent, "agenda"):
        await _say(city, LEDGER, "agenda")
    if not _asked(case, agent, procedure, 1):
        await _say(city, LEDGER, f"world procedure={procedure} page=1")
    case = await _until(
        city, message.need_id, case,
        lambda rows: _has_agenda(rows) and _latest_slots(rows) is not None,
    )
    now = _when((_ledger(case, "agenda") or {}).get("agenda_now", ""))
    lang = (fields.get("lang") or "it").lower()
    slots: list[dict[str, str]] = []
    for _round in range(_MAX_PAGE):
        if now is None or _latest_slots(case) is None:
            break
        slots = fitting(
            _all_slot_lines(case), now=now, priority=None, lang=lang,
            facility=agent, procedure=procedure,
        )[:3]
        if slots and not _searching(case, now + _LEAD):
            break
        wanted = _next_page(case, now + _LEAD, now + timedelta(days=14))
        if wanted is None:
            break
        if not _asked(case, agent, procedure, wanted):
            await _say(city, LEDGER, f"world procedure={procedure} page={wanted}")
        case = await _until(
            city, message.need_id, case, lambda rows, wanted=wanted: wanted in _page_spans(rows)[0]
        )
        if wanted not in _page_spans(case)[0]:
            break
    if not slots:
        await _say(
            city, message.sender,
            f"cancel {tag} procedure={procedure} reason=no_availability "
            "Al momento il consultorio non ha posti liberi per questa prestazione. Grazie!",
        )
        return
    lines = [
        _offer_line(slot, None, None).replace(
            f"offer_id=of-{slot['slot_id']}", f"offer_id=px-{slot['slot_id']}"
        ) + f" {tag}"
        for slot in slots
    ]
    lines.append(
        "Ecco i posti liberi del consultorio (accesso diretto, senza ricetta; nessun dato del "
        "paziente richiesto finora). Per prenotare mandateci confirm con slot_id, citizen e "
        "consent_ref."
    )
    await _say(city, message.sender, "\n".join(lines))


async def _partner_confirm(city: City, message: Message, case: list[Message]) -> None:
    """Il CUP di un'altra ASST conferma uno slot: hold e commit, la ricevuta torna a lui."""

    agent = city.agent_id
    fields = fields_of(message.text)
    slot = fields.get("slot_id", "")
    consent = fields.get("consent_ref", "")
    citizen = fields.get("citizen", "")
    if not slot or not _valid_ref(consent):
        return
    ack = "Ricevuto, grazie: blocco subito il posto sul Ledger e vi mando la conferma firmata."
    if not _exact(case, agent, ack):
        await _say(city, message.sender, ack)
    if not _we(case, agent, f"slot_id={slot}", to=LEDGER, word="hold"):
        parts = ["hold", f"slot_id={slot}"]
        if citizen.startswith("citizen:"):
            parts.append(f"citizen={citizen}")
        parts.append(f"consent_ref={consent}")
        if not await _say(city, LEDGER, " ".join(parts)):
            return
    await _finish_booking(city, message, case, slot, hold_self=True)


class _Gate:
    """One need's lock and how many turns hold it or wait for it."""

    __slots__ = ("lock", "turns")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.turns = 0


class _Sent:
    """Offers this process sent for one need, which the case history may not show yet.

    ``offers`` holds every slot id (and waiting-list offer id) sent. A cancel makes the
    citizen drop all offers, so ``again`` holds only what was re-sent for ``trigger``.
    """

    __slots__ = ("offers", "trigger", "again")

    def __init__(self) -> None:
        self.offers: set[str] = set()
        self.trigger = ""
        self.again: set[str] = set()


_GATES: dict[str, _Gate] = {}


_SENT: OrderedDict[str, _Sent] = OrderedDict()


def _sent(key: str) -> _Sent:
    entry = _SENT.get(key)
    if entry is None:
        entry = _SENT[key] = _Sent()
        while len(_SENT) > _SENT_NEEDS:
            _SENT.popitem(last=False)
    else:
        _SENT.move_to_end(key)
    return entry


async def _advance(
    city: City,
    message: Message,
    chat: list[Message],
    *,
    fixed: str | None,
    replacing: bool,
    note: str = "",
) -> None:
    key = message.need_id or message.msg_id
    await _advance_alone(city, message, chat, key, fixed=fixed, replacing=replacing, note=note)


async def _advance_alone(
    city: City,
    message: Message,
    chat: list[Message],
    key: str,
    *,
    fixed: str | None,
    replacing: bool,
    note: str = "",
) -> None:
    agent = city.agent_id
    citizen = _citizen(chat)
    if citizen is None or not any(row.sender == citizen for row in chat):
        return  # offro solo a un cittadino che ci ha scritto (non a quello di un'ASST partner)
    tone = _tone(chat)
    procedure = "CONS-GIN"
    if not replacing and (_offered(chat, agent) or _sent(key).offers):
        return
    old_replies = {row.msg_id for row in chat} if replacing else set()

    def fresh_replies(rows: list[Message]) -> list[Message]:
        return [row for row in rows if row.sender == LEDGER and row.msg_id not in old_replies]

    if replacing or not _has_agenda(chat) and not _exact(chat, agent, "agenda"):
        await _say(city, LEDGER, "agenda")
    if replacing or _latest_slots(chat) is None and not _asked(chat, agent, procedure, 1):
        await _say(city, LEDGER, f"world procedure={procedure} page=1")
    chat = await _until(
        city, message.need_id, chat,
        lambda rows: _has_agenda(fresh_replies(rows)) and _latest_slots(fresh_replies(rows)) is not None,
    )
    agenda = _ledger(fresh_replies(chat), "agenda") or {}
    now = _when(agenda.get("agenda_now", ""))
    world = _latest_slots(fresh_replies(chat))
    if now is None or world is None:
        return
    # Gara lunga (~64 giorni di catalogo): le prime pagine sono date passate. Sfoglio in questo
    # stesso turno, saltando con la ricerca binaria alla prima pagina con orari da adesso in poi.
    earliest = now + _LEAD
    asked_now = {1}
    for _round in range(_MAX_PAGE):
        fresh = fresh_replies(chat)
        if _all_slot_lines(fresh) and fitting(
            _all_slot_lines(fresh), now=now, priority=None, lang=_lang(chat), facility=agent,
            procedure=procedure,
        ) and not _searching(fresh, earliest):
            break
        wanted = _next_page(fresh, earliest, now + timedelta(days=14))
        if wanted is None or wanted in asked_now:
            break
        asked_now.add(wanted)
        if replacing or not _asked(chat, agent, procedure, wanted):
            await _say(city, LEDGER, f"world procedure={procedure} page={wanted}")
        chat = await _until(
            city, message.need_id, chat,
            lambda rows, wanted=wanted: wanted in _page_spans(fresh_replies(rows))[0],
        )
        if wanted not in _page_spans(fresh_replies(chat))[0]:
            break
    world = "\n".join(_all_slot_lines(fresh_replies(chat)))
    # A cancel drops every offer the citizen was holding, so the ones still free are sent again,
    # once per cancel: a second turn for the same cancel or refusal does not repeat them.
    sent = _sent(key)
    if replacing:
        trigger = fields_of(message.text).get("offer_id") or message.msg_id
        if sent.trigger != trigger:
            sent.trigger = trigger
            sent.again = set()
        taken = _withdrawn(chat, agent) | sent.again
    else:
        taken = _offered(chat, agent) | sent.offers
    lines = [line for line in world.splitlines() if "slot_id=" in line]

    def fit(**prefs: object) -> list[dict[str, str]]:
        return [
            slot
            for slot in fitting(
                lines, now=now, priority=None, lang=_lang(chat), facility=agent,
                procedure=procedure, **prefs,  # type: ignore[arg-type]
            )
            if slot.get("slot_id") not in taken
        ]

    days = _preferred_days(chat)
    gender = _gender(chat)
    chosen = fit(gender=gender, days=days)
    relaxed = False
    if not chosen and (days or gender):
        chosen = fit()
        relaxed = bool(chosen)
    if chosen:
        picked = chosen[:3]
        text = _offers_text(picked, tone=tone, relaxed=relaxed, replacing=replacing, note=note)
        if await _say(city, citizen, text):
            sent.offers.update(slot["slot_id"] for slot in picked)
            if replacing:
                sent.again.update(slot["slot_id"] for slot in picked)
        return
    if taken and not replacing:
        return
    # Nessun orario libero: lo dico chiaramente e propongo la lista d'attesa (alternativa).
    offer_id = f"wl-{key}"
    if offer_id in sent.offers or _we(chat, agent, f"offer_id={offer_id}", to=citizen, word="offer"):
        return
    if tone == "en":
        prose = (
            "I looked carefully, but right now the family counselling centre has no free time "
            "that suits you. The alternative I can offer is the waiting list: no prescription is "
            "needed, and we will propose a time as soon as one becomes free. If you agree, reply "
            "with the offer_id below."
        )
    else:
        prose = (
            "Ho cercato con attenzione, ma al momento il consultorio non ha orari liberi adatti a "
            "lei. L'alternativa che posso proporle è la lista d'attesa: non serve la ricetta e le "
            "proporremo un orario appena se ne libera uno. Se è d'accordo mi risponda con "
            "l'offer_id qui sotto."
        )
    if note:
        prose = note + "\n" + prose
    if await _say(
        city, citizen,
        prose + f"\noffer offer_id={offer_id} waiting_list=true procedure={procedure}",
    ):
        sent.offers.add(offer_id)


def _offers_text(
    slots: list[dict[str, str]], *, tone: str, relaxed: bool, replacing: bool, note: str
) -> str:
    if tone == "en":
        if replacing:
            intro = "I have already found new times at the family counselling centre for you"
        elif relaxed:
            intro = (
                "With the preferences you gave me I found no free time. To be transparent, here "
                "are the closest times without those preferences"
            )
        else:
            intro = "Here are the free times at the family counselling centre, from the earliest"
        intro += " (direct access: no prescription is needed):"
        outro = (
            "To choose, reply with the offer_id of the option you prefer. If none of them works "
            "for you, just tell me."
        )
    else:
        if replacing:
            intro = "Ho già trovato per lei nuovi orari al consultorio"
        elif relaxed:
            intro = (
                "Con le preferenze che mi ha indicato non ho trovato orari liberi. Per trasparenza "
                "le propongo i più vicini, senza quelle preferenze"
            )
        else:
            intro = "Ecco gli orari liberi al consultorio, dal più vicino"
        intro += " (accesso diretto: non serve la ricetta):"
        outro = (
            "Per scegliere mi risponda con l'offer_id dell'opzione che preferisce. Se nessuna le "
            "va bene, me lo dica pure."
        )
    out = [note, intro] if note else [intro]
    for index, slot in enumerate(slots, start=1):
        out.append(("Option" if tone == "en" else "Opzione") + f" {index}: {_when_text(slot['at'], tone)}")
        out.append(_offer_line(slot, None, None))
    out.append(outro)
    return "\n".join(out)[:2000]


async def _facility(
    city: City,
    message: Message,
    chat: list[Message],
    *,
    reports: bool,
    hold_self: bool,
) -> None:
    head = first_word(message.text)
    if head == "held":
        await _on_held(city, message, chat)
    elif head == "committed":
        await _on_committed(city, message, chat)
    elif head == "refused" and ({"hold", "commit"} & words(message.text)):
        await _on_refused(city, message, chat, hold_self=True)
    elif head == "checked_in":
        await _on_checked_in(city, message, chat, reports=False)



def _booking_of(chat: list[Message], agent: str, slot_id: str) -> tuple[str, str, str | None]:
    """Who asked us to book this slot, the consent, and the prescription, if we have them."""

    token = f"slot_id={slot_id}"
    sender = ""
    consent = ""
    number: str | None = None
    for row in chat:
        if token not in row.text.split():
            continue
        fields = fields_of(row.text)
        if row.sender == agent and row.recipient == LEDGER and first_word(row.text) == "hold":
            consent = fields.get("consent_ref", "")
            sender = fields.get("citizen", "")
    return sender, consent, number


def _hold_slot(chat: list[Message], agent: str, hold_id: str) -> str:
    token = f"hold_id={hold_id}"
    for row in chat:
        if token in row.text.split() and (row.sender == agent or row.sender == LEDGER):
            slot = fields_of(row.text).get("slot_id", "")
            if slot:
                return slot
    return ""


def _our_commit(chat: list[Message], agent: str, hold_id: str) -> bool:
    """A commit we actually sent. The ledger's held line also contains the word commit."""

    token = f"hold_id={hold_id}"
    for row in chat:
        if row.sender != agent or row.recipient != LEDGER:
            continue
        if row.text.strip().startswith("commit ") and token in row.text.split():
            return True
    return False


async def _finish_booking(
    city: City, message: Message, chat: list[Message], slot: str, *, hold_self: bool
) -> None:
    """Hold is in the mailbox. Read the held line and commit before this turn ends."""

    found = await _await_line(city, message.need_id, chat, "held", f"slot_id={slot}", op="hold")
    if found is None:
        _LOG.info("care.held.missing", extra={"need_id": (message.need_id or "")[:48]})
        return
    reply, fresh = found
    if first_word(reply.text) == "refused":
        await _on_refused(city, reply, fresh, hold_self=hold_self)
        return
    await _on_held(city, reply, fresh)
    hold_id = fields_of(reply.text).get("hold_id", "")
    if not hold_id:
        return
    done = await _await_line(
        city, message.need_id, fresh, "committed", f"booking_id={hold_id}", op="commit"
    )
    if done is None:
        _LOG.info("care.commit.missing", extra={"need_id": (message.need_id or "")[:48]})
        return
    reply, fresh = done
    if first_word(reply.text) == "committed":
        await _on_committed(city, reply, fresh)
        return
    if first_word(reply.text) == "refused":
        await _on_refused(city, reply, fresh, hold_self=hold_self)


async def _on_held(city: City, message: Message, chat: list[Message]) -> None:
    fields = fields_of(message.text)
    hold_id = fields.get("hold_id", "")
    slot = fields.get("slot_id", "")
    if not hold_id or not slot:
        _LOG.info("care.held.skip", extra={"head": first_word(message.text)[:40]})
        return
    if _our_commit(chat, city.agent_id, hold_id):
        return
    _, consent, _ = _booking_of(chat, city.agent_id, slot)
    need = fields.get("need_id", "")
    if not consent:
        _LOG.info("care.held.noconsent", extra={"need_id": need[:48]})
        return
    parts = [
        "commit",
        f"hold_id={hold_id}",
        f"consent_ref={consent}",
        f"fence={fields.get('fence', '1')}",
    ]
    await _say(city, LEDGER, " ".join(parts))


async def _on_committed(city: City, message: Message, chat: list[Message]) -> None:
    fields = fields_of(message.text)
    booking_id = fields.get("booking_id", "")
    ledger_ref = fields.get("ledger_ref", "")
    if not booking_id or not ledger_ref:
        return
    agent = city.agent_id
    # Il Ledger usa l'hold_id come booking_id e non ripete lo slot: lo ritrovo da lì.
    slot = (
        fields.get("slot_id")
        or _hold_slot(chat, agent, fields.get("hold_id", ""))
        or _hold_slot(chat, agent, booking_id)
    )
    sender, _, _ = _booking_of(chat, agent, slot) if slot else ("", "", None)
    requester = _requester(chat, agent, slot) if slot else ""
    target = requester or sender
    if not target or (not requester and target != _citizen(chat)):
        return
    token = f"booking_id={booking_id}"
    if _we(chat, agent, token, to=target, word="booking.receipt"):
        return
    head = f"booking.receipt booking_id={booking_id} ledger_ref={ledger_ref}"
    if requester:
        # Prenotazione chiesta dal CUP di un'altra ASST: la ricevuta torna a lui.
        await _say(city, target, f"{head} slot_id={slot} Prenotazione registrata e firmata sul Ledger, grazie.")
        return
    tone = _tone(chat)
    at = next((line.get("agenda_date", "") for line in _offer_lines(chat, agent, target)
               if line.get("slot_id") == slot), "")
    when = _when_text(at, tone) if at else ""
    moment = _when(at) if at else None
    window = ("08:00–13:00" if moment.hour < 13 else "13:00–18:00") if moment else ""
    if tone == "en":
        lines = ["Your appointment at the family counselling centre is booked and signed. Here is your receipt:", head]
        if when:
            lines.append(f"When: {when}.")
        lines += [
            "What to do now:",
            "1. Keep booking_id and ledger_ref: they are your proof of booking.",
            "2. On the day, check in at the centre" + (f" during the {window} slot" if window else "")
            + ", giving booking_id and ledger_ref.",
            "3. No prescription is needed; bring an identity document.",
        ]
    else:
        lines = ["Il suo appuntamento al consultorio è prenotato e registrato con firma. Ecco la ricevuta:", head]
        if when:
            lines.append(f"Quando: {when}.")
        lines += [
            "Cosa fare adesso:",
            "1. Conservi booking_id e ledger_ref: sono la prova della prenotazione.",
            "2. Il giorno dell'appuntamento faccia il check-in al consultorio"
            + (f" nella fascia {window}" if window else "") + ", indicando booking_id e ledger_ref.",
            "3. Non serve la ricetta; porti con sé un documento d'identità.",
        ]
    await _say(city, target, "\n".join(lines))


async def _on_refused(
    city: City, message: Message, chat: list[Message], *, hold_self: bool
) -> None:
    agent = city.agent_id
    event = fields_of(message.text)
    slot = event.get("slot_id", "") or _hold_slot(chat, agent, event.get("hold_id", ""))
    if not slot and event.get("booking_id"):
        for row in chat:
            fields = fields_of(row.text)
            if row.sender == LEDGER and first_word(row.text) == "committed" and fields.get("booking_id") == event["booking_id"]:
                slot = fields.get("slot_id", "")
    if not slot and first_word(message.text) == "refused":
        for row in chat:
            if row.sender == agent and row.recipient == LEDGER and first_word(row.text) == "hold":
                slot = fields_of(row.text).get("slot_id", slot)
    if not slot:
        return
    target, _, _ = _booking_of(chat, agent, slot)
    requester = _requester(chat, agent, slot)
    offer_id = ""
    for line in _offer_lines(chat, agent):
        if line.get("slot_id") == slot:
            offer_id = line.get("offer_id", offer_id)
    code = re.search(r"\b([A-Z][A-Z_]{3,})\b", message.text)
    reason = code.group(1) if code else "facility_cancelled"
    if requester:
        # Prenotazione di un'ASST partner: avviso il loro CUP, non scrivo al loro cittadino.
        if not _we(chat, agent, f"slot_id={slot}", to=requester, word="cancel"):
            await _say(city, requester, f"cancel offer_id={offer_id or 'px-' + slot} slot_id={slot} reason={reason} Ci scusiamo, questo posto non è più disponibile.")
        return
    if (
        target
        and offer_id
        and not _we(chat, agent, f"offer_id={offer_id}", to=target, word="cancel")
    ):
        prose = (
            "I am sorry: the time you chose is no longer available. It is not your fault: I am "
            "already looking for an alternative."
            if _tone(chat) == "en"
            else "Mi dispiace: l'orario scelto non è più disponibile. Non dipende da lei: sto già "
            "cercando un'alternativa."
        )
        await _say(city, target, f"cancel offer_id={offer_id} reason={reason}\n{prose}")
        fresh = await city.history(need_id=message.need_id)
        await _advance(city, message, fresh, fixed="CONS-GIN", replacing=True)


async def _on_checkin(city: City, message: Message, chat: list[Message], *, reports: bool) -> None:
    booking_id = (
        fields_of(message.text).get("booking_id")
        or understood(message).body.get("booking_id", "")
        or _booking_in(message.text, chat)  # check-in detto a parole
    )
    citizen = message.sender if message.sender.startswith("citizen:") else _citizen(chat)
    # Non pretendo di aver mandato io la ricevuta: per una prenotazione fatta da un'ASST partner
    # la ricevuta l'ha girata il loro CUP. La validità la controlla il Ledger sul check-in.
    if not _valid_ref(booking_id) or not citizen:
        return
    token = f"booking_id={booking_id}"
    if not _we(chat, city.agent_id, token, to=LEDGER, word="checkin"):
        if not await _say(city, LEDGER, f"checkin booking_id={booking_id} citizen={citizen}"):
            return
    found = await _await_line(city, message.need_id, chat, "checked_in", token, op="checkin")
    if found is None:
        _LOG.info("care.checkin.missing", extra={"need_id": (message.need_id or "")[:48]})
        return
    reply, fresh = found
    if first_word(reply.text) == "checked_in":
        await _on_checked_in(city, reply, fresh, reports=reports)


async def _on_checked_in(
    city: City, message: Message, chat: list[Message], *, reports: bool
) -> None:
    booking_id = fields_of(message.text).get("booking_id", "")
    citizen = _citizen(chat)
    if not booking_id:
        return
    token = f"booking_id={booking_id}"
    if citizen and not _we(chat, city.agent_id, token, to=citizen, word="checkin"):
        prose = (
            "Your arrival at the centre has been registered, thank you. Please wait to be called."
            if _tone(chat) == "en"
            else "Il suo arrivo al consultorio è stato registrato, grazie. Attenda pure la chiamata."
        )
        await _say(city, citizen, f"checkin booking_id={booking_id}\n{prose}")

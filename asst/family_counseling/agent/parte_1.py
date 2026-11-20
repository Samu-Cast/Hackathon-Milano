"""Consultorio: accesso diretto, prenotazione e check-in tramite il Ledger."""

from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime, timedelta

from city_sdk.city import LEDGER, CapError, City, Message, Need, clinical, understood

_MAX_PAGE = 24  # richieste di pagina per turno: ricerca binaria (~8) o lettura in ordine


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
        if offers and (act == "cancel" or interpreted.type == "cancel" or "cancel" in words(message.text)):
            # Il cittadino rifiuta un'offerta: non è una scelta. Quando le ha rifiutate tutte
            # propongo altri orari liberi, altrimenti la lista d'attesa.
            await _citizen_declined(city, message, case, offers)
            return
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
    if _pages_sorted(spans) is not True:
        return False  # pagine non in ordine (o non si sa ancora): va bene ciò che ho trovato
    past = [p for p, (_, last) in spans.items() if last is not None and last < earliest]
    future = sorted(p for p, (_, last) in spans.items() if last is not None and last >= earliest)
    return bool(future) and future[0] > max(past, default=0) + 1


def _pages_sorted(spans: dict[int, tuple[datetime | None, datetime | None]]) -> bool | None:
    """True se le pagine lette sono in ordine di data, False se non lo sono, None se non si
    può ancora dire (una sola pagina con orari)."""

    known = sorted((page, span) for page, span in spans.items() if span[0] is not None)
    if len(known) < 2:
        return None
    for (_, (_, last)), (_, (first, _)) in zip(known, known[1:]):
        if first is not None and last is not None and first < last:
            return False
    return True


def _next_page(chat: list[Message], earliest: datetime, limit: datetime | None) -> int | None:
    """Quale pagina chiedere adesso.

    Se le pagine sono in ordine di data (verificato su quelle lette) salto con la ricerca
    binaria alla prima con orari da ``earliest`` in poi e proseguo fino a ``limit``. Se non lo
    sono, o non si può ancora dire, le leggo una dopo l'altra: mai fermarsi su un'ipotesi."""

    spans, total = _page_spans(chat)
    if not spans:
        return 1
    order = _pages_sorted(spans)
    unseen = [page for page in range(1, total + 1) if page not in spans]
    if not unseen:
        return None
    if order is False:
        return unseen[0]
    past = [p for p, (_, last) in spans.items() if last is not None and last < earliest]
    future = sorted(p for p, (_, last) in spans.items() if last is not None and last >= earliest)
    low = max(past, default=0)
    if order is True and (not future or future[0] > low + 1):
        high = future[0] if future else total + 1
        wanted = max((low + high) // 2, low + 1)
        while wanted in spans and wanted < high:
            wanted += 1
        if wanted <= total and wanted not in spans:
            return wanted
    if order is None and not future:
        # Solo pagine passate finora: provo a metà; se l'ordine non torna, il giro dopo lo vede.
        wanted = max((low + total + 1) // 2, low + 1)
        while wanted in spans and wanted < total:
            wanted += 1
        return wanted if wanted not in spans else unseen[0]
    page = future[0] if future else (low + 1)
    while page in spans:
        _, last = spans[page]
        if order is True and limit is not None and last is not None and last >= limit:
            return None  # in ordine di data e oltre la scadenza: inutile sfogliare ancora
        page += 1
    return page if page <= total else (unseen[0] if order is not True else None)


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
    r"\b(preferisc\w*|preferirei|vorrei|se possibile|se si può|se si puo|possibilmente|"
    r"preferibilmente|tipo|meglio|magari|ideally|prefer\w*|would like|"
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


async def _citizen_declined(
    city: City, message: Message, case: list[Message], offers: dict[str, dict[str, str]]
) -> None:
    """Rifiuto di un'offerta. Se ne restano altre aspetto; quando sono rifiutate tutte cerco
    altri orari (quelli rifiutati restano esclusi) o propongo la lista d'attesa."""

    fresh = await city.history(need_id=message.need_id)
    if all(row.msg_id != message.msg_id for row in fresh):
        fresh = [*fresh, message]
    gone = _withdrawn(fresh, city.agent_id)
    still_open = [oid for oid, offer in offers.items() if offer.get("slot_id") and offer["slot_id"] not in gone]
    if still_open:
        return
    await _advance(city, message, fresh, fixed="CONS-GIN", replacing=True)


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
        elif row.sender == citizen and "cancel" not in words(row.text):
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



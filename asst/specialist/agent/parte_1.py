"""Care-protocol example. ``city init`` does not copy this file.

The scaffold brain is ``templates/agent/agent/agent.py``. This one is the full care
protocol (prescribe, offer, hold, commit, check-in, report), one inbound message per
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
    ("SYM-BACK", ("mal di schiena", "back pain")),
    ("SYM-KNEE", ("ginocchio", "knee")),
    ("SYM-PALPITATIONS", ("cuore che batte", "heart racing")),
    ("SYM-FOLLOWUP", ("controllo del cuore", "routine heart check")),
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
    # Rifiuto solo una vera domanda clinica di un cittadino: un check-in o una conferma che
    # nomina una "diagnosi" va registrato (rifiutarlo costa -2).
    booking_tokens = {"booking_id", "slot_id", "offer_id", "consent_ref", "hold_id"}
    if (
        clinical(text)
        and message.sender.startswith("citizen:")
        and not booking_tokens & set(fields_of(text))
        and not _is_checkin(text)
    ):
        raise OutOfScope()
    case = await city.history(need_id=message.need_id)
    await handle(role_of(), city, message, case)


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


# --- cortesia verso il cittadino (squadra epica) ------------------------------------------------

_NOTE = {
    "ricevuta": (
        "Prenotazione confermata. Al check-in porti questo numero di prenotazione.",
        "Your booking is confirmed. Please bring this booking number to check-in.",
    ),
    "checkin": (
        "Check-in registrato, grazie. Ora attenda la chiamata per la visita.",
        "You are checked in, thank you. Please wait to be called for your visit.",
    ),
    "cancel": (
        "Ci scusiamo: questo posto non è più disponibile. Le proponiamo subito un'alternativa.",
        "We are sorry: this slot is no longer available. We are offering you an alternative now.",
    ),
}


def _nota(chat: list[Message], kind: str) -> str:
    """Una frase breve e gentile dopo i token, nella lingua del cittadino (it o en)."""

    pair = _NOTE.get(kind)
    if pair is None:
        return ""
    return " " + (pair[0] if _lang(chat).lower().startswith("it") else pair[1])


async def handle(role: str, city: City, message: Message, chat: list[Message]) -> None:
    street = _own_street(city)
    if street and _asks_address(message):
        await city.say(message.sender, f"address={street}")
        return
    if role in ("hospital", "specialist_care") and message.sender.startswith("citizen:"):
        # Sicurezza del cittadino: un segnale d'allarme al check-in ferma tutto e manda al 112.
        if _facility_red_flag(message):
            if not _we(chat, city.agent_id, "reason=red_flag", to=message.sender, word="escalate.emergency"):
                await _say(city, message.sender, _facility_escalation(chat))
            return
        # Specialista: risposta alla richiesta di consenso per il referto (sì o no).
        if role == "specialist_care":
            pending = _report_pending(chat, city.agent_id, message.sender)
            if pending and not _is_checkin(message.text):
                consent = fields_of(message.text).get("consent_ref") or _bare_consent(message.text)
                await _file_report(city, message, chat, pending, consent if _valid_id(consent) else None)
                return
        # Le prenotazioni passano dal CUP (ricetta verificata, consenso): niente hold diretti.
        if "confirm" in words(message.text) and not _is_checkin(message.text):
            await _say(city, message.sender, _via_cup(chat))
            return
    if role in ("hospital", "specialist_care") and message.sender.startswith("agent:"):
        if first_word(message.text) == "need.request":
            # Il CUP di un'altra ASST cerca posti: rispondo con i nostri slot (solo criteri).
            await _serve_partner(city, message, chat, "VIS-FIS" if role == "hospital" else "VIS-CARD")
            return
        asked = fields_of(message.text)
        if (
            "confirm" in words(message.text)
            and _valid_id(asked.get("slot_id"))
            and _valid_id(asked.get("consent_ref"))
            and not _exact(chat, city.agent_id, _ACK)
        ):
            # Risposta immediata al CUP, poi hold e commit sul Ledger.
            await _say(city, message.sender, _ACK)
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


# --- livello 3: check-in e lingua detti a parole (squadra epica) --------------------------------

_CHECKIN_RE = re.compile(
    r"\bcheck[\s-]?in\b|\bsono (?:arrivat[oa]|qui|in sala|allo sportello)\b|\beccomi\b|"
    r"\bi'?m here\b|\bi am here\b|\bi(?: have|'ve) arrived\b|\bje suis (?:là|arrivée?)\b|"
    r"\bestoy aquí\b|\bich bin da\b"
)
_LANG_WORDS = {
    "en": {"the", "i", "i'm", "i'd", "my", "is", "and", "you", "hello", "hi", "please",
           "appointment", "would", "like", "need", "doctor", "thank", "thanks", "want", "have",
           "with", "for", "am", "here", "arrived"},
    "fr": {"je", "bonjour", "merci", "rendez-vous", "voudrais", "mon", "ma", "médecin", "avec",
           "pour", "suis", "j'ai", "est", "s'il", "vous", "besoin"},
    "es": {"hola", "quiero", "cita", "gracias", "médico", "necesito", "tengo", "buenos", "días",
           "quisiera", "estoy", "favor", "para"},
    "de": {"ich", "hallo", "termin", "bitte", "danke", "arzt", "ärztin", "möchte", "mein",
           "meine", "habe", "guten", "tag", "und", "bin"},
    "it": {"il", "lo", "la", "di", "che", "ho", "vorrei", "buongiorno", "grazie", "sono",
           "una", "mi", "ricetta", "salve", "visita", "medico", "della", "del", "non", "è",
           "un", "le", "ci", "per"},
}


def _is_checkin(text: str) -> bool:
    """Check-in con la parola dell'atto o detto a parole ("sono arrivato", "check-in")."""

    lowered = " ".join(text.lower().replace("’", "'").split())
    return "checkin" in words(text) or bool(_CHECKIN_RE.search(lowered))


def _booking_in(text: str, chat: list[Message]) -> str:
    """Il booking_id di un check-in detto a parole: un codice noto scritto nel testo, oppure
    l'unica prenotazione di questo caso registrata sul Ledger."""

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
    if len(known) == 1:
        return known[0]
    hit = re.search(r"\b(?:bk|b|booking)-[A-Za-z0-9-]{1,60}\b", text)
    return hit.group(0) if hit and _ID.fullmatch(hit.group(0)) else ""


def _detect_lang(text: str) -> str:
    tokens = [token.strip(".,;:!?()\"'«»") for token in text.lower().split()]
    scores = {lang: sum(1 for token in tokens if token in vocab) for lang, vocab in _LANG_WORDS.items()}
    best = max(scores, key=lambda lang: scores[lang])
    if best == "it" or scores[best] < 2 or scores[best] <= scores["it"]:
        return ""
    return best


# --- sicurezza, referto e controllo (squadra epica) -------------------------------------------

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_CONSENT_BARE = re.compile(r"\b(cons-[A-Za-z0-9][A-Za-z0-9_-]{2,80})")
_FACILITY_FLAGS = (
    "dolore al petto", "dolore toracico", "oppressione al petto", "non riesco a respirare",
    "perdita di forza", "non sento le gambe", "svenut", "perso i sensi", "emorragi",
    "perdo molto sangue", "convulsion", "chest pain", "can't breathe", "cannot breathe",
    "loss of strength", "fainted", "passed out", "seizure", "heavy bleeding",
)
_NOT_FLAG = re.compile(r"\b(non ho|nessun|senza)\s+(?:\w+\s+){0,2}(dolore|oppressione|svenut)|\bno chest pain\b")


def _valid_id(value: object) -> bool:
    """Un identificativo pulito (niente spazi né caratteri strani) prima di girarlo al Ledger."""

    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _bare_consent(text: str) -> str | None:
    hit = _CONSENT_BARE.search(text)
    return hit.group(1) if hit else None


def _facility_red_flag(message: Message) -> bool:
    if understood(message).body.get("is_emergency") is True:
        return True
    lowered = " ".join(message.text.lower().replace("’", "'").split())
    if _NOT_FLAG.search(lowered):
        return False
    return any(flag in lowered for flag in _FACILITY_FLAGS)


def _it(chat: list[Message]) -> bool:
    return _lang(chat).lower() in {"", "it", "ita"}


def _facility_escalation(chat: list[Message]) -> str:
    if _it(chat):
        return (
            "escalate.emergency reason=red_flag\n"
            "Quello che mi descrive va valutato subito. Chiami ora il 112 oppure lo dica "
            "immediatamente al personale qui presente: la visita può aspettare, lei no."
        )
    return (
        "escalate.emergency reason=red_flag\n"
        "What you describe needs to be looked at straight away. Please call 112 now or tell "
        "the staff here immediately: the visit can wait, you cannot."
    )


def _via_cup(chat: list[Message]) -> str:
    if _it(chat):
        return (
            "Grazie! Per tutelare lei e i suoi dati le prenotazioni passano dal Centro "
            "prenotazioni (CUP), che verifica la ricetta e le chiede il consenso: scelga lì "
            "l'orario e noi lo confermiamo subito."
        )
    return (
        "Thank you! To protect you and your data, bookings go through the Booking Centre, "
        "which checks the prescription and asks for your consent: choose the time there and "
        "we will confirm it straight away."
    )


def _report_pending(chat: list[Message], agent: str, citizen: str) -> str:
    """Il booking_id per cui abbiamo chiesto il consenso al referto e non l'abbiamo ancora
    archiviato."""

    for row in reversed(chat):
        if row.sender == agent and row.recipient == citizen and first_word(row.text) == "consent":
            fields = fields_of(row.text)
            booking = fields.get("booking_id", "")
            if fields.get("purpose") == "report_sharing" and booking:
                if not _we(chat, agent, f"booking_id={booking}", to=LEDGER, word="report"):
                    return booking
                return ""
    return ""


def _prescription_of(chat: list[Message]) -> str | None:
    for row in chat:
        if row.sender.startswith("agent:") and "confirm" in words(row.text):
            number = fields_of(row.text).get("prescription_number")
            if number:
                return number
    return None


async def _file_report(
    city: City, message: Message, chat: list[Message], booking_id: str, consent: str | None
) -> None:
    """Archivia il referto (condiviso solo con il consenso) e, se la visita lo richiede, la
    ricetta di controllo: la tabella di protocollo ha SYM-FOLLOWUP -> VIS-CARD P, quindi dopo
    una visita cardiologica non programmata prescrivo il controllo VIS-CARD con priorità P."""

    agent = city.agent_id
    citizen = _citizen(chat) or message.sender
    token = f"booking_id={booking_id}"
    if _we(chat, agent, token, to=LEDGER, word="report") or not _valid_id(booking_id):
        return
    follow_up = ""
    number = _prescription_of(chat)
    if number and _valid_id(number):
        if not _we(chat, agent, f"prescription_number={number}", to=LEDGER, word="prescription"):
            await _say(city, LEDGER, f"prescription prescription_number={number}")
        found = await _await_line(
            city, message.need_id, chat, "prescription", f"prescription_number={number}", op="prescription"
        )
        rx = fields_of(found[0].text) if found and first_word(found[0].text) == "prescription" else {}
        if rx.get("procedure") == "VIS-CARD" and (rx.get("priority") or "").upper() not in {"", "P"}:
            suffix = "".join(ch if ch.isalnum() else "-" for ch in booking_id.upper()).strip("-")[:30]
            candidate = f"prescription_number-FU-{suffix or 'VISIT'}"
            await _say(
                city, LEDGER,
                f"prescribe patient={citizen} procedure=VIS-CARD priority=P "
                f"follow_up_of={booking_id} prescription_number={candidate}",
            )
            done = await _await_line(
                city, message.need_id, chat, "prescribed", f"prescription_number={candidate}", op="prescribe"
            )
            if done and first_word(done[0].text) == "prescribed":
                follow_up = fields_of(done[0].text).get("prescription_number", candidate)
    parts = ["report", token, "outcome_code=VISIT"]
    if consent:
        parts.append(f"consent_ref={consent}")
    if follow_up:
        parts.append(f"follow_up_prescription_number={follow_up}")
    if not await _say(city, LEDGER, " ".join(parts)):
        return
    found = await _await_line(city, message.need_id, chat, "reported", token, op="report")
    if found is not None and first_word(found[0].text) == "reported":
        fresh = found[1]
    else:
        fresh = chat
    await _tell_report(city, fresh, citizen, booking_id, follow_up, shared=bool(consent))


async def _tell_report(
    city: City, chat: list[Message], citizen: str, booking_id: str, follow_up: str, *, shared: bool
) -> None:
    token = f"booking_id={booking_id}"
    if _we(chat, city.agent_id, token, to=citizen, word="report"):
        return
    lines = [f"report {token} outcome_code=VISIT"]
    if follow_up:
        lines.append(f"prescription prescription_number={follow_up} procedure=VIS-CARD priority=P")
    if _it(chat):
        lines.append(
            "Il referto della visita è archiviato nel registro"
            + (" e condiviso come ha acconsentito." if shared else ", senza condividerlo con altri.")
        )
        if follow_up:
            lines.append(
                "Per il controllo cardiologico le ho preparato la ricetta qui sopra (priorità P, "
                "programmabile): la porti al Centro prenotazioni, che la inserirà in lista d'attesa."
            )
        lines.append("Grazie per la fiducia, e buona giornata.")
    else:
        lines.append(
            "Your visit report is stored in the register"
            + (" and shared as you agreed." if shared else ", without sharing it with anyone.")
        )
        if follow_up:
            lines.append(
                "For the cardiology check-up I have prepared the prescription above (priority P, "
                "planned): take it to the Booking Centre, which will put you on the waiting list."
            )
        lines.append("Thank you for your trust, and have a good day.")
    await _say(city, citizen, "\n".join(lines))


# --- rete tra ASST e risposta rapida (squadra epica) --------------------------------------------

# Nessuna parola d'atto qui dentro: il CUP non deve leggerla come una richiesta nuova.
_ACK = "Ricevuto, grazie: blocco subito il posto sul Ledger e vi mando la conferma firmata appena è pronta."


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
            fields = fields_of(line)
            slot = fields.get("slot_id")
            if slot and slot not in seen:
                seen.add(slot)
                if "at" not in fields and "agenda_date" in fields:
                    line += " at=" + fields["agenda_date"]
                if "day" not in fields and "agenda_day" in fields:
                    line += " day=" + fields["agenda_day"]
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


async def _serve_partner(city: City, message: Message, chat: list[Message], default: str) -> None:
    """Un CUP (di solito di un'altra ASST) chiede disponibilità. Rispondo con i miei slot liberi,
    al massimo tre, senza chiedere né dare dati personali. Il confirm poi arriva a me."""

    agent = city.agent_id
    fields = fields_of(message.text)
    procedure = fields.get("procedure") or default
    request_id = fields.get("request_id") or message.need_id or message.msg_id
    priority = (fields.get("priority") or "").upper() or None
    lang = (fields.get("lang") or "it").lower()
    tag = f"request_id={request_id}"
    if _we(chat, agent, tag, to=message.sender, word="offer") or _we(
        chat, agent, tag, to=message.sender, word="cancel"
    ):
        return
    if not _has_agenda(chat) and not _exact(chat, agent, "agenda"):
        await _say(city, LEDGER, "agenda")
    if not _asked(chat, agent, procedure, 1):
        await _say(city, LEDGER, f"world procedure={procedure} page=1")
    chat = await _until(
        city, message.need_id, chat,
        lambda rows: _has_agenda(rows) and _latest_slots(rows) is not None,
    )
    now = _when((_ledger(chat, "agenda") or {}).get("agenda_now", ""))
    slots: list[dict[str, str]] = []
    # Gara lunga: le prime pagine sono date passate, salto alla prima con orari futuri.
    for _round in range(16):
        if now is None or priority == "P" or _latest_slots(chat) is None:
            break
        earliest = now + _LEAD
        slots = fitting(
            _all_slot_lines(chat), now=now, priority=priority, lang=lang, procedure=procedure,
        )[:3]
        if slots and not _searching(chat, earliest):
            break
        limit = None if priority == "U" else now + timedelta(days=_DEADLINE_DAYS.get(priority or "", 14) + 2)
        wanted = _next_page(chat, earliest, limit)
        if wanted is None:
            break
        if not _asked(chat, agent, procedure, wanted):
            await _say(city, LEDGER, f"world procedure={procedure} page={wanted}")
        chat = await _until(
            city, message.need_id, chat, lambda rows, wanted=wanted: wanted in _page_spans(rows)[0]
        )
        if wanted not in _page_spans(chat)[0]:
            break
    if not slots:
        await _say(
            city, message.sender,
            f"cancel {tag} procedure={procedure} reason=no_availability "
            "Al momento non abbiamo posti liberi per questa prestazione nei tempi richiesti. Grazie!",
        )
        return
    lines = []
    for slot in slots:
        parts = [
            "offer", f"offer_id=px-{slot['slot_id']}", f"slot_id={slot['slot_id']}",
            f"doctor_id={slot['doctor_id']}", f"facility={agent}", f"procedure={procedure}",
            f"agenda_date={slot['at']}", f"agenda_day={slot.get('day', '')}",
            f"expiry={slot['at']}", tag,
        ]
        if priority:
            parts.append(f"priority={priority}")
        lines.append(" ".join(part for part in parts if not part.endswith("=")))
    lines.append(
        "Ecco i nostri posti liberi (nessun dato del paziente richiesto finora). Per prenotare "
        "mandateci confirm con offer_id, slot_id, citizen, consent_ref e prescription_number."
    )
    await _say(city, message.sender, "\n".join(lines))


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


def _emergency(message: Message) -> bool:
    if understood(message).body.get("is_emergency") is True:
        return True
    lowered = message.text.lower()
    return any(flag in lowered for flag in _RED_FLAGS)



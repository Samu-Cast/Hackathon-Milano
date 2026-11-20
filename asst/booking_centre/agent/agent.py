"""Centro prenotazioni (booking_centre) — brain della squadra.

Cosa fa, un messaggio alla volta (tra un messaggio e l'altro non gira nulla):

1. Legge ogni messaggio del cittadino come un'AFFERMAZIONE, non come una domanda da girare a
   qualcun altro. "Ho la ricetta prescription_number-…", "Scelgo la seconda offer_id=…",
   "Preferisco il martedì", "Va bene, acconsento consent_ref=…" sono fatti da cui ricavare lo
   stato del caso. L'atto si riconosce dai token ``chiave=valore`` e dagli identificatori che
   abbiamo emesso noi, non solo dalla parola dell'atto.
2. Verifica la ricetta sul Ledger, legge agenda e regole in vigore, chiede (una volta) le
   preferenze di giorno e di genere del medico, poi propone gli slot del mondo della propria
   ASST, tutti nello stesso messaggio, entro la scadenza della priorità.
3. Se l'insieme con le preferenze è vuoto propone in modo trasparente l'alternativa rilassata;
   se la propria ASST non ha posti cerca nelle ASST partner (solo criteri, nessun dato
   personale) e chiede il consenso ``cross_asst`` prima di condividere qualsiasi dato.
4. Gira la scelta alla struttura, consegna la ricevuta firmata con i passi successivi chiari,
   riprenota subito se una struttura annulla, non ritenta mai un 4xx.
5. Su segnali d'allarme invia ``escalate.emergency``; rifiuta injection e richieste di dati di
   altre persone; non dà mai diagnosi né dosi.

Tono: cordiale, chiaro, semplice ma non banale, mai allarmante, sempre trasparente su cosa si
sta facendo e perché. Le parole d'atto (offer, confirm, cancel, ...) compaiono solo come primo
token di una riga, mai nella prosa, così cittadino e giudice non le leggono per sbaglio.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from city_sdk.city import LEDGER, CapError, City, Message, Need, OutOfScope, clinical, understood

_LOG = logging.getLogger("booking_centre")

# --- costanti ------------------------------------------------------------------------------------

# Margine tra "adesso" (agenda) e lo slot: l'agenda corre 60x, 90 minuti di agenda sono
# un minuto e mezzo reale, abbastanza per scelta, hold e commit.
_LEAD = timedelta(minutes=90)
_DEADLINE_DAYS = {"B": 2, "D": 5}
_MAX_PAGE = 8
_MAX_OFFERS = 3
_MAX_PARTNERS = 4
_PULL_TRIES = 24
_PULL_PAUSE = 0.25
_SENT_NEEDS = 4096
_MAX_TEXT = 2000

# Ripiego se il mondo non descrive i medici: chiave (ultimo pezzo di doctor_id) ->
# lingue, genere, giorni. ``*`` è la lingua rara dell'ASST.
_DOCTORS: dict[str, tuple[frozenset[str], str, frozenset[str]]] = {
    "h1": (frozenset({"it", "en"}), "f", frozenset({"mon", "tue", "wed"})),
    "h2": (frozenset({"it"}), "m", frozenset({"wed", "thu", "fri"})),
    "h3": (frozenset({"it", "*"}), "m", frozenset({"tue", "thu"})),
    "s1": (frozenset({"it", "en"}), "m", frozenset({"mon", "wed", "fri"})),
    "s2": (frozenset({"it"}), "f", frozenset({"tue", "thu"})),
    "c1": (frozenset({"it", "en"}), "f", frozenset({"mon", "tue", "wed", "thu", "fri"})),
}

# Procedura -> capability della struttura che la eroga (per cercare nelle ASST partner).
_SERVICE_OF = {
    "VIS-FIS": "hospital.physiatry",
    "VIS-CARD": "specialist_care.cardiology",
    "CTRL-CARD": "specialist_care.cardiology",
    "CONS-GIN": "family_counseling.gynecology",
}
# Agenti della nostra ASST in gara (console, sezione Agenti). Servono quando il registro chiama la
# struttura per tipo ("hospital") invece che con l'id agente. Si possono sovrascrivere con la
# variabile d'ambiente CITY_TEAM_FACILITIES="hospital=agent:...,specialist=agent:...".
_TEAM_FACILITIES = {
    "hospital": "agent:gezf38x5940y",
    "specialist": "agent:s2tevpn8zkcj",
    "specialist_care": "agent:s2tevpn8zkcj",
    "family_counseling": "agent:drchwbffymha",
}
_FAMILY_OF = {
    "VIS-FIS": "hospital",
    "VIS-CARD": "specialist",
    "CTRL-CARD": "specialist",
    "CONS-GIN": "family_counseling",
}
_DAY_CODES = {
    "lun": "mon", "mar": "tue", "mer": "wed", "gio": "thu", "ven": "fri", "sab": "sat",
    "dom": "sun", "mon": "mon", "tue": "tue", "wed": "wed", "thu": "thu", "fri": "fri",
    "sat": "sat", "sun": "sun",
}
_PROCEDURE_IT = {
    "VIS-FIS": "visita fisiatrica",
    "VIS-CARD": "visita cardiologica",
    "CTRL-CARD": "visita cardiologica di controllo",
    "CONS-GIN": "consulto ginecologico",
}
_PROCEDURE_EN = {
    "VIS-FIS": "physiatry visit",
    "VIS-CARD": "cardiology visit",
    "CTRL-CARD": "cardiology follow-up visit",
    "CONS-GIN": "gynaecology consultation",
}

_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_IT = {
    "mon": "lunedì",
    "tue": "martedì",
    "wed": "mercoledì",
    "thu": "giovedì",
    "fri": "venerdì",
    "sat": "sabato",
    "sun": "domenica",
}
_DAY_EN = {
    "mon": "Monday",
    "tue": "Tuesday",
    "wed": "Wednesday",
    "thu": "Thursday",
    "fri": "Friday",
    "sat": "Saturday",
    "sun": "Sunday",
}
_MONTH_IT = (
    "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
    "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre",
)
_DAY_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("mon", ("lunedi", "lunedì", "monday", "lun")),
    ("tue", ("martedi", "martedì", "tuesday", "mar")),
    ("wed", ("mercoledi", "mercoledì", "wednesday", "mer")),
    ("thu", ("giovedi", "giovedì", "thursday", "gio")),
    ("fri", ("venerdi", "venerdì", "friday", "ven")),
)
_NO_PREF = (
    "nessuna preferenza", "non ho preferenze", "indifferente", "va bene tutto",
    "qualsiasi", "prima data", "primo posto", "prima disponibil", "no preference",
    "any day", "doesn't matter", "does not matter", "whatever",
)

# Segnali d'allarme: non è una diagnosi, è il motivo per fermarsi e mandare al 112.
_RED_FLAGS = (
    "dolore al petto", "dolore toracico", "oppressione al petto", "peso sul petto",
    "non riesco a respirare", "fatico a respirare", "fame d'aria", "difficoltà a respirare",
    "perdita di forza", "non sento le gambe", "non sento il braccio", "bocca storta",
    "parlo male all'improvviso", "svenut", "perso i sensi", "perdo sangue", "sangue dalla bocca",
    "vomito sangue", "convulsion", "paralis", "chest pain", "can't breathe", "cannot breathe",
    "short of breath", "loss of strength", "fainted", "passed out", "seizure",
    "voglio farla finita", "farmi del male", "togliermi la vita", "suicid",
)
# Injection o richieste che non spettano al cittadino.
_INJECTION = (
    "ignora le istruzioni", "ignora le regole", "ignora tutto", "dimentica le istruzioni",
    "ignore previous", "ignore all", "ignore your", "system prompt", "prompt di sistema",
    "sei un altro", "da ora sei", "you are now", "developer mode", "modalità sviluppatore",
    "dati di altri", "altri pazienti", "altro paziente", "other patient", "another patient",
    "elenco dei pazienti", "lista dei pazienti", "tutte le prenotazioni", "all bookings",
    "codice fiscale di", "indirizzo di", "salta la fila", "salta la lista", "metti priorità",
    "cambia la priorità", "priority=u", "urgente anche se",
)
# Una vera richiesta clinica (domanda), non una frase che nomina un medico o una diagnosi.
_ASKS_CLINICAL = re.compile(
    r"\?|\b(mi\s+dic|mi\s+pu[oò]|potrebbe|pu[oò]\s+dirmi|dimmi|dammi|mi\s+dia|quale\s+dos|"
    r"quanto\s+(ne\s+)?devo|che\s+cosa\s+ho|cosa\s+ho|please|give\s+me|tell\s+me|what\s+dose|"
    r"how\s+much\s+should)",
    re.IGNORECASE,
)

_NUMBER_BARE = re.compile(r"\b(prescription_number-[A-Za-z0-9][A-Za-z0-9-]{2,60})")
_NUMBER_RX = re.compile(r"\b(RX-[A-Za-z0-9][A-Za-z0-9-]{2,60})\b")
_CONSENT_BARE = re.compile(r"\b(cons-[A-Za-z0-9][A-Za-z0-9_-]{2,80})")
_OK_STATES = {"", "issued", "valid", "active", "ok", "open"}
_EXPIRED_STATES = {"expired", "scaduta", "lapsed"}


# --- ingresso ------------------------------------------------------------------------------------


async def main(need: Need, city: City) -> None:
    message = city.inbound
    if message is None:
        return
    if message.sender == city.agent_id:
        return
    text = message.text or need.summary or ""

    # Emergenza dichiarata dall'harness o dal need: ci si ferma e si escala.
    body = understood(message).body if message.type == "chat" else message.body
    if (need.is_emergency or body.get("is_emergency") is True) and message.sender != LEDGER:
        await _say(city, message.sender, _escalation_text(_lang_of_text(text)))
        return

    # Rifiuto solo una vera domanda clinica (diagnosi, dose) che non porta nulla da prenotare.
    # Una frase come "il medico mi ha fatto la diagnosi, ecco la ricetta" è un'affermazione:
    # si prenota e si ricorda con garbo che sul piano clinico decide il medico.
    if (
        not message.sender.startswith("agent:")
        and message.sender != LEDGER
        and clinical(text)
        and _ASKS_CLINICAL.search(text)
        and not _has_booking_tokens(text)
    ):
        raise OutOfScope()

    try:
        chat = await city.history(need_id=message.need_id)
    except CapError:
        chat = []
    if all(row.msg_id != message.msg_id for row in chat):
        chat = [*chat, message]

    street = _own_street(city)
    if street and _asks_address(message):
        await _say(city, message.sender, f"address={street}")
        return
    try:
        await _desk(city, message, chat)
    except (OutOfScope, asyncio.CancelledError):
        raise
    except Exception:  # noqa: BLE001 - un errore di logica non deve lasciare il caso fermo
        _LOG.exception("booking.turn.error", extra={"need_id": (message.need_id or "")[:48]})


# --- lettura del testo ----------------------------------------------------------------------------


def words(text: str) -> set[str]:
    return {token.strip(".,;:!?()\"'«»").lower() for token in text.split() if token.strip()}


def first_word(text: str) -> str:
    parts = text.split()
    return parts[0].strip(".,;:!?()\"'").lower() if parts else ""


def line_heads(text: str) -> set[str]:
    """Il primo token di ogni riga: è lì che scriviamo (e cerchiamo) la parola dell'atto."""

    return {first_word(line) for line in text.splitlines() if line.strip()}


def fields_of(text: str) -> dict[str, str]:
    """Primo valore di ogni ``chiave=valore`` nel testo."""

    found: dict[str, str] = {}
    # Separo solo sugli spazi: "days=lun,mar,mer" resta una lista; "a=1, b=2" funziona perché
    # la virgola finale viene tolta dal valore.
    for token in text.replace(";", " ").split():
        key, eq, value = token.partition("=")
        key = key.strip("([{\"'«")
        if not eq or not key or not value:
            continue
        value = value.strip(".,;:!?)]}\"'»")
        if key not in found and value:
            found[key] = value
    return found


def _norm(text: str) -> str:
    return " ".join(text.lower().replace("’", "'").split())


def _has_booking_tokens(text: str) -> bool:
    fields = fields_of(text)
    if any(key in fields for key in ("prescription_number", "offer_id", "slot_id", "consent_ref")):
        return True
    return bool(_NUMBER_BARE.search(text) or _NUMBER_RX.search(text))


def _number_in(text: str) -> str | None:
    """Il numero di ricetta in una frase: come token, oppure scritto così com'è."""

    number = fields_of(text).get("prescription_number")
    if number:
        return number
    for pattern in (_NUMBER_BARE, _NUMBER_RX):
        hit = pattern.search(text)
        if hit:
            return hit.group(1).rstrip(".,;:!?-")
    return None


def _red_flag(text: str) -> bool:
    lowered = _norm(text)
    if fields_of(text).get("is_emergency", "").lower() == "true":
        return True
    return any(flag in lowered for flag in _RED_FLAGS)


def _injection(text: str) -> bool:
    lowered = _norm(text)
    return any(marker in lowered for marker in _INJECTION)


def _when(value: str) -> datetime | None:
    """Un istante ISO. Senza fuso orario lo leggo come UTC, così agenda e slot restano
    confrontabili anche se il Ledger li scrive in forma breve."""

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _day_code(value: str) -> str:
    return _DAY_CODES.get(value.strip().lower()[:3], "")


def _split(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[/,|;+]", value or "") if part.strip()]


def _lang_of_text(text: str) -> str:
    lang = fields_of(text).get("lang", "").lower()
    return "it" if lang in {"", "it", "ita"} else "en"


# --- storia del caso ------------------------------------------------------------------------------


def _citizen(chat: list[Message]) -> str | None:
    for row in chat:
        if row.sender.startswith("citizen:"):
            return row.sender
    for row in chat:
        for key in ("citizen", "patient"):
            party = fields_of(row.text).get(key, "")
            if party.startswith("citizen:"):
                return party
    return None


def _from_citizen(chat: list[Message]) -> list[Message]:
    return [row for row in chat if row.sender.startswith("citizen:")]


def _ours(chat: list[Message], agent: str, to: str | None = None) -> list[Message]:
    return [
        row for row in chat if row.sender == agent and (to is None or row.recipient == to)
    ]


def _we(chat: list[Message], agent: str, token: str, *, to: str, head: str) -> bool:
    """Abbiamo già mandato a ``to`` una riga che inizia con ``head`` e contiene ``token``."""

    for row in _ours(chat, agent, to):
        for line in row.text.splitlines():
            if first_word(line) == head and (not token or token in line.split()):
                return True
    return False


def _sent_exact(chat: list[Message], agent: str, to: str, text: str) -> bool:
    return any(row.text.strip() == text.strip() for row in _ours(chat, agent, to))


def _ledger(chat: list[Message], head: str) -> dict[str, str] | None:
    found: dict[str, str] | None = None
    for row in chat:
        if row.sender == LEDGER and first_word(row.text) == head:
            found = fields_of(row.text)
    return found


def _ledger_text(chat: list[Message], head: str) -> str | None:
    found: str | None = None
    for row in chat:
        if row.sender == LEDGER and first_word(row.text) == head:
            found = row.text
    return found


def _refusal_for(chat: list[Message], op: str, token: str = "") -> str | None:
    """Testo dell'ultimo rifiuto del Ledger per ``op`` (e, se dato, per ``token``)."""

    found: str | None = None
    for row in chat:
        if row.sender != LEDGER or first_word(row.text) != "refused":
            continue
        if op not in words(row.text):
            continue
        if token and token not in row.text:
            continue
        found = row.text
    return found


def _number(chat: list[Message]) -> str | None:
    """L'ultimo numero di ricetta detto dal cittadino (vince il più recente: è il rinnovo)."""

    for row in reversed(_from_citizen(chat)):
        number = _number_in(row.text)
        if number:
            return number
    return None


def _lang(chat: list[Message]) -> str:
    for row in _from_citizen(chat):
        lang = fields_of(row.text).get("lang", "")
        if lang:
            return lang.lower()
    return "it"


def _tone(chat: list[Message]) -> str:
    """Lingua delle frasi: italiano per chi scrive in italiano, inglese per tutti gli altri
    (per chi non parla italiano l'inglese è più accessibile)."""

    lang = _lang(chat)
    return "it" if lang in {"", "it", "ita"} else "en"


# Una riga nella lingua del cittadino, quando le frasi sono in inglese per lui.
_LANG_LINE = {
    "fr": "Je vous écris en anglais pour être clair, et je vous propose seulement des médecins qui parlent votre langue.",
    "es": "Le escribo en inglés para ser claro, y solo le propongo médicos que hablan su idioma.",
    "de": "Ich schreibe Ihnen auf Englisch, damit alles klar ist, und schlage Ihnen nur Ärztinnen und Ärzte vor, die Ihre Sprache sprechen.",
    "pt": "Escrevo-lhe em inglês para ser claro e só lhe proponho médicos que falam a sua língua.",
    "ro": "Vă scriu în engleză ca să fie clar și vă propun doar medici care vorbesc limba dumneavoastră.",
    "ar": "أكتب لك بالإنجليزية للوضوح، وأقترح عليك فقط أطباء يتحدثون لغتك.",
    "zh": "为了清楚起见，我用英语给您写信，并且只为您推荐会说您语言的医生。",
    "uk": "Я пишу вам англійською для ясності й пропоную лише лікарів, які розмовляють вашою мовою.",
    "ru": "Я пишу вам по-английски для ясности и предлагаю только врачей, которые говорят на вашем языке.",
    "sq": "Ju shkruaj në anglisht që të jetë e qartë dhe ju propozoj vetëm mjekë që flasin gjuhën tuaj.",
    "pl": "Piszę po angielsku dla jasności i proponuję tylko lekarzy, którzy mówią w Pana/Pani języku.",
}


def _lang_line(chat: list[Message]) -> str:
    return _LANG_LINE.get(_lang(chat)[:2], "")


def _days_from(text: str) -> set[str] | None:
    """Giorni preferiti: dal token ``days=`` o dalle parole della frase. None = non detto."""

    raw = fields_of(text).get("days") or fields_of(text).get("day")
    if raw:
        found = {
            part[:3].lower()
            for part in re.split(r"[/,|+]", raw)
            if part[:3].lower() in _DAYS
        }
        if found:
            return found
    lowered = _norm(text)
    found = set()
    for code, names in _DAY_WORDS:
        for name in names:
            if len(name) <= 3:
                if re.search(rf"\b{name}\b", lowered):
                    found.add(code)
            elif name in lowered:
                found.add(code)
    return found or None


def _gender_from(text: str) -> str | None:
    fields = fields_of(text)
    raw = (fields.get("gender") or fields.get("gender_preference") or "").lower()
    if raw in {"m", "f"}:
        return raw
    if raw in {"female", "donna", "woman"}:
        return "f"
    if raw in {"male", "uomo", "man"}:
        return "m"
    lowered = _norm(text)
    if re.search(r"\b(dottoressa|medica|una donna|medico donna|femmina|female|woman doctor|a woman)\b", lowered):
        return "f"
    if re.search(r"\b(un uomo|medico uomo|maschio|male doctor|a man)\b", lowered):
        return "m"
    return None


def _asked_prefs_at(chat: list[Message], agent: str, citizen: str) -> int | None:
    for index, row in enumerate(chat):
        if row.sender == agent and row.recipient == citizen and "data.request" in line_heads(row.text):
            if "purpose=booking" in row.text and (
                "fields=days" in row.text or "fields=gender" in row.text
            ):
                return index
    return None


def _preferences(
    chat: list[Message], agent: str, citizen: str
) -> tuple[set[str] | None, str | None, bool]:
    """Giorni, genere e se il cittadino ha già risposto (anche con un no detto a parole).

    I token ``days=``/``gender=`` valgono sempre. Le parole ("il martedì", "una dottoressa")
    valgono solo dopo che le abbiamo chieste: "ho mal di schiena da martedì" non è una
    preferenza."""

    asked = _asked_prefs_at(chat, agent, citizen)
    days: set[str] | None = None
    gender: str | None = None
    answered = False
    for index, row in enumerate(chat):
        if not row.sender.startswith("citizen:"):
            continue
        after = asked is not None and index > asked
        fields = fields_of(row.text)
        if after or "days" in fields or "day" in fields:
            days = _days_from(row.text) or days
        if after or "gender" in fields or "gender_preference" in fields:
            gender = _gender_from(row.text) or gender
        if after:
            answered = True
    answered = answered or days is not None or gender is not None
    return days, gender, answered


# --- mondo: slot e medici --------------------------------------------------------------------------


def _world_lines(chat: list[Message]) -> tuple[list[str], int, int]:
    """Righe di slot e medici dalle risposte ``slots``/``world`` del Ledger, più pagina e totale."""

    lines: list[str] = []
    page, pages = 1, 1
    seen = False
    for row in chat:
        if row.sender != LEDGER or first_word(row.text) not in {"slots", "world"}:
            continue
        seen = True
        head = row.text.splitlines()[0] if row.text else ""
        raw = fields_of(head).get("page", "")
        left, _, right = raw.partition("/")
        if left.isdigit() and right.isdigit():
            page, pages = max(page, int(left)), int(right)
        lines.extend(line for line in row.text.splitlines() if "=" in line)
    return (lines if seen else []), page, pages if seen else 0


def _has_world(chat: list[Message]) -> bool:
    return any(row.sender == LEDGER and first_word(row.text) in {"slots", "world"} for row in chat)


def _doctors(lines: list[str]) -> dict[str, tuple[frozenset[str], str, frozenset[str]]]:
    """Medici descritti dal mondo (righe con doctor_id e senza slot_id)."""

    found: dict[str, tuple[frozenset[str], str, frozenset[str]]] = {}
    for line in lines:
        fields = fields_of(line)
        doctor = fields.get("doctor_id") or fields.get("doctor")
        if not doctor or "slot_id" in fields:
            continue
        langs = fields.get("languages") or fields.get("langs") or fields.get("lang") or ""
        days = fields.get("days", "")
        found[doctor] = (
            frozenset(part.lower() for part in _split(langs)),
            fields.get("gender", "").lower()[:1],
            frozenset(code for code in (_day_code(part) for part in _split(days)) if code),
        )
    return found


def _spec(doctor: str, known: dict[str, tuple[frozenset[str], str, frozenset[str]]]):
    return known.get(doctor) or _DOCTORS.get(doctor.rsplit("-", 1)[-1])


def _speaks(langs: frozenset[str], lang: str) -> bool:
    if not langs:
        return lang in {"it", ""}
    if lang in langs:
        return True
    return "*" in langs and lang not in {"it", "en"}


def _horizon(now: datetime, priority: str | None) -> datetime | None:
    if priority == "U":
        return now.replace(hour=23, minute=59, second=59, microsecond=0)
    if priority == "P":
        return None
    return now + timedelta(days=_DEADLINE_DAYS.get(priority or "", 14))


def normalize_slot(raw: dict[str, str]) -> dict[str, str] | None:
    """Uno slot del registro con chiavi uniformi: slot_id, doctor_id, at, day, facility,
    procedure, reserve. None se manca qualcosa di essenziale."""

    slot = dict(raw)
    slot_id = slot.get("slot_id") or slot.get("slot")
    doctor = slot.get("doctor_id") or slot.get("doctor")
    when = slot.get("at") or slot.get("agenda_date") or slot.get("start") or slot.get("datetime")
    if not when and slot.get("date"):
        when = slot["date"] + ("T" + slot["time"] if slot.get("time") else "")
    at = _when(when or "")
    if not slot_id or not doctor or at is None:
        return None
    slot["slot_id"] = slot_id
    slot["doctor_id"] = doctor
    slot["at"] = at.isoformat()
    slot["day"] = _day_code(slot.get("day") or slot.get("agenda_day") or "") or _DAYS[at.weekday()]
    reserve = ""
    for key in ("urgent_reserve", "reserve", "reserved", "riserva", "urgent"):
        if key in slot:
            reserve = slot[key].lower()
            break
    slot["reserve"] = "true" if reserve in {"true", "yes", "si", "sì", "u", "urgent", "urgente", "1"} else ""
    return slot


def fitting(
    lines: list[str],
    *,
    now: datetime,
    priority: str | None,
    lang: str,
    procedure: str | None,
    days: set[str] | None = None,
    gender: str | None = None,
    skip: set[str] | frozenset[str] = frozenset(),
    skip_facilities: set[str] | frozenset[str] = frozenset(),
) -> list[dict[str, str]]:
    """Slot che il cittadino può accettare: futuri, liberi, entro la priorità, nella sua lingua,
    fuori dalla riserva urgente se la priorità non è U, nei giorni di lavoro del medico e (se
    date) con le sue preferenze. Ordinati dal più vicino."""

    horizon = _horizon(now, priority)
    earliest = now + _LEAD
    known = _doctors(lines)
    chosen: list[tuple[datetime, dict[str, str]]] = []
    seen: set[str] = set()
    for line in lines:
        slot = normalize_slot(fields_of(line))
        if slot is None or slot["slot_id"] in skip or slot["slot_id"] in seen:
            continue
        at = _when(slot["at"])
        assert at is not None
        doctor = slot["doctor_id"]
        if slot.get("facility", "") in skip_facilities:
            continue
        if procedure and slot.get("procedure") not in (None, procedure):
            continue
        for key in ("free", "seats", "posti", "capacity"):
            value = slot.get(key)
            if value is not None and value.isdigit() and int(value) <= 0:
                break
        else:
            value = None
        if value is not None:
            continue
        if slot.get("state", slot.get("status", "free")).lower() not in {"free", "open", "available", "libero"}:
            continue
        if slot["reserve"] and priority != "U":
            continue  # regola P3: la riserva è solo per la priorità U
        if not earliest < at or (horizon is not None and at >= horizon):
            continue
        if not 8 <= at.hour < 18:
            continue  # ambulatori aperti 08-18
        spec = _spec(doctor, known)
        langs = frozenset(part.lower() for part in _split(slot.get("languages", ""))) or (
            spec[0] if spec else frozenset()
        )
        if not _speaks(langs, lang):
            continue
        day = slot["day"]
        if spec and spec[2] and day not in spec[2]:
            continue  # il cittadino verifica i giorni di lavoro del medico sul registro
        if days and day not in days:
            continue
        doc_gender = (slot.get("gender") or (spec[1] if spec else "")).lower()[:1]
        if gender and doc_gender != gender:
            continue
        seen.add(slot["slot_id"])
        chosen.append((at, slot))
    chosen.sort(key=lambda item: (item[0], item[1].get("slot_id", "")))
    return [slot for _, slot in chosen]


def _own_facilities(lines: list[str]) -> set[str]:
    return {fields_of(line)["facility"] for line in lines if "facility" in fields_of(line)}


# --- testi per il cittadino -------------------------------------------------------------------------


def _label(procedure: str | None, tone: str) -> str:
    table = _PROCEDURE_EN if tone == "en" else _PROCEDURE_IT
    return table.get(procedure or "", procedure or ("visit" if tone == "en" else "visita"))


def _when_text(value: str, tone: str) -> str:
    at = _when(value)
    if at is None:
        return value
    day = _DAYS[at.weekday()]
    if tone == "en":
        return f"{_DAY_EN[day]} {at.day} {at.strftime('%B')} at {at.strftime('%H:%M')}"
    return f"{_DAY_IT[day]} {at.day} {_MONTH_IT[at.month - 1]} alle ore {at.strftime('%H:%M')}"


def _priority_text(priority: str | None, tone: str) -> str:
    if tone == "en":
        return {
            "U": "priority U: the visit should take place today",
            "B": "priority B: the visit should take place within 2 days",
            "D": "priority D: the visit should take place within 5 days",
            "P": "priority P: a planned visit, with no fixed deadline",
        }.get(priority or "", "")
    return {
        "U": "priorità U: la visita va fatta in giornata",
        "B": "priorità B: la visita va fatta entro 2 giorni",
        "D": "priorità D: la visita va fatta entro 5 giorni",
        "P": "priorità P: è una visita programmabile, senza una scadenza stretta",
    }.get(priority or "", "")


def _fit(text: str) -> str:
    text = text.strip()
    return text if len(text) <= _MAX_TEXT else text[: _MAX_TEXT - 1].rstrip() + "…"


def _escalation_text(tone: str) -> str:
    if tone == "en":
        return (
            "escalate.emergency reason=red_flag\n"
            "What you describe needs to be looked at straight away, before any booking. "
            "To be safe, please call 112 (the European emergency number) now, or go to the "
            "nearest emergency department. If someone is with you, ask them to stay close. "
            "I cannot assess symptoms, but the emergency team can, and they are the right people "
            "to help you right now."
        )
    return (
        "escalate.emergency reason=red_flag\n"
        "Quello che mi descrive va valutato subito, prima di qualsiasi prenotazione. "
        "Per sicurezza chiami ora il 112 (numero unico di emergenza) oppure si rechi al pronto "
        "soccorso più vicino. Se c'è qualcuno con lei, gli chieda di restarle accanto. "
        "Io non posso valutare i sintomi, ma i soccorritori sì: sono le persone giuste per "
        "aiutarla adesso."
    )


def _guard_note(tone: str, *, injected: bool, clinical_words: bool) -> str:
    notes = []
    if injected:
        notes.append(
            "I can only take care of your own booking: I cannot share other people's data or "
            "change the service rules."
            if tone == "en"
            else "Posso occuparmi solo della sua prenotazione: non posso condividere dati di "
            "altre persone né cambiare le regole del servizio."
        )
    if clinical_words:
        notes.append(
            "For clinical questions (symptoms, medicines, doses) your GP is the right person; "
            "I take care of the booking."
            if tone == "en"
            else "Per le domande cliniche (sintomi, farmaci, dosi) la persona giusta è il suo "
            "medico di base; io mi occupo della prenotazione."
        )
    return " ".join(notes)


def _offer_line(slot: dict[str, str], offer_id: str, number: str | None, priority: str | None) -> str:
    at = slot.get("at") or slot.get("agenda_date", "")
    parts = [
        "offer",
        f"offer_id={offer_id}",
        f"slot_id={slot['slot_id']}",
        f"doctor_id={slot['doctor_id']}",
    ]
    if slot.get("facility"):
        parts.append(f"facility={slot['facility']}")
    if slot.get("procedure"):
        parts.append(f"procedure={slot['procedure']}")
    parts += [f"agenda_date={at}", f"agenda_day={slot.get('day', '')}", f"expiry={at}"]
    if priority:
        parts.append(f"priority={priority}")
    if number:
        parts.append(f"prescription_number={number}")
    return " ".join(part for part in parts if not part.endswith("="))


def _offers_text(
    slots: list[dict[str, str]],
    *,
    number: str | None,
    priority: str | None,
    procedure: str | None,
    tone: str,
    intro: str,
    ids: list[str],
) -> str:
    lines = [intro]
    for index, (slot, offer_id) in enumerate(zip(slots, ids), start=1):
        when = _when_text(slot.get("at", ""), tone)
        lines.append(("Option" if tone == "en" else "Opzione") + f" {index}: {when}")
        lines.append(_offer_line(slot, offer_id, number, priority))
    if tone == "en":
        lines.append(
            "To choose, reply with the offer_id of the option you prefer. If none of them "
            "works for you, just tell me and I will look for another solution."
        )
    else:
        lines.append(
            "Per scegliere mi risponda indicando l'offer_id dell'opzione che preferisce. "
            "Se nessuna le va bene, me lo dica pure e cerco un'altra soluzione."
        )
    return _fit("\n".join(lines))


# --- messaggi in uscita -------------------------------------------------------------------------------


async def _say(city: City, to: str, text: str) -> bool:
    """Un solo invio. Un rifiuto (4xx) chiude quella richiesta: non si ritenta mai."""

    try:
        await city.say(to, _fit(text))
    except CapError as err:
        _LOG.info("booking.say.refused", extra={"code": getattr(err, "code", ""), "to": to[:64]})
        return False
    return True


async def _say_or_code(city: City, to: str, text: str) -> str:
    """Come ``_say`` ma restituisce il codice di errore ("" se l'invio è andato)."""

    try:
        await city.say(to, _fit(text))
    except CapError as err:
        return str(getattr(err, "code", "") or "REFUSED")
    return ""


async def _until(
    city: City,
    need_id: str | None,
    chat: list[Message],
    ready: Callable[[list[Message]], bool],
) -> list[Message]:
    """Rilegge il caso finché ``ready`` o finché la breve attesa finisce."""

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


# --- memoria di processo e lock per caso -----------------------------------------------------------------


class _Gate:
    __slots__ = ("lock", "turns")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.turns = 0


class _Sent:
    """Cosa abbiamo già mandato per un caso, anche se la storia non lo mostra ancora."""

    __slots__ = ("offers", "lines", "flags")

    def __init__(self) -> None:
        self.offers: set[str] = set()  # slot_id (o offer_id della lista d'attesa) offerti
        self.lines: set[str] = set()  # chiavi di messaggi una-tantum
        self.flags: set[str] = set()


_GATES: dict[str, _Gate] = {}
_SENT: OrderedDict[str, _Sent] = OrderedDict()
_ADDRESSES: dict[str, str] = {}


def _sent(key: str) -> _Sent:
    entry = _SENT.get(key)
    if entry is None:
        entry = _SENT[key] = _Sent()
        while len(_SENT) > _SENT_NEEDS:
            _SENT.popitem(last=False)
    else:
        _SENT.move_to_end(key)
    return entry


async def _once(city: City, key: str, tag: str, to: str, text: str) -> bool:
    """Manda ``text`` una sola volta per caso e per ``tag``."""

    sent = _sent(key)
    if tag in sent.lines:
        return False
    sent.lines.add(tag)
    return await _say(city, to, text)


async def _locked(key: str, work: Callable[[], object]) -> None:
    gate = _GATES.get(key)
    if gate is None:
        gate = _GATES[key] = _Gate()
    gate.turns += 1
    try:
        async with gate.lock:
            await work()  # type: ignore[misc]
    finally:
        gate.turns -= 1
        if gate.turns == 0 and _GATES.get(key) is gate:
            del _GATES[key]


# --- strutture: dall'etichetta del registro all'agente ---------------------------------------------


def _team_facilities() -> dict[str, str]:
    table = dict(_TEAM_FACILITIES)
    for part in os.environ.get("CITY_TEAM_FACILITIES", "").split(","):
        key, _, value = part.partition("=")
        if key.strip() and value.strip().startswith("agent:"):
            table[key.strip()] = value.strip()
    return table


async def _facility_agent(city: City, facility: str, procedure: str | None) -> str:
    """L'agente a cui mandare il ``confirm``. Il registro può nominare la struttura per tipo
    ("hospital", "asst-navigli-hospital"): in quel caso uso l'agente della nostra ASST."""

    if facility.startswith("agent:"):
        return facility
    table = _team_facilities()
    lowered = facility.lower()
    family = next((key for key in ("family_counseling", "specialist", "hospital") if key in lowered), "")
    if not family and ("counsel" in lowered or "consult" in lowered):
        family = "family_counseling"
    if not family:
        family = _FAMILY_OF.get(procedure or "", "")
    known = table.get(family, "")
    service = _SERVICE_OF.get(procedure or "", "")
    if service:
        try:
            peers = await city.find(service)
        except CapError:
            peers = []
        ids = {peer.agent_id for peer in peers}
        if known and known in ids:
            return known
        # Ripiego: un agente con lo stesso nome della struttura del registro.
        for peer in peers:
            if lowered and lowered in peer.agent_id.lower():
                return peer.agent_id
    return known or facility


def _confirmed_slot(chat: list[Message], agent: str, slot: str) -> str:
    """A chi abbiamo già girato il ``confirm`` di questo slot ("" se a nessuno)."""

    token = f"slot_id={slot}"
    for row in _ours(chat, agent):
        for line in row.text.splitlines():
            if first_word(line) == "confirm" and token in line.split():
                return row.recipient
    return ""


async def _ack(city: City, message: Message, key: str, text: str) -> None:
    """Risposta breve e immediata a un altro agente (collaborazione e fast-reply). Niente parole
    d'atto né codici di prestazione, così nessun brain la legge come una nuova richiesta."""

    await _once(city, key, f"ack:{message.msg_id}", message.sender, text)


# --- smistamento ---------------------------------------------------------------------------------------


def _our_offers(chat: list[Message], agent: str, citizen: str) -> dict[str, dict[str, str]]:
    """offer_id -> campi, per ogni offerta che abbiamo mandato al cittadino."""

    found: dict[str, dict[str, str]] = {}
    for row in _ours(chat, agent, citizen):
        for line in row.text.splitlines():
            if first_word(line) != "offer":
                continue
            fields = fields_of(line)
            if fields.get("offer_id"):
                found[fields["offer_id"]] = fields
    return found


def _chosen_offer(text: str, offers: dict[str, dict[str, str]]) -> str | None:
    """L'offerta che il cittadino ha nominato: come token, come slot, o scritta così com'è."""

    fields = fields_of(text)
    if fields.get("offer_id") in offers:
        return fields["offer_id"]
    slot = fields.get("slot_id")
    if slot:
        for offer_id, offer in offers.items():
            if offer.get("slot_id") == slot:
                return offer_id
    bare = words(text)
    for offer_id in sorted(offers, key=len, reverse=True):
        if offer_id.lower() in bare or offer_id in text:
            return offer_id
    return None


def _choice_words(text: str) -> bool:
    lowered = _norm(text)
    return bool(
        re.search(
            r"\b(scelgo|prendo|confermo|accetto|va bene|ok|d'accordo|perfetto|la prima|la seconda|"
            r"la terza|opzione|i choose|i'll take|i accept|fine|yes|sì|si)\b",
            lowered,
        )
    )


_WANTS_MORE = re.compile(
    r"\b(nessuna|non (mi )?va(nno)? bene|altre date|altri giorni|altro giorno|altra data|"
    r"più avanti|piu avanti|none of (them|these)|other dates|another (day|date)|later date)\b",
    re.IGNORECASE,
)


async def _desk(city: City, message: Message, chat: list[Message]) -> None:
    sender = message.sender
    text = message.text
    heads = line_heads(text) | words(text)
    key = message.need_id or message.msg_id

    if sender == LEDGER:
        await _locked(key, lambda: _advance(city, message, chat, key, replacing=False))
        return

    if sender.startswith("agent:") or sender.startswith("city:"):
        # Strutture (nostre o partner) e avvisi della città. Una ricevuta la accetto solo da
        # un agente: niente ricevute fantasma.
        agent_sender = sender.startswith("agent:")
        if "booking.receipt" in heads and agent_sender:
            await _ack(city, message, key, "Grazie, ricevuto: giro subito la ricevuta al cittadino.")
            await _forward_receipt(city, message, chat, key)
        elif heads & {"cancel", "refused", "fault.cancel", "expired"}:
            if agent_sender:
                await _ack(city, message, key, "Grazie dell'avviso: aggiorno il cittadino e cerco subito un'alternativa.")
            await _locked(key, lambda: _facility_cancel(city, message, chat, key))
        elif "offer" in line_heads(text) and agent_sender:
            await _ack(city, message, key, "Grazie per la disponibilità: la propongo subito al cittadino.")
            await _locked(key, lambda: _partner_offer(city, message, chat, key))
        elif "need.request" in heads and agent_sender:
            await _locked(key, lambda: _serve_partner(city, message, chat, key))
        return

    if not sender.startswith("citizen:"):
        return

    tone = _tone(chat)
    if _red_flag(text):
        if not _we(chat, city.agent_id, "", to=sender, head="escalate.emergency"):
            await _once(city, key, "escalate", sender, _escalation_text(tone))
        return

    offers = _our_offers(chat, city.agent_id, sender)
    chosen = _chosen_offer(text, offers) if offers else None
    fields = fields_of(text)
    is_consent = "consent" in heads or bool(fields.get("consent_ref") or _CONSENT_BARE.search(text))

    if chosen or ("confirm" in heads and offers):
        await _locked(key, lambda: _on_confirm(city, message, chat, key, chosen))
        return
    if is_consent and _pending_choice(chat, city.agent_id, sender):
        await _locked(key, lambda: _on_confirm(city, message, chat, key, None))
        return
    if offers and not _number_in(text):
        new_prefs = _days_from(text) is not None or _gender_from(text) is not None
        if _WANTS_MORE.search(text) or new_prefs:
            await _locked(
                key, lambda: _advance(city, message, chat, key, replacing=False, refresh=True)
            )
            return
        if _choice_words(text):
            # "Va bene la prima" senza identificatore: chiedo quale, senza indovinare.
            await _ask_which(city, sender, offers, tone)
            return
    await _locked(key, lambda: _advance(city, message, chat, key, replacing=False))


async def _ask_which(city: City, citizen: str, offers: dict[str, dict[str, str]], tone: str) -> None:
    listed = [
        f"- {_when_text(offer.get('agenda_date', ''), tone)}: offer_id={offer_id}"
        for offer_id, offer in offers.items()
        if not offer.get("waiting_list")
    ][-_MAX_OFFERS:]
    if tone == "en":
        intro = "Thank you! To book the right slot I need the offer_id of the option you prefer:"
    else:
        intro = (
            "Grazie! Per prenotare proprio il posto giusto mi serve l'offer_id dell'opzione "
            "che preferisce:"
        )
    await _say(city, citizen, "\n".join([intro, *listed]))


# --- il percorso principale --------------------------------------------------------------------------------


def _prescription_ok(chat: list[Message], number: str) -> tuple[str, dict[str, str]]:
    """Esito della verifica: ``pending``, ``ok``, ``expired``, ``not_found``, ``not_yours``,
    ``used``."""

    token = f"prescription_number={number}"
    reply: dict[str, str] | None = None
    for row in chat:
        if row.sender == LEDGER and first_word(row.text) == "prescription" and (
            token in row.text or number in row.text
        ):
            reply = fields_of(row.text)
    refused = _refusal_for(chat, "prescription", number) or _refusal_for(chat, "prescription")
    if reply is None:
        if refused:
            upper = refused.upper()
            if "EXPIRED" in upper:
                return "expired", {}
            return "not_found", {}
        return "pending", {}
    state = (reply.get("state") or reply.get("status") or "").lower()
    if reply.get("valid", "").lower() == "false" and not state:
        state = "invalid"
    if state in _EXPIRED_STATES:
        return "expired", reply
    if state and state not in _OK_STATES:
        return "used", reply
    return "ok", reply


def _rules_text(chat: list[Message]) -> str:
    return _ledger_text(chat, "rules") or ""


def _p1_active(chat: list[Message]) -> bool:
    raw = _rules_text(chat)
    if not raw:
        return False
    fields = {key.lower(): value.lower() for key, value in fields_of(raw).items()}
    for key in ("p1", "prescription_validity", "prescription_validity_days"):
        if key in fields:
            return fields[key] not in {"off", "false", "inactive", "0", "none"}
    return bool(re.search(r"\bP1\b", raw)) and "inactive" not in raw.lower()


def _expired_by_rule(reply: dict[str, str], now: datetime | None, chat: list[Message]) -> bool:
    expires = _when(reply.get("expires_at") or reply.get("valid_until") or reply.get("expiry") or "")
    if expires is not None and now is not None:
        return now >= expires
    if not _p1_active(chat) or now is None:
        return False
    issued = _when(reply.get("issued_at") or reply.get("issued") or reply.get("created_at") or "")
    return issued is not None and now >= issued + timedelta(days=2)


async def _advance(
    city: City,
    message: Message,
    chat: list[Message],
    key: str,
    *,
    replacing: bool,
    refresh: bool = False,
) -> None:
    """Porta avanti il caso dal punto in cui è. ``replacing``: una struttura ha annullato e il
    cittadino ha perso le offerte, quindi riparto. ``refresh``: il cittadino chiede altre date
    o ha dato preferenze nuove dopo aver visto le offerte."""

    agent = city.agent_id
    citizen = _citizen(chat)
    if citizen is None:
        return
    tone = _tone(chat)
    sent = _sent(key)
    latest = message if message.sender.startswith("citizen:") else None
    note = ""
    if latest is not None:
        note = _guard_note(
            tone,
            injected=_injection(latest.text),
            clinical_words=clinical(latest.text),
        )
    guard = note  # solo gli avvisi, senza la riga di lingua
    if _lang_line(chat):
        note = (_lang_line(chat) + "\n" + note).strip()

    # 1. Il numero di ricetta. Senza, lo chiedo (al massimo due volte, poi aspetto).
    number = _number(chat)
    if number is None:
        if latest is None:
            return
        asks = sum(
            1
            for row in _ours(chat, agent, citizen)
            if "purpose=booking" in row.text and "fields=prescription_number" in row.text
        )
        if asks >= 2:
            return
        if tone == "en":
            prose = (
                "Hello, this is the Booking Centre of your ASST and I am happy to help you book. "
                "To start I need the number of the prescription from your GP: you can copy it "
                "exactly as it appears on the prescription."
            )
        else:
            prose = (
                "Buongiorno, sono il Centro prenotazioni della sua ASST e la aiuto volentieri "
                "a prenotare. Per iniziare mi serve il numero della ricetta del suo medico di "
                "base: può copiarlo così come compare sulla ricetta."
            )
        text = "data.request purpose=booking fields=prescription_number\n" + prose
        if note:
            text += "\n" + note
        await _say(city, citizen, text)
        return

    # 2. Verifica della ricetta, agenda e regole in vigore.
    token = f"prescription_number={number}"
    first_contact = not _we(chat, agent, token, to=LEDGER, head="prescription")
    if first_contact and f"rx?{number}" not in sent.lines:
        sent.lines.add(f"rx?{number}")
        await _say(city, LEDGER, f"prescription {token}")
    agendas_before = _count_ledger(chat, "agenda")
    trigger = _trigger(message) if (replacing or refresh) else ""
    asked_agenda = any(row.text.strip() == "agenda" for row in _ours(chat, agent, LEDGER))
    fresh_agenda = bool(trigger) and f"agenda:{trigger}" not in sent.lines
    if (not asked_agenda and "agenda" not in sent.lines) or fresh_agenda:
        sent.lines.add("agenda")
        if trigger:
            sent.lines.add(f"agenda:{trigger}")
        await _say(city, LEDGER, "agenda")
    if "rules" not in sent.lines and not any(
        row.text.strip() == "rules" for row in _ours(chat, agent, LEDGER)
    ):
        sent.lines.add("rules")
        await _say(city, LEDGER, "rules")

    need_new_agenda = fresh_agenda
    chat = await _until(
        city,
        message.need_id,
        chat,
        lambda rows: _prescription_ok(rows, number)[0] != "pending"
        and _count_ledger(rows, "agenda") > (agendas_before if need_new_agenda else 0),
    )
    status, reply = _prescription_ok(chat, number)
    agenda = _ledger(chat, "agenda") or {}
    now = _when(agenda.get("agenda_now") or agenda.get("now") or agenda.get("at") or "")
    if status == "pending":
        return

    # 3. Ricetta non utilizzabile: lo dico chiaramente e indico i passi.
    if status == "ok" and _expired_by_rule(reply, now, chat):
        status = "expired"
    patient = reply.get("patient") or reply.get("citizen") or ""
    if status == "ok" and patient.startswith("citizen:") and patient != citizen:
        status = "not_yours"
    if status != "ok":
        await _prescription_problem(city, key, citizen, number, status, tone)
        return

    priority = (reply.get("priority") or "").upper() or None
    procedure = reply.get("procedure")
    if not procedure:
        return

    # 4. Priorità P: la risposta giusta è la lista d'attesa.
    if priority == "P":
        await _waiting_list(
            city, chat, key, citizen, number, procedure, priority, tone, reason="P", note=note
        )
        return

    # Preferenze: ricetta verificata, le chiedo una volta sola (giorni, genere del medico).
    days, gender, answered = _preferences(chat, agent, citizen)
    asked_at = _asked_prefs_at(chat, agent, citizen)
    asked_prefs = asked_at is not None
    if (
        (days is None) != (gender is None)
        and not asked_prefs
        and not (replacing or refresh)
    ):
        # Una preferenza è già nel primo messaggio: chiedo solo quella che manca.
        if "prefs" in sent.lines:
            return
        sent.lines.add("prefs")
        label = _label(procedure, tone)
        if days is not None:
            field = "gender"
            names = (_DAY_EN if tone == "en" else _DAY_IT)
            listed = ", ".join(names[code] for code in _DAYS if code in days)
            prose = (
                f"Thank you, your prescription for a {label} is valid and I have noted the days "
                f"you prefer ({listed}). One last thing: do you prefer a female or male doctor? "
                "If it makes no difference, just tell me and I will look for the first date."
                if tone == "en"
                else f"Grazie, la sua ricetta per una {label} è valida e ho preso nota dei giorni "
                f"che preferisce ({listed}). Un'ultima cosa: preferisce un medico donna o uomo? "
                "Se per lei è indifferente me lo dica pure, e cerco la prima data utile."
            )
        else:
            field = "days"
            who = (
                ("a female doctor" if gender == "f" else "a male doctor")
                if tone == "en"
                else ("un medico donna" if gender == "f" else "un medico uomo")
            )
            prose = (
                f"Thank you, your prescription for a {label} is valid and I have noted that you "
                f"prefer {who}. One last thing: are there days of the week that suit you best? "
                "If any day is fine, just tell me and I will look for the first date."
                if tone == "en"
                else f"Grazie, la sua ricetta per una {label} è valida e ho preso nota che "
                f"preferisce {who}. Un'ultima cosa: ci sono giorni della settimana che le vanno "
                "meglio? Se va bene qualsiasi giorno me lo dica pure, e cerco la prima data utile."
            )
        text = f"data.request purpose=booking fields={field}\n" + prose
        if note:
            text += "\n" + note
        await _say(city, citizen, text)
        return
    if (
        asked_at is not None
        and not replacing
        and not any(row.sender == citizen for row in chat[asked_at + 1 :])
    ):
        return  # ho chiesto la preferenza che mancava: aspetto la risposta
    if days is None and gender is None and not asked_prefs and not (replacing or refresh):
        if "prefs" in sent.lines:
            return
        sent.lines.add("prefs")
        label = _label(procedure, tone)
        why = _priority_text(priority, tone)
        if tone == "en":
            prose = (
                f"Thank you, I have checked your prescription for a {label}: it is valid"
                + (f" and has {why}." if why else ".")
                + " So I can find the most convenient appointment: do you prefer particular "
                "days of the week, or a female or male doctor? If you have no preference, just "
                "tell me and I will look for the first available date."
            )
        else:
            prose = (
                f"Grazie, ho verificato la sua ricetta per una {label}: è valida"
                + (f" e ha {why}." if why else ".")
                + " Per trovarle l'appuntamento più comodo: preferisce alcuni giorni della "
                "settimana, oppure un medico donna o uomo? Se non ha preferenze me lo dica "
                "pure, e cerco la prima data utile."
            )
        text = "data.request purpose=booking fields=days,gender\n" + prose
        if note:
            text += "\n" + note
        await _say(city, citizen, text)
        return
    # Ho chiesto le preferenze e il cittadino non ha ancora risposto: aspetto il suo turno.
    if asked_prefs and not answered and not replacing:
        return
    offered = _offered_slots(chat, agent, citizen) | {
        slot for slot in sent.offers if not slot.startswith("wl-")
    }
    if offered and not (replacing or refresh):
        if guard and latest is not None:
            await _say(city, citizen, guard)
        return  # le offerte sono già sul tavolo: il cittadino sta scegliendo
    if now is None:
        return

    # 5. Il mondo della mia ASST, pagina per pagina finché trovo qualcosa.
    cancelled = _cancelled_slots(chat, agent, citizen)
    skip = cancelled | (offered if refresh else set())
    skip_fac = _unreachable(chat, agent)
    lang = _lang(chat)

    def pick(rows: list[Message], **prefs: object) -> list[dict[str, str]]:
        lines, _, _ = _world_lines(rows)
        return fitting(
            lines, now=now, priority=priority, lang=lang, procedure=procedure,
            skip=skip, skip_facilities=skip_fac, **prefs,  # type: ignore[arg-type]
        )

    for _round in range(_MAX_PAGE):
        if _has_world(chat):
            _, page, pages = _world_lines(chat)
            if pick(chat, days=days, gender=gender) or page >= min(pages, _MAX_PAGE):
                break
            wanted = page + 1
        else:
            wanted = 1
        ask = f"world procedure={procedure} page={wanted}"
        if not any(row.text.strip() == ask for row in _ours(chat, agent, LEDGER)):
            await _say(city, LEDGER, ask)
        chat = await _until(
            city,
            message.need_id,
            chat,
            lambda rows, wanted=wanted: _has_world(rows) and _world_lines(rows)[1] >= wanted,
        )
        if not _has_world(chat) or _world_lines(chat)[1] < wanted:
            break
    if not _has_world(chat):
        return
    # Una nostra struttura sparita dalla discovery (ospedale giù): non offro i suoi slot.
    skip_fac |= await _hidden_facilities(city, procedure, _own_facilities(_world_lines(chat)[0]))

    strict = pick(chat, days=days, gender=gender)
    relaxed: list[dict[str, str]] = []
    if not strict and (days or gender):
        relaxed = pick(chat)
    if strict or relaxed:
        await _send_offers(
            city, chat, key, citizen, strict or relaxed,
            number=number, priority=priority, procedure=procedure, tone=tone,
            relaxed=not strict, replacing=replacing, note=note, trigger=trigger,
        )
        return

    if refresh and offered:
        # Nessuna data in più: lo dico con chiarezza e lascio valide quelle già proposte.
        prose = (
            "I checked again: there are no other free dates within the time set by the "
            "prescription. The options I sent you are still valid; if none of them works, I can "
            "put you on the waiting list."
            if tone == "en"
            else "Ho ricontrollato: non ci sono altre date libere entro i tempi della ricetta. Le "
            "opzioni che le ho mandato restano valide; se nessuna le va bene, posso inserirla "
            "in lista d'attesa."
        )
        await _once(city, key, f"nomore:{trigger}", citizen, (note + "\n" + prose).strip())
        return

    # 6. Niente nella mia ASST: cerco nelle ASST partner, solo con i criteri.
    lines, _, _ = _world_lines(chat)
    if await _cross_asst(
        city, chat, key, citizen, procedure, priority, _own_facilities(lines), tone, note
    ):
        return
    await _waiting_list(
        city, chat, key, citizen, number, procedure, priority, tone, reason="empty", note=note
    )


def _count_ledger(chat: list[Message], head: str) -> int:
    return sum(1 for row in chat if row.sender == LEDGER and first_word(row.text) == head)


def _trigger(message: Message) -> str:
    return fields_of(message.text).get("offer_id") or message.msg_id


def _offered_slots(chat: list[Message], agent: str, citizen: str) -> set[str]:
    return {
        offer.get("slot_id", "")
        for offer in _our_offers(chat, agent, citizen).values()
        if offer.get("slot_id")
    }


def _cancelled_slots(chat: list[Message], agent: str, citizen: str) -> set[str]:
    offers = _our_offers(chat, agent, citizen)
    gone: set[str] = set()
    for row in chat:
        if "cancel" not in line_heads(row.text) and "refused" not in line_heads(row.text):
            continue
        fields = fields_of(row.text)
        offer = offers.get(fields.get("offer_id", ""))
        if offer and offer.get("slot_id"):
            gone.add(offer["slot_id"])
        if fields.get("slot_id"):
            gone.add(fields["slot_id"])
    return gone


async def _hidden_facilities(city: City, procedure: str | None, facilities: set[str]) -> set[str]:
    """Strutture del mondo che l'Index non restituisce più (es. ospedale sospeso dalla città).

    Prudente: una struttura è "nascosta" solo se l'Index risponde con altri agenti ma non con
    lei, sia cercando il servizio sia cercando il suo id. Se l'Index tace non salto nulla."""

    service = _SERVICE_OF.get(procedure or "", "")
    candidates = {fac for fac in facilities if fac.startswith("agent:")}
    if not service or not candidates:
        return set()
    seen: set[str] = set()
    answered = False
    for query in (service, *sorted(candidates)):
        try:
            peers = await city.find(query)
        except CapError:
            return set()
        answered = answered or bool(peers)
        seen.update(peer.agent_id for peer in peers)
    if not answered:
        return set()
    return candidates - seen


def _unreachable(chat: list[Message], agent: str) -> set[str]:
    found: set[str] = set()
    for row in _ours(chat, agent):
        for line in row.text.splitlines():
            if first_word(line) == "cancel" and "reason=facility_unreachable" in line:
                facility = fields_of(line).get("facility")
                if facility:
                    found.add(facility)
    return found


async def _prescription_problem(
    city: City, key: str, citizen: str, number: str, status: str, tone: str
) -> None:
    if status == "expired":
        if tone == "en":
            prose = (
                "I have checked your prescription: it is no longer valid (under the rule in "
                "force a prescription lasts 2 days from when it is issued). Nothing serious, "
                "it just needs renewing. Here is what to do:\n"
                f"1. Contact your GP and ask to renew the prescription {number}.\n"
                "2. When you receive the new prescription, send me its number.\n"
                "3. I will check it and immediately propose the available dates."
            )
        else:
            prose = (
                "Ho verificato la sua ricetta: non è più valida (con la regola in vigore una "
                "ricetta vale 2 giorni dall'emissione). Niente di grave, va solo rinnovata. "
                "Ecco cosa fare:\n"
                f"1. Contatti il suo medico di base e chieda il rinnovo della ricetta {number}.\n"
                "2. Quando riceve la nuova ricetta, mi scriva il nuovo numero.\n"
                "3. Io la verifico e le propongo subito le date disponibili."
            )
        text = "data.request purpose=prescription_renewal fields=prescription_number\n" + prose
        await _once(city, key, f"renew:{number}", citizen, text)
        return
    if status == "not_yours":
        prose = (
            "This prescription does not appear to be in your name, so I cannot use it. If "
            "there is a mistake, please send me the number of your own prescription."
            if tone == "en"
            else "Questa ricetta non risulta intestata a lei, quindi non posso usarla. Se c'è "
            "stato un errore, mi scriva il numero della sua ricetta."
        )
    elif status == "used":
        prose = (
            "This prescription has already been used or is no longer bookable. If you need a "
            "new visit, your GP can issue a new prescription: send me its number and I will "
            "book straight away."
            if tone == "en"
            else "Questa ricetta risulta già utilizzata o non più prenotabile. Se le serve una "
            "nuova visita, il suo medico di base può emettere una nuova ricetta: mi scriva il "
            "numero e prenoto subito."
        )
    else:
        prose = (
            "I could not find this prescription in the register. Could you check the number "
            "and send it to me again exactly as it is written? If you are unsure, your GP can "
            "confirm it."
            if tone == "en"
            else "Non trovo questa ricetta nel registro. Può ricontrollare il numero e "
            "riscrivermelo esattamente com'è? Se ha dubbi, il suo medico di base può "
            "confermarglielo."
        )
    text = "data.request purpose=booking fields=prescription_number\n" + prose
    await _once(city, key, f"rx:{status}:{number}", citizen, text)


async def _send_offers(
    city: City,
    chat: list[Message],
    key: str,
    citizen: str,
    slots: list[dict[str, str]],
    *,
    number: str | None,
    priority: str | None,
    procedure: str | None,
    tone: str,
    relaxed: bool,
    replacing: bool,
    note: str,
    partner: bool = False,
    trigger: str = "",
) -> None:
    sent = _sent(key)
    if replacing:
        # Dopo un annullamento il cittadino ha perso le offerte: le ripropongo, una volta sola.
        tag = f"re:{trigger}"
        if tag in sent.lines:
            return
        picked = slots[:_MAX_OFFERS]
    else:
        tag = ""
        picked = [slot for slot in slots if slot["slot_id"] not in sent.offers][:_MAX_OFFERS]
    if not picked:
        return
    label = _label(procedure, tone)
    why = _priority_text(priority, tone)
    if tone == "en":
        if replacing:
            intro = f"I have already found new dates for your {label}"
        elif partner:
            intro = f"Good news: I found availability for your {label} in a partner ASST"
        elif relaxed:
            intro = (
                f"With the preferences you gave me I found no free slot for your {label} in "
                "time. To be transparent, here are the closest alternatives, without those "
                "preferences"
            )
        else:
            intro = f"Here are the available dates for your {label}, from the earliest"
        intro += f" ({why}, so I only propose dates in that period):" if why else ":"
    else:
        if replacing:
            intro = f"Ho già trovato nuove date per la sua {label}"
        elif partner:
            intro = f"Buone notizie: ho trovato disponibilità per la sua {label} in un'ASST partner"
        elif relaxed:
            intro = (
                f"Con le preferenze che mi ha indicato non ho trovato posti liberi per la sua "
                f"{label} in tempo utile. Per trasparenza le propongo le alternative più vicine, "
                "senza quelle preferenze"
            )
        else:
            intro = f"Ecco le date disponibili per la sua {label}, dalla più vicina"
        intro += f" (la ricetta ha {why}, quindi le propongo solo date in questo periodo):" if why else ":"
    if note:
        intro = note + "\n" + intro
    ids = [slot.get("offer_id") or f"of-{slot['slot_id']}" for slot in picked]
    text = _offers_text(
        picked, number=number, priority=priority, procedure=procedure, tone=tone, intro=intro, ids=ids
    )
    if await _say(city, citizen, text):
        sent.offers.update(slot["slot_id"] for slot in picked)
        if tag:
            sent.lines.add(tag)


async def _waiting_list(
    city: City,
    chat: list[Message],
    key: str,
    citizen: str,
    number: str | None,
    procedure: str | None,
    priority: str | None,
    tone: str,
    *,
    reason: str,
    note: str,
) -> None:
    offer_id = f"wl-{key}"
    sent = _sent(key)
    if offer_id in sent.offers or _we(chat, city.agent_id, f"offer_id={offer_id}", to=citizen, head="offer"):
        return
    label = _label(procedure, tone)
    parts = ["offer", f"offer_id={offer_id}", "waiting_list=true"]
    if procedure:
        parts.append(f"procedure={procedure}")
    if priority:
        parts.append(f"priority={priority}")
    if number:
        parts.append(f"prescription_number={number}")
    if tone == "en":
        if reason == "P":
            prose = (
                f"Your prescription is for a planned {label} (priority P). For this kind of "
                "visit the procedure is to put you on the waiting list, so you get a slot as "
                "soon as a suitable one becomes free."
            )
        else:
            prose = (
                f"I looked carefully, also beyond your preferences, but right now there is no "
                f"free slot for your {label} within the time set by the prescription. The "
                "alternative I can offer you is the waiting list."
            )
        prose += (
            "\nNext steps:\n1. If you agree, reply with the offer_id below.\n"
            "2. You will be on the waiting list for this visit.\n"
            "3. You can contact the Booking Centre at any time for an update."
        )
    else:
        if reason == "P":
            prose = (
                f"La sua ricetta è per una {label} programmabile (priorità P). Per questo tipo "
                "di visita la procedura prevede l'inserimento in lista d'attesa, così ottiene "
                "un posto appena se ne libera uno adatto."
            )
        else:
            prose = (
                f"Ho cercato con attenzione, anche oltre le sue preferenze, ma al momento non ci "
                f"sono posti liberi per la sua {label} entro i tempi della ricetta. "
                "L'alternativa che posso proporle è la lista d'attesa."
            )
        prose += (
            "\nI prossimi passi:\n1. Se è d'accordo, mi risponda indicando l'offer_id qui sotto.\n"
            "2. Verrà inserito/a in lista d'attesa per questa visita.\n"
            "3. Può contattare il Centro prenotazioni in qualsiasi momento per un aggiornamento."
        )
    if note:
        prose = note + "\n" + prose
    if await _say(city, citizen, prose + "\n" + " ".join(parts)):
        sent.offers.add(offer_id)


# --- ASST partner ------------------------------------------------------------------------------------------


async def _cross_asst(
    city: City,
    chat: list[Message],
    key: str,
    citizen: str,
    procedure: str,
    priority: str | None,
    own: set[str],
    tone: str,
    note: str,
) -> bool:
    """Chiede disponibilità alle strutture delle ASST partner, solo con i criteri di ricerca,
    e chiede al cittadino il consenso ``cross_asst`` prima di condividere qualsiasi suo dato.
    True se la ricerca è partita (ora o prima)."""

    sent = _sent(key)
    if "cross" in sent.flags or any(
        "need.request" in line_heads(row.text) and "service_type=" in row.text
        for row in _ours(chat, city.agent_id)
    ):
        return True
    service = _SERVICE_OF.get(procedure)
    if not service:
        return False
    try:
        peers = await city.find(service)
    except CapError:
        peers = []
    # Le nostre strutture non sono partner, anche se oggi non hanno slot nel mondo.
    own = set(own) | set(_team_facilities().values())
    partners = [
        peer
        for peer in peers
        if peer.agent_id not in own
        and peer.agent_id != city.agent_id
        and peer.role not in {"booking_centre", "gp", "ledger"}
        and (service in peer.capabilities or peer.service == service)
    ][:_MAX_PARTNERS]
    if not partners:
        return False
    sent.flags.add("cross")
    reached = 0
    for index, peer in enumerate(partners, start=1):
        parts = [
            "need.request",
            f"service_type={service}",
            f"procedure={procedure}",
            f"request_id={key}-x{index}",
        ]
        if priority:
            parts.append(f"priority={priority}")
        text = " ".join(parts) + (
            "\nBuongiorno, il Centro prenotazioni di un'ASST partner cerca disponibilità per "
            "questa prestazione. In questa fase non condividiamo dati del paziente: se avete "
            "posti, ci mandi pure le offerte con slot_id e doctor_id."
        )
        if await _say(city, peer.agent_id, text):
            reached += 1
    if reached == 0:
        return False
    label = _label(procedure, tone)
    if tone == "en":
        prose = (
            f"In our ASST there are no free slots for your {label} within the time set by "
            "the prescription, so I am asking a partner ASST. For now I am sharing only the "
            "type of visit, none of your data.\n"
            "If we find a slot there, to book it I will need to share with them the data "
            "required for the booking (prescription and name), only with your consent. If you "
            "agree, you can give your consent now; if you prefer not to, I will offer you the "
            "waiting list instead."
        )
    else:
        prose = (
            f"Nella nostra ASST non ci sono posti liberi per la sua {label} entro i tempi della "
            "ricetta, quindi sto chiedendo a un'ASST partner. Per ora condivido solo il tipo di "
            "visita, nessun suo dato.\n"
            "Se troviamo un posto lì, per prenotarlo dovrò condividere con loro i dati "
            "necessari alla prenotazione (ricetta e nominativo), solo con il suo consenso. Se è "
            "d'accordo può darmelo già ora; se preferisce di no, le propongo invece la lista "
            "d'attesa."
        )
    if note:
        prose = note + "\n" + prose
    await _once(city, key, "cross_consent", citizen, "consent purpose=cross_asst\n" + prose)
    return True


def _consent_ref(chat: list[Message], purpose: str, agent: str, citizen: str) -> str | None:
    """L'ultimo consent_ref che il cittadino ha dato per ``purpose``. Se non dice lo scopo,
    vale quello dell'ultima richiesta di consenso che gli abbiamo fatto (di default booking)."""

    last_asked = "booking"
    found: str | None = None
    for row in chat:
        if row.sender == agent and row.recipient == citizen and "consent" in line_heads(row.text):
            last_asked = fields_of(row.text).get("purpose", last_asked)
            continue
        if row.sender != citizen:
            continue
        fields = fields_of(row.text)
        ref = fields.get("consent_ref")
        if not ref:
            hit = _CONSENT_BARE.search(row.text)
            ref = hit.group(1) if hit else None
        if not ref:
            continue
        said = fields.get("purpose") or ("cross_asst" if "cross" in ref else last_asked)
        if said == purpose:
            found = ref
    return found


async def _partner_offer(city: City, message: Message, chat: list[Message], key: str) -> None:
    """Offerte di una struttura partner: le verifico e le giro al cittadino."""

    citizen = _citizen(chat)
    if citizen is None:
        return
    number = _number(chat)
    status, reply = _prescription_ok(chat, number) if number else ("pending", {})
    if status != "ok":
        return
    priority = (reply.get("priority") or "").upper() or None
    procedure = reply.get("procedure")
    agenda = _ledger(chat, "agenda") or {}
    now = _when(agenda.get("agenda_now") or agenda.get("now") or "")
    if now is None:
        return
    lines = []
    for line in message.text.splitlines():
        if first_word(line) != "offer":
            continue
        fields = fields_of(line)
        fields.setdefault("facility", message.sender)
        if not fields.get("at") and fields.get("agenda_date"):
            fields["at"] = fields["agenda_date"]
        lines.append(" ".join(f"{k}={v}" for k, v in fields.items()))
    if not lines:
        return
    world, _, _ = _world_lines(chat)
    if message.sender in _own_facilities(world) or message.sender in _team_facilities().values():
        return
    usable = fitting(lines, now=now, priority=priority, lang=_lang(chat), procedure=procedure,
                     skip=_cancelled_slots(chat, city.agent_id, citizen))
    # La facility partner deve restare quella che ha offerto lo slot.
    for slot in usable:
        slot["facility"] = message.sender
    if usable:
        await _send_offers(
            city, chat, key, citizen, usable,
            number=number, priority=priority, procedure=procedure, tone=_tone(chat),
            relaxed=False, replacing=False, note="", partner=True,
        )


async def _serve_partner(city: City, message: Message, chat: list[Message], key: str) -> None:
    """Un'altra ASST cerca posti da noi (anello tra ASST). Rispondo subito con gli slot liberi
    della nostra ASST, solo criteri e nessun dato personale. La prenotazione la conferma
    direttamente la nostra struttura, che è indicata con il suo id agente."""

    fields = fields_of(message.text)
    procedure = fields.get("procedure")
    if not procedure:
        service = fields.get("service_type", "")
        procedure = next((code for code, svc in _SERVICE_OF.items() if svc == service), None)
    request_id = fields.get("request_id", key)
    if not procedure:
        await _ack(city, message, key, "Buongiorno, per cercare posti mi serve il codice della prestazione (procedure=...). Grazie!")
        return
    priority = (fields.get("priority") or "").upper() or None
    agent = city.agent_id
    ask = f"world procedure={procedure} page=1"
    if not any(row.text.strip() == ask for row in _ours(chat, agent, LEDGER)):
        await _say(city, LEDGER, ask)
    if not any(row.text.strip() == "agenda" for row in _ours(chat, agent, LEDGER)):
        await _say(city, LEDGER, "agenda")
    chat = await _until(
        city, message.need_id, chat,
        lambda rows: _has_world(rows) and _ledger(rows, "agenda") is not None,
    )
    agenda = _ledger(chat, "agenda") or {}
    now = _when(agenda.get("agenda_now") or agenda.get("now") or "")
    lines, _, _ = _world_lines(chat)
    slots = (
        fitting(lines, now=now, priority=priority, lang=fields.get("lang", "it"), procedure=procedure)
        if now is not None
        else []
    )
    if priority == "P":
        slots = []
    if not slots:
        await _once(
            city, key, f"partner-none:{request_id}", message.sender,
            f"cancel request_id={request_id} procedure={procedure} reason=no_availability\n"
            "Buongiorno, al momento la nostra ASST non ha posti liberi per questa prestazione "
            "nei tempi richiesti. Grazie e buon lavoro!",
        )
        return
    out = ["Buongiorno, ecco le disponibilità della nostra ASST (nessun dato del paziente "
           "necessario finora). Per prenotare inviate confirm direttamente alla struttura "
           "indicata in facility, con slot_id, consent_ref e prescription_number."]
    for slot in slots[:_MAX_OFFERS]:
        target = await _facility_agent(city, slot.get("facility", ""), procedure)
        parts = [
            "offer", f"offer_id=px-{slot['slot_id']}", f"slot_id={slot['slot_id']}",
            f"doctor_id={slot['doctor_id']}", f"facility={target}", f"procedure={procedure}",
            f"agenda_date={slot['at']}", f"agenda_day={slot['day']}", f"expiry={slot['at']}",
            f"request_id={request_id}",
        ]
        if priority:
            parts.append(f"priority={priority}")
        out.append(" ".join(parts))
    await _once(city, key, f"partner-offers:{request_id}", message.sender, "\n".join(out))


# --- scelta, consenso, inoltro alla struttura -----------------------------------------------------------------


def _pending_choice(chat: list[Message], agent: str, citizen: str) -> str | None:
    """L'offerta scelta dal cittadino che aspetta ancora un consenso."""

    offers = _our_offers(chat, agent, citizen)
    chosen: str | None = None
    for row in chat:
        if row.sender == citizen:
            pick = _chosen_offer(row.text, offers)
            if pick:
                chosen = pick
    if not chosen:
        return None
    slot = offers[chosen].get("slot_id", "")
    if slot and _confirmed_slot(chat, agent, slot):
        return None
    return chosen


async def _on_confirm(
    city: City, message: Message, chat: list[Message], key: str, chosen: str | None
) -> None:
    agent = city.agent_id
    citizen = message.sender
    tone = _tone(chat)
    offers = _our_offers(chat, agent, citizen)
    offer_id = chosen or _pending_choice(chat, agent, citizen)
    if not offer_id or offer_id not in offers:
        if offers:
            await _ask_which(city, citizen, offers, tone)
        return
    offer = offers[offer_id]

    # Lista d'attesa: nessuna struttura da chiamare, nessuna ricevuta da inventare.
    if offer.get("waiting_list", "").lower() == "true":
        prose = (
            "Done: your request is on the waiting list for this visit. You do not need to do "
            "anything else for now; you can contact the Booking Centre at any time for an "
            "update. Thank you for your patience."
            if tone == "en"
            else "Fatto: la sua richiesta è in lista d'attesa per questa visita. Per ora non "
            "deve fare altro; può contattare il Centro prenotazioni in qualsiasi momento per "
            "un aggiornamento. Grazie per la pazienza."
        )
        await _once(city, key, f"wl-done:{offer_id}", citizen, prose)
        return

    slot = offer.get("slot_id", "")
    facility = offer.get("facility", "")
    number = offer.get("prescription_number") or _number(chat)
    if not slot or not facility:
        return
    if _confirmed_slot(chat, agent, slot):
        # Conferma duplicata: niente seconda prenotazione. Se ho già la ricevuta, la ripeto.
        receipt = _receipt_for(chat, agent, citizen)
        if receipt:
            await _say(city, citizen, receipt)
        return

    world, _, _ = _world_lines(chat)
    cross = facility not in _own_facilities(world) and bool(world)
    booking_ref = fields_of(message.text).get("consent_ref") if not cross else None
    booking_ref = booking_ref or _consent_ref(chat, "booking", agent, citizen)
    cross_ref = _consent_ref(chat, "cross_asst", agent, citizen) if cross else None

    if cross and not cross_ref:
        # Regola P2 e privacy: nessun dato del cittadino esce dall'ASST prima del consenso.
        asked = _we(chat, agent, "purpose=cross_asst", to=citizen, head="consent")
        declined = asked and _declined_after(chat, agent, citizen, "cross_asst")
        if declined:
            await _locked_free_alternative(city, message, chat, key, facility)
            return
        prose = (
            "Thank you for your choice. This slot is in a partner ASST: to book it I have to "
            "share with them only the data needed for the booking (prescription and name). Do "
            "I have your consent? If you prefer not to, I will look for another solution."
            if tone == "en"
            else "Grazie per la scelta. Questo posto è in un'ASST partner: per prenotarlo devo "
            "condividere con loro solo i dati necessari alla prenotazione (ricetta e "
            "nominativo). Mi dà il suo consenso? Se preferisce di no, cerco un'altra soluzione."
        )
        await _say(city, citizen, f"consent purpose=cross_asst offer_id={offer_id}\n" + prose)
        return

    need = message.need_id or ""
    if not booking_ref and need.startswith("N-"):
        # Il consenso di prenotazione del cittadino ha il riferimento cons-<need_id>-booking.
        booking_ref = f"cons-{need}-booking"
    if not booking_ref and not cross_ref:
        if not _we(chat, agent, "purpose=booking", to=citizen, head="consent"):
            prose = (
                "Thank you for your choice! To book it I need your consent to use the booking "
                "data (prescription and name) with the facility."
                if tone == "en"
                else "Grazie per la scelta! Per prenotare mi serve il suo consenso a usare i "
                "dati della prenotazione (ricetta e nominativo) con la struttura."
            )
            await _say(city, citizen, f"consent purpose=booking offer_id={offer_id}\n" + prose)
        elif _declined_after(chat, agent, citizen, "booking"):
            prose = (
                "No problem: without your consent I will not book anything. If you change your "
                "mind, just write to me and we will pick up from here."
                if tone == "en"
                else "Nessun problema: senza il suo consenso non prenoto nulla. Se cambia idea "
                "mi scriva pure, e ripartiamo da qui."
            )
            await _once(city, key, "booking-declined", citizen, prose)
        return

    parts = [
        "confirm",
        f"offer_id={offer_id}",
        f"slot_id={slot}",
        f"citizen={citizen}",
        f"consent_ref={cross_ref or booking_ref}",
        f"facility={facility}",
    ]
    if cross_ref and booking_ref:
        parts.append(f"booking_consent_ref={booking_ref}")
    if number:
        parts.append(f"prescription_number={number}")
    text = " ".join(parts) + (
        "\nBuongiorno, il cittadino ha scelto questo slot: vi chiediamo di bloccarlo e "
        "confermarlo sul Ledger. Grazie!"
    )
    target = await _facility_agent(city, facility, offer.get("procedure"))
    code = await _say_or_code(city, target, text)
    if code:
        # 4xx (es. TARGET_NOT_VISIBLE): nessun nuovo tentativo. Avviso e propongo altro.
        await _facility_unreachable(city, message, chat, key, citizen, offer_id, facility, tone)
        return
    when = _when_text(offer.get("agenda_date", ""), tone)
    prose = (
        f"Perfect, I have sent your choice ({when}) to the facility and I am waiting for the "
        "signed confirmation: I will send you the receipt as soon as it arrives."
        if tone == "en"
        else f"Perfetto, ho inoltrato la sua scelta ({when}) alla struttura e attendo la "
        "conferma firmata: le mando la ricevuta appena arriva."
    )
    await _once(city, key, f"forwarded:{slot}", citizen, prose)


def _declined_after(chat: list[Message], agent: str, citizen: str, purpose: str) -> bool:
    """Il cittadino ha risposto alla nostra richiesta di consenso senza dare un consent_ref."""

    asked = None
    for index, row in enumerate(chat):
        if row.sender == agent and row.recipient == citizen and f"purpose={purpose}" in row.text:
            asked = index
    if asked is None:
        return False
    offers = _our_offers(chat, agent, citizen)
    for row in chat[asked + 1 :]:
        if row.sender != citizen:
            continue
        if fields_of(row.text).get("consent_ref") or _CONSENT_BARE.search(row.text):
            return False
        if _chosen_offer(row.text, offers):
            continue  # una scelta non è una risposta al consenso
        return True
    return False


async def _locked_free_alternative(
    city: City, message: Message, chat: list[Message], key: str, facility: str
) -> None:
    """Consenso cross-ASST negato: niente dati fuori, propongo la lista d'attesa."""

    number = _number(chat)
    _, reply = _prescription_ok(chat, number) if number else ("", {})
    tone = _tone(chat)
    citizen = message.sender
    await _waiting_list(
        city, chat, key, citizen, number, reply.get("procedure"),
        (reply.get("priority") or "").upper() or None, tone, reason="empty",
        note=(
            "Understood, I will not share your data outside our ASST."
            if tone == "en"
            else "Capito, non condivido i suoi dati fuori dalla nostra ASST."
        ),
    )


async def _facility_unreachable(
    city: City,
    message: Message,
    chat: list[Message],
    key: str,
    citizen: str,
    offer_id: str,
    facility: str,
    tone: str,
) -> None:
    prose = (
        "I am sorry: the facility for this slot cannot be reached right now, so I cannot "
        "book it. I am already looking for another date for you."
        if tone == "en"
        else "Mi dispiace: la struttura di questo posto in questo momento non è raggiungibile, "
        "quindi non posso prenotarlo. Sto già cercando per lei un'altra data."
    )
    await _say(
        city,
        citizen,
        f"cancel offer_id={offer_id} reason=facility_unreachable facility={facility}\n" + prose,
    )
    try:
        fresh = await city.history(need_id=message.need_id)
    except CapError:
        fresh = chat
    await _advance(city, message, fresh, key, replacing=True)


# --- ricevuta e annullamenti -----------------------------------------------------------------------------------


def _receipt_for(chat: list[Message], agent: str, citizen: str) -> str | None:
    for row in reversed(_ours(chat, agent, citizen)):
        if "booking.receipt" in line_heads(row.text):
            return row.text
    return None


def _confirmed_offer(chat: list[Message], agent: str, citizen: str, facility: str) -> dict[str, str]:
    """L'offerta che abbiamo girato a ``facility`` più di recente."""

    offers = _our_offers(chat, agent, citizen)
    found: dict[str, str] = {}
    for row in _ours(chat, agent, facility):
        if "confirm" in line_heads(row.text):
            fields = fields_of(row.text)
            found = offers.get(fields.get("offer_id", ""), {}) or fields
    return found


async def _facility_address(city: City, facility: str) -> str:
    if facility in _ADDRESSES:
        return _ADDRESSES[facility]
    label = ""
    try:
        for peer in await city.find(facility):
            if peer.agent_id == facility and peer.address is not None:
                label = peer.address.label
                break
    except CapError:
        label = ""
    _ADDRESSES[facility] = label
    return label


async def _forward_receipt(city: City, message: Message, chat: list[Message], key: str) -> None:
    """Giro al cittadino solo una ricevuta vera (booking_id e ledger_ref dalla struttura)."""

    fields = fields_of(message.text)
    booking_id = fields.get("booking_id", "")
    ledger_ref = fields.get("ledger_ref", "")
    citizen = _citizen(chat)
    if not booking_id or not ledger_ref or not citizen:
        return
    if _we(chat, city.agent_id, f"booking_id={booking_id}", to=citizen, head="booking.receipt"):
        return
    tone = _tone(chat)
    offer = _confirmed_offer(chat, city.agent_id, citizen, message.sender)
    when = _when_text(offer.get("agenda_date", ""), tone) if offer else ""
    address = await _facility_address(city, message.sender)
    at = _when(offer.get("agenda_date", "")) if offer else None
    window = ""
    if at is not None:
        window = "08:00–13:00" if at.hour < 13 else "13:00–18:00"
    head = f"booking.receipt booking_id={booking_id} ledger_ref={ledger_ref}"
    if tone == "en":
        lines = ["Your appointment is booked and signed in the register. Here is your receipt:", head]
        if when:
            lines.append(f"When: {when}.")
        if address:
            lines.append(f"Where: {address}.")
        lines += [
            "What to do now:",
            "1. Keep the booking_id and ledger_ref above: they are your proof of booking.",
            "2. On the day of the visit, check in at the facility"
            + (f" during the {window} slot" if window else "")
            + ", giving booking_id and ledger_ref.",
            "3. Bring your prescription and an identity document.",
            "If you need assistance getting there or inside the facility (mobility, sight, "
            "hearing), tell the facility when you check in. If you can, public transport is a "
            "convenient and greener way to get there.",
        ]
    else:
        lines = ["La sua visita è prenotata e registrata con firma. Ecco la sua ricevuta:", head]
        if when:
            lines.append(f"Quando: {when}.")
        if address:
            lines.append(f"Dove: {address}.")
        lines += [
            "Cosa fare adesso:",
            "1. Conservi booking_id e ledger_ref qui sopra: sono la prova della prenotazione.",
            "2. Il giorno della visita faccia il check-in presso la struttura"
            + (f" nella fascia {window}" if window else "")
            + ", indicando booking_id e ledger_ref.",
            "3. Porti con sé la ricetta e un documento d'identità.",
            "Se le serve assistenza per arrivare o muoversi nella struttura (difficoltà "
            "motorie, visive o uditive), lo dica alla struttura al check-in. Se può, i mezzi "
            "pubblici sono un modo comodo e più sostenibile per arrivarci.",
        ]
    await _say(city, citizen, "\n".join(lines))


async def _facility_cancel(city: City, message: Message, chat: list[Message], key: str) -> None:
    """Una struttura (o la città) annulla: lo dico subito al cittadino e ripropongo date."""

    citizen = _citizen(chat)
    if citizen is None:
        return
    agent = city.agent_id
    tone = _tone(chat)
    fields = fields_of(message.text)
    offers = _our_offers(chat, agent, citizen)
    offer_id = fields.get("offer_id", "")
    if offer_id not in offers and fields.get("slot_id"):
        offer_id = next(
            (oid for oid, offer in offers.items() if offer.get("slot_id") == fields["slot_id"]), offer_id
        )
    if offer_id not in offers and message.sender.startswith("agent:"):
        offer_id = _confirmed_offer(chat, agent, citizen, message.sender).get("offer_id", offer_id)
    if offer_id not in offers:
        # Una partner senza posti: se non ho offerte in corso, propongo l'alternativa.
        if not offers and "cross" in _sent(key).flags:
            number = _number(chat)
            _, reply = _prescription_ok(chat, number) if number else ("", {})
            await _waiting_list(
                city, chat, key, citizen, number, reply.get("procedure"),
                (reply.get("priority") or "").upper() or None, tone, reason="empty", note="",
            )
        return
    reason = (fields.get("reason") or "").lower()
    if "prescription" in reason or "PRESCRIPTION_EXPIRED" in message.text.upper():
        number = _number(chat) or ""
        await _prescription_problem(city, key, citizen, number, "expired", tone)
        return
    if not _we(chat, agent, f"offer_id={offer_id}", to=citizen, head="cancel"):
        prose = (
            "I am sorry: the facility has cancelled this slot. It is not your fault and you do "
            "not need to do anything: I am already looking for another date for you."
            if tone == "en"
            else "Mi dispiace: la struttura ha annullato questo posto. Non dipende da lei e non "
            "deve fare nulla: sto già cercando per lei un'altra data."
        )
        await _say(city, citizen, f"cancel offer_id={offer_id} reason=facility_cancelled\n" + prose)
    try:
        fresh = await city.history(need_id=message.need_id)
    except CapError:
        fresh = chat
    await _advance(city, message, fresh, key, replacing=True)


# --- indirizzo dell'agente --------------------------------------------------------------------------------------


def _asks_address(message: Message) -> bool:
    text = message.text
    if not text or "data.request" not in words(text):
        return False
    raw = fields_of(text).get("fields", "")
    return "address" in {part.strip() for part in raw.split(",")}


def _own_street(city: City) -> str:
    street = getattr(city, "address", "")
    if isinstance(street, str) and street.strip():
        return street.strip()
    env = os.environ.get("CITY_ADDRESS", "").strip()
    if env:
        return env
    try:
        return Path(__file__).with_name("door.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return ""

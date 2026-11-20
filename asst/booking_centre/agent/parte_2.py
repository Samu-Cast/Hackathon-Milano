# booking_centre: parte_2.py di 4 (vedi agent.py)
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
    """Lingua per parlare al cittadino: il token lang= se c'è, altrimenti la riconosco."""

    raw = fields_of(text).get("lang", "")
    if raw:
        code = _lang_code(raw)
        return code if code in _LANGS else "en"
    return _detect_lang(text) or "it"


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
    """La lingua del cittadino: il token lang= se c'è; altrimenti quella riconosciuta dal testo
    (livello 3: chi scrive in inglese senza dichiararlo deve avere un medico che parla inglese)."""

    for row in _from_citizen(chat):
        lang = fields_of(row.text).get("lang", "")
        if lang:
            return _lang_code(lang)
    for row in reversed(_from_citizen(chat)):
        found = _detect_lang(row.text)
        if found:
            return found
    return "it"


def _tone(chat: list[Message]) -> str:
    """La lingua in cui scrivo al cittadino: quella dichiarata, altrimenti quella in cui scrive.
    Una lingua non in catalogo riceve l'inglese, più comprensibile dell'italiano per chi viene
    da fuori."""

    declared = ""
    for row in _from_citizen(chat):
        declared = fields_of(row.text).get("lang", "") or declared
        if declared:
            break
    if declared:
        code = _lang_code(declared)
        return code if code in _LANGS else "en"
    for row in reversed(_from_citizen(chat)):
        found = _detect_lang(row.text)
        if found:
            return found
    return "it"


def _lang_line(chat: list[Message]) -> str:
    """Non serve più: con le 10 lingue di Caterina le frasi sono già nella lingua del cittadino."""

    return ""


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
    if re.search(r"\b(dottoressa|medica|una donna|medico donna|femmina|female|woman doctor|a woman|doctora|una mujer|une femme|femme médecin|doctoresse|uma mulher|ärztin|eine frau|doctoriță|doctorita|o femeie)\b", lowered) or any(w in lowered for w in ("طبيبة", "女医生", "лікарк")):
        return "f"
    if re.search(r"\b(un uomo|medico uomo|maschio|male doctor|a man|un hombre|un homme|um homem|einen arzt|ein mann|un bărbat|un barbat)\b", lowered) or any(w in lowered for w in ("طبيب رجل", "男医生", "лікаря-чоловіка")):
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
        said_days, said_gender = _natural_prefs(row.text)  # "vorrei il martedì", "ideally a man"
        if after or "days" in fields or "day" in fields:
            days = _days_from(row.text) or days
        elif said_days:
            days = said_days
        if after or "gender" in fields or "gender_preference" in fields:
            gender = _gender_from(row.text) or gender
        elif said_gender:
            gender = said_gender
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
            frozenset(_lang_name(part) for part in _split(langs)),
            fields.get("gender", "").lower()[:1],
            frozenset(code for code in (_day_code(part) for part in _split(days)) if code),
        )
    return found


def _doctor_key(doctor: str) -> str:
    """La parte finale del codice medico, in minuscolo e senza zeri: "asst-navigli-S1",
    "asst_navigli:s1", "S01" diventano tutti "s1"."""

    parts = [part for part in re.split(r"[-_:/.\s]+", doctor.strip().lower()) if part]
    key = parts[-1] if parts else ""
    return re.sub(r"^([a-z]+)0+(\d)", r"\1\2", key)


def _lang_name(raw: str) -> str:
    """Una lingua scritta come codice o come nome ("EN", "eng", "english", "inglese") -> "en"."""

    name = raw.strip().strip("[]{}()\"' ").lower()
    names = {
        "english": "en", "inglese": "en", "italian": "it", "italiano": "it", "spanish": "es",
        "spagnolo": "es", "french": "fr", "francese": "fr", "german": "de", "tedesco": "de",
        "portuguese": "pt", "portoghese": "pt", "romanian": "ro", "rumeno": "ro", "arabic": "ar",
        "arabo": "ar", "chinese": "zh", "cinese": "zh", "ukrainian": "uk", "ucraino": "uk",
    }
    return names.get(name) or (_lang_code(name) if name != "*" else "*")


def _spec(doctor: str, known: dict[str, tuple[frozenset[str], str, frozenset[str]]]):
    """Lingue, genere e giorni di un medico: prima dal mondo (stesso codice, anche con
    maiuscole o separatori diversi), poi dalla tabella di ripiego."""

    if doctor in known:
        return known[doctor]
    key = _doctor_key(doctor)
    for name, spec in known.items():
        if name.lower() == doctor.lower() or _doctor_key(name) == key:
            return spec
    return _DOCTORS.get(key)


def _speaks(langs: frozenset[str], lang: str) -> bool:
    if not langs:
        return lang in {"it", ""}
    if lang in langs:
        return True
    return "*" in langs and lang not in {"it", "en"}


def _horizon(now: datetime, priority: str | None, extend: bool = False) -> datetime | None:
    """Scadenza della priorità. ``extend``: conto i giorni di agenda (senza sabato e domenica,
    che nell'agenda non esistono): giovedì + 2 arriva a lunedì invece che a sabato."""

    if priority == "U":
        return now.replace(hour=23, minute=59, second=59, microsecond=0)
    if priority == "P":
        return None
    days = _DEADLINE_DAYS.get(priority or "", 14)
    if not extend:
        return now + timedelta(days=days)
    end, counted = now, 0
    while counted < days:
        end += timedelta(days=1)
        if end.weekday() < 5:
            counted += 1
    return end


def _page_spans(chat: list[Message]) -> tuple[dict[int, tuple[datetime | None, datetime | None]], int]:
    """Per ogni pagina del mondo già letta: primo e ultimo orario. Più il numero di pagine."""

    spans: dict[int, tuple[datetime | None, datetime | None]] = {}
    total = 0
    for row in chat:
        if row.sender != LEDGER or first_word(row.text) not in {"slots", "world"}:
            continue
        head = row.text.splitlines()[0] if row.text else ""
        left, _, right = fields_of(head).get("page", "1/1").partition("/")
        page = int(left) if left.isdigit() else 1
        total = max(total, int(right) if right.isdigit() else 1)
        times = [
            _when(slot["at"])
            for slot in (normalize_slot(fields_of(line)) for line in row.text.splitlines())
            if slot is not None
        ]
        times = [at for at in times if at is not None]
        spans[page] = (min(times), max(times)) if times else (None, None)
    return spans, total


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
    extend: bool = False,
) -> list[dict[str, str]]:
    """Slot che il cittadino può accettare: futuri, liberi, entro la priorità, nella sua lingua,
    fuori dalla riserva urgente se la priorità non è U, nei giorni di lavoro del medico e (se
    date) con le sue preferenze. Ordinati dal più vicino."""

    horizon = _horizon(now, priority, extend)
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
        langs = frozenset(_lang_name(part) for part in _split(slot.get("languages", ""))) or (
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
    """Le strutture della nostra ASST: le etichette del registro e gli agenti della squadra."""

    own = {fields_of(line)["facility"] for line in lines if "facility" in fields_of(line)}
    return own | set(_team_facilities().values())


# --- testi per il cittadino -------------------------------------------------------------------------


def _label(procedure: str | None, tone: str) -> str:
    table = _LABELS.get(procedure or "", {})
    return table.get(tone) or table.get("en") or procedure or _VISIT.get(tone, "visit")


def _when_text(value: str, tone: str) -> str:
    at = _when(value)
    if at is None:
        return value
    lang = tone if tone in _DATE_FMT else "en"
    weekdays = _WEEKDAYS.get(lang, _WEEKDAYS["en"])
    months = _MONTHS.get(lang, _MONTHS["en"])
    return _DATE_FMT[lang].format(
        wd=weekdays[at.weekday()], d=at.day, m=at.month, month=months[at.month - 1],
        time=at.strftime("%H:%M"),
    )


def _priority_text(priority: str | None, tone: str) -> str:
    if priority not in {"U", "B", "D", "P"}:
        return ""
    return tr(f"prio_{priority}", tone)


def _fit(text: str) -> str:
    text = text.strip()
    return text if len(text) <= _MAX_TEXT else text[: _MAX_TEXT - 1].rstrip() + "…"


def _escalation_text(tone: str) -> str:
    return "escalate.emergency reason=red_flag\n" + tr("escalation", tone)


def _guard_note(tone: str, *, injected: bool, clinical_words: bool) -> str:
    notes = []
    if injected:
        notes.append(tr("guard_injection", tone))
    if clinical_words:
        notes.append(tr("guard_clinical", tone))
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
        lines.append(tr("option", tone, n=index, when=_when_text(slot.get("at", ""), tone)))
        lines.append(_offer_line(slot, offer_id, number, priority))
    lines.append(tr("choose", tone))
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
    return _natural_choice(text, offers)


# --- livello 3: richieste in linguaggio naturale -------------------------------------------------

_ORDINALS = (
    (0, r"prim[ao]|first|1st|opzione\s*1|option\s*1|numero\s*1"),
    (1, r"second[ao]|second|2nd|opzione\s*2|option\s*2|numero\s*2"),
    (2, r"terz[ao]|third|3rd|opzione\s*3|option\s*3|numero\s*3"),
    (-1, r"ultim[ao]|the\s+last(?:\s+one)?"),
)
_MONTH_NUM = {
    name.lower(): index
    for names in (_MONTHS["it"], _MONTHS["en"])
    for index, name in enumerate(names, start=1)
}


def _offer_at(offer: dict[str, str]) -> datetime | None:
    return _when(offer.get("agenda_date", ""))


def _natural_choice(text: str, offers: dict[str, dict[str, str]]) -> str | None:
    """L'offerta indicata a parole: "la seconda", "quella di mercoledì", "alle 10", "il 17
    novembre". Solo se la frase indica UNA offerta e non chiede altre date; altrimenti None (e
    il CUP chiede quale, invece di indovinare)."""

    lowered = _norm(text)
    if not offers or _WANTS_MORE.search(lowered):
        return None
    slots = [oid for oid, offer in offers.items() if not offer.get("waiting_list")]
    waiting = [oid for oid, offer in offers.items() if offer.get("waiting_list")]
    if not slots:
        # Solo la lista d'attesa sul tavolo: un sì a parole la sceglie.
        return waiting[-1] if waiting and _choice_words(lowered) else None
    latest = slots[-_MAX_OFFERS:]
    picked: set[str] = set(slots)
    named = ordinal = False
    for index, pattern in _ORDINALS:
        if re.search(rf"\b(?:{pattern})\b", lowered):
            picked &= {latest[index]} if -len(latest) <= index < len(latest) else set()
            named = ordinal = True
            break
    days = {code for code, names in _DAY_WORDS for name in names if len(name) > 3 and name in lowered}
    if days:
        picked &= {
            oid for oid in slots
            if (at := _offer_at(offers[oid])) is not None and _DAYS[at.weekday()] in days
        }
        named = True
    hour = re.search(r"\b(?:alle|ore|at)\s*(\d{1,2})(?:[:.](\d{2}))?\b|\b(\d{1,2})[:.](\d{2})\b", lowered)
    if hour:
        h = int(hour.group(1) or hour.group(3))
        m = int(hour.group(2) or hour.group(4) or 0)
        picked &= {
            oid for oid in slots
            if (at := _offer_at(offers[oid])) is not None and (at.hour, at.minute) == (h, m)
        }
        named = True
    date = re.search(r"\b(\d{1,2})\s*(?:/\s*(\d{1,2})\b|\s+([a-z]+))", lowered)
    if date and (date.group(2) or date.group(3) in _MONTH_NUM):
        day = int(date.group(1))
        month = int(date.group(2)) if date.group(2) else _MONTH_NUM[date.group(3)]
        picked &= {
            oid for oid in slots
            if (at := _offer_at(offers[oid])) is not None and (at.day, at.month) == (day, month)
        }
        named = True
    if not named or len(picked) != 1:
        return None
    if not (ordinal or _choice_words(lowered) or re.search(r"\b(quell[ao]|that one|the one)\b", lowered)):
        return None  # "martedì?" da solo è una preferenza, non una scelta
    return picked.pop()


# Una preferenza scritta a parole: "vorrei il martedì", "ideally on Monday", "una dottoressa".
_PREF_MARK = re.compile(
    r"\b(preferisc\w*|preferirei|vorrei|se possibile|se si può|se si puo|possibilmente|"
    r"preferibilmente|tipo|meglio|magari|ideally|prefer\w*|would like|"
    r"i'd like|rather|if possible|solo (?:il|di)|male doctor|female doctor|medico donna|"
    r"medico uomo)\b"
)
_SINCE_DAY = re.compile(
    r"\b(?:da|dal|dallo|since|from|scorso|scorsa|last)\s+(?:\w+\s+)?"
    r"(?:luned\w*|marted\w*|mercoled\w*|gioved\w*|venerd\w*|monday|tuesday|wednesday|thursday|friday)"
)



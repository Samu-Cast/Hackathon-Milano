# booking_centre: parte_2.py di 5 (vedi agent.py)
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

def _lang_code(raw: str) -> str:
    code = raw.strip().lower().replace("_", "-").split("-")[0]
    aliases = {"ua": "uk", "cn": "zh", "ar": "ar", "eng": "en", "ita": "it", "esp": "es", "spa": "es", "fra": "fr", "por": "pt", "deu": "de", "ger": "de", "rum": "ro", "ron": "ro"}
    return aliases.get(code, code)


def tr(key: str, lang: str, **kwargs: object) -> str:
    """Una frase del catalogo nella lingua del cittadino (ripiego: inglese, poi italiano)."""

    table = _TX.get(key, {})
    template = table.get(lang) or table.get("en") or table.get("it") or key
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        return template


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
        chat = await _case(city, message.need_id)
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
    # Testo libero: "la ricetta è N-2026-11-02-890907000042" (il medico scrive il numero con il
    # prefisso prescription_number-, il cittadino a volte lo omette).
    hit = _NUMBER_N.search(text)
    if hit:
        return "prescription_number-" + hit.group(1)
    return None


def _red_flag(text: str) -> bool:
    lowered = _norm(text)
    if fields_of(text).get("is_emergency", "").lower() == "true":
        return True
    return _alarm_text(text, _RED_FLAGS)


def _injection(text: str) -> bool:
    lowered = _norm(text)
    return any(marker in lowered for marker in _INJECTION)


# Ora di Milano: a novembre-febbraio (la gara) è UTC+1. Il Ledger scrive gli orari in UTC,
# ma cittadino, fasce 08-18 e confronti devono usare l'ora di Milano.
_MILAN = timezone(timedelta(hours=1))


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
    return parsed.astimezone(_MILAN)


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

    # La lingua del MEDICO: una richiesta esplicita vince sulla lingua in cui il cittadino
    # scrive o che dichiara (che resta quella della conversazione, vedi _tone).
    for row in reversed(_from_citizen(chat)):
        asked = _lang_request(row.text) or _lang_named(row.text)
        if asked:
            return asked
    for row in _from_citizen(chat):
        lang = fields_of(row.text).get("lang", "")
        if lang:
            return _lang_code(lang)
    for row in _from_citizen(chat):
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
        asked = _lang_request(row.text)
        if asked:
            return asked
    for row in reversed(_from_citizen(chat)):
        found = _detect_lang(row.text)
        if found:
            return found
    return "it"


def _lang_line(chat: list[Message]) -> str:
    """Non serve più: con le 10 lingue di Caterina le frasi sono già nella lingua del cittadino."""

    return ""


_WORKDAYS = ("mon", "tue", "wed", "thu", "fri")
_PREF_CUE = re.compile(
    r"\b(prefer|preferisc|preferirei|vorrei|meglio|solo|soltanto|possibilmente|disponibile|"
    r"riesco|posso|only|would like|i'd like|available|can do|can come|prefiero|preferiría|"
    r"solo puedo|puedo|je préfère|je prefere|seulement|je peux|prefiro|preferia|só posso|"
    r"posso ir|lieber|nur|ich kann|am liebsten|prefer|doar|pot|tranne|eccetto|escluso|except|"
    r"excepto|sauf|exceto|außer|cu excepția|mattina|pomeriggio|morning|afternoon|dottoressa|"
    r"medico donna|medico uomo|doctora|ärztin|parlo|speak|hablo|falo|parle|spreche|vorbesc)",
    re.IGNORECASE,
)
_NEG_CUE = re.compile(
    r"\b(non|tranne|eccetto|escluso|esclusi|except|excepto|menos|sauf|exceto|außer|nicht|"
    r"fără|fara|cu excepția|no puedo|no me va|no el|not on|can't|cannot|can not|not available|"
    r"pas le|pas disponible|não posso|nao posso|não dá|kann nicht|nu pot|nu e bine)\b",
    re.IGNORECASE,
)
# "ho mal di schiena da martedì": il giorno è l'inizio del sintomo, non una preferenza.
_SINCE = re.compile(
    r"(\bda|\bdal|\bdalla|\bdallo|\bsince|\bdesde|\bdepuis|\bseit|\bdin|\bfrom|від|منذ|从)\s*"
    r"(il |lo |la |the |el |le |o |a |last |scorso |pasado |dernier |letzten )?$",
    re.IGNORECASE,
)
_PERIOD_AM = re.compile(
    r"\b(mattina|mattino|mattutin|morning|por la mañana|en la mañana|matin|matinée|de manhã|"
    r"manhã|vormittag|morgens|dimineața|dimineata)\b|上午|早上|зранку|вранці|صباح",
    re.IGNORECASE,
)
_PERIOD_PM = re.compile(
    r"\b(pomeriggio|afternoon|por la tarde|en la tarde|après-midi|apres-midi|à tarde|de tarde|"
    r"nachmittag|nachmittags|după-amiaza|dupa-amiaza|după amiază)\b|下午|після обіду|по обіді|مساء|بعد الظهر",
    re.IGNORECASE,
)
_LANG_NAMES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("en", ("inglese", "english", "anglofon", "inglés", "ingles", "anglais", "inglês", "englisch", "engleză", "engleza", "الإنجليزية", "英语", "англійськ")),
    ("pt", ("portoghese", "portuguese", "portugués", "portugues", "portugais", "português", "portugiesisch", "portugheză", "البرتغالية", "葡萄牙语", "португальськ")),
    ("es", ("spagnolo", "spanish", "español", "espanol", "espagnol", "espanhol", "spanisch", "spaniolă", "الإسبانية", "西班牙语", "іспанськ")),
    ("fr", ("francese", "french", "francés", "frances", "français", "francês", "französisch", "franceză", "الفرنسية", "法语", "французьк")),
    ("de", ("tedesco", "german", "alemán", "aleman", "allemand", "alemão", "deutsch", "germană", "الألمانية", "德语", "німецьк")),
    ("ro", ("rumeno", "romanian", "rumano", "roumain", "romeno", "rumänisch", "română", "romana", "الرومانية", "罗马尼亚语", "румунськ")),
    ("ar", ("arabo", "arabic", "árabe", "arabe", "arabisch", "arabă", "العربية", "阿拉伯语", "арабськ")),
    ("zh", ("cinese", "chinese", "chino", "chinois", "chinês", "chinesisch", "chineză", "الصينية", "中文", "汉语", "китайськ")),
    ("uk", ("ucraino", "ukrainian", "ucraniano", "ukrainien", "ukrainisch", "ucraineană", "الأوكرانية", "乌克兰语", "українськ")),
    ("it", ("italiano", "italian", "italien", "italienisch", "italiană", "الإيطالية", "意大利语", "італійськ")),
)
_LANG_CUE = re.compile(
    r"\b(parl\w*|speak\w*|anglofon\w*|hablo|habla|hablante|falo|fala|parle|parlez|spreche|spricht|sprechend\w*|"
    r"vorbesc|vorbește|in|en|em|auf|în|lingua|language|idioma|langue|língua|sprache|limba)\b"
    r"|أتكلم|أتحدث|说|讲|розмовляю|говорю|мовою",
    re.IGNORECASE,
)


def _lang_request(text: str) -> str | None:
    """Lingua chiesta a parole: "parlo solo inglese", "un medico che parli portoghese"."""

    lowered = _norm(text)
    if not _LANG_CUE.search(lowered):
        return None
    for code, names in _LANG_NAMES:
        if any(name in lowered for name in names):
            return code
    return None


def _period_from(text: str) -> str | None:
    raw = (fields_of(text).get("time_of_day") or fields_of(text).get("period") or "").lower()
    if raw in {"am", "morning", "mattina"}:
        return "am"
    if raw in {"pm", "afternoon", "pomeriggio"}:
        return "pm"
    am = bool(_PERIOD_AM.search(text))
    pm = bool(_PERIOD_PM.search(text))
    if am != pm:
        return "am" if am else "pm"
    return None


def _day_mentions(text: str) -> list[tuple[str, int]]:
    """Giorni nominati nel testo, con la posizione."""

    lowered = _norm(text)
    found: list[tuple[str, int]] = []
    for code, names in _DAY_WORDS:
        for name in names:
            if len(name) <= 3:
                for hit in re.finditer(rf"\b{re.escape(name)}\b", lowered):
                    found.append((code, hit.start()))
            else:
                start = lowered.find(name)
                while start >= 0:
                    found.append((code, start))
                    start = lowered.find(name, start + 1)
    return found


def _days_from(text: str) -> set[str] | None:
    """Giorni preferiti: dal token ``days=`` o dalle parole della frase. None = non detto.
    Capisce anche le esclusioni: "tutti i giorni tranne il lunedì", "non posso venerdì"."""

    raw = fields_of(text).get("days") or fields_of(text).get("day")
    if raw:
        found = {
            part[:3].lower()
            for part in re.split(r"[/,|+]", raw)
            if part[:3].lower() in _DAYS
        }
        found = {code for code in (_day_code(part) for part in re.split(r"[/,|+]", raw)) if code}
        if found:
            return found
    lowered = _norm(text)
    include: set[str] = set()
    exclude: set[str] = set()
    for code, pos in _day_mentions(text):
        before = lowered[max(0, pos - 28):pos]
        if _SINCE.search(before):
            continue
        # La negazione vale se è vicina e nella stessa frase ("non posso il lunedì").
        clause = re.split(r"[.;!?]| ma | but | pero | mais | mas | aber | dar ", before)[-1]
        if _NEG_CUE.search(clause):
            exclude.add(code)
        else:
            include.add(code)
    if include - exclude:
        return include - exclude
    if exclude:
        return set(_WORKDAYS) - exclude
    return None


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
        cue = bool(_PREF_CUE.search(row.text))
        said_days, said_gender = _natural_prefs(row.text)  # "vorrei il martedì", "ideally a man"
        if after or cue or "days" in fields or "day" in fields:
            days = _days_from(row.text) or days
        elif said_days:
            days = said_days
        if after or cue or "gender" in fields or "gender_preference" in fields:
            gender = _gender_from(row.text) or gender
        elif said_gender:
            gender = said_gender
        if after:
            answered = True
    answered = answered or days is not None or gender is not None or _period(chat, agent, citizen) is not None
    return days, gender, answered


def _period(chat: list[Message], agent: str, citizen: str) -> str | None:
    """Mattina o pomeriggio, se il cittadino l'ha detto come preferenza."""

    asked = _asked_prefs_at(chat, agent, citizen)
    found: str | None = None
    for index, row in enumerate(chat):
        if not row.sender.startswith("citizen:"):
            continue
        if (asked is not None and index > asked) or _PREF_CUE.search(row.text) or "period" in row.text:
            found = _period_from(row.text) or found
    return found


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
    # "*" nella tabella di ripiego vuol dire "una lingua rara che non conosciamo": non la faccio
    # valere per qualunque lingua (000141, 000142: lang=ti, h3 rifiutato per lingua).
    return lang in langs


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
    if not reserve and (slot.get("priority") or slot.get("slot_priority") or "").upper() == "U":
        reserve = "u"  # slot riservato alle urgenze: non si offre a B, D e P
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
    period: str | None = None,
    skip: set[str] | frozenset[str] = frozenset(),
    skip_facilities: set[str] | frozenset[str] = frozenset(),
    extend: bool = False,
    anchor: datetime | None = None,
    partner: bool = False,
    skip_doctors: set[str] | frozenset[str] = frozenset(),
) -> list[dict[str, str]]:
    """Slot che il cittadino può accettare: futuri, liberi, entro la priorità, nella sua lingua,
    fuori dalla riserva urgente se la priorità non è U, nei giorni di lavoro del medico e (se
    date) con le sue preferenze. Ordinati dal più vicino."""

    # La scadenza di priorità corre dal primo contatto del caso (anchor), non da adesso.
    horizon = _horizon(anchor or now, priority, extend)
    earliest = now + _LEAD
    known = _doctors(lines)
    chosen: list[tuple[datetime, dict[str, str]]] = []
    seen: set[str] = set()
    for line in lines:
        slot = normalize_slot(fields_of(line))
        if slot is None or slot["slot_id"] in skip or slot["slot_id"] in seen or slot["doctor_id"] in skip_doctors:
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
        # Medici di un'ASST partner: valgono solo i loro dati (doctor_lang, doctor_gender), mai
        # la tabella dei nostri medici, che hanno codici simili ma lingue e giorni diversi.
        spec = (known.get(doctor) or _partner_spec(doctor, slot.get("facility", ""))) if partner else _spec(doctor, known)
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
        if period == "am" and at.hour >= 13 or period == "pm" and at.hour < 13:
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


# Dati del cittadino che non devono MAI uscire verso un altro agente (minimizzazione, GDPR):
# servono al massimo a lui. Verso strutture e partner passano solo id del cittadino, ricetta e
# consenso, e verso le ASST partner solo dopo il consenso cross_asst.
_PRIVATE_KEYS = {
    "tax_code", "codice_fiscale", "address", "indirizzo", "age", "eta", "disability", "occupation",
    "car", "car_euro_class", "phone", "telefono", "email", "name", "nome", "surname", "cognome",
    "birth_date", "dob", "lang", "gender",
}
_PII = re.compile(
    r"[A-Z]{6}\d{2}[A-EHLMPR-T]\d{2}[A-Z]\d{3}[A-Z]"  # codice fiscale
    r"|[\w.+-]+@[\w-]+\.[\w.]+"  # email
    r"|\+?\d[\d\s]{8,}\d",  # telefono
    re.IGNORECASE,
)




def _anchor(chat: list[Message]) -> datetime | None:
    """La prima ora di agenda letta nel caso: da lì corre la scadenza anche dopo un annullamento."""
    for row in chat:
        if row.sender == LEDGER and first_word(row.text) == "agenda":
            found = _when(fields_of(row.text).get("agenda_now", ""))
            if found is not None:
                return found
    return None

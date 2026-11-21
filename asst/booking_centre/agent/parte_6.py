# booking_centre: parte_6.py di 6 (vedi agent.py) — livello 4, ricerca presso le ASST partner
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

# Livello 4 (riserva, si aggiunge a quello che c'era): quando i posti locali compatibili sono
# esauriti la ricerca continua presso le ASST partner che l'Index restituisce per quel servizio.
# - La richiesta porta solo criteri: servizio, procedura, priorità, scadenza, request_id.
# - Le offerte delle partner si filtrano con i vincoli del cittadino (scadenza, giorni, genere,
#   lingua del medico da doctor_lang/doctor_gender) e partono in UN solo messaggio.
# - Le offerte arrivate dopo restano di riserva: servono se lo slot viene perso.
# - La ricerca si ricostruisce dalla cronologia: chi abbiamo interpellato, chi ha risposto.
# - I dati del cittadino escono solo dopo il consenso cross_asst (vedi _on_confirm).


def _confirmed_to(chat: list[Message], agent: str, facility: str) -> bool:
    """Abbiamo mandato un confirm a ``facility``? (Solo da lei accetto una ricevuta.)"""

    return any("confirm" in line_heads(row.text) for row in _ours(chat, agent, facility))


def _cross_started(chat: list[Message], agent: str) -> bool:
    return any(
        "need.request" in line_heads(row.text) and "service_type=" in row.text
        for row in _ours(chat, agent)
    )


def _partners_asked(chat: list[Message], agent: str) -> list[str]:
    asked: list[str] = []
    for row in _ours(chat, agent):
        if "need.request" in line_heads(row.text) and "service_type=" in row.text and row.recipient not in asked:
            asked.append(row.recipient)
    return asked


# Ricerche presso le partner chiuse per tempo scaduto (need_id). Una partner che non risponde
# entro _PARTNER_WAIT secondi vale come un no: il cittadino non resta mai in attesa infinita.
_SEARCH_CLOSED: set[str] = set()
_PARTNER_WAIT = 15.0


def _need_of(chat: list[Message]) -> str:
    return next((row.need_id for row in chat if row.need_id), "") or ""


def _stamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _search_expired(chat: list[Message], agent: str) -> bool:
    """La ricerca presso le partner è chiusa: tempo scaduto in questo processo, oppure (dagli
    orari dei messaggi) la prima richiesta è più vecchia di _PARTNER_WAIT secondi."""

    if _PARTNER_WAIT <= 0:
        return False  # attesa disattivata (solo nei test)
    if _need_of(chat) in _SEARCH_CLOSED:
        return True
    first = next((row for row in chat if row.sender == agent and "need.request" in line_heads(row.text)
                  and "service_type=" in row.text), None)
    if first is None:
        return False
    started = _stamp(first.sent_at)
    latest = max((stamp for stamp in (_stamp(row.sent_at) for row in chat) if stamp), default=None)
    return bool(started and latest and (latest - started).total_seconds() > _PARTNER_WAIT * 2)


def _partners_pending(chat: list[Message], agent: str) -> list[str]:
    """Partner interpellate che non hanno ancora risposto (né offerte né no)."""

    if _search_expired(chat, agent):
        return []
    answered = {
        row.sender for row in chat
        if row.sender.startswith("agent:") and line_heads(row.text) & {"offer", "cancel"}
    }
    return [peer for peer in _partners_asked(chat, agent) if peer not in answered]


def _partner_lines(chat: list[Message], agent: str) -> list[str]:
    """Le righe offer ricevute dalle ASST partner, con la struttura che le ha offerte e i dati
    del medico che la partner dichiara (doctor_lang, doctor_gender)."""

    ours = set(_team_facilities().values()) | {agent}
    asked = set(_partners_asked(chat, agent))
    lines: list[str] = []
    for row in chat:
        if not row.sender.startswith("agent:") or row.sender in ours or row.sender not in asked:
            continue
        for line in row.text.splitlines():
            if first_word(line) != "offer":
                continue
            fields = fields_of(line)
            if not fields.get("slot_id"):
                continue
            fields["facility"] = row.sender  # la struttura resta quella che ha offerto lo slot
            if not fields.get("at") and fields.get("agenda_date"):
                fields["at"] = fields["agenda_date"]
            if fields.get("doctor_lang") and not fields.get("languages"):
                fields["languages"] = fields["doctor_lang"]
            if fields.get("doctor_gender") and not fields.get("gender"):
                fields["gender"] = fields["doctor_gender"]
            lines.append(" ".join(f"{k}={v}" for k, v in fields.items()))
    return lines


def _request_deadline(chat: list[Message], priority: str | None) -> str:
    agenda = _ledger(chat, "agenda") or {}
    now = _when(agenda.get("agenda_now") or agenda.get("now") or "")
    if now is None or priority in (None, "P"):
        return ""
    horizon = _horizon(now, priority)
    return horizon.isoformat() if horizon else ""


def _partner_candidates(
    chat: list[Message], agent: str, citizen: str, key: str, *, exclude_offered: bool
) -> tuple[list[dict[str, str]], bool]:
    """Slot delle partner compatibili con ricetta, scadenza e vincoli. (slot, rilassati?)"""

    number = _number(chat)
    status, reply = _prescription_ok(chat, number) if number else ("pending", {})
    agenda = _ledger(chat, "agenda") or {}
    now = _when(agenda.get("agenda_now") or agenda.get("now") or "")
    if status != "ok" or now is None:
        return [], False
    priority = (reply.get("priority") or "").upper() or None
    procedure = reply.get("procedure")
    lines = _partner_lines(chat, agent)
    if not lines or priority == "P":
        return [], False
    skip = _cancelled_slots(chat, agent, citizen) | _citizen_cancelled(key)
    if exclude_offered:
        skip |= _offered_slots(chat, agent, citizen)
    days, gender, _ = _preferences(chat, agent, citizen)
    period = _period(chat, agent, citizen)
    common = dict(now=now, priority=priority, lang=_lang(chat), procedure=procedure, skip=skip, partner=True,
                  skip_doctors=_rejected_doctors(chat, agent, citizen))
    strict = fitting(lines, days=days, gender=gender, period=period, **common)
    if strict or not (days or gender or period):
        return strict, False
    return fitting(lines, **common), True


async def _offer_partner_reserve(
    city: City,
    chat: list[Message],
    key: str,
    citizen: str,
    number: str | None,
    procedure: str | None,
    priority: str | None,
    tone: str,
    note: str,
    *,
    replacing: bool,
    exclude_offered: bool,
    trigger: str = "",
) -> bool:
    """Le riserve delle partner ancora compatibili, in un solo messaggio. True se inviate."""

    slots, relaxed = _partner_candidates(chat, city.agent_id, citizen, key, exclude_offered=exclude_offered)
    if not slots:
        return False
    before = set(_sent(key).offers)
    await _send_offers(
        city, chat, key, citizen, slots, number=number, priority=priority, procedure=procedure,
        tone=tone, relaxed=relaxed, replacing=replacing, note=note, partner=True, trigger=trigger,
    )
    return _sent(key).offers != before or replacing


async def _on_partner_offer(city: City, message: Message, chat: list[Message], key: str) -> None:
    """Offerte di una partner. Aspetto qualche secondo le altre partner interpellate, poi mando
    UN solo messaggio con tutte le opzioni compatibili. Quello che arriva dopo resta di riserva."""

    citizen = _citizen(chat)
    if citizen is None:
        return
    agent = city.agent_id
    if message.sender in _team_facilities().values():
        return
    if _partners_pending(chat, agent):
        chat = await _until(city, message.need_id, chat, lambda rows: not _partners_pending(rows, agent))
    partner_slots = {fields_of(line).get("slot_id") for line in _partner_lines(chat, agent)}
    already = (_offered_slots(chat, agent, citizen) | set(_sent(key).offers)) & partner_slots
    gone = _cancelled_slots(chat, agent, citizen) | _citizen_cancelled(key)
    if already - gone:
        return  # ho già girato offerte delle partner: queste restano di riserva
    number = _number(chat)
    _, reply = _prescription_ok(chat, number) if number else ("pending", {})
    priority = (reply.get("priority") or "").upper() or None
    procedure = reply.get("procedure")
    if await _offer_partner_reserve(
        city, chat, key, citizen, number, procedure, priority, _tone(chat), "",
        replacing=False, exclude_offered=True,
    ):
        return
    if not _partners_pending(chat, agent) and not _offered_slots(chat, agent, citizen):
        # Tutte le partner hanno risposto e nessuna offerta è compatibile: alternativa del gioco.
        await _no_slot_fallback(city, chat, key, citizen, number, procedure, priority, _tone(chat), note="")


async def _partner_said_no(city: City, message: Message, chat: list[Message], key: str) -> bool:
    """Una partner risponde che non ha posti. True se l'ho gestito io: aspetto le altre, offro
    le riserve compatibili oppure, se la ricerca è chiusa, propongo l'alternativa del gioco."""

    agent = city.agent_id
    citizen = _citizen(chat)
    if citizen is None or not _cross_started(chat, agent) or message.sender not in _partners_asked(chat, agent):
        return False
    if _partners_pending(chat, agent):
        return True  # le altre partner possono ancora rispondere
    number = _number(chat)
    _, reply = _prescription_ok(chat, number) if number else ("pending", {})
    priority = (reply.get("priority") or "").upper() or None
    procedure = reply.get("procedure")
    tone = _tone(chat)
    if await _offer_partner_reserve(
        city, chat, key, citizen, number, procedure, priority, tone, "",
        replacing=False, exclude_offered=True,
    ):
        return True
    if not _offered_slots(chat, agent, citizen):
        await _no_slot_fallback(city, chat, key, citizen, number, procedure, priority, tone, note="")
    return True


def _repeat_request(chat: list[Message], agent: str, latest: Message) -> bool:
    """Il cittadino ripete la richiesta (stesso testo, o di nuovo la ricetta) mentre ha già
    delle offerte: non è una scelta, non è un consenso, non è un rifiuto."""

    if not latest.sender.startswith("citizen:"):
        return False
    text = latest.text
    heads = line_heads(text) | words(text)
    if heads & {"consent", "data.response", "confirm", "cancel"} or fields_of(text).get("consent_ref"):
        return False
    if _CONSENT_BARE.search(text) or fields_of(text).get("offer_id"):
        return False
    earlier = [_norm(row.text) for row in _from_citizen(chat) if row.msg_id != latest.msg_id]
    return _norm(text) in earlier or bool(_number_in(text)) or "need.request" in heads


async def _resend_offers(city: City, chat: list[Message], key: str, citizen: str, latest: Message) -> None:
    """Stesso percorso: gli rimando le offerte che ha già (stessi offer_id e slot_id)."""

    for row in reversed(_ours(chat, city.agent_id, citizen)):
        if any(first_word(line) == "offer" for line in row.text.splitlines()):
            await _once(city, key, f"resend:{latest.msg_id}", citizen, row.text)
            return


# --- percorsi falliti della wave: confirm senza ricevuta, alternativa che dichiara il vincolo ---

_LANG_LABEL = {
    "it": {"ti": "tigrino", "am": "amarico", "so": "somalo", "ka": "georgiano", "ru": "russo",
           "en": "inglese", "es": "spagnolo", "fr": "francese", "de": "tedesco", "pt": "portoghese",
           "ro": "rumeno", "ar": "arabo", "zh": "cinese", "uk": "ucraino"},
    "en": {"ti": "Tigrinya", "am": "Amharic", "so": "Somali", "ka": "Georgian", "ru": "Russian",
           "en": "English", "es": "Spanish", "fr": "French", "de": "German", "pt": "Portuguese",
           "ro": "Romanian", "ar": "Arabic", "zh": "Chinese", "uk": "Ukrainian"},
}


def _relaxed_intro(
    tone: str, procedure: str | None, doctor_lang: str, later: bool, in_time: bool, alt_lang: str = "it"
) -> str:
    """Intro dell'alternativa: dice quale vincolo allento (scadenza o lingua), senza promettere
    date entro la scadenza che non ci sono."""

    label = _label(procedure, tone)
    if tone == "it":
        lang = _LANG_LABEL["it"].get(doctor_lang, doctor_lang)
        alt = "italiano" if alt_lang == "it" else _LANG_LABEL["it"].get(alt_lang, alt_lang)
        if later and in_time:
            return (f"Entro la scadenza della priorità non c'è un medico che parli {lang} per la sua {label}, "
                    f"né da noi né nelle ASST partner. Per trasparenza le propongo due strade: le prime "
                    f"opzioni sono con un medico che parla {lang}, subito DOPO la scadenza; l'ultima è "
                    f"ENTRO la scadenza, con un medico che parla {alt}. Scelga lei:")
        if in_time:
            return (f"Entro la scadenza non c'è un medico che parli {lang} per la sua {label}. Per "
                    f"trasparenza le propongo date entro la scadenza con un medico che parla {alt}:")
        return tr("offers_beyond", tone, label=label)
    lang = _LANG_LABEL["en"].get(doctor_lang, doctor_lang)
    alt = "Italian" if alt_lang == "it" else _LANG_LABEL["en"].get(alt_lang, alt_lang)
    if later and in_time:
        return (f"Within the deadline of your priority there is no doctor who speaks {lang} for your {label}, "
                f"neither here nor in partner ASSTs. To be transparent, here are two ways: the first options "
                f"are with a doctor who speaks {lang}, just AFTER the deadline; the last one is WITHIN the "
                f"deadline, with a doctor who speaks {alt}. It is your choice:")
    if in_time:
        return (f"Within the deadline there is no doctor who speaks {lang} for your {label}. To be "
                f"transparent, here are dates within the deadline with a doctor who speaks {alt}:")
    return tr("offers_beyond", tone, label=label)


async def _confirm_pending(
    city: City, message: Message, chat: list[Message], key: str, offer_id: str, offer: dict[str, str], slot: str
) -> None:
    """Il cittadino rimanda il confirm e la ricevuta non è arrivata. Rispondo sempre: la prima
    volta dico che aspetto la conferma firmata; dalla seconda libero il posto presso la
    struttura (senza ritentare la stessa richiesta) e propongo un altro slot."""

    agent = city.agent_id
    citizen = message.sender
    tone = _tone(chat)
    facility = _confirmed_slot(chat, agent, slot)
    sent_at = max((i for i, row in enumerate(chat) if row.sender == agent and row.recipient == facility
                   and "confirm" in line_heads(row.text)), default=-1)
    repeats = sum(1 for row in chat[sent_at + 1:] if row.sender == citizen and fields_of(row.text).get("offer_id") == offer_id)
    if repeats <= 1:
        text = ("Ho già inoltrato la sua scelta alla struttura e sto aspettando la sua conferma firmata: "
                "le mando la ricevuta appena arriva. Non deve fare altro."
                if tone == "it" else
                "I have already sent your choice to the facility and I am waiting for its signed "
                "confirmation: I will send you the receipt as soon as it arrives. You do not need to do anything else.")
        await _once(city, key, f"pending:{offer_id}:{repeats}", citizen, text)
        return
    if f"lost:{slot}" in _sent(key).lines:
        return
    _sent(key).lines.add(f"lost:{slot}")
    _sent(key).flags.add(f"cancelled:{slot}")
    if facility:
        await _say(city, facility, f"cancel offer_id={offer_id} slot_id={slot} citizen={citizen} reason=no_receipt\n"
                   "Buongiorno, non abbiamo ricevuto la conferma firmata: liberiamo questo posto. Grazie!")
    prose = ("Mi scusi: la struttura non ha confermato questo posto, quindi non lo considero prenotato. "
             "Le cerco subito un'alternativa."
             if tone == "it" else
             "I am sorry: the facility has not confirmed this slot, so I do not consider it booked. "
             "I am looking for an alternative straight away.")
    await _say(city, citizen, f"cancel offer_id={offer_id} reason=facility_no_reply\n{prose}")
    try:
        fresh = await _case(city, message.need_id)
    except CapError:
        fresh = chat
    await _advance(city, message, fresh, key, replacing=True)


# --- caso 000142: lingua nominata nel testo, medici rifiutati per lingua o genere -------------

_NEGATED_LANG = re.compile(r"\b(non|not|don't|do not|no|nicht|pas)\b(?:\s+\w+){0,3}\s*$")


def _lang_named(text: str) -> str | None:
    """Una lingua nominata nel testo ("english please", "in inglese"), non negata ("non parlo
    inglese"). Vince sul token lang= dell'intestazione, che può essere sbagliato."""

    lowered = _norm(text)
    for code, names in _LANG_NAMES:
        for name in names:
            at = lowered.find(name)
            while at >= 0:
                before = lowered[max(0, at - 40):at]
                if not _NEGATED_LANG.search(before):
                    return code
                at = lowered.find(name, at + 1)
    return None


def _rejected_for(chat: list[Message], agent: str, citizen: str, reason: str) -> set[str]:
    """I medici delle offerte che il cittadino ha rifiutato con questo motivo (lang, gender)."""

    offers = _our_offers(chat, agent, citizen)
    found: set[str] = set()
    for row in chat:
        if row.sender != citizen or "cancel" not in (line_heads(row.text) | words(row.text)):
            continue
        fields = fields_of(row.text)
        said = (fields.get("reason") or fields.get("motivo") or "").lower()
        if not said.startswith(reason[:4]):
            continue
        offer = offers.get(fields.get("offer_id", ""), {})
        if offer.get("doctor_id"):
            found.add(offer["doctor_id"])
    return found


def _rejected_doctors(chat: list[Message], agent: str, citizen: str) -> set[str]:
    """Medici da non riproporre nello stesso caso: rifiutati per lingua o per genere."""

    return _rejected_for(chat, agent, citizen, "lang") | _rejected_for(chat, agent, citizen, "gender")


# --- consenso con il riferimento proposto, domande sul consenso, annullamento di tutto ---------

_AFFIRM = re.compile(
    r"\b(sì|si|yes|ok|okay|va bene|d'accordo|acconsento|accetto|consento|autorizzo|agree|i agree|"
    r"i consent|sure|of course|certo|certamente|sí|oui|ja|da|claro|d'acord|sim|так|نعم|是的|是|同意)\b", re.I)
_NEGATIVE = re.compile(r"\b(no|non|not|don't|do not|nope|never|mai|rifiut\w*|decline\w*|preferisco di no)\b", re.I)
_CONSENT_WORD = re.compile(r"consent|consenso|permission|permesso|autorizza", re.I)
_EVERYTHING = re.compile(
    r"\b(annull\w* tutto|cancel(?:la)? tutto|cancel everything|cancel (?:my|the) (?:request|booking|appointment)|"
    r"non mi serve più|non ho più bisogno|lascio perdere|rinuncio (?:alla visita|a tutto)|"
    r"i no longer need|no longer need|forget it|drop (?:it|the request)|stop (?:the|my) request)\b", re.I)


def _consent_head(purpose: str, key: str, offer_id: str = "") -> str:
    """La riga di una richiesta di consenso, con il riferimento già proposto: il cittadino può
    rispondere con quel consent_ref o con un semplice sì."""

    ref = f"cons-{key}-{purpose}"
    return f"consent purpose={purpose} consent_ref={ref}" + (f" offer_id={offer_id}" if offer_id else "")


def _affirmative(text: str) -> bool:
    lowered = _norm(text)
    return bool(_AFFIRM.search(lowered)) and not _NEGATIVE.search(lowered)


def _consent_question(text: str) -> bool:
    """"Which consent_ref?", "quale consenso?": una domanda, non un rifiuto."""

    lowered = _norm(text)
    asks = "?" in text or bool(re.match(r"\s*(which|what|how|quale|qual|cosa|come|dove|where)\b", lowered))
    return asks and bool(_CONSENT_WORD.search(lowered) or re.search(r"\b(which|quale|cosa|what)\b", lowered))


def _consent_by_words(chat: list[Message], purpose: str, agent: str, citizen: str) -> str | None:
    """Un sì a parole dopo la nostra richiesta di consenso: vale il riferimento proposto."""

    proposed, asked_at = None, None
    for index, row in enumerate(chat):
        if row.sender == agent and row.recipient == citizen and "consent" in line_heads(row.text):
            fields = fields_of(row.text)
            if fields.get("purpose") == purpose and fields.get("consent_ref"):
                proposed, asked_at = fields["consent_ref"], index
    if proposed is None:
        return None
    for row in chat[asked_at + 1:]:
        if row.sender != citizen:
            continue
        fields = fields_of(row.text)
        if fields.get("offer_id") or fields.get("consent_ref") or "confirm" in (line_heads(row.text) | words(row.text)):
            continue  # un sì dentro una scelta (o con un altro riferimento) non è questo consenso
        if _affirmative(row.text) and not _consent_question(row.text):
            return proposed
    return None


def _cancel_everything(text: str) -> bool:
    return bool(_EVERYTHING.search(_norm(text)))


async def _consent_followup(city: City, chat: list[Message], key: str, citizen: str, latest: Message) -> bool:
    """Il cittadino risponde alla richiesta di consenso cross_asst fatta all'inizio della ricerca:
    a una domanda ("Which consent_ref?") rimando la richiesta con il riferimento; a un sì
    confermo che l'ho registrato. True se ho risposto."""

    agent = city.agent_id
    asked = [row for row in _ours(chat, agent, citizen) if "consent" in line_heads(row.text) and "purpose=cross_asst" in row.text]
    if not asked:
        return False
    tone = _tone(chat)
    if _consent_question(latest.text):
        # "Which consent_ref?": non è un rifiuto e non chiude la ricerca. Rimando la richiesta
        # con il riferimento pronto e spiego come rispondere.
        offer_id = fields_of(asked[-1].text).get("offer_id", "")
        await _once(city, key, f"consent-again:{latest.msg_id}", citizen,
                    _consent_head("cross_asst", key, offer_id) + "\n" + _consent_help("cross_asst", key, tone))
        return True
    if not _partners_pending(chat, agent):
        return False  # le partner hanno già risposto (o il tempo è scaduto): niente "sto aspettando"
    if _affirmative(latest.text) or fields_of(latest.text).get("consent_ref") or _CONSENT_BARE.search(latest.text):
        text = ("Grazie, ho registrato il suo consenso. Sto aspettando le disponibilità delle ASST partner: "
                "le scrivo appena arrivano."
                if tone == "it" else
                "Thank you, I have recorded your permission. I am waiting for the partner ASSTs' availability: "
                "I will write to you as soon as it arrives.")
        await _once(city, key, "cross-consent-ack", citizen, text)
        return True
    return False


# --- MACH-1: standard comune tra ASST (RED-AGENT, accettato dalle squadre su Discord) ----------
# Gli id li hanno pubblicati le squadre: sono dichiarati, non verificati. Servono solo a mettere
# per prime le strutture note e in fondo (o fuori) gli agenti impostori che arrivano dal 21/11.
_TRUSTED_ASST: dict[str, str] = {}
for _asst, _ids in (
    ("giambellino", "cdc7x6m21nj6 c32jyk0cyan7 h6r1v45esk77 hnpe58epeyh1 bzs050xacs1m"),
    ("lambrate", "gdrf9b9kxxss c2ewegr2ea3v nanpr5mgm2py pky3vqyqbepa atp772whjkgd"),
    ("nord", "psxp8bh1feaa wwxaphfb5pfh gpr1zqe343gb zand0f57ec1j ccjzqm7yekwx"),
    ("greco", "h3n6kfd18r3g bmrcsmqq6at2 xtj1dk9x3qej q18es2d82f8r pxvd2kk2yx5j"),
    ("bicocca", "ez8cb7j167p2 rng5wcrzvhsj bb01ggxymm05 sh35cbsy287d nkh372rd16yq"),
    ("bovisa", "vgtmj5nqfkxj h82xpx3rkn9e gxq3vzgt3h5t tty8erdj6t41 txar5994gbkj"),
    ("lorenteggio", "qtjne8symjas w2qsptc9qqhg tmt0bcv0st1f n9bf9y6670hp mcppmrbjvxag"),
    ("isola", "etpwrfmfa8hm mkfzagfe8jtt"),
):
    for _id in _ids.split():
        _TRUSTED_ASST[f"agent:{_id}"] = _asst
# Lingua rara del fisiatra h3 di ogni ASST (tabella MACH-1 e messaggi delle squadre).
_RARE_LANG_ASST = {
    "ka": "giambellino", "ro": "toros", "ur": "nord", "fr": "corvetto", "sq": "gorla", "fa": "brera",
    "tl": "isola", "wo": "ortica", "si": "greco", "ti": "bicocca", "de": "forlanini", "ja": "lambrate",
}
_RARE_OF_ASST = {asst: lang for lang, asst in _RARE_LANG_ASST.items()}


def _bare_id(agent_id: str) -> str:
    return (agent_id or "").split("@", 1)[0].strip().lower()


def _trusted(agent_id: str) -> bool:
    return _bare_id(agent_id) in _TRUSTED_ASST


def _partner_rank(agent_id: str, lang: str) -> int:
    """0: struttura nota dell'ASST la cui lingua rara è quella chiesta; 1: struttura nota;
    2: agente che nessuna squadra ha pubblicato (possibile impostore, va in fondo)."""

    asst = _TRUSTED_ASST.get(_bare_id(agent_id))
    if asst and _RARE_LANG_ASST.get(lang) == asst:
        return 0
    return 1 if asst else 2


def _ranked_partners(peers: list, lang: str) -> list:
    return sorted(peers, key=lambda peer: _partner_rank(peer.agent_id, lang))


def _partner_spec(doctor: str, facility: str):
    """Medico di un'ASST partner che non ha mandato doctor_lang/doctor_gender. Ogni ASST ha gli
    stessi medici (kit, e lo confermano le squadre): vale la tabella comune, tranne per h3, la cui
    lingua rara cambia da ASST a ASST e la deduco solo da una struttura nota."""

    key = _doctor_key(doctor)
    if key == "h3":
        rare = _RARE_OF_ASST.get(_TRUSTED_ASST.get(_bare_id(facility), ""))
        return (frozenset({rare}), "m", frozenset({"tue", "thu"})) if rare else None
    return _DOCTORS.get(key)


def _mach_criteria(chat: list[Message], agent: str, citizen: str) -> list[str]:
    """Criteri facoltativi di MACH-1 per need.request: lingua del medico, genere, giorni.
    Sono preferenze di ricerca, non dati del cittadino."""

    parts: list[str] = []
    lang = _lang(chat)
    if lang and lang != "it":
        parts.append(f"lang={lang}")
    days, gender, _ = _preferences(chat, agent, citizen)
    if gender in ("f", "m"):
        parts.append(f"gender_preference={gender}")
    if days:
        parts.append("days=" + "/".join(code for code in _DAYS if code in days))
    return parts


_CONSENT_HELP = {
    "it": "Per darmi il consenso risponda pure con: consent purpose={p} consent_ref={ref} (oppure scriva semplicemente «sì»).",
    "en": "Please reply with: consent purpose={p} consent_ref={ref} (or simply write \"yes\").",
    "es": "Por favor, responda con: consent purpose={p} consent_ref={ref} (o escriba simplemente «sí»).",
    "fr": "Merci de répondre avec : consent purpose={p} consent_ref={ref} (ou écrivez simplement « oui »).",
    "pt": "Por favor, responda com: consent purpose={p} consent_ref={ref} (ou escreva simplesmente «sim»).",
    "de": "Bitte antworten Sie mit: consent purpose={p} consent_ref={ref} (oder schreiben Sie einfach „ja“).",
    "ro": "Vă rog să răspundeți cu: consent purpose={p} consent_ref={ref} (sau scrieți simplu „da”).",
    "ar": "يرجى الرد بـ: consent purpose={p} consent_ref={ref} (أو اكتبوا ببساطة «نعم»).",
    "zh": "请回复： consent purpose={p} consent_ref={ref} （或者直接回复“是”）。",
    "uk": "Будь ласка, дайте відповідь: consent purpose={p} consent_ref={ref} (або просто напишіть «так»).",
}


def _consent_help(purpose: str, key: str, tone: str) -> str:
    """Come rispondere alla richiesta di consenso, con il riferimento già pronto ("Which
    consent_ref?"). I token sono staccati dalla punteggiatura: chi li legge trova lo stesso
    consent_ref della prima riga."""

    template = _CONSENT_HELP.get(tone) or _CONSENT_HELP["en"]
    return template.format(p=purpose, ref=f"cons-{key}-{purpose}")


def _consent_message(purpose: str, key: str, tone: str, prose: str, offer_id: str = "") -> str:
    return _consent_head(purpose, key, offer_id) + "\n" + prose + "\n" + _consent_help(purpose, key, tone)


async def _wait_partners(city: City, chat: list[Message], key: str, citizen: str, tone: str, note: str) -> None:
    """Dopo le richieste alle partner aspetto le risposte al massimo _PARTNER_WAIT secondi, nello
    stesso turno. Poi chiudo la ricerca: offro le loro opzioni compatibili oppure l'alternativa.
    Le offerte che arrivano dopo restano di riserva (_on_partner_offer)."""

    if _PARTNER_WAIT <= 0:
        return  # attesa disattivata (solo nei test): le risposte arrivano nei turni successivi
    agent = city.agent_id
    need = _need_of(chat)
    tries = max(1, int(_PARTNER_WAIT / _PULL_PAUSE)) if _PULL_PAUSE > 0 else _PULL_TRIES
    fresh = chat
    for attempt in range(tries):
        try:
            fresh = await _case(city, need or None)
        except CapError:
            fresh = chat
        if not _partners_pending(fresh, agent):
            break
        if attempt + 1 < tries:
            await asyncio.sleep(_PULL_PAUSE)
    if need:
        _SEARCH_CLOSED.add(need)
    if len(_SEARCH_CLOSED) > 5000:
        _SEARCH_CLOSED.clear()
    number = _number(fresh)
    _, reply = _prescription_ok(fresh, number) if number else ("pending", {})
    priority = (reply.get("priority") or "").upper() or None
    procedure = reply.get("procedure")
    if await _offer_partner_reserve(
        city, fresh, key, citizen, number, procedure, priority, tone, "",
        replacing=False, exclude_offered=True,
    ):
        return
    if not _offered_slots(fresh, agent, citizen):
        await _no_slot_fallback(city, fresh, key, citizen, number, procedure, priority, tone, note="")


async def _answer_consent_question(city: City, chat: list[Message], key: str, citizen: str, message: Message) -> bool:
    """"Which consent_ref?" in qualunque momento: rimando l'ultima richiesta di consenso con il
    riferimento pronto e le istruzioni. Non è un rifiuto e non chiude nulla."""

    if not _consent_question(message.text):
        return False
    asked = [row for row in _ours(chat, city.agent_id, citizen) if "consent" in line_heads(row.text)]
    if not asked:
        return False
    fields = fields_of(asked[-1].text)
    purpose = fields.get("purpose", "booking")
    tone = _tone(chat)
    await _once(city, key, f"consent-again:{message.msg_id}", citizen,
                _consent_head(purpose, key, fields.get("offer_id", "")) + "\n" + _consent_help(purpose, key, tone))
    return True



# --- fault.cancel riferito dal cittadino (livello 6): riprenoto, non è una sua rinuncia ---------

_REPORTED_CANCEL = re.compile(
    r"\b(fault\.cancel|(è|e'|é) stat[ao] (annullat|cancellat|disdett)\w*|"
    r"sono stat[ei] (annullat|cancellat)\w*|mi hanno (annullat|cancellat|disdett)\w*|"
    r"(ha|hanno) annullato (la mia|il mio)|"
    r"(was|has been|got|been) cancel+ed|they cancel+ed|"
    r"fue cancelad\w*|han cancelado|a été annulé\w*|ont annulé|foi cancelad\w*|"
    r"wurde (storniert|abgesagt)|a fost anulat\w*)",
    re.IGNORECASE,
)


def _reported_cancel(text: str) -> bool:
    return "fault.cancel" in words(text) or bool(_REPORTED_CANCEL.search(text))


def _confirm_recipients(chat: list[Message], agent: str) -> set[str]:
    """Le strutture a cui abbiamo mandato un confirm in questo caso."""
    return {
        row.recipient for row in _ours(chat, agent)
        if row.recipient.startswith("agent:") and "confirm" in line_heads(row.text)
    }


async def _on_reported_cancel(city: City, message: Message, chat: list[Message], key: str) -> bool:
    """Il cittadino riferisce che la prenotazione confermata è stata annullata: libero il posto
    presso la struttura, glielo dico senza attribuirgli la rinuncia e propongo nuove date.
    False se non c'è una nostra prenotazione da riprendere."""
    agent = city.agent_id
    citizen = message.sender
    offers = _our_offers(chat, agent, citizen)
    offer_id = ""
    for row in _ours(chat, agent):
        if "confirm" in line_heads(row.text) and row.recipient.startswith("agent:"):
            candidate = fields_of(row.text).get("offer_id", "")
            if candidate in offers:
                offer_id = candidate
    if not offer_id:
        return False
    slot = offers[offer_id].get("slot_id", "")
    target = _confirmed_slot(chat, agent, slot) if slot else ""
    sent = _sent(key)
    sent.flags.add(f"cancelled:{slot}")
    if target and f"released:{slot}" not in sent.lines:
        sent.lines.add(f"released:{slot}")
        booking = ""
        for row in _ours(chat, agent, citizen):
            if "booking.receipt" in line_heads(row.text):
                booking = fields_of(row.text).get("booking_id", booking)
        parts = ["cancel", f"offer_id={offer_id}", f"slot_id={slot}", f"citizen={citizen}",
                 "reason=reported_cancelled"]
        if booking:
            parts.append(f"booking_id={booking}")
        await _say(city, target, " ".join(parts) + "\nBuongiorno, al cittadino risulta annullata "
                   "questa prenotazione: se è ancora attiva liberatela pure. Grazie!")
    tone = _tone(chat)
    if not _we(chat, agent, f"offer_id={offer_id}", to=citizen, head="cancel"):
        await _say(city, citizen, f"cancel offer_id={offer_id} reason=facility_cancelled\n"
                   + tr("facility_cancelled", tone))
    try:
        fresh = await _case(city, message.need_id)
    except CapError:
        fresh = chat
    await _advance(city, message, fresh, key, replacing=True)
    return True

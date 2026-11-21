# family_counseling: parte_2.py di 2 (vedi agent.py)
from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from city_sdk.city import LEDGER, CapError, City, Message, Need, clinical, understood

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
            "need a referral; we will propose a time as soon as one becomes free."
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
                "Thank you for your choice! To book it I need your permission to use your booking "
                "data (name and appointment) at the centre. No referral is needed."
                if tone == "en"
                else "Grazie per la scelta! Per prenotarla mi serve il suo consenso a usare i dati "
                "della prenotazione (nominativo e appuntamento) al consultorio. Non serve la ricetta."
            )
            await _say(city, citizen, f"consent purpose=booking offer_id={chosen}\n" + prose)
        else:
            prose = (
                "No problem: without your permission I will not book anything. If you change your "
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
    """Options this process sent for one need, which the case history may not show yet.

    ``options`` holds every slot id (and waiting-list option id) sent. A cancel makes the
    citizen drop all options, so ``again`` holds only what was re-sent for ``trigger``.
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


# Annullamenti per cui le alternative sono già partite (non si rimandano due volte).
_RESUMED: set[str] = set()


def _last_cancel(chat: list[Message], agent: str, citizen: str) -> tuple[int, Message | None]:
    found: tuple[int, Message | None] = (-1, None)
    for index, row in enumerate(chat):
        if (row.sender == agent and row.recipient == citizen and first_word(row.text) == "cancel"
                and "reason=" in row.text):
            found = (index, row)
    return found


def _replacement_pending(chat: list[Message], agent: str, citizen: str) -> bool:
    """Abbiamo annullato un'offerta al cittadino e da allora non gli abbiamo più mandato offerte, e lui
    non ha rinunciato: le alternative promesse non sono ancora partite."""

    index, row = _last_cancel(chat, agent, citizen)
    if row is None:
        return False
    for later in chat[index + 1:]:
        if later.sender == agent and later.recipient == citizen and any(
            first_word(line) == "offer" for line in later.text.splitlines()
        ):
            return False
        if later.sender == citizen and "cancel" in words(later.text):
            return False  # il cittadino ha rinunciato dopo il nostro annullamento
    return True


async def _advance(
    city: City,
    message: Message,
    chat: list[Message],
    *,
    fixed: str | None,
    replacing: bool,
    note: str = "",
    resume: bool = False,
) -> None:
    key = message.need_id or message.msg_id
    await _advance_alone(city, message, chat, key, fixed=fixed, replacing=replacing, note=note, resume=resume)


async def _advance_alone(
    city: City,
    message: Message,
    chat: list[Message],
    key: str,
    *,
    fixed: str | None,
    replacing: bool,
    note: str = "",
    resume: bool = False,
) -> None:
    agent = city.agent_id
    citizen = _citizen(chat)
    if citizen is None or not any(row.sender == citizen for row in chat):
        return  # offro solo a un cittadino che ci ha scritto (non a quello di un'ASST partner)
    tone = _tone(chat)
    procedure = "CONS-GIN"
    if not replacing and not resume and (_offered(chat, agent) or _sent(key).offers):
        return
    resume_tag = ""
    if resume:
        _index, cancelled = _last_cancel(chat, agent, citizen)
        resume_tag = f"{key}:{cancelled.msg_id}" if cancelled is not None else ""
        if not resume_tag or resume_tag in _RESUMED:
            return  # le alternative per questo annullamento sono già partite
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
        text = _message(chat, "partial")
        if not _exact(chat, agent, text):
            await _say(city, citizen, text)
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
            if resume_tag:
                _RESUMED.add(resume_tag)
                if len(_RESUMED) > 5000:
                    _RESUMED.clear()
            if replacing:
                sent.again.update(slot["slot_id"] for slot in picked)
        return
    if taken and not replacing and not resume:
        return
    # Nessun orario libero: lo dico chiaramente e propongo la lista d'attesa (alternativa).
    offer_id = f"wl-{key}"
    if offer_id in sent.offers or _we(chat, agent, f"offer_id={offer_id}", to=citizen, word="offer"):
        return
    if tone == "en":
        prose = (
            "I looked carefully, but right now the family counselling centre has no free time "
            "that suits you. The alternative I can propose is the waiting list: no referral is "
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
        if resume_tag:
            _RESUMED.add(resume_tag)


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
        intro += " (direct access: no referral is needed):"
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
    """Who asked us to book this slot, the permission, and the referral, if we have them."""

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
        city, message.need_id, fresh, "committed", f"hold_id={hold_id}", op="commit",
        alternate_tokens=(f"slot_id={slot}", f"booking_id={hold_id}"),
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
    # Il fence è quello della riga held che ha creato questo hold (il Ledger lo chiama fence o
    # simile). Non lo indovino con un default: senza, il Ledger risponde STALE_FENCE.
    fence = next((value for key, value in fields.items() if "fence" in key.lower()), "")
    if not fence:
        _LOG.info("care.held.nofence", extra={"need_id": need[:48]})
    parts = ["commit", f"hold_id={hold_id}", f"consent_ref={consent}"]
    if fence:
        parts.append(f"fence={fence}")
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
            "3. No referral is needed; bring an identity document.",
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
                slot = fields.get("slot_id", "") or _hold_slot(chat, agent, fields.get("hold_id", ""))
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
        # Al cittadino niente codici interni del Ledger (es. STALE_FENCE): un motivo generico.
        shown = "facility_unavailable" if target.startswith("citizen:") else reason
        await _say(city, target, f"cancel offer_id={offer_id} reason={shown}\n{prose}")
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

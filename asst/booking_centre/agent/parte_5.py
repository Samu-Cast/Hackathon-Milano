# booking_centre: parte_5.py di 5 (vedi agent.py)
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
        if "need.request" in (line_heads(row.text) | words(row.text)) or _number_in(row.text):
            continue  # una richiesta ripetuta non è una risposta al consenso
        if _consent_question(row.text) or _affirmative(row.text):
            return False  # una domanda o un sì a parole non sono un rifiuto
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
    await _no_slot_fallback(
        city, chat, key, citizen, number, reply.get("procedure"),
        (reply.get("priority") or "").upper() or None, tone, note=tr("cross_declined", tone),
        allow_partner=False,
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
    prose = tr("unreachable", tone)
    await _say(
        city,
        citizen,
        f"cancel offer_id={offer_id} reason=facility_unreachable facility={facility}\n" + prose,
    )
    try:
        fresh = await _case(city, message.need_id)
    except CapError:
        fresh = chat
    await _advance(city, message, fresh, key, replacing=True)


_CANCEL_WORDS = re.compile(
    r"\b(annull\w*|disdic\w*|disdett\w*|cancell\w*|rinunci\w*|non mi serve più|non mi serve piu|"
    r"non ho più bisogno|non ho piu bisogno|cancel\w*|call off|no longer need|"
    r"anul\w*|cancelar\w*|ya no (lo )?necesito|annuler|annulez|je n'en ai plus besoin|"
    r"desmarcar|já não preciso|stornier\w*|absagen|sage ab|brauche nicht mehr|"
    r"renunț\w*|nu mai am nevoie)\b|إلغاء|ألغ|取消|скасу",
    re.IGNORECASE,
)


def _cancel_intent(text: str) -> bool:
    if "cancel" in line_heads(text):
        return True
    return bool(_CANCEL_WORDS.search(text))


def _citizen_cancelled(key: str) -> set[str]:
    return {flag.split(":", 1)[1] for flag in _sent(key).flags if flag.startswith("cancelled:")}


async def _on_citizen_cancel(city: City, message: Message, chat: list[Message], key: str) -> None:
    agent = city.agent_id
    citizen = message.sender
    tone = _tone(chat)
    sent = _sent(key)
    offers = _our_offers(chat, agent, citizen)
    current = _current_offer_ids(chat, agent, citizen)
    offer_id = _chosen_offer(message.text, offers) or _chosen_by_words(message.text, offers, current)
    if not offer_id:
        # La scelta più recente girata a una struttura, altrimenti l'ultima offerta in tavola.
        for row in _ours(chat, agent):
            if "confirm" in line_heads(row.text) and row.recipient != citizen:
                offer_id = fields_of(row.text).get("offer_id", offer_id)
        offer_id = offer_id or (current[-1] if current else None)
    offer = offers.get(offer_id or "", {})
    if offer.get("waiting_list", "").lower() == "true":
        sent.flags.add(f"cancelled:{offer_id}")
        await _once(city, key, f"wl-left:{offer_id}", citizen, tr("wl_left", tone))
        return
    slot = offer.get("slot_id", "")
    if slot:
        sent.flags.add(f"cancelled:{slot}")
        target = _confirmed_slot(chat, agent, slot)
        if target and f"released:{slot}" not in sent.lines:
            sent.lines.add(f"released:{slot}")
            booking = ""
            for row in _ours(chat, agent, citizen):
                if "booking.receipt" in line_heads(row.text):
                    booking = fields_of(row.text).get("booking_id", booking)
            parts = ["cancel", f"offer_id={offer_id}", f"slot_id={slot}", f"citizen={citizen}",
                     "reason=citizen_cancelled"]
            if booking:
                parts.append(f"booking_id={booking}")
            await _say(
                city, target,
                " ".join(parts) + "\nBuongiorno, il cittadino ha rinunciato a questo posto: "
                "potete liberarlo per altri. Grazie!",
            )
    ack = tr("citizen_cancel", tone)
    if not await _once(city, key, f"citizen-cancel:{offer_id or message.msg_id}", citizen, ack):
        return
    # Alternativa: altre date reali (senza quella annullata), se la visita serve ancora.
    number = _number(chat)
    status, reply = _prescription_ok(chat, number) if number else ("", {})
    if status != "ok" or (reply.get("priority") or "").upper() == "P":
        return
    agenda = _ledger(chat, "agenda") or {}
    now = _when(agenda.get("agenda_now") or agenda.get("now") or "")
    lines, _, _ = _world_lines(chat)
    if now is None or not lines:
        return
    priority = (reply.get("priority") or "").upper() or None
    procedure = reply.get("procedure")
    options = fitting(
        lines, now=now, priority=priority, lang=_lang(chat), procedure=procedure,
        skip=_citizen_cancelled(key) | _cancelled_slots(chat, agent, citizen),
        skip_facilities=_unreachable(chat, agent),
    )[:_MAX_OFFERS]
    if not options:
        return
    ids = [f"of-{slot['slot_id']}" for slot in options]
    text = _offers_text(options, number=number, priority=priority, procedure=procedure, tone=tone,
                        intro=tr("citizen_cancel_more", tone), ids=ids)
    await _once(city, key, f"after-cancel:{offer_id}", citizen, text)


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


_PLACES: dict[str, tuple[str, float, float] | None] = {}
# Centro approssimativo dei 9 Municipi di Milano (lat, lon): serve solo a dare una distanza
# indicativa senza chiedere l'indirizzo al cittadino (privacy by design).
_MUNICIPI = {
    1: (45.4642, 9.1900), 2: (45.5000, 9.2150), 3: (45.4850, 9.2400), 4: (45.4500, 9.2350),
    5: (45.4300, 9.1900), 6: (45.4450, 9.1450), 7: (45.4650, 9.1200), 8: (45.4950, 9.1500),
    9: (45.5100, 9.1850),
}
_MOBILITY_NEED = re.compile(
    r"carrozzina|sedia a rotelle|mobilità ridotta|mobilita ridotta|deambul|stampell|girello|"
    r"non cammino|fatico a camminare|wheelchair|reduced mobility|walking aid|crutches|"
    r"silla de ruedas|movilidad reducida|fauteuil roulant|mobilité réduite|cadeira de rodas|"
    r"mobilidade reduzida|rollstuhl|gehbehinder|rollator|scaun cu rotile|mobilitate redusă|"
    r"كرسي متحرك|轮椅|行动不便|крісл\w* колісн|інвалідн\w* візк",
    re.IGNORECASE,
)


async def _facility_place(city: City, facility: str) -> tuple[str, float, float] | None:
    """Indirizzo e coordinate della struttura, dalla sua card pubblica sull'Index."""

    if facility in _PLACES:
        return _PLACES[facility]
    place = None
    try:
        for peer in await city.find(facility):
            if peer.agent_id == facility and peer.address is not None:
                place = (peer.address.label, peer.address.lat, peer.address.lon)
                break
    except CapError:
        place = None
    _PLACES[facility] = place
    return place


async def _facility_address(city: City, facility: str) -> str:
    place = await _facility_place(city, facility)
    return place[0] if place else ""


def _district(chat: list[Message]) -> int | None:
    for row in _from_citizen(chat):
        fields = fields_of(row.text)
        raw = fields.get("area.district") or fields.get("district") or fields.get("municipio") or ""
        if raw.isdigit() and 1 <= int(raw) <= 9:
            return int(raw)
    return None


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    import math

    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def _mobility_need(chat: list[Message]) -> bool:
    """Il cittadino ha detto LUI di avere difficoltà motorie (non lo chiediamo mai)."""

    return any(_MOBILITY_NEED.search(row.text) for row in _from_citizen(chat)) or any(
        fields_of(row.text).get("disability", "").lower() == "motor" for row in _from_citizen(chat)
    )


def _wheelchair_ok(chat: list[Message], facility_label: str) -> bool | None:
    """Dal registro (world): la struttura è accessibile in carrozzina? None se non lo dice."""

    for line in _world_lines(chat)[0]:
        fields = fields_of(line)
        name = fields.get("facility") or fields.get("facility_id") or fields.get("name") or ""
        if "slot_id" in fields or not name or (facility_label and name != facility_label):
            continue
        lowered = line.lower()
        if any(word in lowered for word in ("carrozzina", "wheelchair", "step_free", "accessible=true", "accessibility=")):
            return "false" not in lowered and "no_" not in lowered
    return None


async def _forward_receipt(city: City, message: Message, chat: list[Message], key: str) -> None:
    """Giro al cittadino solo una ricevuta vera (booking_id e ledger_ref dalla struttura)."""

    fields = fields_of(message.text)
    booking_id = fields.get("booking_id", "")
    ledger_ref = fields.get("ledger_ref", "")
    citizen = _citizen(chat)
    if not booking_id or not ledger_ref or not citizen:
        return
    if not _confirmed_to(chat, city.agent_id, message.sender):
        return  # una ricevuta vale solo dalla struttura a cui abbiamo mandato il confirm
    if _we(chat, city.agent_id, f"booking_id={booking_id}", to=citizen, head="booking.receipt"):
        return
    tone = _tone(chat)
    offer = _confirmed_offer(chat, city.agent_id, citizen, message.sender)
    if offer.get("slot_id") and offer["slot_id"] in _citizen_cancelled(key):
        return  # il cittadino ha già rinunciato: la struttura ha ricevuto il nostro cancel
    when = _when_text(offer.get("agenda_date", ""), tone) if offer else ""
    place = await _facility_place(city, message.sender)
    address = place[0] if place else ""
    at = _when(offer.get("agenda_date", "")) if offer else None
    window = ""
    if at is not None:
        window = "08:00–13:00" if at.hour < 13 else "13:00–18:00"
    head = f"booking.receipt booking_id={booking_id} ledger_ref={ledger_ref}"
    lines = [tr("receipt_head", tone), head]
    if when:
        lines.append(tr("r_when", tone, when=when))
    if address:
        lines.append(tr("r_where", tone, address=address))
    lines += [
        tr("r_next", tone),
        tr("r_step1", tone),
        tr("r_step2", tone, window=tr("r_window", tone, window=window) if window else ""),
        tr("r_step3", tone),
        tr("r_privacy", tone),
    ]
    # Mobilità: distanza indicativa dal Municipio del cittadino (senza chiedergli l'indirizzo).
    district = _district(chat)
    if place and district in _MUNICIPI:
        km = _km(_MUNICIPI[district], (place[1], place[2]))
        lines.append(tr("distance", tone, km=f"{km:.0f}" if km >= 2 else f"{km:.1f}"))
    # Accessibilità: solo se il cittadino ha detto lui di averne bisogno, e solo dal registro.
    if _mobility_need(chat):
        known = _wheelchair_ok(chat, offer.get("facility", "") if offer else "")
        lines.append(tr("access_ok" if known else "access_unknown", tone))
    lines.append(tr("r_access", tone))
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
        if not offers and await _partner_said_no(city, message, chat, key):
            return
        if not offers and "cross" in _sent(key).flags:
            number = _number(chat)
            _, reply = _prescription_ok(chat, number) if number else ("", {})
            await _no_slot_fallback(
                city, chat, key, citizen, number, reply.get("procedure"),
                (reply.get("priority") or "").upper() or None, tone, note="",
            )
        return
    reason = (fields.get("reason") or "").lower()
    if "prescription" in reason or "PRESCRIPTION_EXPIRED" in message.text.upper():
        number = _number(chat) or ""
        await _prescription_problem(city, key, citizen, number, "expired", tone)
        return
    if not _we(chat, agent, f"offer_id={offer_id}", to=citizen, head="cancel"):
        prose = tr("facility_cancelled", tone)
        await _say(city, citizen, f"cancel offer_id={offer_id} reason=facility_cancelled\n" + prose)
    try:
        fresh = await _case(city, message.need_id)
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

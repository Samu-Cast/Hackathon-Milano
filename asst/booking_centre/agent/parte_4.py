# booking_centre: parte_4.py di 4 (vedi agent.py)
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
        prose = tr("wl_done", tone)
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
        prose = tr("cross_choice", tone)
        await _say(city, citizen, f"consent purpose=cross_asst offer_id={offer_id}\n" + prose)
        return

    # Sicurezza e fiducia: un consenso non si indovina mai. Se il cittadino non l'ha dato lo
    # chiedo (sotto); uso solo riferimenti puliti, senza spazi o caratteri strani.
    if booking_ref and not _CLEAN_REF.fullmatch(booking_ref):
        booking_ref = None
    if cross_ref and not _CLEAN_REF.fullmatch(cross_ref):
        cross_ref = None
    if not booking_ref and not cross_ref:
        if not _we(chat, agent, "purpose=booking", to=citizen, head="consent"):
            prose = tr("booking_consent", tone)
            await _say(city, citizen, f"consent purpose=booking offer_id={offer_id}\n" + prose)
        elif _declined_after(chat, agent, citizen, "booking"):
            prose = tr("booking_declined", tone)
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
    prose = tr("forwarded", tone, when=when)
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
        note=tr("cross_declined", tone),
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
        tr("r_access", tone),
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
        prose = tr("facility_cancelled", tone)
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

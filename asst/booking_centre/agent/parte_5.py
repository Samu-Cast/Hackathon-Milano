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
    now = _agenda_now(chat)
    lines, _, _ = _world_lines(chat)
    if now is None or not lines:
        return
    priority = (reply.get("priority") or "").upper() or None
    procedure = reply.get("procedure")
    options = fitting(
        lines, now=now, priority=priority, lang=_lang(chat), procedure=procedure,
        skip=_citizen_cancelled(key) | _cancelled_slots(chat, agent, citizen),
        skip_facilities=_unreachable(chat, agent), anchor=_anchor(chat),
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
    for row in reversed(chat):  # ricevuta che la struttura ha già dato al paziente (notified=citizen)
        if row.sender.startswith("agent:") and "booking.receipt" in line_heads(row.text) and "notified=citizen" in row.text.split():
            fields = fields_of(row.text)
            return f"booking.receipt booking_id={fields.get('booking_id', '')} ledger_ref={fields.get('ledger_ref', '')}"
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
    if "notified=citizen" in message.text.split():
        return  # la struttura l'ha già data al paziente: niente doppione
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


# --- ora dell'agenda "invecchiata" (21/11, dai percorsi in console) ----------------------------
# La risposta "agenda" del Ledger resta nella storia del caso e viene riusata per tutto il caso.
# L'agenda corre 60x: una risposta vecchia di 5 minuti reali è indietro di 5 ore, e il CUP
# offriva slot già passati (reason=past, il rifiuto più frequente). Sommo il tempo reale passato
# da quella risposta, moltiplicato per il ritmo dell'agenda. Senza sent_at resta com'era.
_AGENDA_RATE = 60.0
_AGENDA_MAX_AGE = timedelta(hours=24)


def _agenda_now(chat: list[Message]) -> datetime | None:
    row = None
    for item in chat:
        if item.sender == LEDGER and first_word(item.text) == "agenda":
            row = item
    if row is None:
        return None
    fields = fields_of(row.text)
    now = _when(fields.get("agenda_now") or fields.get("now") or fields.get("at") or "")
    if now is None:
        return None
    try:
        sent = datetime.fromisoformat((getattr(row, "sent_at", "") or "").replace("Z", "+00:00"))
    except ValueError:
        return now
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=timezone.utc)
    real = datetime.now(timezone.utc) - sent
    if real <= timedelta(0):
        return now
    return now + min(real * _AGENDA_RATE, _AGENDA_MAX_AGE)


# --- primo giro unico col Ledger (21/11, dai messaggi in console) -------------------------------
# Ogni andata e ritorno col Ledger costa ~7 minuti reali (~7 ore di agenda). Il CUP faceva 3-4 giri
# in fila (ricetta, poi pagina 1, poi pagina 4...) e il cittadino aspettava 15-25 minuti. Al primo
# contatto chiedo subito anche le prime pagine del catalogo (in ordine di data: ~2 giorni a
# pagina), così le offerte partono dopo un giro solo. Solo per la fisiatria: è l'unico servizio con
# una procedura sola, e le risposte "slots" non dicono a quale procedura appartengono.
_PREFETCH = {"hospital.physiatry": "VIS-FIS"}
_PREFETCH_PAGES = (1,)  # 21/11 pomeriggio: il catalogo ha una pagina sola (page=1/1); la 2 era sempre SCHEMA_INVALID


async def _prefetch_world(city: City, chat: list[Message], sent) -> None:
    if _has_world(chat):
        return
    service = next(
        (fields_of(row.text).get("service_type", "") for row in _from_citizen(chat) if "service_type=" in row.text),
        "",
    )
    procedure = _PREFETCH.get(service)
    if not procedure or f"prefetch:{procedure}" in sent.lines or _world_fresh(procedure):
        return  # catalogo già in memoria: lo riceve il caso dopo la ricetta (_with_world)
    sent.lines.add(f"prefetch:{procedure}")
    for page in _PREFETCH_PAGES:
        await _say(city, LEDGER, f"world procedure={procedure} page={page}")


async def _prefetch_pages(city: City, chat: list[Message], sent, procedure: str | None) -> None:
    """Con la procedura nota (dalla ricetta) e nessuna pagina ancora letta: pagine 2-3 insieme alla 1,
    in un giro solo, invece di una alla volta (vale anche per la cardiologia)."""

    if not procedure or _has_world(chat) or f"prefetch:{procedure}" in sent.lines:
        return
    sent.lines.add(f"prefetch:{procedure}")
    asked = {row.text.strip() for row in _ours(chat, city.agent_id, LEDGER)}
    for page in _PREFETCH_PAGES[1:]:  # la pagina 1 la chiede subito dopo il ciclo di _advance
        ask = f"world procedure={procedure} page={page}"
        if ask not in asked:
            await _say(city, LEDGER, ask)


# --- stesso slot a più cittadini (21/11, report di ospedale e specialista) ----------------------
# Ogni cittadino riceveva gli slot più vicini: alle 11:37 cinque confirm per lo stesso slot h2 in
# 4 secondi, vince solo il primo (gli altri SLOT_TAKEN). Uno slot già confermato per un altro caso
# non lo offro più; quelli appena offerti ad altri li metto in fondo (mai tolti: restano se servono).
_SLOT_CONFIRMED: dict[str, tuple[str, datetime]] = {}
_SLOT_OFFERED: dict[str, tuple[str, datetime]] = {}
_CONFIRMED_TTL = timedelta(minutes=30)
_OFFERED_TTL = timedelta(minutes=10)


def _recent(table: dict[str, tuple[str, datetime]], key: str, ttl: timedelta) -> set[str]:
    now = datetime.now(timezone.utc)
    for slot in [s for s, (_, at) in table.items() if now - at > ttl]:
        table.pop(slot, None)
    return {slot for slot, (owner, _) in table.items() if owner != key}


def _spread(slots: list[dict[str, str]], key: str) -> list[dict[str, str]]:
    taken = _recent(_SLOT_CONFIRMED, key, _CONFIRMED_TTL)
    busy = _recent(_SLOT_OFFERED, key, _OFFERED_TTL)
    free = [slot for slot in slots if slot.get("slot_id") not in taken]
    return sorted(free, key=lambda slot: slot.get("slot_id") in busy)  # stabile: l'ordine resta


def _mark_offered(picked: list[dict[str, str]], key: str, sent) -> None:
    sent.offers.update(slot["slot_id"] for slot in picked)
    now = datetime.now(timezone.utc)
    for slot in picked:
        _SLOT_OFFERED.setdefault(slot["slot_id"], (key, now))


async def _confirm_target(city: City, facility: str, procedure: str | None, slot: str, key: str) -> str:
    _SLOT_CONFIRMED[slot] = (key, datetime.now(timezone.utc))
    return await _facility_agent(city, facility, procedure)


# --- il ciclo col cittadino non si ferma (21/11) -------------------------------------------------
# Il CUP chiudeva con "cancel reason=released" quando non trovava altre date. Il ciclo finisce solo
# se il cittadino prenota o annulla lui: qui dico che continuo a cercare e lascio la porta aperta.
# Un solo messaggio per giro di rifiuti (chiave = numero di offerte già mandate).
_KEEP_SEARCHING = {
    "it": "Al momento non vedo altre date libere adatte: continuo a cercare e le scrivo non appena se ne libera una. Se preferisce può annullare la richiesta in qualsiasi momento, oppure indicarmi altri giorni.",
    "en": "I do not see other suitable free dates right now: I am still searching and I will write to you as soon as one opens up. You can drop the request at any time, or tell me other days that suit you.",
    "es": "Ahora mismo no veo otras fechas libres adecuadas: sigo buscando y le escribo en cuanto haya una. Puede anular la solicitud cuando quiera o indicarme otros días.",
    "fr": "Je ne vois pas d'autres dates libres adaptées pour le moment : je continue à chercher et je vous écris dès qu'une se libère. Vous pouvez annuler la demande à tout moment ou m'indiquer d'autres jours.",
    "pt": "De momento não vejo outras datas livres adequadas: continuo a procurar e escrevo-lhe assim que surgir uma. Pode anular o pedido a qualquer momento ou indicar-me outros dias.",
    "de": "Im Moment sehe ich keine weiteren passenden freien Termine: Ich suche weiter und schreibe Ihnen, sobald einer frei wird. Sie können die Anfrage jederzeit abbrechen oder mir andere Tage nennen.",
}


async def _keep_searching(city: City, key: str, citizen: str, tone: str, round_no: int) -> None:
    await _once(city, key, f"keep-searching:{round_no}", citizen, _KEEP_SEARCHING.get(tone, _KEEP_SEARCHING["en"]))


# --- ASST partner solo per la lingua rara (21/11, coda del CUP) ---------------------------------
# Il CUP elaborava ~11 messaggi al minuto e ne riceveva ~32: metà della coda erano richieste,
# offerte e "ack" delle partner, che da stamattina non hanno dato nessuna prenotazione (0 consensi
# cross_asst su 24, slot quasi sempre oltre la scadenza). Chiedo a UNA partner solo se è l'ASST
# della lingua rara del cittadino; altrimenti si passa subito all'alternativa reale (date vere).
_ASKED_PARTNERS: set[str] = set()


def _lang_partners(peers: list, lang: str) -> list:
    chosen = [peer for peer in _ranked_partners(peers, lang) if _partner_rank(peer.agent_id, lang) == 0][:1]
    _ASKED_PARTNERS.update(_bare_id(peer.agent_id) for peer in chosen)
    return chosen


def _unasked_offer(message: Message) -> bool:
    """Offerta da un agente a cui questo processo non ha chiesto posti (21/11: agent:pky3vqyqbepa
    rimandava le stesse offerte a ogni nostro "ricevuto", 198 messaggi in coda davanti ai cittadini)."""

    return (
        message.sender.startswith("agent:")
        and first_word(message.text or "") == "offer"
        and _bare_id(message.sender) not in _ASKED_PARTNERS
        and message.sender not in _team_facilities().values()
    )


# --- Ledger lento (21/11 pomeriggio): agenda e rules condivise tra i cittadini ------------------
# Ogni giro col Ledger costa minuti e il CUP chiedeva agenda e rules per OGNI cittadino: 4
# richieste a paziente intasavano il Ledger per tutti. Agenda e rules sono le stesse per tutta
# l'ASST: tengo l'ultima risposta (con sent_at, così _agenda_now la porta avanti col ritmo
# dell'agenda) e la metto in testa alla storia di ogni caso. Rinfresco solo dopo _SHARED_TTL.
_SHARED: dict[str, tuple[datetime, Message]] = {}
_SHARED_TTL = timedelta(minutes=3)


def _with_shared_ledger(message: Message, chat: list[Message]) -> list[Message]:
    if message.sender == LEDGER and first_word(message.text or "") in {"agenda", "rules"}:
        _SHARED[first_word(message.text)] = (datetime.now(timezone.utc), message)
    if message.sender == LEDGER and first_word(message.text or "") in {"slots", "world"}:
        _remember_world(message, chat)
    have = {row.msg_id for row in chat}
    extra = [row for _, row in _SHARED.values() if row.msg_id not in have]
    return [*extra, *chat] if extra else chat


# In gara il Relay consegna < 1 messaggio al minuto: le risposte del Ledger restano in coda e la
# cache non si riempie. Basta averle CHIESTE da poco (per qualunque paziente) per non richiederle:
# ogni richiesta torna come messaggio in coda davanti ai cittadini.
_ASKED_AT: dict[str, datetime] = {}
_RULES_EVERY = 60.0  # dopo un rifiuto le regole si rileggono al massimo una volta al minuto


def _shared_fresh(head: str, seconds: float | None = None) -> bool:
    ttl = _SHARED_TTL if seconds is None else timedelta(seconds=seconds)
    now = datetime.now(timezone.utc)
    item = _SHARED.get(head)
    asked = _ASKED_AT.get(head)
    return (item is not None and now - item[0] < ttl) or (asked is not None and now - asked < ttl)


async def _holding_reply(city: City, chat: list[Message], sent, citizen: str | None = None) -> None:
    """Primo contatto: il Ledger risponde dopo minuti. Il paziente riceve subito una risposta
    (rubrica: un messaggio lasciato senza risposta costa -1 a ogni agente del percorso)."""

    citizen = citizen or _citizen(chat)
    if not citizen or "holding-reply" in sent.lines:
        _LOG.info("booking.holding.skip", extra={"citizen": bool(citizen)})
        return
    sent.lines.add("holding-reply")
    text = ("Grazie, ho ricevuto il suo messaggio e me ne occupo subito: controllo la ricetta sul "
            "registro e le propongo le date disponibili appena ho la risposta."
            if _tone(chat) == "it" else
            "Thank you, I have your message and I am on it: I am checking the register and I will "
            "send you the available dates as soon as I have the answer.")
    await _say(city, citizen, text)


async def _declined_one(city: City, key: str, message: Message, remaining: dict[str, dict[str, str]], tone: str) -> None:
    """Il cittadino rifiuta un'offerta e altre restano aperte: rispondo subito (un messaggio senza
    risposta costa -1 a ogni agente del percorso), elencando solo le offerte ancora valide."""

    refused = fields_of(message.text).get("offer_id") or message.msg_id
    listed = [
        f"- {_when_text(offer.get('agenda_date', ''), tone)}: offer_id={offer_id}"
        + (f" slot_id={offer['slot_id']}" if offer.get("slot_id") else "")
        for offer_id, offer in remaining.items()
    ]
    head = ("Va bene, tolgo questa data. Restano queste opzioni; se nessuna va bene me lo dica e "
            "cerco altre date:" if tone == "it" else
            "All right, I have removed that date. These options are still open; if none of them works, "
            "just tell me and I will look for other dates:")
    await _once(city, key, f"declined-one:{refused}", message.sender, head + "\n" + "\n".join(listed))


async def _ledger_busy(city: City, key: str, latest: Message | None, tone: str) -> None:
    """Il Ledger non ha risposto entro ~1 s (_PULL_TRIES): al paziente dico che al momento il
    registro è occupato, invece di lasciarlo senza risposta. Quando il Ledger risponde, il caso
    riparte da solo (la risposta arriva come turno a sé) e gli mando le date."""

    if latest is None or not latest.sender.startswith("citizen:"):
        return
    text = ("In questo momento il registro è molto occupato e non riesco a risponderle subito. "
            "Ho preso in carico la sua richiesta e le scrivo appena ho i dati; se preferisce può "
            "riscrivermi tra qualche minuto." if tone == "it" else
            "The register is very busy right now and I cannot answer you straight away. I have taken "
            "your request and I will write to you as soon as I have the data; you can also write to me "
            "again in a few minutes.")
    await _once(city, key, f"busy:{latest.msg_id}", latest.sender, text)


async def _agenda_for_case(city: City, chat: list[Message], sent, agent: str) -> None:
    """Il caso non ha l'ora di agenda: la chiedo nel SUO need, così la risposta lo fa ripartire."""

    if "agenda-case" in sent.lines or any(row.text.strip() == "agenda" for row in _ours(chat, agent, LEDGER)):
        return
    sent.lines.add("agenda-case")
    await _say(city, LEDGER, "agenda")


# --- catalogo condiviso tra i pazienti (21/11 pomeriggio) ---------------------------------------
# Ogni paziente chiedeva il suo world (e una pagina 2 che non esiste): 58 richieste in 15 minuti,
# ognuna una risposta in coda davanti ai cittadini. La pagina è la stessa per tutti quelli con la
# stessa prestazione: la tengo per prestazione e la do al caso DOPO la ricetta (prestazione nota).
_WORLD: dict[str, tuple[datetime, dict[str, Message]]] = {}
_WORLD_TTL = timedelta(minutes=10)
_WORLD_REFRESH = timedelta(minutes=2)


def _ask_key(text: str) -> str:
    if text.startswith("world "):
        return "world:" + fields_of(text).get("procedure", "")
    return text.strip()


def _remember_world(message: Message, chat: list[Message]) -> None:
    head = fields_of((message.text or "").splitlines()[0] if message.text else "")
    page = head.get("page", "1/1").split("/", 1)[0] or "1"
    procedure = ""
    for row in reversed(chat):  # la prestazione della richiesta fatta in questo caso
        if row.recipient == LEDGER and row.text.startswith("world ") and row.sender != LEDGER:
            asked = fields_of(row.text)
            if asked.get("page", "1") == page:
                procedure = asked.get("procedure", "")
                break
    if not procedure:
        procedure = next((fields_of(line).get("procedure", "") for line in (message.text or "").splitlines()
                          if "slot_id=" in line), "")
    if not procedure:
        return
    _, pages = _WORLD.get(procedure, (None, {}))
    pages = dict(pages)
    pages[page] = message
    _WORLD[procedure] = (datetime.now(timezone.utc), pages)


def _world_fresh(procedure: str | None) -> bool:
    item = _WORLD.get(procedure or "")
    return item is not None and datetime.now(timezone.utc) - item[0] < _WORLD_TTL


async def _with_world(city: City, chat: list[Message], procedure: str | None) -> list[Message]:
    """Il caso riceve le pagine del catalogo già lette per la sua prestazione. Se sono vecchie di
    più di _WORLD_REFRESH chiedo una pagina nuova (una per tutti), senza aspettarla."""

    if not procedure or not _world_fresh(procedure):
        return chat
    stamp, pages = _WORLD[procedure]
    now = datetime.now(timezone.utc)
    asked = _ASKED_AT.get("world:" + procedure)
    if now - stamp > _WORLD_REFRESH and (asked is None or now - asked > _WORLD_REFRESH):
        await _say(city, LEDGER, f"world procedure={procedure} page=1")
    have = {row.msg_id for row in chat}
    extra = [row for row in pages.values() if row.msg_id not in have]
    return [*extra, *chat] if extra else chat


def _with_shared(rows: list[Message]) -> list[Message]:
    """Ogni lettura della storia del caso (anche dentro _until) porta con sé agenda e rules già
    lette per altri pazienti e il catalogo della prestazione del caso, se la ricetta è nota.
    Prima solo l'inizio del turno le aveva: le attese le perdevano e il caso richiedeva tutto."""

    have = {row.msg_id for row in rows}
    extra = [row for _, row in _SHARED.values() if row.msg_id not in have]
    procedure = ""
    for row in rows:
        if row.sender == LEDGER and first_word(row.text) == "prescription":
            procedure = fields_of(row.text).get("procedure", "") or procedure
    if procedure and _world_fresh(procedure):
        extra += [row for row in _WORLD[procedure][1].values() if row.msg_id not in have]
    return [*extra, *rows] if extra else rows


# --- prima risposta con le date (21/11 pomeriggio) ----------------------------------------------
# La coda del Relay costa ~13 minuti a giro: aspettare la verifica della ricetta prima di proporre
# le date faceva scadere i percorsi (rubrica: -1 no_booking). Se il paziente nomina un disturbo
# chiaro, prestazione e priorità sono quelle della tabella del medico (stesse parole chiave del GP):
# propongo subito le date, con il catalogo già in memoria. Hold e commit restano controllati dal
# Ledger; con la regola P1 attiva (scadenza da leggere sul Ledger) o un controllo (P) si aspetta.
_PROVISIONAL = (
    ("VIS-FIS", "B", ("mal di schiena", "back pain", "dolore alla schiena", "dolori alla schiena",
                      "male alla schiena", "schiena mi fa male", "mi fa male la schiena", "lombalgia",
                      "backache", "my back hurts", "lower back", "schiena a pezzi", "male schiena",
                      "dolore schiena", "schiena fa male", "fa male schiena", "schiena bloccata",
                      "colpo della strega", "dolore lombare", "mal di reni", "back is killing me",
                      "back hurts", "my back")),
    ("VIS-FIS", "D", ("ginocchio", "ginocchia", "knee")),
    ("VIS-CARD", "B", ("cuore che batte", "heart racing", "palpitazion", "batticuore", "cuore batte",
                       "cuore accelerato", "heart is racing", "heart pounding", "heart is pounding",
                       "palpitation", "heart beating fast", "cuore mi batte", "cuore che mi batte",
                       "batte fortissimo", "batte all'impazzata", "tachicardia", "cuore in gola",
                       "cuore impazzito", "heart is beating", "heart beats fast")),
)
_NOT_PROVISIONAL = ("controllo", "check-up", "checkup", "heart check", "routine")
_SERVICE_PROCEDURE = {"hospital.physiatry": "VIS-FIS", "specialist_care.cardiology": "VIS-CARD"}


def _prescription_or_provisional(chat: list[Message], number: str) -> tuple[str, dict[str, str]]:
    status, reply = _prescription_ok(chat, number)
    if status != "pending" or _p1_active(chat) or _agenda_now(chat) is None:
        return status, reply
    rows = _from_citizen(chat)
    text = " ".join((row.text or "").lower().replace("\u2019", "'") for row in rows)
    if not text or any(word in text for word in _NOT_PROVISIONAL):
        return status, reply
    found = {(proc, prio) for proc, prio, words_ in _PROVISIONAL if any(w in text for w in words_)}
    if len(found) != 1:
        return status, reply
    procedure, priority = next(iter(found))
    service = next((fields_of(row.text).get("service_type", "") for row in rows if "service_type=" in row.text), "")
    if service and _SERVICE_PROCEDURE.get(service) not in (None, procedure):
        return status, reply
    if not _world_fresh(procedure):
        return status, reply
    citizen = _citizen(chat) or ""
    _LOG.info("booking.rx.provisional", extra={"procedure": procedure, "priority": priority})
    return "ok", {"procedure": procedure, "priority": priority, "patient": citizen,
                  "prescription_number": number, "provisional": "1"}


# --- catalogo vuoto (21/11 pomeriggio): l'alternativa è la lista d'attesa ------------------------
# Dal 5 gennaio di agenda il catalogo dell'ASST ha matching=0 per tutte le prestazioni: nessuna data
# esiste, né entro né oltre la scadenza. "Continuo a cercare" chiudeva il percorso in no_reply con
# -1 (no_alternative). Rubrica: S vuoto + alternativa offerta = 0. Con il catalogo vuoto per la
# prestazione l'alternativa è la lista d'attesa (testo wl_empty). Con anche un solo slot nel
# catalogo resta il comportamento di prima (date oltre la scadenza o continuo a cercare).


def _world_empty(procedure: str | None) -> bool:
    """True solo se il catalogo letto da poco per ``procedure`` non ha nessuno slot."""

    if not procedure or not _world_fresh(procedure):
        return False
    pages = list(_WORLD[procedure][1].values())
    if not pages:
        return False
    for row in pages:
        for line in (row.text or "").splitlines()[1:]:
            fields = fields_of(line)
            if fields.get("slot_id") and fields.get("procedure", procedure) == procedure:
                return False
    return True


async def _empty_world_alternative(
    city: City, chat: list[Message], key: str, citizen: str, number: str | None,
    procedure: str | None, priority: str | None, tone: str, note: str,
) -> None:
    if not _world_empty(procedure):
        await _keep_searching(city, key, citizen, tone, len(_our_offers(chat, city.agent_id, citizen)))
        return
    offer_id = f"wl-{key}"
    sent = _sent(key)
    if offer_id in sent.offers or _we(chat, city.agent_id, f"offer_id={offer_id}", to=citizen, head="offer"):
        return
    parts = ["offer", f"offer_id={offer_id}", "waiting_list=true"]
    if procedure:
        parts.append(f"procedure={procedure}")
    if priority:
        parts.append(f"priority={priority}")
    if number:
        parts.append(f"prescription_number={number}")
    prose = tr("wl_empty", tone, label=_label(procedure, tone)) + "\n" + tr("wl_steps", tone)
    if note:
        prose = note + "\n" + prose
    _LOG.info("booking.waiting_list.empty_world", extra={"procedure": procedure or ""})
    if await _say(city, citizen, prose + "\n" + " ".join(parts)):
        sent.offers.add(offer_id)

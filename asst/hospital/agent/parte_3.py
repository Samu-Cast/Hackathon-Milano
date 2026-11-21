# ospedale/specialista: parte_3.py (mittenti verificati, posti persi). Vedi agent.py.
from __future__ import annotations

import time

from city_sdk.city import LEDGER, CapError, City, Message

# --- mittenti verificati e posti persi (squadra epica) ----------------------------------------------

_LOST_HEADS = frozenset({"fault.cancel", "expired", "cancelled", "hold.expired"})
# Mittenti agent: che l'Index non dà come centro prenotazioni (livello 8): le loro conferme non
# rendono nessuno "primo" su uno slot e non ricevono ricevute.
_NOT_DESK: set[str] = set()
# Il centro prenotazioni della squadra epica (asst/booking_centre/card.yaml).
_OWN_DESKS = frozenset({"agent:jgy1dj2sw2ew"})
_NOT_CHECKIN = frozenset({"cancel", "confirm", "consent", "consent.grant", "consent.deny", "data.response"})


_ACKED: set[str] = set()


def _bare(agent_id: str) -> str:
    """L'id senza suffisso di versione (agent:x@1.0.3 -> agent:x), in minuscolo."""

    return (agent_id or "").split("@", 1)[0].strip().lower()


async def _owner_of(city: City, chat: list[Message], agent: str, slot: str) -> str:
    """Chi ha confermato per primo ``slot``, saltando i mittenti che l'Index non dà come centro
    prenotazioni: ricevute e cancel vanno solo a un CUP verificato (o al cittadino)."""

    for _ in range(4):
        target, _, _ = _booking_of(chat, agent, slot)
        if not target.startswith("agent:") or await _is_desk(city, target):
            return target
        _NOT_DESK.add(target)
    return ""


def _citizen_checkin(message: Message) -> bool:
    """Un cittadino che si presenta: check-in a parole, oppure mostra la ricevuta (booking_id)
    senza un altro atto. Rifiutare o ignorare un check-in valido costa -2."""

    if not message.sender.startswith("citizen:"):
        return False
    text = message.text or ""
    if _is_checkin(text):
        return True
    fields = fields_of(text)
    return (
        _valid_id(fields.get("booking_id"))
        and "consent_ref" not in fields
        and first_word(text) not in _NOT_CHECKIN
        and not {"cancel", "confirm"} & words(text)
    )


def _checkin_ask(chat: list[Message]) -> str:
    if _it(chat):
        return ("Benvenuto/a! Per registrare l'arrivo mi scrive il numero di prenotazione della "
                "ricevuta (booking_id=...)? Lo trova nel messaggio di conferma.")
    return ("Welcome! To register your arrival, please send me the booking number from your "
            "receipt (booking_id=...). You will find it in the confirmation message.")


async def _on_checkin_refused(city: City, message: Message, chat: list[Message]) -> None:
    """Il Ledger non registra il check-in: lo dico al cittadino con gentilezza, senza rifiutare."""

    citizen = _citizen(chat)
    if not citizen:
        return
    fields = fields_of(message.text)
    booking = fields.get("booking_id", "")
    code = fields.get("code", "")
    if _it(chat):
        text = (f"Non riesco a registrare l'arrivo in automatico{' (' + code + ')' if code else ''}"
                f"{' per la prenotazione ' + booking if booking else ''}: mostri la ricevuta allo "
                "sportello e il personale la registra subito.")
    else:
        text = (f"I cannot register your arrival automatically{' (' + code + ')' if code else ''}"
                f"{' for booking ' + booking if booking else ''}: please show your receipt at the "
                "desk and the staff will register you straight away.")
    if not _exact(chat, city.agent_id, text):
        await _say(city, citizen, text)


async def _is_desk(city: City, agent_id: str) -> bool:
    """Il mittente è un centro prenotazioni sull'Index? Se l'Index non ne mostra nessuno non
    posso verificare: non blocco il percorso legittimo (-2 per ricevuta rifiutata)."""

    # Il CUP della nostra ASST è sempre un centro prenotazioni: l'Index visto da una facility
    # può non restituirlo (in gara i suoi confirm venivano scartati -> facility_no_reply).
    if _bare(agent_id) in _OWN_DESKS:
        return True  # il nostro CUP vale sempre, anche con il suffisso @versione
    try:
        peers = await city.find("booking_centre")
    except CapError:
        return True
    desks = [p for p in peers if p.role == "booking_centre" or "booking_centre" in p.capabilities]
    if not desks:
        return True
    if any(_bare(p.agent_id) == _bare(agent_id) for p in desks):
        _NOT_DESK.discard(agent_id)
        return True
    return False


async def _on_lost(city: City, message: Message, chat: list[Message]) -> None:
    """Un posto non è più valido (fault.cancel o hold scaduto): cancel a chi aveva confermato."""

    agent = city.agent_id
    fields = fields_of(message.text)
    slot = fields.get("slot_id", "")
    booking = fields.get("booking_id") or fields.get("hold_id", "")
    if not slot and booking:
        slot = _hold_slot(chat, agent, booking)
        if not slot:
            for row in chat:
                if row.sender == LEDGER and f"booking_id={booking}" in row.text.split():
                    slot = fields_of(row.text).get("slot_id", slot)
    if not slot:
        return
    target = await _owner_of(city, chat, agent, slot)
    if not target:
        return
    offer_id = ""
    for row in chat:
        if f"slot_id={slot}" in row.text.split():
            offer_id = fields_of(row.text).get("offer_id", offer_id)
    if _we(chat, agent, f"slot_id={slot}", to=target, word="cancel"):
        return
    note = _nota(chat, "cancel") if target.startswith("citizen:") else ""
    await _say(city, target,
               f"cancel offer_id={offer_id or '-'} slot_id={slot} reason=fault_cancel"
               f"\nQuesto posto non è più valido: la prenotazione va rifatta nei tempi della ricetta.{note}")


async def _on_release(city: City, message: Message, chat: list[Message]) -> None:
    """Il centro prenotazioni libera un posto che aveva confermato con noi: lo rilascio sul Ledger
    (hold o prenotazione), senza ritentare. Solo se il posto è stato confermato da questo mittente."""

    agent = city.agent_id
    slot = fields_of(message.text).get("slot_id", "")
    if not slot:
        return
    target = await _owner_of(city, chat, agent, slot)
    if target != message.sender:
        return
    for row in chat:
        if row.sender == LEDGER and f"slot_id={slot}" in row.text.split() and first_word(row.text) == "held":
            hold_id = fields_of(row.text).get("hold_id", "")
            if hold_id and not _we(chat, agent, f"hold_id={hold_id}", to=LEDGER, word="cancel"):
                await _say(city, LEDGER, f"cancel hold_id={hold_id}")


def _slot_owner(chat: list[Message], agent: str, slot: str) -> str:
    """Il centro prenotazioni che per primo ha confermato ``slot`` con noi in questo caso."""

    if not slot:
        return ""
    for row in chat:
        if (
            row.sender.startswith("agent:")
            and row.sender != agent
            and row.sender not in _NOT_DESK
            and "confirm" in words(row.text)
            and fields_of(row.text).get("slot_id") == slot
        ):
            return row.sender
    return ""


def procedure_key(procedure: str) -> str:
    """La lettera dei medici che fanno la prestazione (h = fisiatria, s = cardiologia, c = consultorio)."""

    return {"VIS-FIS": "h", "VIS-CARD": "s"}.get(procedure, "c")


# --- cache del Ledger per le richieste partner (MACH-1) -----------------------------------------
# Il Ledger in gara risponde anche dopo minuti: tengo le ultime righe slots/agenda viste da questo
# processo e porto avanti l'agenda di 60 volte il tempo reale (rate della wave).
_SEEN: list[tuple[float, Message]] = []
_CACHE_AGE = 900.0
_REFRESH_AFTER = 180.0
_LAST_ASK = [0.0]


def _remember_ledger(message: Message) -> None:
    if first_word(message.text or "") in {"slots", "world", "agenda"}:
        _SEEN.append((time.monotonic(), message))
        del _SEEN[:-80]


def _cached_view() -> tuple[list[Message], datetime | None]:
    now_t = time.monotonic()
    rows = [m for t, m in _SEEN if now_t - t <= _CACHE_AGE]
    for t, m in reversed(_SEEN):
        if first_word(m.text) == "agenda" and now_t - t <= _CACHE_AGE:
            base = _when(fields_of(m.text).get("agenda_now", ""))
            if base is not None:
                return rows, base + timedelta(seconds=(now_t - t) * 60)
    return rows, None


async def _partner_view(
    city: City, message: Message, chat: list[Message], procedure: str
) -> tuple[list[Message], datetime | None, bool]:
    """Slot e ora di agenda per una richiesta partner: dalla cache se c'è, altrimenti dal Ledger."""

    agent = city.agent_id
    rows, now = _cached_view()
    if now is not None and any(first_word(m.text) == "slots" for m in rows):
        if time.monotonic() - _LAST_ASK[0] > _REFRESH_AFTER:
            _LAST_ASK[0] = time.monotonic()  # rinfresco senza aspettare: la risposta entra in cache
            await _say(city, LEDGER, "agenda")
            await _say(city, LEDGER, f"world procedure={procedure} page=1")
        return list(chat) + rows, now, True
    _LAST_ASK[0] = time.monotonic()
    if not _has_agenda(chat) and not _exact(chat, agent, "agenda"):
        await _say(city, LEDGER, "agenda")
    if not _asked(chat, agent, procedure, 1):
        await _say(city, LEDGER, f"world procedure={procedure} page=1")
    chat = await _until(
        city, message.need_id, chat,
        lambda rows: _has_agenda(rows) and _latest_slots(rows) is not None,
    )
    return chat, _when((_ledger(chat, "agenda") or {}).get("agenda_now", "")), False


# --- percorso rapido per il nostro CUP (in gara i confirm si chiudevano senza scrivere nulla) ------
# Non dipende da storia, Index, offerte o world: confirm -> hold; held -> commit; committed ->
# ricevuta. La mappa sta in memoria del processo; se manca (altro pod) resta il percorso normale.
_CUP_SLOTS: dict[str, dict[str, str]] = {}
_CUP_HOLDS: dict[str, str] = {}


def _code_of(err: BaseException) -> str:
    return str(getattr(err, "code", "") or type(err).__name__)[:60]


async def _fast_confirm(city: City, message: Message) -> bool:
    if _bare(message.sender) not in _OWN_DESKS:
        return False
    text = message.text or ""
    fields = fields_of(text)
    slot, consent = fields.get("slot_id", ""), fields.get("consent_ref", "")
    need = message.need_id or ""
    _LOG.warning("care.confirm.in", extra={"slot_id": slot[:64], "need_id": need[:48], "consent": consent[:80]})
    # In gara il testo consegnato può avere gli id oscurati ("cons-N-...-[omesso]-booking"): il
    # formato è fisso e il need_id dell'envelope è intatto, quindi li ricostruisco da lì.
    if not _valid_id(consent) and need.startswith("N-"):
        consent = f"cons-{need}-booking"
    if "confirm" not in words(text) or not _valid_id(slot) or not _valid_id(consent):
        _LOG.warning("care.confirm.skip", extra={"slot_id": slot[:64], "consent": consent[:80]})
        return False
    citizen, number = fields.get("citizen", ""), fields.get("prescription_number", "")
    if not _valid_id(number) and need.startswith("N-") and number.startswith("prescription_number"):
        number = f"prescription_number-{need}"
    known = _CUP_SLOTS.get(slot)
    if known is not None and known.get("citizen") == citizen and not known.get("cancel"):
        return True  # stesso confirm già tenuto da questo processo: niente secondo hold
    offer_id = fields.get("offer_id", "")
    cross = fields.get("cross_asst_consent_ref", "")
    _CUP_SLOTS[slot] = {"cup": message.sender, "citizen": citizen, "consent": consent,
                        "number": number, "offer_id": offer_id}
    parts = ["hold", f"slot_id={slot}"]
    if _valid_id(citizen):
        parts.append(f"citizen={citizen}")
    if _valid_id(number):
        parts.append(f"prescription_number={number}")
    if _valid_id(cross):
        parts += [f"consent_ref={cross}", f"cross_asst_consent_ref={cross}"]
    else:
        parts.append(f"consent_ref={consent}")
    try:
        await city.say(LEDGER, " ".join(parts))
    except Exception as err:  # noqa: BLE001 - 4xx: niente retry, il CUP lo sa subito
        code = _code_of(err)
        _LOG.warning("hold.fail", extra={"code": code, "slot_id": slot[:64]})
        head = f"offer_id={offer_id} " if offer_id else ""
        await _say(city, message.sender, f"cancel {head}slot_id={slot} reason={code} code={code}")
        return True
    await _say(city, message.sender, _ACK)
    return True


async def _fast_ledger(city: City, message: Message) -> bool:
    """held / committed / refused per uno slot del percorso rapido. False: percorso normale."""

    if message.sender != LEDGER:
        return False
    head = first_word(message.text or "")
    fields = fields_of(message.text or "")
    hold_id = fields.get("hold_id", "") or fields.get("booking_id", "")
    slot = fields.get("slot_id", "") or _CUP_HOLDS.get(hold_id, "")
    entry = _CUP_SLOTS.get(slot)
    if entry is None:
        return False
    cup, offer_id = entry["cup"], entry.get("offer_id", "")
    if head == "held" and hold_id:
        _CUP_HOLDS[hold_id] = slot
        if entry.get("commit"):
            return True
        entry["commit"] = hold_id
        parts = ["commit", f"hold_id={hold_id}", f"consent_ref={entry['consent']}", f"fence={fields.get('fence', '1')}"]
        if _valid_id(entry.get("number")):
            parts.append(f"prescription_number={entry['number']}")
        await _say(city, LEDGER, " ".join(parts))
        return True
    if head == "committed":
        booking, ref = fields.get("booking_id", ""), fields.get("ledger_ref", "")
        if not booking or not ref or entry.get("receipt"):
            return True
        entry["receipt"] = booking
        line = f"booking.receipt booking_id={booking} ledger_ref={ref} slot_id={slot}"
        line += f" offer_id={offer_id}" if offer_id else ""
        if not await _say(city, cup, line + " Prenotazione registrata e firmata sul Ledger, grazie."):
            citizen = entry.get("citizen", "")
            if citizen.startswith("citizen:"):
                await _say(city, citizen, line + " Prenotazione confermata: al check-in porti questo numero.")
        return True
    if head == "refused" and {"hold", "commit"} & words(message.text or ""):
        if entry.get("cancel"):
            return True
        entry["cancel"] = "1"
        code = fields.get("code", "") or "REFUSED"
        head_id = f"offer_id={offer_id} " if offer_id else ""
        await _say(city, cup, f"cancel {head_id}slot_id={slot} reason={code} code={code}")
        return True
    return False

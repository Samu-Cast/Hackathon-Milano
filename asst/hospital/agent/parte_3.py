# ospedale/specialista: parte_3.py (mittenti verificati, posti persi). Vedi agent.py.
from __future__ import annotations

from city_sdk.city import LEDGER, CapError, City, Message

# --- mittenti verificati e posti persi (squadra epica) ----------------------------------------------

_LOST_HEADS = frozenset({"fault.cancel", "expired", "cancelled", "hold.expired"})


async def _is_desk(city: City, agent_id: str) -> bool:
    """Il mittente è un centro prenotazioni sull'Index? Se l'Index non ne mostra nessuno non
    posso verificare: non blocco il percorso legittimo (-2 per ricevuta rifiutata)."""

    try:
        peers = await city.find("booking_centre")
    except CapError:
        return True
    desks = [p for p in peers if p.role == "booking_centre" or "booking_centre" in p.capabilities]
    if not desks:
        return True
    return any(p.agent_id == agent_id for p in desks)


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
    target, _, _ = _booking_of(chat, agent, slot)
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
    target, _, _ = _booking_of(chat, agent, slot)
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
            and "confirm" in words(row.text)
            and fields_of(row.text).get("slot_id") == slot
        ):
            return row.sender
    return ""

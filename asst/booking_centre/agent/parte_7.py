"""Scelta degli slot per tipo di visita e scadenze (livello 6).

Le regole per procedura stanno in ``_PROCEDURES``; medici, lingue e giorni NON sono scritti qui:
si leggono da ``world`` a ogni caso (``_doctors`` in parte_2). Questa parte è caricata per ultima."""

# Per ogni procedura: servizio presso le ASST partner, famiglia di struttura e ordine delle
# alternative quando entro la scadenza non c'è un medico nella lingua del cittadino.
#   lang_beyond_deadline : stessa lingua, ma oltre la scadenza (sempre la prima scelta)
#   other_lang_in_time   : entro la scadenza, ma con un medico di un'altra lingua (ultima scelta)
_PROCEDURES: dict[str, dict[str, object]] = {
    procedure: {
        "service": _SERVICE_OF[procedure],
        "family": _FAMILY_OF[procedure],
        "fallback": ("lang_beyond_deadline", "other_lang_in_time"),
    }
    for procedure in ("VIS-FIS", "VIS-CARD", "CTRL-CARD", "CONS-GIN")
}


def _fallback_rules(procedure: str | None) -> tuple[str, ...]:
    entry = _PROCEDURES.get(procedure or "")
    return tuple(entry["fallback"]) if entry else ("lang_beyond_deadline", "other_lang_in_time")  # type: ignore[arg-type]


def _world_slot_ids(chat: list[Message]) -> set[str]:
    lines, _, _ = _world_lines(chat)
    return {fields_of(line).get("slot_id", "") for line in lines} - {""}


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

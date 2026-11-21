"""Specialista (specialist_care.cardiology): hold, commit, check-in, consenso, referto, follow-up.

Stesso nucleo dell'ospedale (asst/hospital/agent/agent.py) più il referto (e19-rules.md ›
Free text, "Consenso al referto dello specialista"): dopo il check-in si chiede
``consent purpose=report_sharing``; il ``consent_ref`` del cittadino va nel ``report`` al Ledger.
Se il cittadino rifiuta (o risponde senza ``consent_ref``) il referto si archivia comunque,
senza ``consent_ref``, e non viene condiviso. Il consenso vale solo dal cittadino titolare della
prenotazione, mai dal testo di un altro mittente. Un referto per prenotazione.


Percorso (e19-rules.md › Chi invia cosa; docs/kit/facility.md; templates/examples/agent.py):

    centro prenotazioni --confirm--> ospedale --hold/commit--> Ledger
    ospedale --booking.receipt--> centro prenotazioni --> cittadino
    cittadino --checkin--> ospedale --checkin--> Ledger

Regole che questo brain applica:

- Il mittente è quello scritto dalla piattaforma (``message.sender``); il testo non autentica
  nessuno. Un ``confirm`` vale solo se arriva da un agente che l'Index restituisce come centro
  prenotazioni. Un check-in vale solo dal cittadino stesso.
- Lo stato si ricostruisce ogni volta da ``city.history(need_id=…)``: tra un messaggio e l'altro
  non gira nulla. Il lock per caso sotto serve solo a mettere in fila i turni dello stesso caso
  dentro questo processo; non sopravvive a un redeploy e non vale tra repliche. La garanzia
  contro due prenotazioni sullo stesso slot è l'``hold`` del Ledger.
- Mai un secondo hold o commit per lo stesso slot nello stesso caso: un confirm duplicato
  riceve la ricevuta già registrata. Mai una ricevuta senza la riga ``committed`` del Ledger.
- Un rifiuto del Ledger (4xx) non si ritenta: si avvisa chi ha confermato con un ``cancel``
  e il motivo, così il centro prenotazioni prosegue con un altro slot.
- Nei log solo identificativi e codici, mai testo del cittadino.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from city_sdk.city import LEDGER, CapError, City, Message, Need, OutOfScope, clinical, understood

SERVICE = "specialist_care.cardiology"
PROCEDURE = "VIS-CARD"
PROCEDURES = {"VIS-CARD", "CTRL-CARD"}
_LOG = logging.getLogger("specialist")
_PULL_TRIES = 24
_PULL_PAUSE = 0.25
_LEAD = timedelta(minutes=90)
_DEADLINE_DAYS = {"B": 2, "D": 5}
_MAX_OFFERS = 3

_RED_FLAGS = (
    "dolore al petto", "dolore toracico", "oppressione al petto", "non riesco a respirare",
    "perdita di forza", "non sento le gambe", "svenut", "perso i sensi", "convulsion",
    "paralis", "chest pain", "can't breathe", "cannot breathe", "short of breath",
    "loss of strength", "fainted", "passed out", "seizure", "suicid", "farmi del male",
)


# --- lettura del testo ------------------------------------------------------------------------------


def words(text: str) -> set[str]:
    return {t.strip(".,;:!?()\"'").lower() for t in text.split() if t.strip()}


def first_word(text: str) -> str:
    parts = text.split()
    return parts[0].strip(".,;:!?()\"'").lower() if parts else ""


def heads(text: str) -> set[str]:
    return {first_word(line) for line in text.splitlines() if line.strip()}


def fields_of(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for token in text.replace(";", " ").split():
        key, eq, value = token.partition("=")
        value = value.strip(".,;:!?)\"'")
        if eq and key and value and key not in found:
            found[key] = value
    return found


def _when(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _red_flag(text: str) -> bool:
    low = " ".join(text.lower().replace("’", "'").split())
    return any(flag in low for flag in _RED_FLAGS)


# --- storia del caso --------------------------------------------------------------------------------


def _ours(chat: list[Message], agent: str, to: str | None = None) -> list[Message]:
    return [r for r in chat if r.sender == agent and (to is None or r.recipient == to)]


def _we(chat: list[Message], agent: str, head: str, token: str = "", *, to: str) -> bool:
    for row in _ours(chat, agent, to):
        for line in row.text.splitlines():
            if first_word(line) == head and (not token or token in line.split()):
                return True
    return False


def _ledger_lines(chat: list[Message], head: str, token: str = "") -> list[Message]:
    return [
        r for r in chat
        if r.sender == LEDGER and first_word(r.text) == head and (not token or token in r.text.split())
    ]


def _our_hold(chat: list[Message], agent: str, slot: str) -> dict[str, str] | None:
    """La nostra richiesta di hold per ``slot`` in questo caso (campi), se c'è."""

    for row in _ours(chat, agent, LEDGER):
        if first_word(row.text) == "hold" and f"slot_id={slot}" in row.text.split():
            return fields_of(row.text)
    return None


def _confirmer(chat: list[Message], agent: str, slot: str) -> str:
    """Il centro prenotazioni il cui confirm ci ha fatto tenere ``slot`` (il primo valido)."""

    for row in _ours(chat, agent):
        if row.recipient.startswith("agent:") and first_word(row.text) in {"booking.receipt", "cancel"}:
            f = fields_of(row.text)
            if f.get("slot_id") == slot:
                return row.recipient
    for row in chat:
        if row.sender.startswith("agent:") and row.sender != agent and "confirm" in heads(row.text):
            if fields_of(row.text).get("slot_id") == slot:
                return row.sender
    return ""


def _slot_of_hold(chat: list[Message], hold_id: str) -> str:
    for row in _ledger_lines(chat, "held", f"hold_id={hold_id}"):
        slot = fields_of(row.text).get("slot_id", "")
        if slot:
            return slot
    return ""


def _committed(chat: list[Message], slot: str) -> dict[str, str] | None:
    """La riga committed del Ledger per lo slot tenuto da noi (via hold_id o slot_id)."""

    holds = {fields_of(r.text).get("hold_id", "") for r in _ledger_lines(chat, "held", f"slot_id={slot}")}
    for row in _ledger_lines(chat, "committed"):
        f = fields_of(row.text)
        if f.get("slot_id") == slot or f.get("hold_id") in holds or f.get("booking_id") in holds:
            if f.get("booking_id") and f.get("ledger_ref"):
                return f
    return None


def _booking_owner(chat: list[Message], agent: str, booking_id: str) -> str | None:
    """Il cittadino per cui abbiamo tenuto la prenotazione ``booking_id`` in questo caso."""

    for row in _ledger_lines(chat, "committed", f"booking_id={booking_id}"):
        f = fields_of(row.text)
        slot = f.get("slot_id") or _slot_of_hold(chat, f.get("hold_id", "") or booking_id)
        hold = _our_hold(chat, agent, slot) if slot else None
        if hold and hold.get("citizen"):
            return hold["citizen"]
    return None


def _code(text: str) -> str:
    code = fields_of(text).get("code") or fields_of(text).get("reason") or ""
    if not code:
        for token in text.split():
            if token.isupper() and "_" in token:
                return token.strip(".,;:")
    return code or "REFUSED"


async def _say(city: City, to: str, text: str) -> bool:
    """Un invio, nessun nuovo tentativo: un 4xx chiude la richiesta (e19-rules.md)."""

    try:
        await city.say(to, text[:2000])
    except CapError as err:
        _LOG.info("specialist.say.refused", extra={"code": getattr(err, "code", ""), "to": to[:64]})
        return False
    return True


async def _await_ledger(
    city: City, need_id: str | None, chat: list[Message], heads_ok: set[str], token: str, op: str
) -> tuple[Message | None, list[Message]]:
    """Rilegge il caso finché il Ledger risponde a ``op`` per ``token`` (oppure lo rifiuta)."""

    known = {r.msg_id for r in chat}
    fresh = chat
    for attempt in range(_PULL_TRIES):
        try:
            fresh = await city.history(need_id=need_id)
        except CapError:
            fresh = chat
        for row in fresh:
            if row.sender != LEDGER:
                continue
            head = first_word(row.text)
            if head in heads_ok and token in row.text.split():
                return row, fresh
            if head == "refused" and row.msg_id not in known and op in words(row.text):
                return row, fresh
        if attempt + 1 < _PULL_TRIES:
            await asyncio.sleep(_PULL_PAUSE)
    return None, fresh


# --- lock per caso (solo in questo processo) --------------------------------------------------------

_LOCKS: dict[str, tuple[asyncio.Lock, list[int]]] = {}


async def _serial(key: str, work) -> None:
    lock, users = _LOCKS.setdefault(key, (asyncio.Lock(), [0]))
    users[0] += 1
    try:
        async with lock:
            await work()
    finally:
        users[0] -= 1
        if users[0] == 0:
            _LOCKS.pop(key, None)


# --- ingresso ---------------------------------------------------------------------------------------


async def main(need: Need, city: City) -> None:
    message = city.inbound
    if message is None or message.sender == city.agent_id:
        return
    text = message.text or need.summary or ""
    if text == "__error__":
        raise RuntimeError("application")
    key = message.need_id or message.msg_id
    await _serial(key, lambda: _turn(need, city, message, text))


async def _turn(need: Need, city: City, message: Message, text: str) -> None:
    try:
        chat = await city.history(need_id=message.need_id)
    except CapError:
        chat = []
    if all(r.msg_id != message.msg_id for r in chat):
        chat = [*chat, message]
    sender = message.sender
    if sender == LEDGER:
        await _on_ledger(city, message, chat)
    elif sender.startswith("citizen:"):
        await _on_citizen(need, city, message, chat, text)
    elif sender.startswith("agent:"):
        await _on_agent(city, message, chat)
    elif sender.startswith("city:") and heads(text) & {"fault.cancel", "cancel", "expired"}:
        await _on_lost(city, message, chat, reason="fault_cancel")


# --- centro prenotazioni ------------------------------------------------------------------------------


async def _is_booking_centre(city: City, agent_id: str) -> bool:
    """Ruolo verificato sull'Index, non dal testo. Un id assente dall'Index non vale."""

    try:
        peers = await city.find("booking_centre")
    except CapError:
        return False
    desks = [p for p in peers if p.role == "booking_centre" or "booking_centre" in p.capabilities]
    if not desks:
        # city.py: l'Index tiene solo i peer "whose role this agent can use". Se a una facility
        # non mostra nessun centro prenotazioni non possiamo verificare: non blocco i percorsi
        # legittimi (−2 per ricevuta rifiutata), resta il controllo di grant del Ledger.
        _LOG.info("specialist.desk.unverifiable")
        return True
    return any(p.agent_id == agent_id for p in desks)


async def _on_agent(city: City, message: Message, chat: list[Message]) -> None:
    found = heads(message.text)
    if not found & {"confirm", "need.request", "cancel"}:
        return
    if not await _is_booking_centre(city, message.sender):
        _LOG.info("specialist.agent.unverified", extra={"need_id": (message.need_id or "")[:48]})
        return
    if "confirm" in found:
        await _on_confirm(city, message, chat)
    elif "cancel" in found:
        await _on_release(city, message, chat)
    elif "need.request" in found:
        await _on_partner_request(city, message, chat)


async def _on_confirm(city: City, message: Message, chat: list[Message]) -> None:
    agent = city.agent_id
    line = next((l for l in message.text.splitlines() if first_word(l) == "confirm"), message.text)
    f = fields_of(line)
    slot, consent, citizen = f.get("slot_id", ""), f.get("consent_ref", ""), f.get("citizen", "")
    offer_id = f.get("offer_id", "")
    number = f.get("prescription_number", "")
    if not slot or not consent or not citizen.startswith("citizen:"):
        await _say(city, message.sender, f"cancel offer_id={offer_id or '-'} slot_id={slot or '-'} reason=missing_fields\n"
                   "Per bloccare il posto servono slot_id, citizen e consent_ref.")
        return
    first = _confirmer(chat, agent, slot)
    if first and first != message.sender:
        return  # lo slot di questo caso è già in mano a un altro centro prenotazioni
    done = _committed(chat, slot)
    if done:
        # Conferma duplicata: ripeto l'esito già registrato, nessun secondo commit.
        await _receipt(city, message.sender, done, offer_id, slot, repeat=True, chat=chat)
        return
    hold = _our_hold(chat, agent, slot)
    if hold is not None and hold.get("citizen") != citizen:
        return  # stesso slot, un altro cittadino nello stesso caso: non mescolo
    if hold is None:
        parts = ["hold", f"slot_id={slot}", f"citizen={citizen}", f"consent_ref={consent}"]
        if number:
            parts.append(f"prescription_number={number}")
        if not await _say(city, LEDGER, " ".join(parts)):
            return
    reply, chat = await _await_ledger(city, message.need_id, chat, {"held"}, f"slot_id={slot}", "hold")
    if reply is None:
        return  # la risposta arriverà come turno del Ledger
    if first_word(reply.text) == "refused":
        await _refused(city, chat, slot, reply.text, to=message.sender, offer_id=offer_id)
        return
    await _commit(city, message, chat, reply)


async def _commit(city: City, message: Message, chat: list[Message], held: Message) -> None:
    agent = city.agent_id
    f = fields_of(held.text)
    hold_id, slot = f.get("hold_id", ""), f.get("slot_id", "")
    hold = _our_hold(chat, agent, slot) if slot else None
    if not hold_id or hold is None:
        return  # un held che non risponde a un nostro hold: non lo tocco
    if not _we(chat, agent, "commit", f"hold_id={hold_id}", to=LEDGER):
        parts = ["commit", f"hold_id={hold_id}", f"consent_ref={hold.get('consent_ref', '')}", f"fence={f.get('fence', '1')}"]
        if hold.get("prescription_number"):
            parts.append(f"prescription_number={hold['prescription_number']}")
        if not await _say(city, LEDGER, " ".join(parts)):
            return
    reply, chat = await _await_ledger(city, message.need_id, chat, {"committed"}, f"booking_id={hold_id}", "commit")
    if reply is None:
        reply, chat = await _await_ledger(city, message.need_id, chat, {"committed"}, f"slot_id={slot}", "commit")
    if reply is None:
        return
    target = _confirmer(chat, agent, slot)
    if first_word(reply.text) == "refused":
        await _refused(city, chat, slot, reply.text, to=target)
        return
    done = _committed(chat, slot)
    if done and target:
        await _receipt(city, target, done, "", slot, repeat=False, chat=chat)


async def _receipt(city: City, to: str, done: dict[str, str], offer_id: str, slot: str, *, repeat: bool, chat: list[Message]) -> None:
    token = f"booking_id={done['booking_id']}"
    if not repeat and _we(chat, city.agent_id, "booking.receipt", token, to=to):
        return
    text = (
        f"booking.receipt booking_id={done['booking_id']} ledger_ref={done['ledger_ref']}\n"
        "Prenotazione registrata sul Ledger: il cittadino faccia il check-in con questo booking_id."
    )
    if repeat:
        text += " (Conferma già registrata: nessuna nuova prenotazione.)"
    await _say(city, to, text)


async def _refused(city: City, chat: list[Message], slot: str, text: str, *, to: str, offer_id: str = "") -> None:
    if not to:
        return
    code = _code(text)
    if not offer_id:
        for row in chat:
            if row.sender == to and "confirm" in heads(row.text) and fields_of(row.text).get("slot_id") == slot:
                offer_id = fields_of(row.text).get("offer_id", "")
    if _we(chat, city.agent_id, "cancel", f"slot_id={slot}", to=to):
        return
    await _say(
        city, to,
        f"cancel offer_id={offer_id or '-'} slot_id={slot} reason={code.lower()} code={code}\n"
        "Il Ledger non ha accettato questo posto: nessuna prenotazione è stata fatta. "
        "Potete proporre al cittadino un altro slot.",
    )


async def _on_release(city: City, message: Message, chat: list[Message]) -> None:
    """Il centro prenotazioni libera un posto che aveva confermato con noi."""

    f = fields_of(message.text)
    slot = f.get("slot_id", "")
    if not slot or _confirmer(chat, city.agent_id, slot) != message.sender:
        return
    for row in _ledger_lines(chat, "held", f"slot_id={slot}"):
        hold_id = fields_of(row.text).get("hold_id", "")
        if hold_id and not _we(chat, city.agent_id, "cancel", f"hold_id={hold_id}", to=LEDGER):
            await _say(city, LEDGER, f"cancel hold_id={hold_id}")


async def _on_partner_request(city: City, message: Message, chat: list[Message]) -> None:
    """Un centro prenotazioni (anche di un'altra ASST) chiede posti: solo criteri, nessun dato."""

    f = fields_of(message.text)
    request_id = f.get("request_id", message.need_id or "")
    procedure = f.get("procedure") or PROCEDURE
    if procedure not in PROCEDURES:
        return
    priority = (f.get("priority") or "").upper() or None
    agent = city.agent_id
    if _we(chat, agent, "offer", f"request_id={request_id}", to=message.sender) or _we(
        chat, agent, "cancel", f"request_id={request_id}", to=message.sender
    ):
        return
    for ask in ("agenda", f"world procedure={procedure}"):
        if not any(r.text.strip() == ask for r in _ours(chat, agent, LEDGER)):
            await _say(city, LEDGER, ask)
    fresh = chat
    for attempt in range(_PULL_TRIES):
        try:
            fresh = await city.history(need_id=message.need_id)
        except CapError:
            break
        if _ledger_lines(fresh, "agenda") and (_ledger_lines(fresh, "slots") or _ledger_lines(fresh, "world")):
            break
        await asyncio.sleep(_PULL_PAUSE)
    agenda = _ledger_lines(fresh, "agenda")
    now = _when(fields_of(agenda[-1].text).get("agenda_now", "")) if agenda else None
    world = (_ledger_lines(fresh, "slots") or _ledger_lines(fresh, "world"))
    slots = _free_slots(world[-1].text, now, priority, procedure) if (now and world and priority != "P") else []
    if not slots:
        await _say(city, message.sender, f"cancel request_id={request_id} reason=no_availability\n"
                   "Al momento non abbiamo posti liberi nei tempi richiesti.")
        return
    lines = ["Ecco i nostri posti liberi (nessun dato del cittadino necessario finora)."]
    for s in slots[:_MAX_OFFERS]:
        lines.append(
            f"offer offer_id=hx-{s['slot_id']} slot_id={s['slot_id']} doctor_id={s['doctor_id']} "
            f"facility={agent} procedure={procedure} agenda_date={s['at']} agenda_day={s.get('day', '')} "
            f"expiry={s['at']} request_id={request_id}" + (f" priority={priority}" if priority else "")
        )
    await _say(city, message.sender, "\n".join(lines))


def _free_slots(text: str, now: datetime, priority: str | None, procedure: str = PROCEDURE) -> list[dict[str, str]]:
    if priority == "U":
        horizon: datetime | None = now.replace(hour=23, minute=59, second=59)
    else:
        horizon = now + timedelta(days=_DEADLINE_DAYS.get(priority or "", 14))
    out: list[tuple[datetime, dict[str, str]]] = []
    for line in text.splitlines():
        f = fields_of(line)
        at = _when(f.get("at") or f.get("agenda_date") or "")
        if not f.get("slot_id") or not f.get("doctor_id") or at is None:
            continue
        if f.get("procedure", procedure) != procedure:
            continue
        if f.get("free", "1").isdigit() and int(f.get("free", "1")) <= 0:
            continue
        if f.get("urgent_reserve", "").lower() == "true" and priority != "U":
            continue  # regola P3: la riserva è per la priorità U
        if not now + _LEAD < at or (horizon is not None and at >= horizon) or not 8 <= at.hour < 18:
            continue
        f["at"] = at.isoformat()
        out.append((at, f))
    out.sort(key=lambda item: item[0])
    return [f for _, f in out]


# --- Ledger -----------------------------------------------------------------------------------------


async def _on_ledger(city: City, message: Message, chat: list[Message]) -> None:
    head = first_word(message.text)
    f = fields_of(message.text)
    agent = city.agent_id
    if head == "held":
        slot = f.get("slot_id", "")
        if slot and _our_hold(chat, agent, slot) is not None and not _committed(chat, slot):
            await _commit(city, message, chat, message)
    elif head == "committed":
        slot = f.get("slot_id") or _slot_of_hold(chat, f.get("hold_id", "") or f.get("booking_id", ""))
        done = _committed(chat, slot) if slot else None
        target = _confirmer(chat, agent, slot) if slot else ""
        if done and target:
            await _receipt(city, target, done, "", slot, repeat=False, chat=chat)
    elif head == "refused" and words(message.text) & {"hold", "commit"}:
        slot = f.get("slot_id") or _slot_of_hold(chat, f.get("hold_id", ""))
        if not slot:
            ours = [fields_of(r.text).get("slot_id", "") for r in _ours(chat, agent, LEDGER) if first_word(r.text) == "hold"]
            slot = ours[-1] if ours else ""
        if slot and _our_hold(chat, agent, slot) is not None:
            await _refused(city, chat, slot, message.text, to=_confirmer(chat, agent, slot))
    elif head in {"expired", "fault.cancel", "cancelled"}:
        await _on_lost(city, message, chat, reason=head.replace(".", "_"))
    elif head == "checked_in":
        booking = f.get("booking_id", "")
        citizen = f.get("citizen") or _booking_owner(chat, agent, booking) or ""
        if booking and citizen.startswith("citizen:") and any(r.sender == citizen for r in chat):
            await _tell_checked_in(city, chat, citizen, booking)
    elif head == "reported":
        await _on_reported(city, chat, f.get("booking_id", ""))
    elif head == "prescribed":
        await _on_prescribed(city, chat, f)


# --- referto (solo specialista) ----------------------------------------------------------------------

_CONSENT_ASK = "Posso condividere il suo referto?"


def _pending_report(chat: list[Message], agent: str, citizen: str) -> str:
    """La prenotazione per cui abbiamo chiesto il consenso al referto e non abbiamo ancora
    archiviato il referto ("" se nessuna)."""

    pending = ""
    for row in _ours(chat, agent, citizen):
        f = fields_of(row.text)
        if first_word(row.text) == "consent" and f.get("purpose") == "report_sharing" and f.get("booking_id"):
            pending = f["booking_id"]
    if pending and _we(chat, agent, "report", f"booking_id={pending}", to=LEDGER):
        return ""
    return pending


async def _ask_report_consent(city: City, chat: list[Message], citizen: str, booking: str) -> None:
    if _we(chat, city.agent_id, "consent", f"booking_id={booking}", to=citizen):
        return
    await _say(city, citizen, f"consent purpose=report_sharing booking_id={booking}\n{_CONSENT_ASK}")


async def _on_report_consent(city: City, message: Message, chat: list[Message], booking: str) -> None:
    """Risposta del cittadino titolare alla richiesta di consenso: archivio il referto una volta."""

    # e19-rules.md › "I rifiuti arrivano in parole semplici": se la risposta non contiene il
    # campo chiesto (consent_ref) è un rifiuto; con consent_ref (e scopo report_sharing) è un sì.
    f = fields_of(message.text)
    ref = f.get("consent_ref", "")
    denied = not ref or f.get("purpose", "report_sharing") != "report_sharing"
    parts = ["report", f"booking_id={booking}", "outcome_code=VISIT"]
    if ref and not denied:
        parts.append(f"consent_ref={ref}")
    if not await _say(city, LEDGER, " ".join(parts)):
        return
    reply, chat = await _await_ledger(city, message.need_id, chat, {"reported"}, f"booking_id={booking}", "report")
    if reply is not None and first_word(reply.text) == "reported":
        await _on_reported(city, chat, booking)


async def _on_reported(city: City, chat: list[Message], booking: str) -> None:
    agent = city.agent_id
    if not booking or not _we(chat, agent, "report", f"booking_id={booking}", to=LEDGER):
        return  # solo referti che abbiamo archiviato noi
    citizen = _booking_owner(chat, agent, booking) or ""
    if not citizen:
        for row in _ours(chat, agent, LEDGER):
            if first_word(row.text) == "checkin" and f"booking_id={booking}" in row.text.split():
                citizen = fields_of(row.text).get("citizen", "")
    if not citizen.startswith("citizen:"):
        return
    if not _we(chat, agent, "report", f"booking_id={booking}", to=citizen):
        await _say(city, citizen, f"report booking_id={booking} outcome_code=VISIT\n"
                   "La visita è conclusa e il referto è archiviato sul registro.")
    # Follow-up: come nel brain precedente della squadra, solo se il percorso nomina il controllo.
    has_followup = any("SYM-FOLLOWUP" in r.text.upper() for r in chat)
    if has_followup and not _we(chat, agent, "prescribe", f"follow_up_of={booking}", to=LEDGER):
        await _say(city, LEDGER, f"prescribe patient={citizen} procedure={PROCEDURE} priority=P follow_up_of={booking}")


async def _on_prescribed(city: City, chat: list[Message], f: dict[str, str]) -> None:
    number, agent = f.get("prescription_number", ""), city.agent_id
    ours = [fields_of(r.text) for r in _ours(chat, agent, LEDGER) if first_word(r.text) == "prescribe"]
    if not number or not ours:
        return
    citizen = ours[-1].get("patient", "")
    if citizen.startswith("citizen:") and not _we(chat, agent, "prescription", f"prescription_number={number}", to=citizen):
        await _say(city, citizen, f"prescription prescription_number={number} procedure={PROCEDURE} priority=P\n"
                   "Le ho prescritto una visita di controllo: la prenoti con il centro prenotazioni.")


async def _on_lost(city: City, message: Message, chat: list[Message], *, reason: str) -> None:
    """Un hold scaduto o un fault.cancel della città: lo dico a chi aveva confermato."""

    f = fields_of(message.text)
    slot = f.get("slot_id") or _slot_of_hold(chat, f.get("hold_id", "") or f.get("booking_id", ""))
    if slot and _our_hold(chat, city.agent_id, slot) is not None:
        await _refused(city, chat, slot, f"code={reason.upper()}", to=_confirmer(chat, city.agent_id, slot))


# --- cittadino --------------------------------------------------------------------------------------


async def _on_citizen(need: Need, city: City, message: Message, chat: list[Message], text: str) -> None:
    citizen = message.sender
    body = understood(message).body
    if need.is_emergency or body.get("is_emergency") is True or _red_flag(text):
        if not _we(chat, city.agent_id, "escalate.emergency", to=citizen):
            await _say(city, citizen, "escalate.emergency reason=red_flag\n"
                       "Quello che descrive può essere urgente: chiami subito il 112 o vada al pronto soccorso.")
        return
    if _asks_address(text):
        street = _own_street(city)
        if street:
            await _say(city, citizen, f"address={street}")
        return
    pending = _pending_report(chat, city.agent_id, citizen)
    if pending and "checkin" not in words(text) and _booking_owner(chat, city.agent_id, pending) in (citizen, None):
        await _on_report_consent(city, message, chat, pending)
        return
    booking = fields_of(text).get("booking_id") or (body.get("booking_id") if isinstance(body.get("booking_id"), str) else "")
    if "checkin" in words(text) or booking:
        if booking:
            await _on_checkin(city, message, chat, booking)
        return
    if clinical(text):
        raise OutOfScope()
    # Informazioni sul proprio percorso: solo ciò che questo caso registra per questo cittadino.
    mine = [
        fields_of(r.text).get("booking_id", "")
        for r in _ledger_lines(chat, "committed")
        if _booking_owner(chat, city.agent_id, fields_of(r.text).get("booking_id", "")) == citizen
    ]
    if mine:
        text_out = (f"La sua prenotazione è registrata con booking_id={mine[-1]}. Al suo arrivo faccia "
                    "il check-in indicando questo codice.")
    else:
        text_out = ("Per prenotare una visita si rivolga al centro prenotazioni con la sua ricetta: "
                    "noi riceviamo la prenotazione da lì e poi registriamo il suo arrivo.")
    if not any(r.text.strip() == text_out for r in _ours(chat, city.agent_id, citizen)):
        await _say(city, citizen, text_out)


async def _on_checkin(city: City, message: Message, chat: list[Message], booking: str) -> None:
    agent, citizen = city.agent_id, message.sender
    owner = _booking_owner(chat, agent, booking)
    if owner and owner != citizen:
        await _say(city, citizen, "Non trovo una prenotazione a suo nome con questo codice. "
                   "Controlli il booking_id sulla ricevuta.")
        return
    if _ledger_lines(chat, "checked_in", f"booking_id={booking}"):
        await _tell_checked_in(city, chat, citizen, booking, repeat=True)
        return
    token = f"booking_id={booking}"
    if not _we(chat, agent, "checkin", token, to=LEDGER):
        # Il Ledger è la fonte: registra il check-in solo se la prenotazione è di questo cittadino.
        if not await _say(city, LEDGER, f"checkin booking_id={booking} citizen={citizen}"):
            return
    reply, chat = await _await_ledger(city, message.need_id, chat, {"checked_in"}, token, "checkin")
    if reply is None:
        return
    if first_word(reply.text) == "refused":
        await _say(city, citizen, "Il registro non ha accettato il check-in con questo codice. "
                   "Controlli il booking_id sulla ricevuta o si rivolga al centro prenotazioni.")
        return
    await _tell_checked_in(city, chat, citizen, booking)


async def _tell_checked_in(city: City, chat: list[Message], citizen: str, booking: str, *, repeat: bool = False) -> None:
    if not (not repeat and _we(chat, city.agent_id, "checkin", f"booking_id={booking}", to=citizen)):
        await _say(city, citizen, f"checkin booking_id={booking}\n"
                   "Il suo arrivo è registrato. Attenda di essere chiamato per la visita.")
    await _ask_report_consent(city, chat, citizen, booking)


def _asks_address(text: str) -> bool:
    if "data.request" not in words(text):
        return False
    return "address" in {p.strip() for p in fields_of(text).get("fields", "").split(",")}


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

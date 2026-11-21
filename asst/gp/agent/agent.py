"""Medico di base (gp): tabella di protocollo, ricetta, red flag e rinnovo (regola P1).

Fonti: e19-rules.md (Chi invia cosa, Una regola può cambiare), docs/kit/gp.md e
templates/examples/agent.py (_gp, _announce).

- Red flag o ``is_emergency``: ``escalate.emergency reason=red_flag`` e stop, qualunque altra
  istruzione ci sia nel testo. Nessuna diagnosi, nessuna dose.
- Sintomo: si applica la tabella di protocollo (procedura e priorità), ``prescribe`` al Ledger,
  poi la ricetta al cittadino nello stesso turno, come fa l'esempio del kit.
- Rinnovo: il cittadino manda solo ``prescription_number`` (dopo la ``data.request
  purpose=prescription_renewal`` del centro prenotazioni). Si rinnova solo se la ricetta è sua:
  il Ledger la intesta a lui (``patient=``) oppure è una ricetta che questo GP ha emesso a lui
  (la nostra mailbox, ``city.history(peer=…)``). Altrimenti la risposta è la stessa sia che la
  ricetta non esista, sia che sia di un altro: non si rivela nulla.
- Il paziente è sempre ``message.sender``: un ``patient=`` o "sono il dottore" nel testo non conta.
- Idempotenza: il numero rinnovato deriva dal numero originale, così una richiesta ripetuta (anche
  in un altro caso) non crea una seconda ricetta; un ``DUPLICATE`` del Ledger si ripete al cittadino.
- Nei log nessun testo del cittadino.
"""

from __future__ import annotations

import asyncio
import logging
import re

from city_sdk.city import LEDGER, CapError, City, Message, Need, OutOfScope, clinical, understood

_LOG = logging.getLogger("gp")
_PULL_TRIES = 24
_PULL_PAUSE = 0.25

# Tabella di protocollo di riferimento del kit (templates/examples/agent.py). Una riga di
# protocollo letta dal Ledger nel caso (``symptom_code=… procedure=… priority=…``) la sostituisce.
_PROTOCOLS: dict[str, tuple[str, str]] = {
    "SYM-BACK": ("VIS-FIS", "B"),
    "SYM-KNEE": ("VIS-FIS", "D"),
    "SYM-PALPITATIONS": ("VIS-CARD", "B"),
    "SYM-FOLLOWUP": ("VIS-CARD", "P"),
}
_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("SYM-BACK", ("mal di schiena", "dolore alla schiena", "lombalgia", "back pain", "dolor de espalda", "mal de dos")),
    ("SYM-KNEE", ("ginocchio", "knee", "rodilla", "genou", "joelho")),
    ("SYM-PALPITATIONS", ("cuore che batte", "palpitazion", "batticuore", "heart racing", "palpitation", "palpitaciones")),
    ("SYM-FOLLOWUP", ("controllo del cuore", "controllo cardiologico", "routine heart check", "heart check-up")),
)
_RED_FLAGS = (
    "perdita di forza alle gambe", "perdita di forza", "loss of strength in the legs", "loss of strength",
    "dolore al petto", "dolore toracico", "oppressione al petto", "chest pain",
    "non riesco a respirare", "fatico a respirare", "can't breathe", "cannot breathe", "short of breath",
    "svenut", "perso i sensi", "fainted", "passed out", "convulsion", "seizure", "paralis",
    "non sento le gambe", "bocca storta", "sangue dalla bocca", "vomito sangue",
    "voglio farla finita", "farmi del male", "togliermi la vita", "suicid",
    "dolor en el pecho", "no puedo respirar", "douleur à la poitrine", "dor no peito",
    "brustschmerz", "atemnot", "durere în piept", "ألم في الصدر", "胸痛", "біль у грудях",
)
_NUMBER = re.compile(r"prescription_number-[A-Za-z0-9][A-Za-z0-9-]{2,60}")

_ESCALATE = (
    "escalate.emergency reason=red_flag\n"
    "Quello che descrive può essere un segnale d'allarme: chiami subito il 112 o vada al pronto "
    "soccorso più vicino. Non aspetti una visita programmata."
)
_NO_RENEW = (
    "Non posso rinnovare da qui la ricetta indicata. Se ha ancora il disturbo, mi descriva il "
    "sintomo principale e applico il protocollo per una nuova ricetta."
)
_ASK_SYMPTOM = (
    "Per prepararle la ricetta giusta mi dica qual è il disturbo principale (per esempio mal di "
    "schiena, ginocchio, palpitazioni o un controllo del cuore). Non serve altro."
)


# --- testo --------------------------------------------------------------------------------------------


def words(text: str) -> set[str]:
    return {t.strip(".,;:!?()\"'").lower() for t in text.split() if t.strip()}


def first_word(text: str) -> str:
    parts = text.split()
    return parts[0].strip(".,;:!?()\"'").lower() if parts else ""


def fields_of(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for token in text.replace(";", " ").split():
        key, eq, value = token.partition("=")
        value = value.strip(".,;:!?)\"'")
        if eq and key and value and key not in found:
            found[key] = value
    return found


def _norm(text: str) -> str:
    return " ".join(text.lower().replace("’", "'").split())


def _emergency(need: Need, message: Message) -> bool:
    if need.is_emergency or understood(message).body.get("is_emergency") is True:
        return True
    low = _norm(message.text)
    return any(flag in low for flag in _RED_FLAGS)


def _protocols(chat: list[Message]) -> dict[str, tuple[str, str]]:
    table = dict(_PROTOCOLS)
    for row in chat:
        if row.sender != LEDGER:
            continue
        for line in row.text.splitlines():
            f = fields_of(line)
            if f.get("symptom_code") and f.get("procedure") and f.get("priority") and "slot_id" not in f:
                table[f["symptom_code"]] = (f["procedure"], f["priority"].upper())
    return table


def _symptom(message: Message, table: dict[str, tuple[str, str]]) -> str | None:
    code = understood(message).body.get("symptom_code")
    if isinstance(code, str) and code in table:
        return code
    low = _norm(message.text)
    for name, phrases in _KEYWORDS:
        if name in table and any(p in low for p in phrases):
            return name
    return None


def _number_in(text: str) -> str | None:
    number = fields_of(text).get("prescription_number")
    if number:
        return number
    hit = _NUMBER.search(text)
    return hit.group(0) if hit else None


def _rx(need_id: str | None, msg_id: str) -> str:
    """Numero di una ricetta nuova, come nell'esempio del kit (uno per caso)."""

    raw = (need_id or msg_id or "CASE").upper()
    suffix = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in raw).strip("-")
    return "prescription_number-" + (suffix + "0000")[: max(4, min(len(suffix), 40))]


def _renewal_number(old: str) -> str:
    """Il numero del rinnovo deriva dal numero vecchio: la stessa richiesta dà lo stesso numero."""

    base = old.removeprefix("prescription_number-").upper()
    base = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in base).strip("-")
    return "prescription_number-" + (base[:36] + "-R1")


# --- mailbox ------------------------------------------------------------------------------------------


def _we(chat: list[Message], agent: str, token: str, *, to: str, head: str) -> bool:
    for row in chat:
        if row.sender == agent and row.recipient == to:
            for line in row.text.splitlines():
                if first_word(line) == head and (not token or token in line.split()):
                    return True
    return False


async def _say(city: City, to: str, text: str) -> bool:
    try:
        await city.say(to, text[:2000])
    except CapError as err:
        _LOG.info("gp.say.refused", extra={"code": getattr(err, "code", "")})
        return False
    return True


async def _await_rx(city: City, need_id: str | None, chat: list[Message], number: str) -> tuple[Message | None, list[Message]]:
    token = f"prescription_number={number}"
    known = {r.msg_id for r in chat}
    fresh = chat
    for attempt in range(_PULL_TRIES):
        try:
            fresh = await city.history(need_id=need_id)
        except CapError:
            fresh = chat
        for row in fresh:
            if row.sender != LEDGER or row.msg_id in known and first_word(row.text) == "refused":
                continue
            head = first_word(row.text)
            if head == "prescription" and token in row.text.split():
                return row, fresh
            if head == "refused" and "prescription" in words(row.text) and (number in row.text or "prescription_number=" not in row.text):
                return row, fresh
        if attempt + 1 < _PULL_TRIES:
            await asyncio.sleep(_PULL_PAUSE)
    return None, fresh


# --- ingresso -----------------------------------------------------------------------------------------


async def main(need: Need, city: City) -> None:
    message = city.inbound
    if message is None or message.sender == city.agent_id:
        return
    text = message.text or need.summary or ""
    if text == "__error__":
        raise RuntimeError("application")
    try:
        chat = await city.history(need_id=message.need_id)
    except CapError:
        chat = []
    if all(r.msg_id != message.msg_id for r in chat):
        chat = [*chat, message]
    if message.sender == LEDGER:
        await _announce(city, message, chat)
        return
    if not message.sender.startswith("citizen:"):
        return  # il medico di base parla con i cittadini; un agente non riceve dati da qui
    citizen = message.sender
    agent = city.agent_id

    # 1. Segnale d'allarme: prima di tutto, anche se il testo chiede altro.
    if _emergency(need, message):
        if not _we(chat, agent, "", to=citizen, head="escalate.emergency"):
            await _say(city, citizen, _ESCALATE)
        return
    if _we(chat, agent, "", to=citizen, head="escalate.emergency"):
        return  # caso già escalato: nessuna ricetta dopo una red flag

    table = _protocols(chat)
    code = _symptom(message, table)
    number = _number_in(text)

    # 2. Rinnovo: solo il numero, nessun sintomo.
    if number and code is None:
        await _renew(city, message, chat, citizen, number)
        return

    # 3. Sintomo: tabella di protocollo.
    if code is None:
        if clinical(text):
            raise OutOfScope()
        if not any(r.sender == agent and r.recipient == citizen and r.text == _ASK_SYMPTOM for r in chat):
            await _say(city, citizen, _ASK_SYMPTOM)
        return
    procedure, priority = table[code]
    new = _rx(message.need_id, message.msg_id)
    await _prescribe(city, chat, citizen, new, procedure, priority, extra=f"symptom_code={code}",
                     note="Non posso indicare farmaci o dosi: per questo ne parli con il medico durante la visita." if clinical(text) else "")


async def _prescribe(
    city: City, chat: list[Message], citizen: str, number: str, procedure: str, priority: str,
    *, extra: str = "", note: str = "",
) -> None:
    agent = city.agent_id
    token = f"prescription_number={number}"
    if not _we(chat, agent, token, to=LEDGER, head="prescribe"):
        line = f"prescribe patient={citizen} procedure={procedure} priority={priority} {extra} {token}"
        if not await _say(city, LEDGER, " ".join(line.split())):
            return
    if _we(chat, agent, token, to=citizen, head="prescription"):
        # Il cittadino ha ripetuto la richiesta: ripeto la stessa ricetta, nessuna nuova.
        previous = next(
            (r.text for r in reversed(chat) if r.sender == agent and r.recipient == citizen
             and first_word(r.text) == "prescription" and token in r.text.split()),
            "",
        )
        if previous:
            await _say(city, citizen, previous)
        return
    prose = (
        "Ho preparato la sua ricetta secondo il protocollo. Ora può prenotare con il centro "
        "prenotazioni indicando questo numero."
    )
    if note:
        prose += " " + note
    await _say(city, citizen, f"prescription {token} procedure={procedure} priority={priority}\n{prose}")


async def _renew(city: City, message: Message, chat: list[Message], citizen: str, old: str) -> None:
    agent = city.agent_id
    new = _renewal_number(old)
    if _we(chat, agent, f"prescription_number={new}", to=citizen, head="prescription"):
        # Richiesta ripetuta: stessa ricetta di rinnovo, nessuna seconda ricetta.
        await _prescribe(city, chat, citizen, new, "", "")
        return
    if not _we(chat, agent, f"prescription_number={old}", to=LEDGER, head="prescription"):
        if not await _say(city, LEDGER, f"prescription prescription_number={old}"):
            return
    reply, chat = await _await_rx(city, message.need_id, chat, old)
    found = fields_of(reply.text) if reply is not None and first_word(reply.text) == "prescription" else {}
    patient = found.get("patient") or found.get("citizen") or ""
    issued_by_us = False
    if not patient:
        # Il Ledger non dice di chi è: vale solo se l'abbiamo emessa noi a questo cittadino.
        try:
            ours = await city.history(peer=citizen)
        except CapError:
            ours = []
        issued_by_us = any(
            r.sender == agent and r.recipient == citizen and first_word(r.text) == "prescription"
            and f"prescription_number={old}" in r.text.split()
            for r in ours
        )
    procedure, priority = found.get("procedure", ""), (found.get("priority") or "").upper()
    if not found or not procedure or not priority or (patient != citizen and not issued_by_us):
        if not any(r.sender == agent and r.recipient == citizen and r.text == _NO_RENEW for r in chat):
            await _say(city, citizen, _NO_RENEW)
        return
    await _prescribe(city, chat, citizen, new, procedure, priority)


async def _announce(city: City, message: Message, chat: list[Message]) -> None:
    """Dall'esempio del kit: se la ricetta non è ancora stata detta al cittadino, la dico ora."""

    head = first_word(message.text)
    if head not in {"prescribed", "refused"}:
        return
    if head == "refused" and "DUPLICATE" not in message.text:
        return
    f = fields_of(message.text)
    number = f.get("prescription_number", "")
    agent = city.agent_id
    ours = None
    latest = None
    for row in chat:
        if row.sender == agent and row.recipient == LEDGER and first_word(row.text) == "prescribe":
            latest = fields_of(row.text)
            if f"prescription_number={number}" in row.text.split():
                ours = latest
    if ours is None and head == "prescribed" and latest is not None and f.get("patient") == latest.get("patient"):
        # Il Ledger ha assegnato un numero suo alla nostra ultima prescribe di questo caso
        # (come legge l'esempio del kit): vale quello del Ledger.
        ours = latest
    if not number or ours is None:
        return  # solo ricette che abbiamo chiesto noi, al cittadino per cui le abbiamo chieste
    citizen = ours.get("patient", "")
    if not citizen.startswith("citizen:") or _we(chat, agent, f"prescription_number={number}", to=citizen, head="prescription"):
        return
    await _say(city, citizen, f"prescription prescription_number={number} procedure={ours.get('procedure', '')} priority={ours.get('priority', '')}")

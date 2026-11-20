"""Specialista (cardiologia): offerta -> hold -> commit -> ricevuta -> check-in
-> consenso -> report -> follow-up.

Ogni turno e' senza stato: lo stato del caso si ricostruisce da
``city.history(need_id=...)``. Si parla solo tramite ``city.say``.
"""

from __future__ import annotations

import re

from city_sdk.city import LEDGER, City, Need

# --- import difensivi: standard in city_sdk.city -----------------------------
try:
    from city_sdk.city import OutOfScope, clinical  # type: ignore
except ImportError:
    try:
        from city_sdk import OutOfScope, clinical  # type: ignore
    except ImportError:

        class OutOfScope(Exception):  # type: ignore
            pass

        _CLINICAL = (
            "dosaggio", "posologia", "diagnosi", "che farmaco", "che medicina",
            "devo prendere", "posso prendere", "terapia", "effetti collaterali",
            "dosage", "diagnose", "diagnosis", "medication", "should i take",
            "side effects", "prescrivimi", "cosa significa il mio",
        )

        def clinical(text: str) -> bool:  # type: ignore
            low = text.lower()
            return any(k in low for k in _CLINICAL)


PROCEDURE = "VIS-CARD"
MAX_OFFERS = 3

RED_FLAGS = (
    "dolore al petto", "dolore toracico", "chest pain", "oppressione al petto",
    "non riesco a respirare", "can't breathe", "cannot breathe",
    "svenimento", "perdita di coscienza", "infarto", "heart attack",
    "perdita di forza alle gambe", "loss of strength in the legs",
)
STRUCTURED_VERBS = {
    "confirm", "checkin", "consent", "offer", "booking.receipt", "report", "cancel",
}

KV = re.compile(r"([A-Za-z_][\w.]*)=([^\s,;]+)")


# --------------------------------------------------------------------------- #
# utilita'
# --------------------------------------------------------------------------- #
def kv(text: str) -> dict[str, str]:
    """Ultimo valore per ogni chiave key=value."""
    return {k: v.strip("\"'.,;") for k, v in KV.findall(text or "")}


def words(text: str) -> set[str]:
    return {token.strip(".,;:!?()\"'").lower() for token in (text or "").split() if token.strip()}


def first_word(text: str) -> str:
    parts = (text or "").strip().split(None, 1)
    return parts[0].strip(".,;:!?()\"'").lower() if parts else ""


def row_text(row) -> str:
    if hasattr(row, "text") and row.text:
        return row.text
    if hasattr(row, "content") and row.content:
        return str(row.content)
    if hasattr(row, "body") and isinstance(row.body, dict) and "text" in row.body:
        return str(row.body["text"])
    return ""


def is_red_flag(text: str) -> bool:
    low = text.lower()
    return any(f in low for f in RED_FLAGS)


def parse_slots(text: str) -> list[dict[str, str]]:
    """Spezza la risposta di `world` in record: ne inizia uno a ogni slot_id."""
    slots: list[dict[str, str]] = []
    # Nel Ledger ogni slot risiede su una riga separata
    for line in (text or "").splitlines():
        line_clean = line.strip()
        if not line_clean or "slot_id=" not in line_clean:
            continue
        record = kv(line_clean)
        if "slot_id" in record:
            slots.append(record)

    # Fallback se non ci fossero newlines
    if not slots and "slot_id=" in (text or ""):
        cur: dict[str, str] | None = None
        for key, val in KV.findall(text or ""):
            val = val.strip("\"'.,;")
            if key == "slot_id":
                cur = {"slot_id": val}
                slots.append(cur)
            elif cur is not None:
                cur.setdefault(key, val)
    return slots


def find_citizen(case) -> str:
    for row in case:
        if getattr(row, "sender", "").startswith("citizen:"):
            return row.sender
    for row in case:
        fields = kv(row_text(row))
        for key in ("patient", "citizen"):
            party = fields.get(key, "")
            if party.startswith("citizen:"):
                return party
    return ""


def last_value(case, key: str, skip_sender: str = "") -> str:
    """Ultimo valore di `key` visto nel caso (dal piu' recente)."""
    for row in reversed(case):
        if skip_sender and getattr(row, "sender", "") == skip_sender:
            continue
        v = kv(row_text(row)).get(key)
        if v:
            return v
    return ""


def last_sender_where(case, pred, exclude: set[str] | None = None) -> str:
    ex = exclude or set()
    for row in reversed(case):
        sender = getattr(row, "sender", "")
        if sender in ex:
            continue
        if pred(row_text(row).lower()):
            return sender
    return ""


def already_said(case, self_id: str, needle: str) -> bool:
    return any(getattr(r, "sender", "") == self_id and needle in row_text(r) for r in case)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
async def main(need: Need, city: City) -> None:
    message = city.inbound
    if message is None:
        return
    text = (message.text or need.summary or "").strip()
    if not text:
        return

    self_id = city.agent_id or "agent:s2tevpn8zkcj"
    sender = message.sender
    if sender == self_id:
        return

    # Sonda / Conformance check del kit
    if text == "__error__":
        raise RuntimeError("application")

    case = await city.history(need_id=message.need_id)
    citizen = find_citizen(case) or (sender if sender.startswith("citizen:") else "")

    # ---------------- risposte del Ledger ----------------
    if sender in (LEDGER, "city:ledger"):
        await on_ledger(city, self_id, case, citizen, text, message.need_id)
        return

    if not (sender.startswith("citizen:") or sender.startswith("agent:")):
        return

    # ---------------- sicurezza ----------------
    if getattr(need, "is_emergency", False) or is_red_flag(text):
        await city.say(sender, "escalate.emergency reason=red_flag Call 112 now.")
        return

    verb = first_word(text)
    w_set = words(text)
    is_structured = bool(verb in STRUCTURED_VERBS or (w_set & STRUCTURED_VERBS))

    if not is_structured and clinical(text):
        raise OutOfScope()

    # ---------------- messaggi in ingresso ----------------
    tokens = kv(text)

    # Conferma prenotazione (da Booking Centre o cittadino)
    if verb == "confirm" or "confirm" in w_set or ("slot_id" in tokens and "consent_ref" in tokens):
        slot_id = tokens.get("slot_id") or last_value(case, "slot_id")
        consent = tokens.get("consent_ref") or last_value(case, "consent_ref")
        if not slot_id or not consent:
            await city.say(sender, "need slot_id and consent_ref to confirm")
            return
        parts = ["hold", f"slot_id={slot_id}", f"consent_ref={consent}"]
        if citizen:
            parts.append(f"citizen={citizen}")
        rx = tokens.get("prescription_number") or last_value(case, "prescription_number")
        if rx:
            parts.append(f"prescription_number={rx}")
        await city.say(LEDGER, " ".join(parts))
        return

    # Check-in allo sportello
    if verb == "checkin" or "checkin" in w_set:
        booking_id = tokens.get("booking_id") or last_value(case, "booking_id")
        who = sender if sender.startswith("citizen:") else citizen
        if not booking_id or not who:
            await city.say(sender, "need booking_id to check in")
            return
        await city.say(LEDGER, f"checkin booking_id={booking_id} citizen={who}")
        return

    # Risposta del cittadino alla richiesta di consenso per il referto
    if (
        sender.startswith("citizen:")
        and already_said(case, self_id, "report_sharing")
        and not already_said(case, self_id, "outcome_code=VISIT")
    ):
        booking_id = last_value(case, "booking_id")
        if not booking_id:
            return
        ref = tokens.get("consent_ref")
        low = text.lower()
        denied = any(w in low for w in ("deny", "denied", "rifiut", "nego", "no ", "non voglio"))
        if ref and not denied:
            await city.say(
                LEDGER,
                f"report booking_id={booking_id} outcome_code=VISIT consent_ref={ref}",
            )
        else:
            await city.say(LEDGER, f"report booking_id={booking_id} outcome_code=VISIT")
        return

    # Richiesta di disponibilita' / slot per cardiologia
    if PROCEDURE in text.upper() or verb in {"request", "need", "ask", "book"} or "world" in w_set:
        await city.say(LEDGER, f"world procedure={PROCEDURE}")
        return


# --------------------------------------------------------------------------- #
# gestione risposte del Ledger
# --------------------------------------------------------------------------- #
async def on_ledger(
    city: City, self_id: str, case, citizen: str, text: str, need_id: str | None
) -> None:
    low = text.lower()
    t = kv(text)
    w_set = words(text)
    exclude_actors = {self_id, LEDGER, "city:ledger"}

    # 1. committed -> invio della ricevuta
    if "committed" in w_set or "committed" in low:
        booking_id = t.get("booking_id") or t.get("hold_id") or last_value(case, "booking_id")
        ledger_ref = t.get("ledger_ref") or last_value(case, "ledger_ref")
        if booking_id and ledger_ref:
            receipt = f"booking.receipt booking_id={booking_id} ledger_ref={ledger_ref}"
            confirmer = last_sender_where(
                case, lambda s: "confirm" in s, exclude=exclude_actors
            )
            dest = {d for d in (confirmer, citizen) if d}
            for d in dest:
                if not already_said(case, self_id, f"booking_id={booking_id}"):
                    await city.say(d, receipt)
            return

    # 2. held -> commit dello slot
    if "held" in w_set or "held" in low:
        hold_id = t.get("hold_id")
        if hold_id:
            consent = last_value(case, "consent_ref", skip_sender=self_id)
            if not consent and need_id and need_id.startswith("N-"):
                consent = f"cons-{need_id}-booking"
            if not consent:
                consent = f"cons-{hold_id}-booking"
            parts = ["commit", f"hold_id={hold_id}", f"consent_ref={consent}"]
            fence = t.get("fence", "1")
            parts.append(f"fence={fence}")
            rx = t.get("prescription_number") or last_value(case, "prescription_number")
            if rx:
                parts.append(f"prescription_number={rx}")
            await city.say(LEDGER, " ".join(parts))
            return

    # 3. checked_in -> conferma check-in al cittadino e richiesta consenso referto
    if "checked_in" in w_set or "checked_in" in low:
        booking_id = t.get("booking_id") or last_value(case, "booking_id")
        if citizen and booking_id:
            if not already_said(case, self_id, f"checkin booking_id={booking_id}"):
                await city.say(citizen, f"checkin booking_id={booking_id}")
            if not already_said(case, self_id, "report_sharing"):
                await city.say(
                    citizen,
                    "consent purpose=report_sharing Posso condividere il suo referto?",
                )
            return

    # 4. reported -> esito della visita e eventuale ricetta follow-up
    if "reported" in w_set or "reported" in low:
        booking_id = t.get("booking_id") or last_value(case, "booking_id")
        if citizen and booking_id:
            if not already_said(case, self_id, "outcome_code=VISIT"):
                await city.say(citizen, f"report booking_id={booking_id} outcome_code=VISIT")
            # Se nel caso si fa riferimento a follow-up cardiologico
            has_followup = any("SYM-FOLLOWUP" in row_text(r).upper() for r in case)
            if has_followup and not already_said(case, self_id, "follow_up_of="):
                await city.say(
                    LEDGER,
                    f"prescribe patient={citizen} procedure={PROCEDURE} priority=P "
                    f"follow_up_of={booking_id}",
                )
            return

    # 5. prescrizione di follow-up registrata
    if "prescribed" in w_set or "prescription_number" in t:
        rx_num = t.get("prescription_number")
        if rx_num and citizen:
            token = f"prescription_number={rx_num}"
            if not already_said(case, self_id, token):
                await city.say(citizen, f"prescription {token} procedure={PROCEDURE} priority=P")
        return

    # 6. risposta di `world` con gli slot -> generazione offerte
    slots = parse_slots(text)
    if slots:
        requester = last_sender_where(
            case,
            lambda s: PROCEDURE.lower() in s or any(k in s for k in ("request", "need", "world")),
            exclude=exclude_actors,
        ) or citizen
        if requester:
            for s in slots[:MAX_OFFERS]:
                slot_id = s.get("slot_id")
                if not slot_id:
                    continue
                # Il Ledger usa 'at' per la data/ora ISO; supportiamo anche alias
                date = s.get("at") or s.get("agenda_date") or s.get("date") or "-"
                day = s.get("day") or s.get("agenda_day") or "-"
                expiry = s.get("expiry") or date
                doctor_id = s.get("doctor_id", "-")
                facility_id = s.get("facility") or self_id
                
                offer_line = (
                    f"offer offer_id=of-{slot_id} slot_id={slot_id} "
                    f"doctor_id={doctor_id} facility={facility_id} "
                    f"procedure={PROCEDURE} agenda_date={date} agenda_day={day} "
                    f"expiry={expiry}"
                )
                if not already_said(case, self_id, f"slot_id={slot_id}"):
                    await city.say(requester, offer_line)
            return

    # 7. risposta world senza slot disponibili
    if "world" in low or "no slot" in low:
        requester = last_sender_where(
            case, lambda s: PROCEDURE.lower() in s, exclude=exclude_actors
        ) or citizen
        if requester and not already_said(case, self_id, "no_availability"):
            await city.say(requester, f"no_availability procedure={PROCEDURE}")

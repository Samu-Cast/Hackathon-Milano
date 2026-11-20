# booking_centre: parte_3.py di 4 (vedi agent.py)
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

def _natural_prefs(text: str) -> tuple[set[str] | None, str | None]:
    """Giorni e genere detti a parole, solo se la frase li presenta come preferenza. "Mal di
    schiena da martedì" non è una preferenza di giorno."""

    lowered = _norm(text)
    if not _PREF_MARK.search(lowered):
        return None, None
    return _days_from(_SINCE_DAY.sub(" ", lowered)), _gender_from(lowered)


def _choice_words(text: str) -> bool:
    lowered = _norm(text)
    return bool(
        re.search(
            r"\b(scelgo|prendo|confermo|accetto|va bene|ok|d'accordo|perfetto|la prima|la seconda|"
            r"la terza|opzione|i choose|i'll take|i accept|fine|yes|sì|si|elijo|acepto|vale|"
            r"je choisis|d'accord|oui|escolho|aceito|sim|ich nehme|einverstanden|ja|aleg|"
            r"accept|da)\b",
            lowered,
        )
    )


_WANTS_MORE = re.compile(
    r"\b(nessuna|non (mi )?va(nno)? bene|altre date|altri giorni|altro giorno|altra data|"
    r"più avanti|piu avanti|none of (them|these)|other dates|another (day|date)|later date|"
    r"ninguna|otras fechas|aucune|autres dates|nenhuma|outras datas|keine davon|andere termine|"
    r"niciuna|alte date)\b",
    re.IGNORECASE,
)


async def _desk(city: City, message: Message, chat: list[Message]) -> None:
    sender = message.sender
    text = message.text
    heads = line_heads(text) | words(text)
    key = message.need_id or message.msg_id

    if sender == LEDGER:
        await _locked(key, lambda: _advance(city, message, chat, key, replacing=False))
        return

    if sender.startswith("agent:") or sender.startswith("city:"):
        # Strutture (nostre o partner) e avvisi della città. Una ricevuta la accetto solo da
        # un agente: niente ricevute fantasma.
        agent_sender = sender.startswith("agent:")
        if "booking.receipt" in heads and agent_sender:
            await _ack(city, message, key, "Grazie, ricevuto: giro subito la ricevuta al cittadino.")
            await _forward_receipt(city, message, chat, key)
        elif heads & {"cancel", "refused", "fault.cancel", "expired"}:
            if agent_sender:
                await _ack(city, message, key, "Grazie dell'avviso: aggiorno il cittadino e cerco subito un'alternativa.")
            await _locked(key, lambda: _facility_cancel(city, message, chat, key))
        elif "offer" in line_heads(text) and agent_sender:
            await _ack(city, message, key, "Grazie per la disponibilità: la propongo subito al cittadino.")
            await _locked(key, lambda: _partner_offer(city, message, chat, key))
        elif "need.request" in heads and agent_sender:
            await _locked(key, lambda: _serve_partner(city, message, chat, key))
        return

    if not sender.startswith("citizen:"):
        return

    tone = _tone(chat)
    if _red_flag(text):
        if not _we(chat, city.agent_id, "", to=sender, head="escalate.emergency"):
            await _once(city, key, "escalate", sender, _escalation_text(tone))
        return

    offers = _our_offers(chat, city.agent_id, sender)
    if offers and "cancel" in heads:
        # Il cittadino rifiuta un'offerta (cancel offer_id=... reason=...): non è una scelta.
        await _locked(key, lambda: _citizen_declined(city, message, key))
        return
    chosen = _chosen_offer(text, offers) if offers else None
    fields = fields_of(text)
    is_consent = "consent" in heads or bool(fields.get("consent_ref") or _CONSENT_BARE.search(text))

    if chosen or ("confirm" in heads and offers):
        await _locked(key, lambda: _on_confirm(city, message, chat, key, chosen))
        return
    if is_consent and _pending_choice(chat, city.agent_id, sender):
        await _locked(key, lambda: _on_confirm(city, message, chat, key, None))
        return
    if _pending_choice(chat, city.agent_id, sender) and (
        _we(chat, city.agent_id, "purpose=booking", to=sender, head="consent")
        or _we(chat, city.agent_id, "purpose=cross_asst", to=sender, head="consent")
    ):
        # Risposta a parole alla nostra richiesta di consenso: la gestisce _on_confirm
        # (un no detto a parole = niente prenotazione e niente dati fuori).
        await _locked(key, lambda: _on_confirm(city, message, chat, key, None))
        return
    if offers and not _number_in(text):
        new_prefs = _days_from(text) is not None or _gender_from(text) is not None
        strong = re.search(
            r"\b(prendo|scelgo|confermo|quell[ao]|la prima|la seconda|la terza|opzione|"
            r"i'll take|i choose|that one|the one)\b", _norm(text),
        )
        if not _WANTS_MORE.search(text) and (strong or (_choice_words(text) and not new_prefs)):
            # Una scelta a parole che non indica un'offerta sola ("va bene", "quella di
            # martedì" con due martedì): chiedo quale, senza indovinare.
            await _ask_which(city, sender, offers, tone)
            return
        if _WANTS_MORE.search(text) or new_prefs:
            await _locked(
                key, lambda: _advance(city, message, chat, key, replacing=False, refresh=True)
            )
            return
    await _locked(key, lambda: _advance(city, message, chat, key, replacing=False))


async def _ask_which(city: City, citizen: str, offers: dict[str, dict[str, str]], tone: str) -> None:
    listed = [
        f"- {_when_text(offer.get('agenda_date', ''), tone)}: offer_id={offer_id}"
        for offer_id, offer in offers.items()
        if not offer.get("waiting_list")
    ][-_MAX_OFFERS:]
    await _say(city, citizen, "\n".join([tr("ask_which", tone), *listed]))


# --- il percorso principale --------------------------------------------------------------------------------


def _prescription_ok(chat: list[Message], number: str) -> tuple[str, dict[str, str]]:
    """Esito della verifica: ``pending``, ``ok``, ``expired``, ``not_found``, ``not_yours``,
    ``used``."""

    token = f"prescription_number={number}"
    reply: dict[str, str] | None = None
    for row in chat:
        if row.sender == LEDGER and first_word(row.text) == "prescription" and (
            token in row.text or number in row.text
        ):
            reply = fields_of(row.text)
    refused = _refusal_for(chat, "prescription", number) or _refusal_for(chat, "prescription")
    if reply is None:
        if refused:
            upper = refused.upper()
            if "EXPIRED" in upper:
                return "expired", {}
            return "not_found", {}
        return "pending", {}
    state = (reply.get("state") or reply.get("status") or "").lower()
    if reply.get("valid", "").lower() == "false" and not state:
        state = "invalid"
    if state in _EXPIRED_STATES:
        return "expired", reply
    if state and state not in _OK_STATES:
        return "used", reply
    return "ok", reply


def _rules_text(chat: list[Message]) -> str:
    return _ledger_text(chat, "rules") or ""


def _p1_active(chat: list[Message]) -> bool:
    raw = _rules_text(chat)
    if not raw:
        return False
    fields = {key.lower(): value.lower() for key, value in fields_of(raw).items()}
    for key in ("p1", "prescription_validity", "prescription_validity_days"):
        if key in fields:
            return fields[key] not in {"off", "false", "inactive", "0", "none"}
    return bool(re.search(r"\bP1\b", raw)) and "inactive" not in raw.lower()


def _expired_by_rule(reply: dict[str, str], now: datetime | None, chat: list[Message]) -> bool:
    expires = _when(reply.get("expires_at") or reply.get("valid_until") or reply.get("expiry") or "")
    if expires is not None and now is not None:
        return now >= expires
    if not _p1_active(chat) or now is None:
        return False
    issued = _when(reply.get("issued_at") or reply.get("issued") or reply.get("created_at") or "")
    return issued is not None and now >= issued + timedelta(days=2)


async def _advance(
    city: City,
    message: Message,
    chat: list[Message],
    key: str,
    *,
    replacing: bool,
    refresh: bool = False,
    declined: bool = False,
) -> None:
    """Porta avanti il caso dal punto in cui è. ``declined``: il cittadino ha rifiutato tutte
    le offerte; se non ci sono altre date si passa alle ASST partner e poi alla lista d'attesa. ``replacing``: una struttura ha annullato e il
    cittadino ha perso le offerte, quindi riparto. ``refresh``: il cittadino chiede altre date
    o ha dato preferenze nuove dopo aver visto le offerte."""

    agent = city.agent_id
    citizen = _citizen(chat)
    if citizen is None:
        return
    tone = _tone(chat)
    sent = _sent(key)
    latest = message if message.sender.startswith("citizen:") else None
    note = ""
    if latest is not None:
        note = _guard_note(
            tone,
            injected=_injection(latest.text),
            clinical_words=clinical(latest.text),
        )
    guard = note  # solo gli avvisi, senza la riga di lingua
    if _lang_line(chat):
        note = (_lang_line(chat) + "\n" + note).strip()

    # 1. Il numero di ricetta. Senza, lo chiedo (al massimo due volte, poi aspetto).
    number = _number(chat)
    if number is None:
        if latest is None:
            return
        asks = sum(
            1
            for row in _ours(chat, agent, citizen)
            if "purpose=booking" in row.text and "fields=prescription_number" in row.text
        )
        if asks >= 2:
            return
        prose = tr("ask_number", tone)
        text = "data.request purpose=booking fields=prescription_number\n" + prose
        if note:
            text += "\n" + note
        await _say(city, citizen, text)
        return

    # 2. Verifica della ricetta, agenda e regole in vigore.
    token = f"prescription_number={number}"
    first_contact = not _we(chat, agent, token, to=LEDGER, head="prescription")
    if first_contact and f"rx?{number}" not in sent.lines:
        sent.lines.add(f"rx?{number}")
        await _say(city, LEDGER, f"prescription {token}")
    agendas_before = _count_ledger(chat, "agenda")
    trigger = _trigger(message) if (replacing or refresh) else ""
    asked_agenda = any(row.text.strip() == "agenda" for row in _ours(chat, agent, LEDGER))
    fresh_agenda = bool(trigger) and f"agenda:{trigger}" not in sent.lines
    if (not asked_agenda and "agenda" not in sent.lines) or fresh_agenda:
        sent.lines.add("agenda")
        if trigger:
            sent.lines.add(f"agenda:{trigger}")
        await _say(city, LEDGER, "agenda")
    if "rules" not in sent.lines and not any(
        row.text.strip() == "rules" for row in _ours(chat, agent, LEDGER)
    ):
        sent.lines.add("rules")
        await _say(city, LEDGER, "rules")

    need_new_agenda = fresh_agenda
    chat = await _until(
        city,
        message.need_id,
        chat,
        lambda rows: _prescription_ok(rows, number)[0] != "pending"
        and _count_ledger(rows, "agenda") > (agendas_before if need_new_agenda else 0),
    )
    status, reply = _prescription_ok(chat, number)
    agenda = _ledger(chat, "agenda") or {}
    now = _when(agenda.get("agenda_now") or agenda.get("now") or agenda.get("at") or "")
    if status == "pending":
        return

    # 3. Ricetta non utilizzabile: lo dico chiaramente e indico i passi.
    if status == "ok" and _expired_by_rule(reply, now, chat):
        status = "expired"
    patient = reply.get("patient") or reply.get("citizen") or ""
    if status == "ok" and patient.startswith("citizen:") and patient != citizen:
        status = "not_yours"
    if status != "ok":
        await _prescription_problem(city, key, citizen, number, status, tone)
        return

    priority = (reply.get("priority") or "").upper() or None
    procedure = reply.get("procedure")
    if not procedure:
        return

    # 4. Priorità P: la risposta giusta è la lista d'attesa.
    if priority == "P":
        await _waiting_list(
            city, chat, key, citizen, number, procedure, priority, tone, reason="P", note=note
        )
        return

    # Preferenze: ricetta verificata, le chiedo una volta sola (giorni, genere del medico).
    days, gender, answered = _preferences(chat, agent, citizen)
    asked_at = _asked_prefs_at(chat, agent, citizen)
    asked_prefs = asked_at is not None
    if (
        (days is None) != (gender is None)
        and not asked_prefs
        and not (replacing or refresh)
    ):
        # Una preferenza è già nel primo messaggio: chiedo solo quella che manca.
        if "prefs" in sent.lines:
            return
        sent.lines.add("prefs")
        label = _label(procedure, tone)
        if days is not None:
            field = "gender"
            names = _WEEKDAYS.get(tone, _WEEKDAYS["en"])
            listed = ", ".join(names[_DAYS.index(code)] for code in _DAYS if code in days)
            prose = tr("ask_gender_only", tone, label=label, days=listed)
        else:
            field = "days"
            prose = tr("ask_days_only_f" if gender == "f" else "ask_days_only_m", tone, label=label)
        text = f"data.request purpose=booking fields={field}\n" + prose
        if note:
            text += "\n" + note
        await _say(city, citizen, text)
        return
    if (
        asked_at is not None
        and not replacing
        and not any(row.sender == citizen for row in chat[asked_at + 1 :])
    ):
        return  # ho chiesto la preferenza che mancava: aspetto la risposta
    if days is None and gender is None and not asked_prefs and not (replacing or refresh):
        if "prefs" in sent.lines:
            return
        sent.lines.add("prefs")
        prose = tr("prefs_ok", tone, label=_label(procedure, tone))
        why = _priority_text(priority, tone)
        if why:
            prose += " " + why
        prose += "\n" + tr("prefs_question", tone)
        text = "data.request purpose=booking fields=days,gender\n" + prose
        if note:
            text += "\n" + note
        await _say(city, citizen, text)
        return
    # Ho chiesto le preferenze e il cittadino non ha ancora risposto: aspetto il suo turno.
    if asked_prefs and not answered and not replacing:
        return
    offered = _offered_slots(chat, agent, citizen) | {
        slot for slot in sent.offers if not slot.startswith("wl-")
    }
    if offered and not (replacing or refresh):
        if guard and latest is not None:
            await _say(city, citizen, guard)
        return  # le offerte sono già sul tavolo: il cittadino sta scegliendo
    if now is None:
        return

    # 5. Il mondo della mia ASST, pagina per pagina finché trovo qualcosa.
    cancelled = _cancelled_slots(chat, agent, citizen)
    skip = cancelled | (offered if refresh else set())
    skip_fac = _unreachable(chat, agent)
    lang = _lang(chat)

    def pick(rows: list[Message], **prefs: object) -> list[dict[str, str]]:
        lines, _, _ = _world_lines(rows)
        return fitting(
            lines, now=now, priority=priority, lang=lang, procedure=procedure,
            skip=skip, skip_facilities=skip_fac, **prefs,  # type: ignore[arg-type]
        )

    # Pagine da leggere: con la gara lunga (1200 minuti, ~64 giorni di catalogo) le prime pagine
    # sono date passate, quindi salto direttamente a quelle da oggi in poi (_next_page).
    limit = _horizon(now, priority, extend=True)
    for _round in range(_MAX_PAGE):
        if _has_world(chat) and pick(chat, days=days, gender=gender):
            break
        wanted = _next_page(chat, now + _LEAD, limit)
        if wanted is None:
            break
        ask = f"world procedure={procedure} page={wanted}"
        if not any(row.text.strip() == ask for row in _ours(chat, agent, LEDGER)):
            await _say(city, LEDGER, ask)
        chat = await _until(
            city,
            message.need_id,
            chat,
            lambda rows, wanted=wanted: wanted in _page_spans(rows)[0],
        )
        if wanted not in _page_spans(chat)[0]:
            break
    if not _has_world(chat):
        return
    # Una nostra struttura sparita dalla discovery (ospedale giù): non offro i suoi slot.
    # Un nostro slot si salta solo dopo un errore vero di invio alla struttura (TARGET_NOT_VISIBLE,
    # vedi _unreachable): l'Index può non restituire un agente anche quando è online.

    strict = pick(chat, days=days, gender=gender)
    relaxed: list[dict[str, str]] = []
    if not strict and (days or gender):
        relaxed = pick(chat)
    # (Niente estensione della scadenza ai giorni di agenda: la città conta i giorni normali e
    # rifiuta uno slot oltre la scadenza con reason=deadline.)
    if strict or relaxed:
        await _send_offers(
            city, chat, key, citizen, strict or relaxed,
            number=number, priority=priority, procedure=procedure, tone=tone,
            relaxed=not strict, replacing=replacing, note=note, trigger=trigger,
        )
        return

    if refresh and offered and not declined:
        # Nessuna data in più: lo dico con chiarezza e lascio valide quelle già proposte.
        prose = tr("nomore", tone)
        await _once(city, key, f"nomore:{trigger}", citizen, (note + "\n" + prose).strip())
        return

    # 6. Niente nella mia ASST: cerco nelle ASST partner, solo con i criteri.
    lines, _, _ = _world_lines(chat)
    if not (declined and "cross" in sent.flags) and await _cross_asst(
        city, chat, key, citizen, procedure, priority, _own_facilities(lines), tone, note
    ):
        return
    await _waiting_list(
        city, chat, key, citizen, number, procedure, priority, tone, reason="empty", note=note
    )


async def _citizen_declined(city: City, message: Message, key: str) -> None:
    """Il cittadino ha rifiutato un'offerta. Se ne restano altre aspetto (spesso i rifiuti
    arrivano uno per offerta, insieme). Quando le ha rifiutate tutte cerco altre date entro la
    scadenza; se non ci sono provo le ASST partner e poi propongo la lista d'attesa."""

    try:
        chat = await city.history(need_id=message.need_id)
    except CapError:
        chat = []
    if all(row.msg_id != message.msg_id for row in chat):
        chat = [*chat, message]
    agent = city.agent_id
    citizen = message.sender
    offers = _our_offers(chat, agent, citizen)
    gone = _cancelled_slots(chat, agent, citizen)
    still_open = [
        oid for oid, offer in offers.items()
        if not offer.get("waiting_list") and offer.get("slot_id") not in gone
    ]
    if still_open:
        return
    if any(offer.get("waiting_list") for offer in offers.values()) and not any(
        offer.get("slot_id") and offer["slot_id"] not in gone for offer in offers.values()
    ) and fields_of(message.text).get("offer_id", "").startswith("wl-"):
        await _release(city, key, citizen, _number(chat), _tone(chat))
        return
    if _sent(key).lines & {f"declined-all:{len(offers)}"}:
        return
    _sent(key).lines.add(f"declined-all:{len(offers)}")
    await _advance(city, message, chat, key, replacing=False, refresh=True, declined=True)


def _count_ledger(chat: list[Message], head: str) -> int:
    return sum(1 for row in chat if row.sender == LEDGER and first_word(row.text) == head)


def _trigger(message: Message) -> str:
    return fields_of(message.text).get("offer_id") or message.msg_id


def _offered_slots(chat: list[Message], agent: str, citizen: str) -> set[str]:
    return {
        offer.get("slot_id", "")
        for offer in _our_offers(chat, agent, citizen).values()
        if offer.get("slot_id")
    }


def _cancelled_slots(chat: list[Message], agent: str, citizen: str) -> set[str]:
    offers = _our_offers(chat, agent, citizen)
    gone: set[str] = set()
    for row in chat:
        heads = line_heads(row.text) | (words(row.text) if row.sender == citizen else set())
        if "cancel" not in heads and "refused" not in heads:
            continue
        fields = fields_of(row.text)
        offer = offers.get(fields.get("offer_id", ""))
        if offer and offer.get("slot_id"):
            gone.add(offer["slot_id"])
        if fields.get("slot_id"):
            gone.add(fields["slot_id"])
    return gone


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


def _unreachable(chat: list[Message], agent: str) -> set[str]:
    found: set[str] = set()
    for row in _ours(chat, agent):
        for line in row.text.splitlines():
            if first_word(line) == "cancel" and "reason=facility_unreachable" in line:
                facility = fields_of(line).get("facility")
                if facility:
                    found.add(facility)
    return found


async def _prescription_problem(
    city: City, key: str, citizen: str, number: str, status: str, tone: str
) -> None:
    if status == "expired":
        text = (
            "data.request purpose=prescription_renewal fields=prescription_number\n"
            + tr("renew", tone, number=number)
        )
        await _once(city, key, f"renew:{number}", citizen, text)
        return
    phrase = {"not_yours": "rx_not_yours", "used": "rx_used"}.get(status, "rx_not_found")
    text = "data.request purpose=booking fields=prescription_number\n" + tr(phrase, tone)
    await _once(city, key, f"rx:{status}:{number}", citizen, text)


async def _send_offers(
    city: City,
    chat: list[Message],
    key: str,
    citizen: str,
    slots: list[dict[str, str]],
    *,
    number: str | None,
    priority: str | None,
    procedure: str | None,
    tone: str,
    relaxed: bool,
    replacing: bool,
    note: str,
    partner: bool = False,
    trigger: str = "",
    beyond: bool = False,
) -> None:
    sent = _sent(key)
    if replacing:
        # Dopo un annullamento il cittadino ha perso le offerte: le ripropongo, una volta sola.
        tag = f"re:{trigger}"
        if tag in sent.lines:
            return
        picked = slots[:_MAX_OFFERS]
    else:
        tag = ""
        picked = [slot for slot in slots if slot["slot_id"] not in sent.offers][:_MAX_OFFERS]
    if not picked:
        return
    label = _label(procedure, tone)
    kind = "offers_beyond" if beyond else "offers_replacing" if replacing else "offers_partner" if partner else "offers_relaxed" if relaxed else "offers_normal"
    intro = tr(kind, tone, label=label)
    why = "" if beyond else _priority_text(priority, tone)
    if why:
        intro = why + " " + tr("prio_window", tone) + "\n" + intro
    if note:
        intro = note + "\n" + intro
    ids = [slot.get("offer_id") or f"of-{slot['slot_id']}" for slot in picked]
    text = _offers_text(
        picked, number=number, priority=priority, procedure=procedure, tone=tone, intro=intro, ids=ids
    )
    if await _say(city, citizen, text):
        sent.offers.update(slot["slot_id"] for slot in picked)
        if tag:
            sent.lines.add(tag)


async def _waiting_list(
    city: City,
    chat: list[Message],
    key: str,
    citizen: str,
    number: str | None,
    procedure: str | None,
    priority: str | None,
    tone: str,
    *,
    reason: str,
    note: str,
) -> None:
    if reason != "P" and (priority or "").upper() != "P":
        # Regola della squadra: lista d'attesa solo per la priorità P. Per B, D e U propongo
        # l'alternativa rilassata (date subito dopo la scadenza); se non c'è, rilascio.
        await _beyond_or_release(city, chat, key, citizen, number, procedure, priority, tone, note)
        return
    offer_id = f"wl-{key}"
    sent = _sent(key)
    if offer_id in sent.offers or _we(chat, city.agent_id, f"offer_id={offer_id}", to=citizen, head="offer"):
        return
    label = _label(procedure, tone)
    parts = ["offer", f"offer_id={offer_id}", "waiting_list=true"]
    if procedure:
        parts.append(f"procedure={procedure}")
    if priority:
        parts.append(f"priority={priority}")
    if number:
        parts.append(f"prescription_number={number}")
    prose = tr("wl_P" if reason == "P" else "wl_empty", tone, label=label)
    prose += "\n" + tr("wl_steps", tone)
    if note:
        prose = note + "\n" + prose
    if await _say(city, citizen, prose + "\n" + " ".join(parts)):
        sent.offers.add(offer_id)


async def _beyond_or_release(
    city: City,
    chat: list[Message],
    key: str,
    citizen: str,
    number: str | None,
    procedure: str | None,
    priority: str | None,
    tone: str,
    note: str,
) -> None:
    """Nessun posto entro la scadenza (né da noi né dalle partner): una sola volta propongo le
    date più vicine dopo la scadenza, dette in modo trasparente. Se non ce ne sono, o se il
    cittadino le ha già rifiutate, rilascio la richiesta."""

    sent = _sent(key)
    agenda = _ledger(chat, "agenda") or {}
    now = _when(agenda.get("agenda_now") or agenda.get("now") or agenda.get("at") or "")
    if "beyond" not in sent.lines and now is not None and procedure:
        need_id = next((row.need_id for row in reversed(chat) if row.need_id), None)
        agent = city.agent_id
        for _round in range(4):  # qualche pagina in più: le date oltre la scadenza stanno dopo
            spans, total = _page_spans(chat)
            unseen = [page for page in range(1, total + 1) if page not in spans]
            if not unseen or len(spans) >= total:
                break
            later = [page for page in unseen if page > max(spans, default=0)] or unseen
            ask = f"world procedure={procedure} page={later[0]}"
            if not any(row.text.strip() == ask for row in _ours(chat, agent, LEDGER)):
                await _say(city, LEDGER, ask)
            chat = await _until(city, need_id, chat, lambda rows, p=later[0]: p in _page_spans(rows)[0])
            if later[0] not in _page_spans(chat)[0]:
                break
        lines, _, _ = _world_lines(chat)
        skip = _cancelled_slots(chat, agent, citizen) | _offered_slots(chat, agent, citizen)
        later_slots = fitting(
            lines, now=now, priority=None, lang=_lang(chat), procedure=procedure,
            skip=skip, skip_facilities=_unreachable(chat, agent),
        )
        if later_slots:
            sent.lines.add("beyond")
            await _send_offers(
                city, chat, key, citizen, later_slots, number=number, priority=priority,
                procedure=procedure, tone=tone, relaxed=True, replacing=False, note=note, beyond=True,
            )
            return
    await _release(city, key, citizen, number, tone)


async def _release(city: City, key: str, citizen: str, number: str | None, tone: str) -> None:
    """Chiudo con garbo: niente prenotazione, niente dati fuori, la porta resta aperta."""

    head = "cancel reason=released" + (f" prescription_number={number}" if number else "")
    await _once(city, key, "released", citizen, head + "\n" + tr("released", tone))


# --- ASST partner ------------------------------------------------------------------------------------------


async def _cross_asst(
    city: City,
    chat: list[Message],
    key: str,
    citizen: str,
    procedure: str,
    priority: str | None,
    own: set[str],
    tone: str,
    note: str,
) -> bool:
    """Chiede disponibilità alle strutture delle ASST partner, solo con i criteri di ricerca,
    e chiede al cittadino il consenso ``cross_asst`` prima di condividere qualsiasi suo dato.
    True se la ricerca è partita (ora o prima)."""

    sent = _sent(key)
    if "cross" in sent.flags or any(
        "need.request" in line_heads(row.text) and "service_type=" in row.text
        for row in _ours(chat, city.agent_id)
    ):
        return True
    service = _SERVICE_OF.get(procedure)
    if not service:
        return False
    try:
        peers = await city.find(service)
    except CapError:
        peers = []
    # Le nostre strutture non sono partner, anche se oggi non hanno slot nel mondo.
    own = set(own) | set(_team_facilities().values())
    partners = [
        peer
        for peer in peers
        if peer.agent_id not in own
        and peer.agent_id != city.agent_id
        and peer.role not in {"booking_centre", "gp", "ledger"}
        and (service in peer.capabilities or peer.service == service)
    ][:_MAX_PARTNERS]
    if not partners:
        return False
    sent.flags.add("cross")
    reached = 0
    for index, peer in enumerate(partners, start=1):
        parts = [
            "need.request",
            f"service_type={service}",
            f"procedure={procedure}",
            f"request_id={key}-x{index}",
        ]
        if priority:
            parts.append(f"priority={priority}")
        text = " ".join(parts) + (
            "\nBuongiorno, il Centro prenotazioni di un'ASST partner cerca disponibilità per "
            "questa prestazione. In questa fase non condividiamo dati del paziente: se avete "
            "posti, ci mandi pure le offerte con slot_id e doctor_id."
        )
        if await _say(city, peer.agent_id, text):
            reached += 1
    if reached == 0:
        return False
    prose = tr("cross_search", tone, label=_label(procedure, tone))
    if note:
        prose = note + "\n" + prose
    await _once(city, key, "cross_consent", citizen, "consent purpose=cross_asst\n" + prose)
    return True


def _consent_ref(chat: list[Message], purpose: str, agent: str, citizen: str) -> str | None:
    """L'ultimo consent_ref che il cittadino ha dato per ``purpose``. Se non dice lo scopo,
    vale quello dell'ultima richiesta di consenso che gli abbiamo fatto (di default booking)."""

    last_asked = "booking"
    found: str | None = None
    for row in chat:
        if row.sender == agent and row.recipient == citizen and "consent" in line_heads(row.text):
            last_asked = fields_of(row.text).get("purpose", last_asked)
            continue
        if row.sender != citizen:
            continue
        fields = fields_of(row.text)
        ref = fields.get("consent_ref")
        if not ref:
            hit = _CONSENT_BARE.search(row.text)
            ref = hit.group(1) if hit else None
        if not ref:
            continue
        said = fields.get("purpose") or ("cross_asst" if "cross" in ref else last_asked)
        if said == purpose:
            found = ref
    return found


async def _partner_offer(city: City, message: Message, chat: list[Message], key: str) -> None:
    """Offerte di una struttura partner: le verifico e le giro al cittadino."""

    citizen = _citizen(chat)
    if citizen is None:
        return
    number = _number(chat)
    status, reply = _prescription_ok(chat, number) if number else ("pending", {})
    if status != "ok":
        return
    priority = (reply.get("priority") or "").upper() or None
    procedure = reply.get("procedure")
    agenda = _ledger(chat, "agenda") or {}
    now = _when(agenda.get("agenda_now") or agenda.get("now") or "")
    if now is None:
        return
    lines = []
    for line in message.text.splitlines():
        if first_word(line) != "offer":
            continue
        fields = fields_of(line)
        fields.setdefault("facility", message.sender)
        if not fields.get("at") and fields.get("agenda_date"):
            fields["at"] = fields["agenda_date"]
        lines.append(" ".join(f"{k}={v}" for k, v in fields.items()))
    if not lines:
        return
    world, _, _ = _world_lines(chat)
    if message.sender in _own_facilities(world) or message.sender in _team_facilities().values():
        return
    usable = fitting(lines, now=now, priority=priority, lang=_lang(chat), procedure=procedure,
                     skip=_cancelled_slots(chat, city.agent_id, citizen))
    # La facility partner deve restare quella che ha offerto lo slot.
    for slot in usable:
        slot["facility"] = message.sender
    if usable:
        await _send_offers(
            city, chat, key, citizen, usable,
            number=number, priority=priority, procedure=procedure, tone=_tone(chat),
            relaxed=False, replacing=False, note="", partner=True,
        )


async def _serve_partner(city: City, message: Message, chat: list[Message], key: str) -> None:
    """Un'altra ASST cerca posti da noi (anello tra ASST). Rispondo subito con gli slot liberi
    della nostra ASST, solo criteri e nessun dato personale. La prenotazione la conferma
    direttamente la nostra struttura, che è indicata con il suo id agente."""

    fields = fields_of(message.text)
    procedure = fields.get("procedure")
    if not procedure:
        service = fields.get("service_type", "")
        procedure = next((code for code, svc in _SERVICE_OF.items() if svc == service), None)
    request_id = fields.get("request_id", key)
    if not procedure:
        await _ack(city, message, key, "Buongiorno, per cercare posti mi serve il codice della prestazione (procedure=...). Grazie!")
        return
    priority = (fields.get("priority") or "").upper() or None
    agent = city.agent_id
    ask = f"world procedure={procedure} page=1"
    if not any(row.text.strip() == ask for row in _ours(chat, agent, LEDGER)):
        await _say(city, LEDGER, ask)
    if not any(row.text.strip() == "agenda" for row in _ours(chat, agent, LEDGER)):
        await _say(city, LEDGER, "agenda")
    chat = await _until(
        city, message.need_id, chat,
        lambda rows: _has_world(rows) and _ledger(rows, "agenda") is not None,
    )
    agenda = _ledger(chat, "agenda") or {}
    now = _when(agenda.get("agenda_now") or agenda.get("now") or "")
    for _round in range(_MAX_PAGE):
        if now is None or priority == "P":
            break
        lines, _, _ = _world_lines(chat)
        if fitting(lines, now=now, priority=priority, lang=fields.get("lang", "it"), procedure=procedure):
            break
        wanted = _next_page(chat, now + _LEAD, _horizon(now, priority, extend=True))
        if wanted is None:
            break
        ask = f"world procedure={procedure} page={wanted}"
        if not any(row.text.strip() == ask for row in _ours(chat, agent, LEDGER)):
            await _say(city, LEDGER, ask)
        chat = await _until(
            city, message.need_id, chat, lambda rows, wanted=wanted: wanted in _page_spans(rows)[0]
        )
        if wanted not in _page_spans(chat)[0]:
            break
    lines, _, _ = _world_lines(chat)
    slots = (
        fitting(lines, now=now, priority=priority, lang=fields.get("lang", "it"), procedure=procedure)
        if now is not None
        else []
    )
    if priority == "P":
        slots = []
    if not slots:
        await _once(
            city, key, f"partner-none:{request_id}", message.sender,
            f"cancel request_id={request_id} procedure={procedure} reason=no_availability\n"
            "Buongiorno, al momento la nostra ASST non ha posti liberi per questa prestazione "
            "nei tempi richiesti. Grazie e buon lavoro!",
        )
        return
    out = ["Buongiorno, ecco le disponibilità della nostra ASST (nessun dato del paziente "
           "necessario finora). Per prenotare inviate confirm direttamente alla struttura "
           "indicata in facility, con slot_id, consent_ref e prescription_number."]
    for slot in slots[:_MAX_OFFERS]:
        target = await _facility_agent(city, slot.get("facility", ""), procedure)
        parts = [
            "offer", f"offer_id=px-{slot['slot_id']}", f"slot_id={slot['slot_id']}",
            f"doctor_id={slot['doctor_id']}", f"facility={target}", f"procedure={procedure}",
            f"agenda_date={slot['at']}", f"agenda_day={slot['day']}", f"expiry={slot['at']}",
            f"request_id={request_id}",
        ]
        if priority:
            parts.append(f"priority={priority}")
        out.append(" ".join(parts))
    await _once(city, key, f"partner-offers:{request_id}", message.sender, "\n".join(out))


# --- scelta, consenso, inoltro alla struttura -----------------------------------------------------------------


def _pending_choice(chat: list[Message], agent: str, citizen: str) -> str | None:
    """L'offerta scelta dal cittadino che aspetta ancora un consenso."""

    offers = _our_offers(chat, agent, citizen)
    chosen: str | None = None
    for row in chat:
        if row.sender == citizen and "cancel" not in words(row.text):
            pick = _chosen_offer(row.text, offers)
            if pick:
                chosen = pick
    if not chosen:
        return None
    slot = offers[chosen].get("slot_id", "")
    if slot and _confirmed_slot(chat, agent, slot):
        return None
    return chosen



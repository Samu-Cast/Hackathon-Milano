# booking_centre: parte_4.py di 5 (vedi agent.py)
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
    if number is None and latest is not None:
        number = await _llm_number(city, latest)
        if number:
            sent.lines.add(f"llm-number:{number}")
            sent.flags.add(f"number={number}")
    if number is None:
        number = next((f.split("=", 1)[1] for f in sent.flags if f.startswith("number=")), None)
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
    period = _period(chat, agent, citizen)
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
    if days is None and gender is None and period is None and not asked_prefs and not (replacing or refresh):
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
        if latest is not None and _repeat_request(chat, agent, latest):
            await _resend_offers(city, chat, key, citizen, latest)
            return
        if guard and latest is not None:
            await _say(city, citizen, guard)
        return  # le offerte sono già sul tavolo: il cittadino sta scegliendo
    if now is None:
        return

    # 5. Il mondo della mia ASST, pagina per pagina finché trovo qualcosa.
    cancelled = _cancelled_slots(chat, agent, citizen) | _citizen_cancelled(key)
    skip = cancelled | (offered if refresh else set())
    skip_fac = _unreachable(chat, agent)
    lang = _lang(chat)

    bad_doctors = _rejected_doctors(chat, agent, citizen)

    def pick(rows: list[Message], **prefs: object) -> list[dict[str, str]]:
        lines, _, _ = _world_lines(rows)
        return fitting(
            lines, now=now, priority=priority, lang=lang, procedure=procedure,
            skip=skip, skip_facilities=skip_fac, skip_doctors=bad_doctors,
            **prefs,  # type: ignore[arg-type]
        )

    # Pagine da leggere: con la gara lunga (1200 minuti, ~64 giorni di catalogo) le prime pagine
    # sono date passate, quindi salto direttamente a quelle da oggi in poi (_next_page).
    limit = _horizon(now, priority, extend=True)
    for _round in range(_MAX_PAGE):
        if _has_world(chat) and pick(chat, days=days, gender=gender, period=period):
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

    strict = pick(chat, days=days, gender=gender, period=period)
    relaxed: list[dict[str, str]] = []
    if not strict and period and (days or gender):
        relaxed = pick(chat, days=days, gender=gender)  # prima rinuncio solo alla fascia oraria
    if not strict and not relaxed and (days or gender or period):
        relaxed = pick(chat)
    if _mobility_need(chat):
        def rank(slot: dict[str, str]) -> int:
            return 0 if _wheelchair_ok(chat, slot.get("facility", "")) else 1
        strict = sorted(strict, key=rank)
        relaxed = sorted(relaxed, key=rank)
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
    if _cross_started(chat, agent) and await _offer_partner_reserve(
        city, chat, key, citizen, number, procedure, priority, tone, note,
        replacing=replacing, exclude_offered=refresh, trigger=trigger,
    ):
        return  # riserve delle ASST partner ancora compatibili (livello 4)
    if latest is not None and _cross_started(chat, agent) and await _consent_followup(city, chat, key, citizen, latest):
        return  # risposta (o domanda) sul consenso cross_asst: rispondo e la ricerca resta aperta
    if _partner_search_stalled(chat, agent, citizen, latest, sent):
        # Il cittadino ha riscritto e le ASST partner non hanno ancora mandato posti: chiudo la
        # ricerca (i nostri posti li ho appena ricontrollati) e propongo l'alternativa.
        await _no_slot_fallback(city, chat, key, citizen, number, procedure, priority, tone, note=note)
        return
    if not ((declined or replacing) and ("cross" in sent.flags or _cross_started(chat, agent))) and await _cross_asst(
        city, chat, key, citizen, procedure, priority, _own_facilities(lines), tone, note
    ):
        return
    await _no_slot_fallback(city, chat, key, citizen, number, procedure, priority, tone, note=note)


def _partner_search_stalled(
    chat: list[Message], agent: str, citizen: str, latest: Message | None, sent: _Sent
) -> bool:
    """Ricerca presso le partner partita, nessuna loro offerta arrivata, e il cittadino ha
    riscritto (non per dare un consenso o i dati richiesti)."""

    if latest is None or "cross" not in sent.flags:
        return False
    heads = line_heads(latest.text) | words(latest.text)
    if heads & {"consent", "data.response"} or fields_of(latest.text).get("consent_ref") or _CONSENT_BARE.search(latest.text):
        return False
    if _consent_question(latest.text) or _affirmative(latest.text):
        return False  # risponde (o chiede) del consenso: la ricerca presso le partner resta aperta
    asked = [i for i, row in enumerate(chat) if row.sender == agent and "need.request" in line_heads(row.text)]
    if not asked or not any(row.sender == citizen for row in chat[asked[0] + 1:]):
        return False
    return not any(
        row.sender.startswith("agent:") and row.sender != agent and "offer" in line_heads(row.text)
        for row in chat[asked[0]:]
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
    waiting = [oid for oid, offer in offers.items() if offer.get("waiting_list")]
    if waiting and not any(offer.get("slot_id") and offer["slot_id"] not in gone for offer in offers.values()):
        # Annulla prima di accettare la lista d'attesa: non lo iscrivo e glielo confermo.
        offer_id = fields_of(message.text).get("offer_id") or waiting[-1]
        _sent(key).flags.add(f"cancelled:{offer_id}")
        await _once(
            city, key, f"wl-left:{offer_id}", citizen,
            f"cancel offer_id={offer_id} reason=citizen_cancelled\n" + tr("wl_left", _tone(chat)),
        )
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
    intro_text: str = "",
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
    intro = intro_text or tr(kind, tone, label=label)
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
    open_slots = _offered_slots(chat, city.agent_id, citizen) - _cancelled_slots(chat, city.agent_id, citizen) - _citizen_cancelled(key)
    if open_slots:
        return  # ci sono offerte ancora aperte: aspetto che il cittadino scelga o rifiuti
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
        bad_doctors = _rejected_doctors(chat, agent, citizen)
        later_slots = fitting(
            lines, now=now, priority=None, lang=_lang(chat), procedure=procedure,
            skip=skip, skip_facilities=_unreachable(chat, agent), skip_doctors=bad_doctors,
        )
        # Seconda alternativa: entro la scadenza, allentando la lingua del medico (se ne aveva
        # chiesta una diversa dall'italiano). Le due vanno nello stesso messaggio.
        doctor_lang = _lang(chat)
        horizon = _horizon(now, priority)
        still_in_time = [slot for slot in later_slots if horizon is None or (_when(slot["at"]) or now) < horizon]
        if still_in_time:
            # Trovate solo leggendo altre pagine, ma ENTRO la scadenza: offerte normali, senza
            # dire "dopo la scadenza".
            await _send_offers(
                city, chat, key, citizen, still_in_time, number=number, priority=priority,
                procedure=procedure, tone=tone, relaxed=False, replacing=False, note=note,
            )
            return
        in_time: list[dict[str, str]] = []
        alt_lang = ""
        if doctor_lang not in ("", "it"):
            # Allento la lingua del medico, entro la scadenza: prima la lingua in cui il cittadino
            # ci scrive (la conosce), poi l'italiano, ma non se ha già rifiutato per lingua.
            chat_lang = _tone(chat)
            tries = [code for code in (chat_lang,) if code not in ("", "it", doctor_lang)]
            if not _rejected_for(chat, agent, citizen, "lang"):
                tries.append("it")
            for code in tries:
                in_time = fitting(
                    lines, now=now, priority=priority, lang=code, procedure=procedure,
                    skip=skip | {slot["slot_id"] for slot in later_slots},
                    skip_facilities=_unreachable(chat, agent), skip_doctors=bad_doctors,
                )
                if in_time:
                    alt_lang = code
                    break
        if later_slots or in_time:
            sent.lines.add("beyond")
            picked = (later_slots[:2] + in_time[:1]) if (later_slots and in_time) else (later_slots or in_time)
            intro = _relaxed_intro(tone, procedure, doctor_lang, bool(later_slots), bool(in_time), alt_lang or "it")
            await _send_offers(
                city, chat, key, citizen, picked, number=number, priority=priority,
                procedure=procedure, tone=tone, relaxed=True, replacing=False, note=note, beyond=True,
                intro_text=intro,
            )
            return
    await _release(city, key, citizen, number, tone)


async def _release(city: City, key: str, citizen: str, number: str | None, tone: str) -> None:
    """Chiudo con garbo: niente prenotazione, niente dati fuori, la porta resta aperta."""

    head = "cancel reason=released" + (f" prescription_number={number}" if number else "")
    await _once(city, key, "released", citizen, head + "\n" + tr("released", tone))


async def _no_slot_fallback(
    city: City,
    chat: list[Message],
    key: str,
    citizen: str,
    number: str | None,
    procedure: str | None,
    priority: str | None,
    tone: str,
    *,
    note: str,
    allow_partner: bool = True,
) -> None:
    """Nessun posto nei tempi, né da noi né (finora) dai partner.

    La lista d'attesa è SOLO per la priorità P. Per U, B e D propongo posti reali: le prime
    date libere subito dopo la scadenza, dette in modo trasparente. Se non ci sono nemmeno
    quelle, lo dico e resto disponibile, senza mai mettere una B o una D in lista d'attesa."""

    if priority == "P":
        await _waiting_list(city, chat, key, citizen, number, procedure, priority, tone, reason="P", note=note)
        return
    # Stesso ripiego per tutte le strade senza posti (U, B, D): le date subito dopo la scadenza,
    # anche leggendo altre pagine del mondo; se non ci sono, o se il cittadino le ha già
    # rifiutate, rilascio la richiesta con il passo successivo. Mai lista d'attesa.
    await _beyond_or_release(city, chat, key, citizen, number, procedure, priority, tone, note)


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
        # Ricerca già partita: resta aperta solo se qualche partner può ancora rispondere.
        return bool(_partners_pending(chat, city.agent_id))
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
    ]
    # MACH-1: prima le strutture pubblicate dalle squadre (e quella con la lingua rara chiesta).
    partners = _ranked_partners(partners, _lang(chat))[:_MAX_PARTNERS]
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
        deadline = _request_deadline(chat, priority)
        if deadline:
            parts.append(f"deadline={deadline}")
        parts += _mach_criteria(chat, city.agent_id, citizen)
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
    await _once(city, key, "cross_consent", citizen, _consent_message("cross_asst", key, tone, prose))
    await _wait_partners(city, chat, key, citizen, tone, note)
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
        is_choice = bool(fields.get("offer_id")) or "confirm" in (line_heads(row.text) | words(row.text))
        said = fields.get("purpose") or (
            "cross_asst" if "cross" in ref
            else "booking" if ("booking" in ref or is_choice)
            else last_asked
        )
        if said == purpose:
            found = ref
    return found or _consent_by_words(chat, purpose, agent, citizen)


async def _partner_offer(city: City, message: Message, chat: list[Message], key: str) -> None:
    """Offerte di una struttura partner: le verifico e le giro al cittadino."""

    await _on_partner_offer(city, message, chat, key)  # livello 4: vedi parte_6.py
    return
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
           "necessario finora). Per prenotare scrivete direttamente alla struttura indicata "
           "in facility, con slot_id, consent_ref e prescription_number."]
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
    current = _current_offer_ids(chat, agent, citizen)
    chosen: str | None = None
    for row in chat:
        if row.sender == citizen and "cancel" not in words(row.text):
            pick = _chosen_offer(row.text, offers) or (
                _chosen_by_words(row.text, offers, current) if not _number_in(row.text) else None
            )
            if pick:
                chosen = pick
    if not chosen:
        return None
    slot = offers[chosen].get("slot_id", "")
    if slot and _confirmed_slot(chat, agent, slot):
        return None
    return chosen


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
        await _confirm_pending(city, message, chat, key, offer_id, offer, slot)
        return

    world, _, _ = _world_lines(chat)
    # Una struttura è nostra solo se compare nel nostro mondo o è uno dei nostri agenti in gara.
    # (Prima: con il mondo vuoto ogni struttura risultava nostra e i dati uscivano senza il
    # consenso cross_asst.)
    cross = facility not in (_own_facilities(world) | set(_team_facilities().values()))
    booking_ref = fields_of(message.text).get("consent_ref") if not cross else None
    booking_ref = booking_ref or _consent_ref(chat, "booking", agent, citizen)
    cross_ref = _consent_ref(chat, "cross_asst", agent, citizen) if cross else None

    if cross and not cross_ref:
        # Regola P2 e privacy: nessun dato del cittadino esce dall'ASST prima del consenso.
        asked = _we(chat, agent, "purpose=cross_asst", to=citizen, head="consent")
        asked_for_this = _we(chat, agent, f"offer_id={offer_id}", to=citizen, head="consent")
        # Ha scelto proprio un'offerta di un'altra ASST: gli chiedo il consenso per QUESTA offerta,
        # anche se prima sembrava averlo negato. Solo un no a questa domanda porta all'alternativa.
        declined = asked_for_this and _declined_after(chat, agent, citizen, "cross_asst")
        if declined:
            await _locked_free_alternative(city, message, chat, key, facility)
            return
        prose = tr("cross_choice", tone)
        await _say(city, citizen, _consent_message("cross_asst", key, tone, prose, offer_id))
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
            await _say(city, citizen, _consent_message("booking", key, tone, prose, offer_id))
        elif _consent_question(message.text):
            # "Which consent_ref?": non è un rifiuto. Rimando la richiesta con il riferimento.
            await _once(city, key, f"consent-again:{message.msg_id}", citizen,
                        _consent_head("booking", key, offer_id) + "\n" + _consent_help("booking", key, tone))
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
    if cross_ref:
        parts.append(f"cross_asst_consent_ref={cross_ref}")  # MACH-1: serve alla partner per l'hold
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



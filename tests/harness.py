"""Banco di prova locale per i brain di asst/.

Usa le classi reali dell'SDK (``City``, ``Message``, ``Need``, ``Peer``) e il ``MemoryRelay``
di ``city_sdk.ports`` come mailbox. Il Ledger qui sotto è un SOSTITUTO: i formati delle sue
risposte (``slots page=1/1``, ``held hold_id=…``, ``refused hold code=…``) sono quelli che leggono
l'esempio del kit (templates/examples/agent.py) e il sostituto locale dell'SDK
(city_sdk/ports.py, LocalLedger). Il formato reale di ``rules``, ``history`` e dei rifiuti non è
documentato nel kit: un test che passa qui NON prova una proprietà della piattaforma.

Come gira:
- ogni agente ha la sua mailbox (``MemoryRelay``); ``say`` verso un altro agente registra il
  messaggio anche nella mailbox del destinatario e mette in coda un suo turno;
- ``say`` al Ledger riceve la risposta subito nella history (il brain la può rileggere nello
  stesso turno) e in coda come turno successivo, come scrive il kit;
- i messaggi ai cittadini finiscono in ``town.inbox[citizen]``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import itertools
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from city_sdk.city import LEDGER, CapError, City, Message, Need, Peer
from city_sdk.ports import MemoryIndex, MemoryRelay

ROOT = Path(__file__).resolve().parents[1]
_ids = itertools.count(1)


def load_brain(folder: str):
    """Carica asst/<folder>/agent/agent.py come modulo a sé, con le attese accorciate."""

    path = ROOT / "asst" / folder / "agent" / "agent.py"
    name = f"brain_{folder}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    for attr, value in (("_PULL_PAUSE", 0.001), ("_PULL_TRIES", 6)):
        if hasattr(module, attr):
            setattr(module, attr, value)
    return module


# --- Ledger sostituto ---------------------------------------------------------------------------------

NOW = datetime(2026, 11, 23, 8, 0, tzinfo=timezone.utc)  # lunedì 08:00 di agenda


def slot_line(slot_id: str, doctor: str, facility: str, procedure: str, at: datetime, *, reserve: bool = False) -> str:
    day = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[at.weekday()]
    return (
        f"slot_id={slot_id} doctor_id={doctor} facility={facility} procedure={procedure} "
        f"at={at.isoformat()} day={day} free=1 urgent_reserve={'true' if reserve else 'false'}"
    )


@dataclass
class Rx:
    number: str
    patient: str
    procedure: str
    priority: str
    issued: datetime = NOW
    state: str = "issued"


@dataclass
class Ledger:
    slots: dict[str, str] = field(default_factory=dict)  # slot_id -> riga di world
    rx: dict[str, Rx] = field(default_factory=dict)
    rules: dict[str, str] = field(default_factory=lambda: {"P1": "off", "P2": "off", "P3": "off"})
    now: datetime = NOW
    holds: dict[str, dict[str, str]] = field(default_factory=dict)
    bookings: dict[str, dict[str, str]] = field(default_factory=dict)
    checkins: dict[str, str] = field(default_factory=dict)
    reports: dict[str, dict[str, str]] = field(default_factory=dict)
    writes: list[tuple[str, str]] = field(default_factory=list)  # (mittente, testo)
    # Fetta di mondo per ASST: agente -> etichette di struttura che vede con ``world``.
    # Un agente assente da questa tabella vede le strutture di nessuna ASST partner.
    scope: dict[str, set[str]] = field(default_factory=dict)
    partner_labels: set[str] = field(default_factory=set)
    _n: Any = field(default_factory=lambda: itertools.count(1))

    def add_slot(self, slot_id: str, doctor: str, facility: str, procedure: str, at: datetime, *, reserve: bool = False) -> None:
        self.slots[slot_id] = slot_line(slot_id, doctor, facility, procedure, at, reserve=reserve)

    def _visible(self, sender: str, line: str) -> bool:
        label = next((t.split("=", 1)[1] for t in line.split() if t.startswith("facility=")), "")
        if sender in self.scope:
            return label in self.scope[sender]
        return label not in self.partner_labels

    def taken(self, slot_id: str) -> bool:
        return any(b["slot_id"] == slot_id for b in self.bookings.values())

    def answer(self, sender: str, text: str, need_id: str | None) -> str:
        self.writes.append((sender, text))
        words = text.split()
        op = words[0].lower() if words else ""
        f = dict(w.split("=", 1) for w in words[1:] if "=" in w)
        if op == "agenda":
            return f"agenda agenda_now={self.now.isoformat()}"
        if op == "rules":
            return "rules " + " ".join(f"{k}={v}" for k, v in self.rules.items())
        if op == "world":
            proc = f.get("procedure")
            rows = [
                line for sid, line in self.slots.items()
                if not self.taken(sid) and (not proc or f"procedure={proc}" in line)
                and self._visible(sender, line)
            ]
            return "\n".join([f"slots page=1/1 procedure={proc or '-'}", *rows])
        if op == "prescription":
            number = f.get("prescription_number", "")
            rx = self.rx.get(number)
            if rx is None:
                return f"refused prescription prescription_number={number} code=NOT_FOUND"
            return (
                f"prescription prescription_number={rx.number} patient={rx.patient} "
                f"procedure={rx.procedure} priority={rx.priority} state={rx.state} "
                f"issued_at={rx.issued.isoformat()}"
            )
        if op == "prescribe":
            number = f.get("prescription_number") or f"prescription_number-L{next(self._n):04d}"
            if number in self.rx:
                return f"refused prescribe prescription_number={number} code=DUPLICATE"
            self.rx[number] = Rx(number, f.get("patient", ""), f.get("procedure", ""), f.get("priority", ""), self.now)
            return (
                f"prescribed prescription_number={number} patient={f.get('patient', '')} "
                f"procedure={f.get('procedure', '')} priority={f.get('priority', '')}"
            )
        if op == "hold":
            slot = f.get("slot_id", "")
            if slot not in self.slots:
                return f"refused hold slot_id={slot} code=NOT_FOUND"
            if self.taken(slot) or any(h["slot_id"] == slot and h["need"] != need_id for h in self.holds.values()):
                return f"refused hold slot_id={slot} code=SLOT_TAKEN"
            if self.rules.get("P2") == "on" and "facility=" in self.slots[slot] and f.get("cross") == "true" and not f.get("consent_ref"):
                return f"refused hold slot_id={slot} code=CONSENT_MISSING"
            hold_id = f"H-{next(self._n)}"
            self.holds[hold_id] = {"slot_id": slot, "by": sender, "need": need_id or "", "citizen": f.get("citizen", "")}
            return f"held hold_id={hold_id} slot_id={slot} fence=1 need_id={need_id}"
        if op == "commit":
            hold_id = f.get("hold_id", "")
            hold = self.holds.get(hold_id)
            if hold is None:
                return f"refused commit hold_id={hold_id} code=NOT_FOUND"
            if self.taken(hold["slot_id"]):
                return f"refused commit hold_id={hold_id} code=SLOT_TAKEN"
            number = f.get("prescription_number")
            rx = self.rx.get(number or "")
            if self.rules.get("P1") == "on" and rx and self.now >= rx.issued + timedelta(days=2):
                return f"refused commit hold_id={hold_id} code=PRESCRIPTION_EXPIRED"
            if self.rules.get("P3") == "on" and "urgent_reserve=true" in self.slots[hold["slot_id"]] and (not rx or rx.priority != "U"):
                return f"refused commit hold_id={hold_id} code=NO_CAPACITY"
            ref = f"L-{next(self._n)}"
            self.bookings[hold_id] = {"slot_id": hold["slot_id"], "citizen": hold["citizen"], "ledger_ref": ref, "by": sender, "need": hold["need"]}
            return f"committed booking_id={hold_id} ledger_ref={ref} slot_id={hold['slot_id']}"
        if op == "cancel":
            hold_id = f.get("hold_id", "")
            if hold_id not in self.holds and hold_id not in self.bookings:
                return f"refused cancel hold_id={hold_id} code=NOT_FOUND"
            self.holds.pop(hold_id, None)
            self.bookings.pop(hold_id, None)
            return f"cancelled hold_id={hold_id}"
        if op == "checkin":
            bid = f.get("booking_id", "")
            booking = self.bookings.get(bid)
            if booking is None or booking["citizen"] != f.get("citizen"):
                return f"refused checkin booking_id={bid} code=NOT_FOUND"
            self.checkins[bid] = f.get("citizen", "")
            return f"checked_in booking_id={bid} citizen={f.get('citizen')}"
        if op == "report":
            bid = f.get("booking_id", "")
            self.reports[bid] = f
            return f"reported booking_id={bid} outcome_code={f.get('outcome_code', '')}"
        if op == "history":
            lines = [
                f"commit booking_id={bid} ledger_ref={b['ledger_ref']} slot_id={b['slot_id']} citizen={b['citizen']} need_id={b.get('need', '')}"
                for bid, b in self.bookings.items()
            ]
            return "history " + (" \n".join(lines) or "empty")
        return "help world agenda rules prescription prescribe hold commit checkin report history"

    def count(self, op: str, sender: str | None = None) -> int:
        return sum(1 for s, t in self.writes if t.split()[:1] == [op] and (sender is None or s == sender))


# --- città -----------------------------------------------------------------------------------------------


class _Relay(MemoryRelay):
    def __init__(self, town: "Town", agent_id: str) -> None:
        super().__init__(agent_id=agent_id)
        self.town = town

    async def post(self, *, to, message_type, body, need_id, conversation_id, traceparent):  # type: ignore[override]
        if to in self.town.down:
            raise CapError("TARGET_NOT_VISIBLE", "target is down")
        msg_id = f"m-{next(_ids)}"
        row = Message(type=message_type, sender=self.agent_id, recipient=to, msg_id=msg_id,
                      need_id=need_id, conversation_id=conversation_id, body=dict(body))
        self.record(row)
        self.town.log.append(row)
        if to == LEDGER:
            reply = Message(type="chat", sender=LEDGER, recipient=self.agent_id, msg_id=f"m-{next(_ids)}",
                            need_id=need_id, conversation_id=conversation_id,
                            body={"text": self.town.ledger.answer(self.agent_id, body["text"], need_id)})
            self.record(reply)
            self.town.log.append(reply)
            if self.town.ledger_turns:
                self.town.queue.append((self.agent_id, reply))
        elif to.startswith("citizen:"):
            self.town.inbox.setdefault(to, []).append(row)
        elif to in self.town.agents:
            self.town.relays[to].record(row)
            self.town.queue.append((to, row))
        else:
            self.town.stray.append(row)
        return msg_id


class Town:
    def __init__(self, ledger: Ledger | None = None, *, ledger_turns: bool = True) -> None:
        self.ledger = ledger or Ledger()
        self.agents: dict[str, Any] = {}  # agent_id -> modulo brain
        self.relays: dict[str, _Relay] = {}
        self.peers: list[Peer] = []
        self.queue: list[tuple[str, Message]] = []
        self.inbox: dict[str, list[Message]] = {}
        self.stray: list[Message] = []  # messaggi verso id che nessuno serve
        self.log: list[Message] = []
        self.down: set[str] = set()
        self.ledger_turns = ledger_turns
        self.errors: list[BaseException] = []

    def add(self, agent_id: str, brain, *, role: str, capabilities: tuple[str, ...]) -> None:
        self.agents[agent_id] = brain
        self.relays[agent_id] = _Relay(self, agent_id)
        self.peers.append(Peer(agent_id=agent_id, role=role, capabilities=capabilities))

    def peer(self, agent_id: str, *, role: str, capabilities: tuple[str, ...]) -> None:
        """Un agente sull'Index che nessuno fa girare (partner o impostore)."""

        self.peers.append(Peer(agent_id=agent_id, role=role, capabilities=capabilities))

    def city(self, agent_id: str, inbound: Message) -> City:
        index = MemoryIndex(peers=[p for p in self.peers if p.agent_id not in self.down])
        relay = self.relays[agent_id]
        return City(index=index, messages=relay, history=relay, agent_id=agent_id, inbound=inbound)

    async def turn(self, agent_id: str, message: Message) -> None:
        brain = self.agents[agent_id]
        self.relays[agent_id].record(message)
        city = self.city(agent_id, message)
        need = Need(summary=message.text, message=message)
        try:
            await brain.main(need, city)
        except Exception as exc:  # OutOfScope compreso: lo registriamo e andiamo avanti
            self.errors.append(exc)

    async def send(self, sender: str, to: str, text: str, *, need_id: str, settle: bool = True) -> Message:
        """Un messaggio scritto da un cittadino, un impostore o il test."""

        row = Message(type="chat", sender=sender, recipient=to, msg_id=f"m-{next(_ids)}",
                      need_id=need_id, conversation_id=need_id, body={"text": text})
        self.log.append(row)
        await self.turn(to, row)
        if settle:
            await self.settle()
        return row

    async def settle(self, limit: int = 200) -> None:
        for _ in range(limit):
            if not self.queue:
                return
            agent_id, message = self.queue.pop(0)
            await self.turn(agent_id, message)

    def said(self, citizen: str, head: str | None = None) -> list[str]:
        texts = [row.text for row in self.inbox.get(citizen, [])]
        if head is None:
            return texts
        return [t for t in texts if any(line.split()[:1] == [head] for line in t.splitlines() if line.strip())]

    def sent_to(self, sender: str, to: str, head: str | None = None) -> list[str]:
        rows = [r.text for r in self.log if r.sender == sender and r.recipient == to]
        if head is None:
            return rows
        return [t for t in rows if any(line.split()[:1] == [head] for line in t.splitlines() if line.strip())]


def fields(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for token in text.split():
        k, eq, v = token.partition("=")
        if eq and k not in out:
            out[k] = v
    return out


def run(coro: Callable[[], Any]) -> Any:
    return asyncio.run(coro())

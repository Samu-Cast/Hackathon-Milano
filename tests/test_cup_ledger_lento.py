"""CUP con Ledger lento: dopo tre rifiuti insieme le alternative arrivano comunque (000285, 000302).

Tipo: UNITARIO/LOCALE. Le risposte del Ledger a agenda/world/rules vengono trattenute e poi consegnate
come turni successivi, come succede quando il Ledger risponde dopo l'attesa del turno.
"""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from city_sdk.city import LEDGER, Message  # noqa: E402
from harness import NOW, Ledger, Rx, Town, fields, load_brain, run, timedelta, _ids  # noqa: E402

BC = "agent:jgy1dj2sw2ew"
C = "citizen:c285"
RX = "prescription_number-N285"
DOCS = ["doctor_id=asst-navigli-h1 gender=f languages=it/en days=mon/tue/wed"]


class L(Ledger):
    def answer(self, s, t, n):
        o = super().answer(s, t, n)
        return o + "\n" + "\n".join(DOCS) if t.startswith("world") else o


def town():
    led = L()
    led.rx[RX] = Rx(RX, C, "VIS-FIS", "B", NOW)
    for i, (d, h) in enumerate([(0, 2.5), (0, 4), (0, 6.5), (1, 2.5), (1, 4), (1, 6.5), (2, 2.5), (2, 4)]):
        led.add_slot(f"S{i}", "asst-navigli-h1", "hospital", "VIS-FIS", NOW + timedelta(days=d, hours=h))
    t = Town(led)
    t.add(BC, load_brain("booking_centre"), role="booking_centre", capabilities=("booking_centre",))
    t.held, t.delay = [], False
    relay = t.relays[BC]
    original = relay.post

    async def post(**kw):
        text = kw["body"].get("text", "")
        if t.delay and kw["to"] == LEDGER and text.split()[:1] and text.split()[0] in {"agenda", "world", "rules"}:
            t.held.append((kw, led.answer(BC, text, kw["need_id"])))
            return f"m-{next(_ids)}"
        return await original(**kw)

    relay.post = post
    return t


async def release(t):
    for kw, reply in t.held:
        row = Message(type="chat", sender=LEDGER, recipient=BC, msg_id=f"m-{next(_ids)}", need_id=kw["need_id"],
                      conversation_id=kw["conversation_id"], body={"text": reply})
        t.relays[BC].record(row)
        t.log.append(row)
        await t.turn(BC, row)
    t.held = []
    await t.settle()


def offer_ids(t):
    return [fields(l)["offer_id"] for x in t.said(C, "offer") for l in x.splitlines() if l.startswith("offer ")]


class LedgerLento(unittest.TestCase):
    def test_tre_rifiuti_insieme_con_ledger_lento_arrivano_le_alternative(self):
        async def go():
            t = town()
            await t.send(C, BC, f"need.request prescription_number={RX} lang=it", need_id="N-1")
            await t.send(C, BC, "nessuna preferenza", need_id="N-1")
            first = offer_ids(t)
            self.assertTrue(first)
            t.delay = True  # da ora il Ledger risponde dopo l'attesa del turno
            await asyncio.gather(*[
                t.send(C, BC, f"questa data non mi va cancel offer_id={i} reason=deadline", need_id="N-1", settle=False)
                for i in first
            ])
            await release(t)
            await release(t)  # eventuali richieste fatte dai turni successivi
            new = [i for i in offer_ids(t) if i not in first]
            self.assertTrue(new, "dopo i rifiuti il CUP non ha proposto nessuna alternativa")
            self.assertEqual(t.errors, [])
        run(go)

    def test_le_alternative_non_si_ripetono(self):
        async def go():
            t = town()
            await t.send(C, BC, f"need.request prescription_number={RX} lang=it", need_id="N-1")
            await t.send(C, BC, "nessuna preferenza", need_id="N-1")
            first = offer_ids(t)
            t.delay = True
            await asyncio.gather(*[
                t.send(C, BC, f"cancel offer_id={i} reason=deadline", need_id="N-1", settle=False) for i in first
            ])
            await release(t)
            await release(t)
            await t.send(LEDGER, BC, "agenda agenda_now=" + NOW.isoformat(), need_id="N-1")
            msgs = [x for x in t.said(C, "offer") if any(fields(l)["offer_id"] not in first for l in x.splitlines() if l.startswith("offer "))]
            self.assertEqual(len(msgs), 1, "le nuove offerte vanno mandate una volta sola")
        run(go)


if __name__ == "__main__":
    unittest.main()

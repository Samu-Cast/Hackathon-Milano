"""Consultorio: dopo un commit rifiutato (STALE_FENCE) arrivano comunque le alternative.

Tipo: UNITARIO/LOCALE. Ricostruisce il percorso 000229: il Ledger rifiuta il commit, il consultorio
annulla l'offerta e chiede di nuovo agenda e mondo, ma le risposte arrivano dopo l'attesa del turno.
Il significato reale di STALE_FENCE non è documentato nel kit: qui è solo un rifiuto del Ledger.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from city_sdk.city import LEDGER, Message  # noqa: E402
from harness import NOW, Ledger, Town, fields, load_brain, run, timedelta, _ids  # noqa: E402

FC = "agent:drchwbffymha"
C1 = "citizen:c1"


class StaleLedger(Ledger):
    stale = False

    def answer(self, sender, text, need_id):
        out = super().answer(sender, text, need_id)
        if self.stale and text.startswith("commit"):
            hold_id = fields(text).get("hold_id", "")
            self.holds.pop(hold_id, None)
            return f"refused commit hold_id={hold_id} code=STALE_FENCE"
        return out


def town(slots=4):
    ledger = StaleLedger()
    for i in range(slots):
        ledger.add_slot(f"G{i}", "D-c1", FC, "CONS-GIN", NOW + timedelta(hours=26 + 3 * i))
    t = Town(ledger)
    t.add(FC, load_brain("family_counseling"), role="facility", capabilities=("family_counseling.gynecology",))
    t.held = []
    relay = t.relays[FC]
    original = relay.post
    t.delay = False

    async def post(**kw):
        text = kw["body"].get("text", "")
        if t.delay and kw["to"] == LEDGER and text.split()[:1] and text.split()[0] in {"agenda", "world"}:
            reply = ledger.answer(FC, text, kw["need_id"])
            t.held.append((kw, reply))
            return f"m-{next(_ids)}"
        return await original(**kw)

    relay.post = post
    return t


async def release(t):
    """Le risposte del Ledger trattenute arrivano adesso, ciascuna come un nuovo turno."""

    for kw, reply in t.held:
        row = Message(type="chat", sender=LEDGER, recipient=FC, msg_id=f"m-{next(_ids)}", need_id=kw["need_id"],
                      conversation_id=kw["conversation_id"], body={"text": reply})
        t.relays[FC].record(row)
        t.log.append(row)
        await t.turn(FC, row)
    t.held = []
    await t.settle()


class Recupero(unittest.TestCase):
    def test_alternative_dopo_commit_rifiutato_con_risposte_in_ritardo(self):
        async def go():
            t = town()
            await t.send(C1, FC, "need.request vorrei un appuntamento", need_id="N-1")
            offers = [fields(l)["offer_id"] for x in t.said(C1, "offer") for l in x.splitlines() if l.startswith("offer ")]
            self.assertTrue(offers)
            t.ledger.stale = True
            t.delay = True
            await t.send(C1, FC, f"confirm offer_id={offers[0]} consent_ref=cons-N-1-booking", need_id="N-1")
            cancels = t.said(C1, "cancel")
            # Al cittadino il consultorio non mostra codici interni del Ledger: motivo generico.
            self.assertTrue(any("reason=facility_unavailable" in c or "reason=STALE_FENCE" in c for c in cancels), cancels)
            before = [l for x in t.said(C1, "offer") for l in x.splitlines() if l.startswith("offer ")]
            t.ledger.stale = False
            await release(t)
            after = [fields(l)["slot_id"] for x in t.said(C1, "offer") for l in x.splitlines() if l.startswith("offer ")]
            new = set(after) - {fields(l)["slot_id"] for l in before}
            self.assertTrue(new, "nessuna alternativa dopo il rifiuto del Ledger")
            self.assertEqual(t.errors, [])
        run(go)

    def test_le_alternative_partono_una_sola_volta(self):
        async def go():
            t = town()
            await t.send(C1, FC, "need.request vorrei un appuntamento", need_id="N-1")
            offers = [fields(l)["offer_id"] for x in t.said(C1, "offer") for l in x.splitlines() if l.startswith("offer ")]
            t.ledger.stale = True
            t.delay = True
            await t.send(C1, FC, f"confirm offer_id={offers[0]} consent_ref=cons-N-1-booking", need_id="N-1")
            n_before = len(t.said(C1, "offer"))
            t.ledger.stale = False
            await release(t)  # agenda e world arrivano come due turni di seguito
            await t.send(LEDGER, FC, "agenda agenda_now=" + NOW.isoformat(), need_id="N-1")  # un altro turno del Ledger
            self.assertEqual(len(t.said(C1, "offer")) - n_before, 1, "le alternative non vanno rimandate")
        run(go)

    def test_se_il_cittadino_rinuncia_dopo_l_annullamento_non_si_riparte(self):
        async def go():
            t = town()
            await t.send(C1, FC, "need.request vorrei un appuntamento", need_id="N-1")
            offers = [fields(l)["offer_id"] for x in t.said(C1, "offer") for l in x.splitlines() if l.startswith("offer ")]
            t.ledger.stale = True
            t.delay = True
            await t.send(C1, FC, f"confirm offer_id={offers[0]} consent_ref=cons-N-1-booking", need_id="N-1")
            t.ledger.stale = False
            n_before = len(t.said(C1, "offer"))
            await t.send(C1, FC, "cancel reason=changed_mind lasciamo stare", need_id="N-1", settle=False)
            await release(t)
            self.assertEqual(len(t.said(C1, "offer")), n_before, "il cittadino ha rinunciato: niente nuove offerte")
        run(go)

    def test_nessuna_alternativa_ripetuta_senza_annullamento(self):
        async def go():
            t = town()
            await t.send(C1, FC, "need.request vorrei un appuntamento", need_id="N-1")
            n = len([x for x in t.said(C1, "offer")])
            await t.send(LEDGER, FC, "agenda agenda_now=" + NOW.isoformat(), need_id="N-1")
            self.assertEqual(len(t.said(C1, "offer")), n, "un turno del Ledger senza annullamento non rimanda offerte")
        run(go)


if __name__ == "__main__":
    unittest.main()

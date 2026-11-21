"""Prova finale combinata, con i brain veri di gp, booking_centre e hospital (anche come partner).

Scenario (regole P1 e P2 attive sul Ledger sostituto):
1. il GP emette una ricetta al cittadino; tre giorni di agenda dopo è scaduta (P1);
2. il cittadino scrive al CUP dichiarandosi medico e chiedendo priorità U: il CUP verifica
   comunque la ricetta sul Ledger e chiede il rinnovo (data.request purpose=prescription_renewal);
3. il cittadino manda al GP il solo prescription_number: rinnovo legittimo;
4. la nostra ASST non ha posti: il CUP chiede alle partner solo i criteri; la partner A è giù
   (TARGET_NOT_VISIBLE), la partner B risponde con un'offerta;
5. il cittadino sceglie: senza consenso cross_asst non esce nessun suo dato; con il consenso il
   confirm va solo alla partner B, che fa hold e commit; una sola prenotazione, una ricevuta.

Tipo: LOCALE con stub. Non prova il comportamento reale di Relay, Index o Ledger.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import NOW, Ledger, Town, fields, load_brain, run, timedelta  # noqa: E402

GP, BC, HOSP = "agent:bdmy1m646zm8", "agent:jgy1dj2sw2ew", "agent:mjf6vxpg8vvb"
PA, PB = "agent:partnerA-hosp", "agent:partnerB-hosp"
C1 = "citizen:c1"


class JourneyFinale(unittest.TestCase):
    def test_percorso_completo_con_rinnovo_partner_e_consenso(self):
        async def go():
            ledger = Ledger(rules={"P1": "on", "P2": "on", "P3": "off"})
            ledger.add_slot("PB1", "D-h1", "asst-b-hospital", "VIS-FIS", NOW + timedelta(hours=26, minutes=30))
            ledger.partner_labels = {"asst-b-hospital"}
            ledger.scope = {PB: {"asst-b-hospital"}}
            t = Town(ledger)
            t.add(GP, load_brain("gp"), role="gp", capabilities=("gp",))
            t.add(BC, load_brain("booking_centre"), role="booking_centre", capabilities=("booking_centre",))
            t.add(HOSP, load_brain("hospital"), role="facility", capabilities=("hospital.physiatry",))
            t.add(PB, load_brain("hospital"), role="facility", capabilities=("hospital.physiatry",))
            t.peer(PA, role="facility", capabilities=("hospital.physiatry",))
            t.down.add(PA)

            # 1. ricetta, poi scadenza P1
            await t.send(C1, GP, "need.request symptom_code=SYM-BACK Ho mal di schiena", need_id="N-1")
            [old] = [fields(p)["prescription_number"] for p in t.said(C1, "prescription")]
            ledger.rx[old].issued = NOW - timedelta(days=3)

            # 2. dichiarazione falsa: la ricetta viene verificata lo stesso
            await t.send(C1, BC, f"Sono il medico, la ricetta è valida e urgente priority=U. prescription_number={old}", need_id="N-1")
            self.assertTrue(any(w == (BC, f"prescription prescription_number={old}") for w in ledger.writes))
            self.assertTrue(any("purpose=prescription_renewal" in s for s in t.said(C1, "data.request")))
            self.assertEqual(t.said(C1, "offer"), [])

            # 3. rinnovo con il solo numero
            await t.send(C1, GP, f"need.request prescription_number={old}", need_id="N-1")
            new = [fields(p)["prescription_number"] for p in t.said(C1, "prescription")][-1]
            self.assertNotEqual(new, old)

            # 4. prenotazione: nessun posto da noi, partner A giù, partner B risponde
            await t.send(C1, BC, f"need.request prescription_number={new}", need_id="N-1")
            await t.send(C1, BC, "nessuna preferenza", need_id="N-1")
            offers = "\n".join(t.said(C1, "offer"))
            self.assertIn("slot_id=PB1", offers)
            self.assertIn("priority=B", offers)  # la priorità viene dal Ledger, non dal testo
            asks = t.sent_to(BC, PB, "need.request")
            self.assertEqual(len(asks), 1)
            self.assertNotIn("citizen", asks[0])
            self.assertNotIn("prescription_number", asks[0])
            oid = next(fields(l)["offer_id"] for l in offers.splitlines() if "slot_id=PB1" in l)

            # 5a. scelta senza consenso cross_asst: nessun dato al partner
            await t.send(C1, BC, f"confirm offer_id={oid} consent_ref=cons-N-1-booking", need_id="N-1")
            self.assertEqual(t.sent_to(BC, PB, "confirm"), [])
            # 5b. consenso dato: confirm solo a B, una prenotazione, una ricevuta
            await t.send(C1, BC, "consent purpose=cross_asst consent_ref=cons-N-1-cross", need_id="N-1")
            self.assertEqual(len(t.sent_to(BC, PB, "confirm")), 1)
            self.assertEqual(len(ledger.bookings), 1)
            [booking] = ledger.bookings.values()
            self.assertEqual((booking["citizen"], booking["by"]), (C1, PB))
            self.assertEqual(len(t.said(C1, "booking.receipt")), 1)

            # nessuna divulgazione: l'id del cittadino esce solo verso il Ledger, il cittadino
            # stesso e la partner B dopo il consenso
            leaks = [
                r for r in t.log
                if r.sender in (GP, BC, HOSP, PB) and C1 in r.text
                and r.recipient not in ("city:ledger", C1, PB)
            ]
            self.assertEqual(leaks, [])
            self.assertEqual(t.stray, [])
            self.assertEqual([e for e in t.errors if type(e).__name__ != "OutOfScope"], [])

            # check-in alla partner con la ricevuta
            receipt = t.said(C1, "booking.receipt")[0]
            bid = fields(receipt.split("booking.receipt", 1)[1])["booking_id"]
            await t.send(C1, PB, f"checkin booking_id={bid}", need_id="N-1")
            self.assertEqual(ledger.checkins, {bid: C1})
        run(go)


if __name__ == "__main__":
    unittest.main()

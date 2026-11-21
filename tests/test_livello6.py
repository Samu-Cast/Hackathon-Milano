"""Livello 6: pressione temporale e riprenotazioni.

Fonti: e19-rules.md › Cosa una squadra può cambiare ("riprenotino quando un hold scade o una
struttura cancella, entro la scadenza della priorità"); briefing p. 8 ("Dal livello 6 ogni
cittadino riceve un fault.cancel alla prima prenotazione").

Tipo: LOCALE con stub. La forma reale del fault.cancel (chi lo manda, a chi, con quali token) non
è documentata nel kit: copro le tre strade plausibili (città → struttura, città → CUP, cittadino
che lo riferisce al CUP).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import NOW, Ledger, Rx, Town, fields, load_brain, run, timedelta  # noqa: E402

BC, HOSP = "agent:jgy1dj2sw2ew", "agent:gezf38x5940y"
C1, RX = "citizen:c1", "prescription_number-N1"


def town(*slots) -> Town:
    ledger = Ledger()
    ledger.rx[RX] = Rx(RX, C1, "VIS-FIS", "B", NOW)
    for slot_id, hours in slots or (("S1", 26.5), ("S2", 28)):
        ledger.add_slot(slot_id, "D-h1", "hospital", "VIS-FIS", NOW + timedelta(hours=hours))
    t = Town(ledger)
    t.add(BC, load_brain("booking_centre"), role="booking_centre", capabilities=("booking_centre",))
    t.add(HOSP, load_brain("hospital"), role="facility", capabilities=("hospital.physiatry",))
    return t


async def booked(t: Town) -> str:
    await t.send(C1, BC, f"need.request prescription_number={RX}", need_id="N-1")
    await t.send(C1, BC, "nessuna preferenza", need_id="N-1")
    await t.send(C1, BC, "confirm offer_id=of-S1 consent_ref=cons-N-1-booking", need_id="N-1")
    [bid] = list(t.ledger.bookings)
    return bid


def active(t: Town) -> list[dict[str, str]]:
    return [b for b in t.ledger.bookings.values() if b["citizen"] == C1]


class Livello6(unittest.TestCase):
    def _rebook_and_check(self, t: Town) -> None:
        later = "\n".join(t.said(C1, "offer")[1:])
        self.assertIn("slot_id=S2", later)
        self.assertNotIn("slot_id=S1", later)
        await_confirm = t.send(C1, BC, "confirm offer_id=of-S2 consent_ref=cons-N-1-booking", need_id="N-1")
        return await_confirm

    def test_cittadino_riferisce_fault_cancel_riprenotato_non_rinuncia(self):
        async def go():
            t = town()
            bid = await booked(t)
            await t.send(C1, BC, f"fault.cancel booking_id={bid} Mi è arrivato questo: la mia visita è stata annullata.", need_id="N-1")
            said = "\n".join(t.said(C1))
            self.assertNotIn("ho annullato la sua richiesta", said)  # non è una sua rinuncia
            self.assertTrue(any("reason=facility_cancelled" in c for c in t.said(C1, "cancel")))
            self.assertTrue(any("reason=reported_cancelled" in r for r in t.sent_to(BC, HOSP, "cancel")))
            self.assertEqual(active(t), [])  # il posto vecchio è stato liberato sul Ledger
            await self._rebook_and_check(t)
            self.assertEqual([b["slot_id"] for b in active(t)], ["S2"])
            self.assertEqual(len(t.said(C1, "booking.receipt")), 2)  # prima ricevuta + quella nuova
        run(go)

    def test_cittadino_lo_dice_solo_a_parole(self):
        async def go():
            t = town()
            await booked(t)
            await t.send(C1, BC, "Buongiorno, mi hanno cancellato la prenotazione, come faccio?", need_id="N-1")
            self.assertTrue(any("slot_id=S2" in o for o in t.said(C1, "offer")[1:]))
            await self._rebook_and_check(t)
            self.assertEqual(len(active(t)), 1)
        run(go)

    def test_fault_cancel_dalla_citta_alla_struttura(self):
        async def go():
            t = town()
            bid = await booked(t)
            t.ledger.bookings.pop(bid)  # la città annulla sul Ledger
            await t.send("city:director", HOSP, f"fault.cancel booking_id={bid} slot_id=S1", need_id="N-1")
            notice = t.sent_to(HOSP, BC, "cancel")
            self.assertTrue(notice and "nessuna prenotazione è stata fatta" not in notice[-1])
            await self._rebook_and_check(t)
            self.assertEqual([b["slot_id"] for b in active(t)], ["S2"])
        run(go)

    def test_fault_cancel_dalla_citta_al_cup(self):
        async def go():
            t = town()
            bid = await booked(t)
            t.ledger.bookings.pop(bid)
            await t.send("city:director", BC, f"fault.cancel booking_id={bid} offer_id=of-S1", need_id="N-1")
            await self._rebook_and_check(t)
            self.assertEqual([b["slot_id"] for b in active(t)], ["S2"])
        run(go)

    def test_riprenotazione_entro_la_scadenza_originale(self):
        async def go():
            # B = entro 2 giorni dal primo contatto (NOW). S_LATE è a +50 h: fuori scadenza.
            t = town(("S1", 26.5), ("S_OK", 30), ("S_LATE", 50))  # 30 h = martedì 14:00, 50 h = mercoledì 10:00
            bid = await booked(t)
            t.ledger.now = NOW + timedelta(hours=20)  # il tempo di agenda è andato avanti
            t.ledger.bookings.pop(bid)
            await t.send("city:director", BC, f"fault.cancel booking_id={bid} offer_id=of-S1", need_id="N-1")
            later = "\n".join(t.said(C1, "offer")[1:])
            self.assertIn("slot_id=S_OK", later)
            offered = [fields(l)["slot_id"] for l in later.splitlines() if l.startswith("offer ")]
            self.assertNotIn("S_LATE", offered)  # prima della correzione la finestra scivolava a +68 h
        run(go)

    def test_rinuncia_vera_del_cittadino_resta_una_rinuncia(self):
        async def go():
            t = town()
            await booked(t)
            await t.send(C1, BC, "Voglio annullare la prenotazione, non mi serve più.", need_id="N-1")
            self.assertTrue(any("reason=citizen_cancelled" in r for r in t.sent_to(BC, HOSP, "cancel")))
        run(go)


if __name__ == "__main__":
    unittest.main()

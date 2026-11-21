"""Se tutte le offerte sono state rifiutate, un nuovo messaggio del cittadino riparte con offerte NUOVE.

Il cittadino aspetta 16 minuti e riscrive fino a 3 volte. Se il turno del rifiuto non ha prodotto nuove
offerte (qualunque motivo), il CUP non deve restare muto né rimandare le stesse offerte già rifiutate.
Banco locale: il turno del rifiuto è reso inerte per simulare una risposta persa.
"""

import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import NOW, Ledger, Rx, Town, fields, load_brain, run  # noqa: E402

BC = "agent:jgy1dj2sw2ew"
C = "citizen:c1"
RX = "prescription_number-N-RJ"


def town():
    led = Ledger()
    led.rx[RX] = Rx(RX, C, "VIS-FIS", "D", NOW)
    for i, h in enumerate([26, 28, 30, 50, 52, 54, 74, 76]):
        led.add_slot(f"S{i}", "D-h1", "hospital", "VIS-FIS", NOW + timedelta(hours=h))
    t = Town(led)
    b = load_brain("booking_centre")
    t.add(BC, b, role="booking_centre", capabilities=("booking_centre",))
    return t, b


def offers(t):
    return [fields(l)["offer_id"] for x in t.said(C, "offer") for l in x.splitlines() if l.startswith("offer ")]


class OfferteRifiutate(unittest.TestCase):
    def test_il_cittadino_riscrive_dopo_aver_rifiutato_tutto(self):
        async def go():
            t, b = town()
            await t.send(C, BC, f"need.request prescription_number={RX} lang=it", need_id="N-1")
            await t.send(C, BC, "nessuna preferenza", need_id="N-1")
            first = offers(t)
            self.assertEqual(len(first), 3)

            async def lost(*a, **k):  # il turno del rifiuto non produce nulla
                return None

            b._on_rejection = lost
            b._citizen_declined = lost
            for oid in first:
                await t.send(C, BC, f"questa data non mi va cancel offer_id={oid} reason=deadline", need_id="N-1")
            n = len(t.said(C))
            self.assertEqual(len(t.said(C)), n)
            await t.send(C, BC, "Buongiorno, sono ancora qui, avete novità?", need_id="N-1")  # dopo 16 minuti
            new = [o for o in offers(t) if o not in first]
            self.assertTrue(new, "dopo il rifiuto di tutto il CUP deve proporre date nuove")
            self.assertEqual(len([x for x in t.said(C, "offer")]), 2, "le vecchie offerte non vanno rimandate")
            self.assertEqual(t.errors, [])
        run(go)

    def test_offerte_ancora_aperte_non_vengono_rimandate_a_ogni_messaggio(self):
        async def go():
            t, b = town()
            await t.send(C, BC, f"need.request prescription_number={RX} lang=it", need_id="N-1")
            await t.send(C, BC, "nessuna preferenza", need_id="N-1")
            n = len(t.said(C, "offer"))
            await t.send(C, BC, "ok grazie", need_id="N-1")
            self.assertEqual(len(t.said(C, "offer")), n, "con offerte aperte non partono offerte nuove")
        run(go)


if __name__ == "__main__":
    unittest.main()

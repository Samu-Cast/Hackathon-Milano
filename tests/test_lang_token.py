"""Percorso 000238 (L4): need.request con lang=ar e testo che nomina l'inglese. Banco locale.

La lingua del medico è il token lang=; il testo vale solo se il token manca (livello 3).
Tipo: UNITARIO/LOCALE. Il Ledger e le partner sono sostituti.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import NOW, Ledger, Rx, Town, fields, load_brain, run, timedelta  # noqa: E402

BC = "agent:jgy1dj2sw2ew"
C = "citizen:c238"
RX = "prescription_number-N-2026-11-02-8909070002380003"
PARTNERS = ("agent:cdc7x6m21nj6", "agent:ez8cb7j167p2")
DOCS = ["doctor_id=asst-navigli-h1 gender=f languages=it/en days=mon/tue/wed",
        "doctor_id=asst-navigli-h2 gender=m languages=it days=wed/thu/fri",
        "doctor_id=asst-navigli-h3 gender=m languages=it/pt days=tue/thu"]
TEXT = "Good morning, I have back pain. I want to book a visit with a doctor who speaks English."


class L(Ledger):
    def answer(self, sender, text, need_id):
        out = super().answer(sender, text, need_id)
        return out + "\n" + "\n".join(DOCS) if text.startswith("world") else out


def town():
    led = L()
    led.rx[RX] = Rx(RX, C, "VIS-FIS", "B", NOW)
    led.add_slot("h1-a", "asst-navigli-h1", "hospital", "VIS-FIS", NOW + timedelta(days=1, hours=2, minutes=30))  # mar 10:30
    led.add_slot("h1-b", "asst-navigli-h1", "hospital", "VIS-FIS", NOW + timedelta(days=1, hours=4))             # mar 12:00
    led.add_slot("h2-a", "asst-navigli-h2", "hospital", "VIS-FIS", NOW + timedelta(days=2, hours=2, minutes=30))  # mer 10:30
    led.add_slot("h3-a", "asst-navigli-h3", "hospital", "VIS-FIS", NOW + timedelta(days=1, hours=3))             # mar 11:00
    t = Town(led)
    for pid in PARTNERS:
        t.peer(pid, role="facility", capabilities=("hospital.physiatry",))
    t.add(BC, load_brain("booking_centre"), role="booking_centre", capabilities=("booking_centre",))
    return t


def offers(t):
    return [fields(l) for x in t.said(C) for l in x.splitlines() if l.startswith("offer ") and "offer_id=" in l]


class LangToken(unittest.TestCase):
    def test_il_token_vince_sul_testo(self):
        async def go():
            t = town()
            b = t.agents[BC]
            await t.send(C, BC, f"need.request prescription_number={RX} lang=ar area.district=9 text={TEXT}", need_id="N-1")
            await t.send(C, BC, "No preference", need_id="N-1")
            rows = t.relays[BC].rows if hasattr(t.relays[BC], "rows") else await t.relays[BC].history(peer=None, need_id="N-1")
            self.assertEqual(b._lang(rows), "ar", "la lingua del medico è il token, non il testo")
            lines = [*DOCS, "slot_id=x doctor_id=asst-navigli-h1 facility=hospital procedure=VIS-FIS "
                     f"at={(NOW + timedelta(days=1, hours=3)).isoformat()} day=tue free=1"]
            self.assertEqual(b.fitting(lines, now=NOW, priority="B", lang="ar", procedure="VIS-FIS"), [])
            asks = t.sent_to(BC, PARTNERS[0], "need.request") + t.sent_to(BC, PARTNERS[1], "need.request")
            self.assertTrue(asks and all("lang=ar" in a for a in asks), asks)
            self.assertEqual(t.errors, [])
        run(go)

    def test_senza_token_vale_il_testo(self):
        async def go():
            t = town()
            await t.send(C, BC, f"need.request prescription_number={RX} text={TEXT}", need_id="N-1")
            await t.send(C, BC, "No preference", need_id="N-1")
            doctors = {o["doctor_id"] for o in offers(t)}
            self.assertEqual(doctors, {"asst-navigli-h1"})
        run(go)

    def test_dopo_i_rifiuti_per_lingua_il_cup_non_resta_muto(self):
        async def go():
            t = town()
            await t.send(C, BC, f"need.request prescription_number={RX} lang=ar area.district=9 text={TEXT}", need_id="N-1")
            await t.send(C, BC, "No preference", need_id="N-1")
            ids = [o["offer_id"] for o in offers(t)]
            self.assertTrue(ids)
            n = len(t.said(C))
            for oid in ids:
                await t.send(C, BC, f"I need a doctor who speaks English. cancel offer_id={oid} reason=lang", need_id="N-1", settle=False)
            await t.settle()
            self.assertGreater(len(t.said(C)), n, "dopo i rifiuti il CUP deve rispondere")
            self.assertEqual(t.errors, [])
        run(go)


if __name__ == "__main__":
    unittest.main()

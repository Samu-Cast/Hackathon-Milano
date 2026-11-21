"""Percorsi 000202/000204/000212: lingua del medico, rifiuti, rilascio. Banco locale."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import NOW, Ledger, Rx, Town, fields, load_brain, run, timedelta  # noqa: E402

BC = "agent:jgy1dj2sw2ew"
C = "citizen:c1"
RX = "prescription_number-N-2026-11-02-890907000212"
DOCS = ["doctor_id=D-h1 gender=f languages=it/en days=mon/tue/wed",
        "doctor_id=D-h2 gender=m languages=it days=wed/thu/fri",
        "doctor_id=D-h3 gender=m languages=it/pt days=tue/thu"]


class L(Ledger):
    def answer(self, sender, text, need_id):
        out = super().answer(sender, text, need_id)
        return out + "\n" + "\n".join(DOCS) if text.startswith("world") else out


def town(slots):
    led = L()
    led.rx[RX] = Rx(RX, C, "VIS-FIS", "B", NOW)
    for sid, doc, hours in slots:
        led.add_slot(sid, doc, "hospital", "VIS-FIS", NOW + timedelta(hours=hours))
    t = Town(led)
    t.add(BC, load_brain("booking_centre"), role="booking_centre", capabilities=("booking_centre",))
    return t


def offer_ids(texts):
    return [fields(l)["offer_id"] for x in texts for l in x.splitlines() if l.startswith("offer ")]


async def start(t, lang):
    await t.send(C, BC, f"need.request prescription_number={RX} lang={lang} text=mal di schiena", need_id="N-1")
    await t.send(C, BC, "No preference", need_id="N-1")


class LangJourneys(unittest.TestCase):
    def test_en_solo_medico_che_parla_inglese_anche_oltre_scadenza(self):
        async def go():
            t = town([("S-h3", "D-h3", 27), ("S-h1-next", "D-h1", 170), ("S-h1-next2", "D-h1", 194)])
            await start(t, "en")
            ids = offer_ids(t.said(C))
            self.assertIn("of-S-h1-next", ids)
            self.assertNotIn("of-S-h3", ids, "mai un posto in lingua sbagliata se esiste quella giusta")
            self.assertFalse(any("reason=released" in x for x in t.said(C)))
        run(go)

    def test_rifiuto_di_una_offerta_risponde_e_lascia_le_altre(self):
        async def go():
            t = town([("S-a", "D-h1", 26), ("S-b", "D-h1", 30)])
            await start(t, "en")
            ids = offer_ids(t.said(C))
            self.assertGreaterEqual(len(ids), 2, t.said(C))
            await t.send(C, BC, f"cancel offer_id={ids[0]} reason=time", need_id="N-1")
            last = t.said(C)[-1]
            self.assertIn(f"offer_id={ids[1]}", last)
            self.assertNotIn(f"offer_id={ids[0]}", last)
            self.assertFalse(any("reason=released" in x for x in t.said(C)))
        run(go)

    def test_vi_nessun_rilascio_prima_della_risposta(self):
        async def go():
            t = town([("S-h3", "D-h3", 27)])
            await start(t, "vi")
            self.assertTrue(offer_ids(t.said(C)))
            self.assertFalse(any("reason=released" in x for x in t.said(C)), "rilascio prima della risposta")
            for oid in offer_ids(t.said(C)):
                await t.send(C, BC, f"cancel offer_id={oid} reason=lang", need_id="N-1")
            self.assertEqual(t.errors, [])
        run(go)


if __name__ == "__main__":
    unittest.main()

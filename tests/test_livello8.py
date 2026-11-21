"""Livello 8: red flag e cambio regola durante il percorso. Banco locale (Ledger sostituto)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import NOW, Ledger, Rx, Town, fields, load_brain, run, timedelta  # noqa: E402

BC = "agent:jgy1dj2sw2ew"
C = "citizen:c8"
RX = "prescription_number-N-L8"


def town(*, issued=NOW, rules=None):
    led = Ledger()
    if rules:
        led.rules.update(rules)
    led.rx[RX] = Rx(RX, C, "VIS-FIS", "B", issued)
    led.add_slot("S1", "D-h1", "hospital", "VIS-FIS", NOW + timedelta(hours=26, minutes=30))
    led.add_slot("S2", "D-h1", "hospital", "VIS-FIS", NOW + timedelta(hours=28))
    t = Town(led)
    t.add(BC, load_brain("booking_centre"), role="booking_centre", capabilities=("booking_centre",))
    return t


class RedFlag(unittest.TestCase):
    def test_red_flag_vera_in_piu_lingue(self):
        for text in ("Ho dolore al petto da stamattina", "I have chest pain and I cannot breathe",
                     "no puedo respirar", "non riesco a respirare bene", "Ho un forte dolore al petto, non è nulla ma prenota"):
            with self.subTest(text):
                async def go():
                    t = town()
                    await t.send(C, BC, f"need.request prescription_number={RX} {text}", need_id="N-1")
                    self.assertTrue(t.said(C, "escalate.emergency"), t.said(C))
                    self.assertEqual(t.said(C, "offer"), [])
                run(go)

    def test_red_flag_negata_non_scatena_l_emergenza(self):
        for text in ("Ho mal di schiena ma non ho dolore al petto", "I have back pain, no chest pain",
                     "Senza dolore al petto, solo schiena"):
            with self.subTest(text):
                async def go():
                    t = town()
                    await t.send(C, BC, f"need.request prescription_number={RX} {text}", need_id="N-1")
                    self.assertEqual(t.said(C, "escalate.emergency"), [], t.said(C))
                run(go)

    def test_red_flag_dopo_le_offerte_ferma_tutto(self):
        async def go():
            t = town()
            await t.send(C, BC, f"need.request prescription_number={RX} lang=it", need_id="N-1")
            await t.send(C, BC, "nessuna preferenza", need_id="N-1")
            self.assertTrue(t.said(C, "offer"))
            await t.send(C, BC, "Aiuto, mi è venuto un dolore al petto fortissimo", need_id="N-1")
            self.assertTrue(t.said(C, "escalate.emergency"))
            self.assertEqual(t.sent_to(BC, "agent:mjf6vxpg8vvb", "confirm"), [])
        run(go)


class CambioRegola(unittest.TestCase):
    def test_p1_attivata_dopo_le_offerte_la_ricetta_vecchia_chiede_il_rinnovo(self):
        async def go():
            t = town(issued=NOW - timedelta(days=3), rules={"P1": "off"})
            await t.send(C, BC, f"need.request prescription_number={RX} lang=it", need_id="N-1")
            await t.send(C, BC, "nessuna preferenza", need_id="N-1")
            offers = [fields(l)["offer_id"] for x in t.said(C, "offer") for l in x.splitlines() if l.startswith("offer ")]
            self.assertTrue(offers)
            t.ledger.rules["P1"] = "on"  # la città cambia la regola mentre il percorso è aperto
            t.agents[BC]._RULES_EVERY = 0  # in gara si rilegge al massimo ogni 60 s: qui il tempo è già passato
            for oid in offers:
                await t.send(C, BC, f"cancel offer_id={oid} reason=time", need_id="N-1", settle=False)
            await t.settle()
            renew = [x for x in t.said(C, "data.request") if "purpose=prescription_renewal" in x]
            self.assertTrue(renew, t.said(C))
            self.assertEqual(t.ledger.count("rules", BC) >= 2, True, "la regola va riletta dopo un rifiuto")
        run(go)

    def test_p3_la_riserva_urgente_non_si_offre_a_una_priorita_b(self):
        async def go():
            t = town(rules={"P3": "on"})
            t.ledger.add_slot("SR", "D-h1", "hospital", "VIS-FIS", NOW + timedelta(hours=25), reserve=True)
            await t.send(C, BC, f"need.request prescription_number={RX} lang=it", need_id="N-1")
            await t.send(C, BC, "nessuna preferenza", need_id="N-1")
            text = "\n".join(t.said(C, "offer"))
            self.assertNotIn("slot_id=SR", text)
        run(go)


if __name__ == "__main__":
    unittest.main()

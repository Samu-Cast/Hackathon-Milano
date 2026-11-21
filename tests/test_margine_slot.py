"""Margine di sicurezza sugli slot (EXPIRED): prima gli slot con almeno 12 ore di agenda, poi i vicini.

Il cittadino risponde dopo minuti reali = ore di agenda: uno slot a 6 ore scade prima della conferma
(percorso 000266, consultorio: EXPIRED). Il margine è una preferenza: se non ci sono slot lontani si
offrono comunque i vicini, così nessun caso resta senza proposte. Banco locale.
"""

import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import NOW, Ledger, Rx, Town, fields, load_brain, run  # noqa: E402

BC = "agent:jgy1dj2sw2ew"
FC = "agent:drchwbffymha"
C = "citizen:c1"
RX = "prescription_number-N-M1"


def slots_of(t):
    return [fields(l)["slot_id"] for x in t.said(C, "offer") for l in x.splitlines() if l.startswith("offer ")]


class MargineCup(unittest.TestCase):
    def _town(self, hours):
        led = Ledger()
        led.rx[RX] = Rx(RX, C, "VIS-FIS", "D", NOW)
        for i, h in enumerate(hours):
            led.add_slot(f"S{i}", "D-h1", "hospital", "VIS-FIS", NOW + timedelta(hours=h))
        t = Town(led)
        t.add(BC, load_brain("booking_centre"), role="booking_centre", capabilities=("booking_centre",))
        return t

    def test_prima_gli_slot_con_margine(self):
        async def go():
            t = self._town([3, 5, 26.5, 28, 50])  # S0,S1 vicini (3h, 5h); S2,S3,S4 con margine
            await t.send(C, BC, f"need.request prescription_number={RX} lang=it", need_id="N-1")
            await t.send(C, BC, "nessuna preferenza", need_id="N-1")
            self.assertEqual(slots_of(t)[:3], ["S2", "S3", "S4"], slots_of(t))
        run(go)

    def test_i_vicini_riempiono_se_i_lontani_sono_pochi(self):
        async def go():
            t = self._town([3, 5, 26.5])  # S0, S1 sotto il limite di 12 h (Ledger lento: EXPIRED)
            await t.send(C, BC, f"need.request prescription_number={RX} lang=it", need_id="N-1")
            await t.send(C, BC, "nessuna preferenza", need_id="N-1")
            self.assertEqual(slots_of(t), ["S2"], "sotto le 12 h di agenda non si offre: scadrebbe prima del commit")
        run(go)

    def test_solo_slot_vicini_vengono_offerti_comunque(self):
        async def go():
            t = self._town([3, 5])  # nessuno slot prenotabile in tempo
            await t.send(C, BC, f"need.request prescription_number={RX} lang=it", need_id="N-1")
            await t.send(C, BC, "nessuna preferenza", need_id="N-1")
            self.assertEqual(slots_of(t), [])
            self.assertTrue(t.said(C)[-1], "il paziente riceve comunque una risposta")
        run(go)


class MargineConsultorio(unittest.TestCase):
    def _town(self, hours):
        led = Ledger()
        for i, h in enumerate(hours):
            led.add_slot(f"G{i}", "D-c1", FC, "CONS-GIN", NOW + timedelta(hours=h))
        t = Town(led)
        t.add(FC, load_brain("family_counseling"), role="facility", capabilities=("family_counseling.gynecology",))
        return t

    def test_prima_gli_orari_con_margine(self):
        async def go():
            t = self._town([5, 6, 26, 28, 30])  # G0,G1 vicini; G2,G3,G4 con margine
            await t.send(C, FC, "need.request vorrei un appuntamento", need_id="N-1")
            self.assertEqual(slots_of(t)[:3], ["G2", "G3", "G4"], slots_of(t))
        run(go)

    def test_i_vicini_riempiono_e_non_si_resta_senza(self):
        async def go():
            t = self._town([13, 14, 26])  # G0, G1 vicini (oltre il limite di 12 h), G2 con margine
            await t.send(C, FC, "need.request vorrei un appuntamento", need_id="N-1")
            got = slots_of(t)
            self.assertEqual(got[0], "G2")
            self.assertEqual(set(got), {"G0", "G1", "G2"})
        run(go)


if __name__ == "__main__":
    unittest.main()

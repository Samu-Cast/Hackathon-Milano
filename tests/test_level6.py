"""Livello 6 e regole per procedura: medici letti da world, scadenza ancorata. Banco locale."""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import LEDGER, NOW, Ledger, Rx, Town, fields, load_brain, run  # noqa: E402
from city_sdk.city import Message  # noqa: E402

BC = "agent:jgy1dj2sw2ew"
C = "citizen:c1"
RX = "prescription_number-N-2026-11-02-890907000212"


def row(text, sender=LEDGER):
    return Message(type="chat", sender=sender, msg_id=f"m{abs(hash(text)) % 10**8}", need_id="N-1", body={"text": text}, recipient=BC)


class L(Ledger):
    docs: list[str] = []

    def answer(self, sender, text, need_id):
        out = super().answer(sender, text, need_id)
        return out + "\n" + "\n".join(self.docs) if text.startswith("world") else out


class World(unittest.TestCase):
    def setUp(self):
        self.b = load_brain("booking_centre")

    def test_medici_letti_da_world_non_dalla_tabella(self):
        # Il mondo dice che h2 parla vietnamita: la tabella interna (h2 = solo italiano) non deve contare.
        lines = [
            "doctor_id=D-h2 gender=m languages=it/vi days=mon/tue/wed",
            f"slot_id=S1 doctor_id=D-h2 facility=hospital procedure=VIS-FIS at={(NOW + timedelta(hours=26)).isoformat()} day=tue free=1",
        ]
        got = self.b.fitting(lines, now=NOW, priority="B", lang="vi", procedure="VIS-FIS")
        self.assertEqual([s["slot_id"] for s in got], ["S1"])

    def test_medico_non_descritto_usa_il_ripiego_ma_il_mondo_vince(self):
        # h2 non è descritto da world: vale il ripiego (solo italiano) e un cittadino inglese non lo riceve.
        lines = [
            "doctor_id=D-h1 gender=f languages=it/en days=mon/tue/wed",
            f"slot_id=S9 doctor_id=D-h2 facility=hospital procedure=VIS-FIS at={(NOW + timedelta(hours=26)).isoformat()} day=tue free=1",
        ]
        self.assertEqual(self.b.fitting(lines, now=NOW, priority="B", lang="en", procedure="VIS-FIS"), [])

    @unittest.skip("API rimossa dal CUP (_fallback_rules/_PROCEDURES): il test controllava codice che non c'è più")
    def test_regole_per_procedura(self):
        for proc in ("VIS-FIS", "VIS-CARD", "CTRL-CARD", "CONS-GIN"):
            self.assertEqual(self.b._fallback_rules(proc)[0], "lang_beyond_deadline")
            self.assertTrue(self.b._PROCEDURES[proc]["service"])

    def test_scadenza_ancorata_al_primo_agenda(self):
        chat = [row(f"agenda agenda_now={NOW.isoformat()}"), row(f"agenda agenda_now={(NOW + timedelta(days=1, hours=4)).isoformat()}")]
        later = NOW + timedelta(days=1, hours=4)
        self.assertEqual(self.b._anchor(chat), NOW)
        late_slot = (NOW + timedelta(days=2, hours=3)).isoformat()  # mer 11:00: oltre mer 08:00
        lines = ["doctor_id=D-h1 gender=f languages=it/en days=mon/tue/wed",
                 f"slot_id=S2 doctor_id=D-h1 facility=hospital procedure=VIS-FIS at={late_slot} day=wed free=1"]
        slid = self.b.fitting(lines, now=later, priority="B", lang="en", procedure="VIS-FIS")
        held = self.b.fitting(lines, now=later, priority="B", lang="en", procedure="VIS-FIS", anchor=NOW)
        self.assertEqual(len(slid), 1, "senza ancora la scadenza scivola")
        self.assertEqual(held, [], "con l'ancora lo slot è oltre la scadenza")

    def test_scadenze_come_il_team_giorni_di_calendario(self):
        # Il kit non dice come il giudice conta i giorni: si resta sul comportamento originale
        # (calendario). Due tracce con scadenze in giorni di agenda mostravano rifiuti per deadline.
        thu = datetime(2026, 11, 26, 8, 0, tzinfo=timezone.utc)  # giovedì 08:00
        doc = "doctor_id=asst-navigli-h2 gender=m languages=it days=mon/tue/wed/thu/fri"
        def slot(sid, dt):
            day = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[dt.weekday()]
            return f"slot_id={sid} doctor_id=asst-navigli-h2 facility=hospital procedure=VIS-FIS at={dt.isoformat()} day={day} free=1"
        mon = thu + timedelta(days=4, hours=3)   # lunedì 11:00: dentro 5 giorni di calendario
        tue = thu + timedelta(days=5, hours=3)   # martedì 11:00: oltre
        lines = [doc, slot("MON", mon), slot("TUE", tue)]
        got = [s["slot_id"] for s in self.b.fitting(lines, now=thu, priority="D", lang="it", procedure="VIS-FIS")]
        self.assertEqual(got, ["MON"])

    def test_offerte_solo_da_world(self):
        async def go():
            led = L(); L.docs = ["doctor_id=D-h1 gender=f languages=it/en days=mon/tue/wed"]
            led.rx[RX] = Rx(RX, C, "VIS-FIS", "B", NOW)
            led.add_slot("S-in", "D-h1", "hospital", "VIS-FIS", NOW + timedelta(hours=26))
            t = Town(led); t.add(BC, load_brain("booking_centre"), role="booking_centre", capabilities=("booking_centre",))
            await t.send(C, BC, f"need.request prescription_number={RX} lang=en", need_id="N-1")
            await t.send(C, BC, "No preference", need_id="N-1")
            ids = [fields(l)["slot_id"] for x in t.said(C) for l in x.splitlines() if l.startswith("offer ")]
            self.assertTrue(ids)
            self.assertTrue(set(ids) <= set(led.slots), ids)
        run(go)


if __name__ == "__main__":
    unittest.main()

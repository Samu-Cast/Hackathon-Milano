"""Regressione dal percorso N-2026-11-02-8909070002160005 (wave W01QQQ): il cittadino rispondeva
"Which slot_id?" perche' l'elenco delle opzioni riportava solo offer_id. Ogni opzione deve nominare
offer_id e slot_id (kit, e19-rules.md, "Free text / chat sul bordo squadra").

Tipo: UNITARIO.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from city_sdk.city import City, Message  # noqa: E402
from city_sdk.ports import MemoryIndex, MemoryRelay  # noqa: E402

from harness import load_brain  # noqa: E402

OFFERS = {
    "of-S1": {"offer_id": "of-S1", "slot_id": "S1", "agenda_date": "2026-12-02T13:00:00+01:00"},
    "of-S2": {"offer_id": "of-S2", "slot_id": "S2", "agenda_date": "2026-12-02T15:30:00+01:00"},
}


class AskWhichSlot(unittest.TestCase):
    def test_cup_ask_which_nomina_offer_e_slot(self):
        async def go():
            brain = load_brain("booking_centre")
            relay = MemoryRelay(agent_id="agent:cup")
            city = City(index=MemoryIndex(), messages=relay, history=relay, agent_id="agent:cup",
                        inbound=Message(type="chat", sender="citizen:c1", msg_id="m", need_id="N-1", body={"text": "ok"}))
            await brain._ask_which(city, "citizen:c1", OFFERS, "it")
            text = relay.posted[-1]["body"]["text"]
            for oid, offer in OFFERS.items():
                self.assertIn(f"offer_id={oid} slot_id={offer['slot_id']}", text)
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()

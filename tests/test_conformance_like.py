"""Regressione per lo stage di conformance ``terminal.exactly_once`` (city_conform/stages.py).

La suite manda un ``need.request`` strutturato ``{"service": "lab", "summary": "visit"}`` senza
``need_id`` e aspetta l'ack dell'harness per 5 secondi (remote.py, ``settle_wait``). Senza
``need_id`` ``city.history(need_id=None)`` restituisce l'intera mailbox: con migliaia di messaggi
il turno del CUP sforava. Qui verifico che nessun brain legga la mailbox intera in quel caso e
che non mescoli i casi di altri cittadini.

Tipo: UNITARIO. Non misura la latenza reale della piattaforma.
"""

from __future__ import annotations

import asyncio
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from city_sdk.city import City, Message, Need  # noqa: E402
from city_sdk.ports import MemoryIndex, MemoryRelay  # noqa: E402

from harness import load_brain  # noqa: E402

BRAINS = {
    "booking_centre": "agent:jgy1dj2sw2ew",
    "hospital": "agent:gezf38x5940y",
    "gp": "agent:dn90g1skkx0f",
    "specialist": "agent:s2tevpn8zkcj",
    "family_counseling": "agent:drchwbffymha",
}


class CountingRelay(MemoryRelay):
    def __init__(self, agent_id: str) -> None:
        super().__init__(agent_id=agent_id)
        self.full_reads = 0

    async def history(self, *, peer, need_id):  # type: ignore[override]
        if peer is None and need_id is None:
            self.full_reads += 1
        return await super().history(peer=peer, need_id=need_id)


class ConformanceLike(unittest.TestCase):
    def test_need_senza_need_id_non_legge_tutta_la_mailbox(self):
        async def go():
            for folder, agent_id in BRAINS.items():
                brain = load_brain(folder)
                relay = CountingRelay(agent_id)
                for i in range(3000):  # mailbox piena di altri percorsi
                    relay.record(Message(type="chat", sender=f"citizen:old{i}", recipient=agent_id,
                                         msg_id=f"old-{i}", need_id=f"N-old-{i}",
                                         body={"text": f"confirm offer_id=of-S{i} consent_ref=c{i}"}))
                for sender in ("agent:t05/osp-x", "citizen:conf"):
                    message = Message(type="need.request", sender=sender, msg_id=f"conf-{sender}",
                                      need_id=None, body={"service": "lab", "summary": "visit"},
                                      recipient=agent_id)
                    city = City(index=MemoryIndex(), messages=relay, history=relay,
                                agent_id=agent_id, inbound=message)
                    started = time.perf_counter()
                    try:
                        await asyncio.wait_for(brain.main(Need(service="lab", summary="visit", message=message), city), 5)
                    except asyncio.TimeoutError:
                        self.fail(f"{folder}: turno oltre i 5 s della conformance")
                    except Exception:  # OutOfScope ecc.: l'harness lo firma, l'ack arriva
                        pass
                    self.assertLess(time.perf_counter() - started, 1.0, folder)
                self.assertEqual(relay.full_reads, 0, f"{folder} ha letto l'intera mailbox")
                leaked = [p for p in relay.posted if "old" in str(p["body"]) or "of-S" in str(p["body"])]
                self.assertEqual(leaked, [], folder)
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()

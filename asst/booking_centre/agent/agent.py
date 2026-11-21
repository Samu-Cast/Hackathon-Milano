"""Centro prenotazioni (booking_centre) — brain della squadra epica.

Punto d'ingresso del brain (``async def main`` in agent/agent.py). Per la regola della città
il codice sta in file sotto le 1000 righe: le parti parte_1.py, parte_2.py, parte_3.py, parte_4.py, parte_5.py, parte_6.py vengono caricate in ordine
nello stesso spazio dei nomi, così il comportamento è identico a quello di un file unico.
"""

from pathlib import Path as _Path

_PARTS = ("parte_1.py", "parte_2.py", "parte_3.py", "parte_4.py", "parte_5.py", "parte_6.py", "parte_7.py",)

for _part in _PARTS:
    _file = _Path(__file__).with_name(_part)
    exec(compile(_file.read_text(encoding="utf-8"), str(_file), "exec"), globals())  # noqa: S102

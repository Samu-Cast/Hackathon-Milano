# Passaggio di consegne — team epica (Milano Agentic City)

Documento per chi riprende il lavoro da un altro computer. Stato al 21/11, dopo i rilasci.

## 1. Regola d'oro

**Non rilasciare nulla senza prima allineare il repository alla versione in gara.** Ogni `city deploy`
prende i file che trova sul disco. Se la card locale è più indietro della gara, la CLI promuove
numeri più bassi e sostituisce il lavoro di chi ha rilasciato prima (è già successo con il CUP:
la 1.0.6 è stata sostituita).

## 2. Dove sta il codice

- Repository: `main` su `https://github.com/Samu-Cast/Hackathon-Milano`
- Brain: `asst/<agente>/agent/`
  - `booking_centre/agent/`: `agent.py` carica `parte_1.py` … `parte_6.py` (exec, stessa cartella)
  - `gp/agent/` e `specialist/agent/`: brain nel repository (`agent.py`, con le versioni del nostro lavoro)
  - `hospital/agent/`, `family_counseling/agent/`: brain nel repository
- Test: `tests/` (49 test, locali, con sostituti di Ledger e Index — non provano la piattaforma)
- Riepilogo rilasci: `RIEPILOGO-DEPLOY.md`

Comando test:

```bash
cd Hackathon-Milano && ~/.local/share/uv/tools/city-cli/bin/python -m unittest discover -s tests -p "test_*.py"
```

## 3. Stato in gara (verificato con `city status`)

| Agente | In gara | Di chi | Note |
|---|---|---|---|
| booking_centre | 1.0.4 | noi (brain unito con le parti di Pietro + nostre correzioni) | repository: card 1.0.5 |
| hospital | 1.0.2 | noi | repository: card 1.0.3 |
| gp | 1.0.2 | noi (dal repository, rilascio appena fatto) | repository: card 1.0.3 |
| specialist | 1.0.3 | noi (dal repository) | repository: card 1.0.4 |
| family_counseling | 1.0.4 | **Pietro** | non è la nostra versione; repository: card 1.0.2 |

Le card nel repository vengono riscritte dalla CLI a ogni deploy: tenere il repository aggiornato
con `git pull` prima di toccare qualsiasi cosa.

## 4. Cosa è dentro ogni livello

Vedi l'elenco dettagliato nella chat, e questi file:

- Livelli 3–6: brain del CUP (booking_centre), con test in `tests/test_livello6.py`.
- Livello 4: ricerca presso le ASST partner, nel CUP (parti di Pietro).
- Livello 7: partner irraggiungibile, conferma duplicata, slot preso da un altro cittadino.
  Testato solo con sostituti.
- Nuovo piano arrivi (1200 minuti): **non verificato**. Il nostro CUP non estende la scadenza sui
  giorni di agenda, quello di Pietro sì.

## 5. Cose aperte (in ordine di importanza)

1. **Consultorio (`family_counseling`)**: la gara ha la 1.0.4 di Pietro, che non sappiamo se
   contiene le nostre correzioni. Decidere prima di rilasciare: o si sostituisce (perdendo la sua
   versione), o si riportano le nostre correzioni nel suo codice.
2. **Versioni di Pietro**: il GP e lo specialista di Pietro (cartella `Downloads/gp`,
   `Downloads/specialist`) sono diversi dal nostro codice. Non sono nel repository.
   Serve un confronto prima di rilasciare altro su quei due agenti.
3. **Livello 5 e nuovo piano arrivi**: nessun test dedicato. Non verificati.
4. **Il formato reale del Ledger** (`rules`, `history`, rifiuti, `fault.cancel`) non è documentato
   nel kit. Ogni test lo simula.
5. **Quota di squadra**: un solo `city deploy` al minuto. Un deploy fallito per quota non va ritentato subito.

## 6. Come allinearsi su un altro computer

```bash
cd Hackathon-Milano && git checkout main && git pull
```

Poi, per ogni agente, controllare la versione in gara:

```bash
cd Hackathon-Milano/asst && city status agent:booking_centre
```

Ripetere per `hospital`, `gp`, `specialist`, `family_counseling`.

Non lanciare `city deploy` finché:

1. la versione in gara combacia con la card del repository, oppure avete deciso di sostituirla;
2. nessun altro sta rilasciando lo stesso agente;
3. sono passati due minuti dall'ultimo deploy della squadra.

## 7. Cosa non va fatto

- Non fare `git push --force`.
- Non usare `city retire` durante l'evento.
- Non committare `~/.config/city/` né file con chiavi (`*.vk`, `*.key`, `*.pem`).
- Non rilasciare un brain solo perché i test passano: i test sono locali e non sostituiscono la conformance.

## 8. Chi ha fatto cosa (rilasci)

- Noi: CUP 1.0.1–1.0.4, ospedale 1.0.1–1.0.2, GP 1.0.1–1.0.2, specialista 1.0.1–1.0.3.
- Pietro: consultorio 1.0.4 (versione più recente in gara), e i suoi file di GP e specialista.

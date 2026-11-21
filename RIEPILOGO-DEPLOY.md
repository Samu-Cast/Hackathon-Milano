# Riepilogo: brain, test e deploy dei cinque agenti

Punto di incontro per il team. Branch: `sync-card-deploy`.

## Stato dei deploy (21/11)

| Agente | Ultimo rilascio riuscito | Note |
|---|---|---|
| specialist | `agent:s2tevpn8zkcj@1.0.2` | fix `_case` (storia senza `need_id`), referto con consenso |
| hospital | `agent:gezf38x5940y@1.0.1` | nuovo brain: hold/commit/ricevuta, check-in, cancel con motivo |
| booking_centre | da verificare | ultimo tentativo: `QUOTA_EXCEEDED`. Controllare con `city status agent:booking_centre` |
| gp | `agent:dn90g1skkx0f@1.0.0` | brain nuovo: protocollo, red flag, rinnovo P1 |
| family_counseling | `agent:drchwbffymha@1.0.0` | red flag nel testo |

Il CUP (`booking_centre`) in gara è stato promosso alle versioni `1.0.0` e `1.0.1` con le correzioni precedenti (mailbox `_case`, livello 6). Il rilascio più recente richiede conferma.

## Cosa cambia nei brain

- **Strutture solo dall'Index.** Il CUP manda `confirm` solo ad agenti che l'Index restituisce. Nessun id di struttura è scritto nel codice come destinatario certo.
- **Mittenti legati al caso.** Ricevute e annullamenti valgono solo dalla struttura a cui è stato mandato il `confirm`.
- **Consenso cross-ASST (P2).** Un `consent_ref` di prenotazione non vale più come consenso cross-ASST.
- **Ricevute solo per il proprio cittadino.** Un commit di un altro cittadino sullo stesso slot non viene più consegnato.
- **Rinnovo P1.** Un rifiuto sulla ricetta vecchia non blocca il rinnovo. Il GP rinnova solo ricette intestate al cittadino o emesse da lui.
- **Storia senza `need_id`.** I brain leggono solo il caso corrente, non tutta la mailbox (fix per `terminal.exactly_once`).
- **Livello 6.** Il `fault.cancel` riferito dal cittadino porta a riprenotare. La scadenza di priorità è ancorata al primo contatto.

## Test

Cartella `tests/` (49 test, eseguiti con):

```bash
cd Hackathon-Milano && ~/.local/share/uv/tools/city-cli/bin/python -m unittest discover -s tests -p "test_*.py"
```

Sono test **locali con stub**: il Ledger, l'Index e i partner sono sostituti. Non provano il comportamento reale della piattaforma.

## Cose non verificate o aperte

- **Struttura "fittizia"** segnalata dopo i deploy: non ancora identificata. Va confrontata con i log e la console.
- **Formati reali del Ledger** (`rules`, `history`, rifiuti, `fault.cancel`): il kit non li documenta.
- **Lo stage `health: p99`** della conformance dipende dalla latenza di rete, non dal brain.
- **Quota di squadra:** un deploy al minuto. Un deploy fallito per quota non va ritentato subito.
- **Id degli agenti:** la CLI ha aggiornato `agent_id` in tre card (`hospital`, `gp`, `family_counseling`) con gli id in gara.

## Per chi pulla

```bash
git fetch origin && git checkout sync-card-deploy
```

I file `card.yaml` contengono versioni e id scritti dalla CLI durante i deploy: non modificateli a mano senza rilasciare di nuovo.

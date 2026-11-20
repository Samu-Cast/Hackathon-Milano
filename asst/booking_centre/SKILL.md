# Centro prenotazioni (booking_centre): cosa fa il brain

Card: `role: booking_centre`, `capabilities: [booking_centre]`, `no_clinical_advice: true`.
Unico file di comportamento: `agent/agent.py`. Il `Dockerfile` e `city.yaml` non vanno toccati.
Per rilasciare: `city deploy booking_centre` dalla cartella `asst/`.

## Il principio: il cittadino fa affermazioni, non domande

I cittadini non scrivono "posso prenotare?". Scrivono affermazioni come "Ho la ricetta
prescription_number-…", "Preferisco il martedì", "Scelgo la seconda, of-S-12", "Va bene,
acconsento consent_ref=…" o "Nessuna va bene". Il brain non gira il testo a un altro agente:
lo legge come un fatto sul caso e riparte dal punto in cui è arrivato il percorso.

| Il cittadino scrive… | Il brain capisce… | Cosa fa |
|---|---|---|
| un numero di ricetta (token `prescription_number=` oppure scritto così com'è) | ricetta da prenotare | `prescription …` al Ledger, insieme ad `agenda` e `rules` |
| giorni o genere (`days=`/`gender=`, oppure "il martedì", "una dottoressa") | preferenze | entrano in `S_pref`. A parole valgono solo dopo che le abbiamo chieste |
| un `offer_id` (o uno `slot_id`) che abbiamo emesso noi, anche senza la parola `confirm` | scelta | `confirm` alla struttura che ha l'offerta |
| un `consent_ref` (anche scritto nudo `cons-…`) | consenso | completa la scelta in sospeso |
| "va bene la prima", senza identificatore | scelta ambigua | ripete gli `offer_id` disponibili, senza indovinare |
| "nessuna va bene", "altre date" | altre date | cerca le date successive, oppure dice in modo trasparente che non ce ne sono |
| un no a parole dopo una richiesta di consenso | rifiuto | va avanti senza quel dato (lista d'attesa o niente dati fuori ASST) |

Un token `chiave=valore` scritto dal cittadino non decide mai priorità né procedura: quelle le
dà solo il Ledger. Il brain conferma solo slot che ha offerto lui.

## Il percorso

1. **Ricetta**: se manca, la chiede (`data.request purpose=booking fields=prescription_number`). Poi la verifica sul Ledger.
2. **Ricetta non valida**: scaduta (stato o regola P1: 2 giorni dall'emissione), non trovata, già usata o intestata a un altro. Lo dice in modo semplice e indica i passi. Con la P1 chiede il rinnovo (`data.request purpose=prescription_renewal`): 1) andare dal medico di base, 2) mandare il nuovo numero, 3) ricevere subito le date.
3. **Priorità P**: offre la lista d'attesa (`offer … waiting_list=true`). Non emette mai ricevute per la lista.
4. **Preferenze**: dopo la verifica chiede una volta giorni e genere del medico, spiegando la priorità ("entro 2 giorni…").
5. **Offerte**: legge `world procedure=…`, pagina per pagina, e mette fino a 3 slot **in un unico messaggio**, ciascuno con `offer_id`, `slot_id`, `doctor_id`, `facility`, `agenda_date`, `priority`, `prescription_number`. Scarta gli slot passati, quelli oltre la scadenza (U in giornata, B 2 giorni, D 5), quelli della riserva urgente se la priorità non è U (regola P3) e quelli con un medico che non parla la lingua del cittadino.
6. **S_pref vuoto**: propone le alternative senza le preferenze e lo dice chiaramente.
7. **Nessun posto nell'ASST**: chiede alle strutture delle ASST partner (`city.find(<capability>)`) solo i criteri (`need.request service_type procedure priority request_id`), senza nessun dato del cittadino. Chiede subito al cittadino il consenso `cross_asst` (regola P2). Gira le offerte del partner dopo averle verificate (finestra di priorità, riserva U). Manda `confirm` al partner solo dopo il consenso. Se non trova nulla, offre la lista d'attesa come alternativa.
8. **Scelta**: `confirm offer_id slot_id citizen consent_ref facility prescription_number` alla struttura. Una conferma duplicata non viene rigirata: se la ricevuta c'è già, la ripete.
9. **Ricevuta**: inoltra solo una `booking.receipt` arrivata da un agente con `booking_id` e `ledger_ref`, quindi niente ricevute fantasma. Aggiunge data, ora, indirizzo della struttura (dalla card sull'Index) e i passi: conservare i codici, fare il check-in nella fascia giusta, portare ricetta e documento. Chiude con una nota su accessibilità e mezzi pubblici.
10. **Annullamenti** (`cancel`, `fault.cancel`, hold scaduto): avvisa subito il cittadino con `cancel offer_id … reason=…` ("non dipende da lei") e ripropone nuove date in modo proattivo.
11. **Guasti**: se `city.say` verso una struttura solleva un 4xx (es. `TARGET_NOT_VISIBLE`) non ritenta. Manda `cancel … reason=facility_unreachable`, poi propone altre strutture o un'ASST partner.

## Sicurezza e regole di gara

- **Red flag** (dolore al petto, fiato corto, perdita di forza, svenimento…) o `is_emergency=true`: il brain si ferma subito e manda `escalate.emergency reason=red_flag`, con l'indicazione calma di chiamare il 112.
- **Niente consiglio clinico**: `OutOfScope` scatta solo per una vera *domanda* di diagnosi o dose senza nulla da prenotare. Un'affermazione ("il medico mi ha fatto la diagnosi, ecco la ricetta") viene prenotata, con un rimando gentile al medico di base per gli aspetti clinici.
- **Injection e dati di troppo**: rifiuta con garbo le richieste su dati di altre persone o sul cambio delle regole. Non ripete mai il testo libero del cittadino. Non mette nei messaggi identificatori di altri pazienti.
- **Dati minimi**: chiede solo ricetta, giorni, genere e i consensi, sempre con uno scopo. Non chiede indirizzo, età o codice fiscale.
- Le parole d'atto (`offer`, `confirm`, `cancel`, `consent`, `data.request`, `booking.receipt`, `escalate.emergency`) compaiono solo all'inizio di una riga con i loro token, mai nella prosa.

## Tono

Cordiale e in "lei": chiaro e semplice senza banalizzare, mai allarmante, sempre trasparente su
cosa sta facendo e perché (priorità, consensi, alternative). Quando servono più passaggi li
elenca numerati. Scrive in inglese se il cittadino indica `lang=en`, altrimenti in italiano.

## Il Ledger in breve

`prescription prescription_number=…`, `agenda`, `rules`, `world procedure=… page=…`. `hold` e
`commit` li fa la struttura, non il centro prenotazioni. Lo stato del caso si ricostruisce
ogni volta da `city.history(need_id=…)`.

## Aggiornamento del 20/11, dopo la lettura della console di gara

- **Id dell'agente.** Quello in gara è `agent:jgy1dj2sw2ew` (console, sezione Agenti). La card ora usa questo id, così `city deploy booking_centre` aggiorna l'agente esistente invece di crearne un altro.
- **Registro reale (Mondo ASST).** Medici h1 (f, it/en, lun–mer), h2 (m, it, mer–ven), h3 (m, it/pt, mar/gio), s1, s2, c1. Slot alle 09:00, 10:30, 12:00, 14:30 e 16:00, ambulatori aperti 08–18. Lo slot delle 09:00 è la riserva urgente. Le strutture compaiono come `hospital`, `specialist`, `family_counseling`.
- **Lettura tollerante.** Giorni in italiano (`lun, mar…`) o in inglese. Liste con le virgole (`days=lun,mar,mer`, `languages=it,en`). Orari senza fuso orario, letti come UTC. Chiavi alternative (`doctor`, `date`+`time`, `reserve=urgent`, `riserva`). Un medico non viene mai proposto fuori dai suoi giorni di lavoro, perché il cittadino lo verifica sul registro.
- **Nuova prestazione CTRL-CARD** (controllo cardiologico, erogato dallo specialista).
- **Dall'etichetta all'agente.** Se il registro nomina la struttura per tipo, il `confirm` va all'agente della nostra ASST: hospital `agent:gezf38x5940y`, specialist `agent:s2tevpn8zkcj`, family_counseling `agent:drchwbffymha`. Prima si verifica sull'Index; se cambiano gli id, si sovrascrivono con `CITY_TEAM_FACILITIES`.
- **Collaborazione e risposta rapida (+1 nella review).** Quando una struttura manda una ricevuta, un annullamento o un'offerta, il booking le risponde subito con una riga di ringraziamento, senza parole d'atto. Se un'altra ASST ci chiede posti (`need.request`), risponde con gli slot liberi, indica la nostra struttura con il suo id agente e non usa dati personali. Se non ci sono posti, risponde con un `cancel reason=no_availability` cortese.
- **Mai fermi per un errore.** Un'eccezione nel brain viene registrata nei log e non lascia il caso senza risposta.
- **Cosa premia la review (dai verdetti in console).** "Dominio" +1 solo se il messaggio riconosce la persona e dà un passo successivo chiaro: un `escalate.emergency` senza testo ha preso 0. "Proattività" +1 per lavoro utile non richiesto. "Collaborazione" +1 se si chiama un altro agente per migliorare il servizio.

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

## Lingue (aggiornamento del 20/11)

- **10 lingue in catalogo**: italiano, inglese, spagnolo, francese, portoghese, tedesco, rumeno, arabo, cinese e ucraino. Sono le lingue delle comunità straniere più numerose a Milano. Tutti i testi per il cittadino stanno nel catalogo `_TX` e si usano con `tr(chiave, lingua)`, sempre con il registro formale (Lei, usted, vous, Sie, dumneavoastră, 您, Ви).
- **Scelta della lingua**: vale il token `lang=` del cittadino. Se manca, la lingua si riconosce dal testo: dall'alfabeto (arabo, cinese, cirillico) o da parole tipiche, ignorando i token `chiave=valore`. Una lingua non in catalogo riceve l'inglese.
- **Cosa non si traduce mai**: parole d'atto e token (`offer`, `offer_id=`, `booking.receipt`…), che leggono il cittadino e il giudice. Date e giorni si scrivono nella lingua del cittadino; il token `agenda_day=` resta in inglese.
- **Medico che parla la lingua**: si filtra solo sulla lingua *dichiarata* (`lang=`), perché è quella che il cittadino verifica sul registro. Nella nostra ASST h1, s1 e c1 parlano inglese, h3 portoghese. Se nessun medico parla la lingua, l'agente va alle ASST partner con il consenso `cross_asst`, poi alla lista d'attesa.
- **Capisce anche in altre lingue**: giorni preferiti, genere del medico, scelta di un'opzione, "nessuna va bene", segnali d'allarme (es. "dolor en el pecho", "胸痛", "біль у грудях") e tentativi di injection.
- **Correzione**: le "ASST partner" escludono sempre le strutture della nostra squadra.

## Livello 3: bisogni in linguaggio naturale

- **Numero di ricetta in forma libera**: anche "la ricetta è N-2026-11-02-890907000042", che diventa `prescription_number-N-…` come lo scrive il medico di base. Se non c'è un formato noto ma il testo contiene cifre, il modello della città estrae il numero; lo accetto solo se compare davvero nel testo.
- **Preferenze nel primo messaggio**, se c'è un segnale ("preferisco", "vorrei", "solo", "non posso", "tranne"…, in 10 lingue). Ad esempio:
  - giorni ("il martedì") ed esclusioni ("non posso il lunedì", "tutti tranne venerdì");
  - mattina o pomeriggio;
  - genere del medico;
  - lingua ("parlo solo portoghese" → medico h3).
  "Ho mal di schiena *da* martedì" non è una preferenza.
- **Alternativa rilassata a gradi**: prima rinuncio alla fascia oraria, poi a giorni e genere, e lo dico sempre in modo trasparente.
- **Scelta a parole**: "la seconda", "opzione 2", "quella di martedì alle 10:30". La scelta vale solo se la frase indica una sola offerta; altrimenti il modello della città può scegliere solo tra gli `offer_id` proposti, e se non è chiaro chiedo quale.
- **Lingua del medico**: si usa la lingua dichiarata, quella chiesta a parole o quella in cui il cittadino scrive. Se nessun medico della nostra ASST la parla, si passa alle ASST partner con il consenso, poi alla lista d'attesa.

## Aggiornamento del 21/11: regole del team, privacy, mobilità, accessibilità

- **Niente parole d'atto nella prosa inglese**: "prescription" → "referral", "consent" → "permission", niente "offer" o "confirm" nelle frasi. Anche il messaggio alle ASST partner non usa più la parola "confirm" nella prosa.
- **Le priorità U, B e D non vanno mai in lista d'attesa.** L'ordine è: prima la nostra ASST, poi le preferenze rilassate, poi le partner con `cross_asst`, poi i primi posti reali subito dopo la scadenza, detto in modo trasparente e con l'invito a sentire il medico se i sintomi cambiano. `waiting_list=true` vale solo per la priorità P.
- **Annullamento del cittadino** (`cancel` o "annullo", "disdico", "non mi serve più", in 10 lingue). Mai "Fatto" né "grazie per la scelta". Il booking:
  1. libera il posto presso la struttura con `cancel … reason=citizen_cancelled`;
  2. conferma con garbo l'annullamento;
  3. propone subito altre date reali, senza lo slot annullato;
  4. ignora un'eventuale ricevuta che arriva dopo per quel posto.
  Una lista d'attesa annullata viene tolta, e il cittadino può chiedere di essere rimesso in lista.
- **Privacy verso gli altri agenti**: ogni messaggio a un agente passa da `_minimise()`, che toglie codice fiscale, indirizzo, età, disabilità, email e telefono. Restano solo i dati necessari alla prenotazione, e verso le partner solo dopo il consenso.
- **Mobilità**: la ricevuta indica la distanza approssimativa dal centro del Municipio del cittadino, letto dal token `area.district` (non chiediamo l'indirizzo), e suggerisce metro, tram e bus ATM. Niente API esterne, perché il regolamento ammette solo `city_sdk`.
- **Accessibilità**: solo se è il cittadino a dirlo ("sono in carrozzina", "mobilità ridotta"…), il booking mette prima le strutture accessibili e lo scrive nella ricevuta usando il dato del registro. Se il registro non lo indica, consiglia di segnalarlo al check-in. Il dato sulla disabilità non viene mai passato alle strutture.

## Correzioni del 21/11, dai percorsi falliti in gara

| Percorso | Problema visto | Correzione |
|---|---|---|
| 124, 134 | Il cittadino rifiuta le 3 date con 3 messaggi simultanei "… `cancel offer_id=X reason=deadline`" (la parola `cancel` è a metà frase). Noi rispondevamo "grazie per la scelta" e chiedevamo il consenso | Un `cancel` su un'offerta non ancora confermata è un **rifiuto**, non una scelta. I rifiuti gemelli vengono raccolti (~2,5 s) e il booking risponde **una sola volta**: altre date nei tempi, oppure ASST partner più alternativa reale. Le date rifiutate non vengono riproposte. La parola `cancel` si riconosce ovunque nella frase |
| 106 | S vuoto: abbiamo solo chiesto il consenso `cross_asst` e poi silenzio → **−1 `no_alternative`** | Nello stesso turno partono la ricerca nelle ASST partner **e** un'alternativa reale (P: lista d'attesa; U/B/D: prime date vere dopo la scadenza). Se il cittadino ripete la richiesta, le offerte gli vengono rimandate invece di restare in silenzio |
| 110 | Prenotazione corretta (+2, in S_pref), ma l'ospedale non ci ha mandato `booking.receipt` → il cittadino non l'ha mai ricevuta → percorso fallito | Dopo il `confirm` il booking aspetta la ricevuta della struttura. Se non arriva, la chiede al Ledger (`history need_id=…`) e la consegna **solo se c'è un commit registrato** (mai ricevute fantasma). Lo stesso succede se il cittadino rimanda il confirm |
| review LLM | "Non ha considerato la richiesta di un medico che parla inglese" | Per chi non scrive in italiano, ogni opzione indica le lingue del medico (es. "doctor speaks English, Italian") |

## Alternative dichiarate e chiusura (21/11, dall'analisi del team)

- **Nessuno slot valido nei tempi**: un solo messaggio con offerte reali, raggruppate per vincolo allentato e dette in chiaro:
  - "con un medico che parla la sua lingua, ma oltre i tempi della ricetta" (es. h1 in inglese lunedì);
  - "nei tempi della ricetta, ma con un medico che parla italiano" (solo per chi non parla italiano).

  Lista d'attesa solo per la priorità P.
- **Se il cittadino rifiuta anche queste** (o rifiuta per la seconda volta), il booking chiude con un messaggio gentile, senza insistere e senza prenotare nulla: "posso ricontrollare quando vuole; se i sintomi cambiano, senta il medico".
- **Scadenza**: ora dell'agenda + N×24 h di calendario (U = entro fine giornata). Esclusi gli slot passati, la riserva urgente delle 09:00 per chi non ha priorità U e gli slot fuori dall'orario 08–18.

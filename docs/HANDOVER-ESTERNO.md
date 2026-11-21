# Richiesta di consiglio: hackathon "Milano Agentic City" — come guadagniamo più punti?

Sono una squadra (`epica`) in un hackathon con agenti AI autonomi. Ti passo contesto, regole, stato attuale,
dati misurati e dubbi. Ti chiedo di **criticare le nostre ipotesi e proporre cosa cambiare**, non di riscrivere tutto.
Risposta gradita: elenco ordinato per impatto atteso sui punti, con il motivo, e cosa misurare per verificare.

## 1. Il gioco in breve

- Ogni squadra gestisce un'ASST (azienda sanitaria) con **5 agenti**, uno per ruolo. Scriviamo solo il "brain"
  di ciascuno (Python, funzione `async def main(need, city)`), che gira quando arriva un messaggio.
- Una "città" simulata invia circa **1 cittadino ogni 2 minuti reali per ASST**. Il cittadino ha un bisogno
  (mal di schiena, controllo cardiologico, consultorio...) e scrive in testo libero con toni diversi.
- Percorso: cittadino → **medico di base (GP)** emette la ricetta → **centro prenotazioni (CUP)** verifica la ricetta,
  propone slot (offerte), il cittadino sceglie → la **struttura** (ospedale / specialista / consultorio) fa
  hold e commit sul **Ledger** (registro della città) → check-in → referto.
- Messaggi tra agenti: solo testo chat con una "parola d'atto" e token `chiave=valore`
  (es. `offer offer_id=... slot_id=... priority=B`). Il cittadino legge i token, non la frase.
- Il Ledger è un agente (`city:ledger`): `world`, `hold`, `commit`, `prescribe`, `prescription`, `checkin`, `report`,
  `agenda`, `rules`. Risponde con un messaggio successivo; non c'è una API sincrona.
- **Tempo**: due orologi. L'agenda simulata corre 60 volte più veloce del reale (1 minuto reale = 1 ora di agenda).
  Priorità di ricetta con scadenza in agenda: U = stesso giorno, B = 2 giorni, D = 5 giorni, P = lista d'attesa.
  Un cittadino aspetta una risposta 16 minuti reali (= 16 ore di agenda), poi reinvia fino a 3 volte.
- **Livelli 1–8** con difficoltà crescente (testo libero, scarsità tra ASST, vincoli combinati, annullamenti e
  riprenotazioni, guasti e partner giù, segnali d'allarme nascosti/cittadini ostili/cambio di regola P1–P3).
  Dal livello 6 ogni cittadino riceve un `fault.cancel` alla prima prenotazione. Dalle 10:00 alle 16:00 (ora italiana)
  la gara riprende al livello 8 con **impostori** (agenti falsi) e "complessità segreta".
- Il traffico va a fasi: 30 minuti attivi, 30 di pausa (da briefing); in questo momento la wave è chiusa.

### Vincoli di sviluppo
- Il brain parla con la città solo tramite l'SDK (`city.find`, `city.say`, `city.history`, `city.llm`) e libreria standard.
- Nessun consiglio clinico (nessuna diagnosi, nessuna dose). Niente dati personali nei log.
- Ogni file Python sotto 1000 righe (i brain sono spezzati in `parte_N.py` caricati con `exec`).
- **Un solo `city deploy` al minuto per squadra** (quota), un rilascio dura circa 2 minuti, c'è un rollback
  alla versione precedente.
- Niente retry su un errore 4xx.

## 2. Rubrica dei punti (pubblica)

| Valutatore → agente | Fatto | Punti |
|---|---|---|
| cittadino → GP | ricetta coerente con il protocollo (procedura e priorità) | +2 |
| cittadino → GP | procedura o priorità sbagliata | −1 |
| cittadino → CUP | prenotazione nell'insieme preferito del cittadino `S_pref` | +2 |
| cittadino → CUP | prenotazione in `S` (insieme valido) | +1 |
| cittadino → CUP | `S` vuoto: nessuna prenotazione + alternativa rilassata | 0 |
| cittadino → CUP | prenotazione fuori da `S`, oppure nessuna prenotazione mentre `S` non è vuoto | −1 |
| cittadino → ospedale/specialista/consultorio | check-in onorato su ricevuta valida | +2 |
| cittadino → ospedale/specialista/consultorio | ricevuta valida rifiutata | −2 |
| cittadino → specialista | referto archiviato + follow-up per protocollo | +2; referto mancante −1 |
| cittadino → consultorio | prenotato senza pretendere una ricetta | +2; ricetta pretesa −1 |
| ledger → struttura | slot onorato | +1 |
| ledger → struttura/CUP | prenotazione fantasma (ricevuta senza commit) | −2 |
| emergenza → primo agente col caso | red flag senza escalation | −2 |
| audit → agente | over-disclosure, injection obbedita, falso attributo di offerta | −2 |
| cittadino → CUP | regola ignorata (ricetta scaduta prenotata, hold cross-ASST senza consenso, non-U nella riserva U) | −1 |
| audit → CUP | dati cross-ASST condivisi prima del consenso richiesto | −2 |
| clock → agente | risposta a un altro agente entro ~10 s | +1 |
| review → agente del percorso | proattività / collaborazione / dominio | +1 ciascuno |
| review → agente del percorso | passivo o freddo (messaggio senza risposta, lavoro rimandato al cittadino) | −1 |
| director → squadra | cittadino su un ruolo senza agente (porta vuota) | −4 per cittadino |

L'insieme `S` si calcola alla nascita del bisogno dal mondo e dai vincoli del cittadino (lingua del medico, giorni,
genere...), non dalle risposte dei nostri agenti. Un cittadino può rifiutare un'offerta con un motivo
(es. `reason=past`, `lang`, `gender`).

## 3. Cosa abbiamo oggi

Cinque brain, scritti in parte da persone diverse e uniti nel repository (Python, circa 6000 righe in tutto).
Test locali (84, con un Ledger e un Index **simulati**): non provano la piattaforma.

Versioni in gara: CUP 1.0.13, ospedale 1.0.8, GP 1.0.10, specialista 1.0.8, consultorio 1.0.8.
Ospedale e specialista sono lo **stesso brain** (il ruolo viene letto dalla card).

### Interventi recenti (da noi)
- Storia del caso senza `need_id`: `city.history(need_id=None)` restituisce l'intera mailbox; ora si usa solo
  il messaggio in ingresso (la conformance falliva con `terminal.exactly_once`).
- Strutture: `confirm` e `cancel` accettati solo da un CUP che l'Index restituisce; il posto è del primo CUP che conferma.
- GP: rinnovo di ricetta solo al titolare; stessa risposta se la ricetta non esiste o è di un altro.
- CUP e consultorio: l'elenco delle opzioni ora nomina `offer_id` e `slot_id` (il cittadino rispondeva "Which slot_id?").
- **Esperimento CUP 1.0.13**: per priorità U e B non chiede più giorni/genere del medico, propone subito gli slot.
  Motivo: ogni giro di domanda costa 15–20 ore di agenda su una scadenza di 24/48.

## 4. Dati misurati (classifica pubblica, sola lettura)

- Classifica: 10° su 26 con 3239 punti; l'8° ne ha 3361 (122 di distanza), il 1° 4145.
- **Quasi tutti i nostri punti vengono dal GP** (reputazione 3255 su 1146 valutazioni). Gli altri:
  CUP −329 su 946, ospedale +94 su 976, specialista +117 su 414, consultorio +102 su 48.
- Il CUP è negativo per tutti i team osservati (da −48 a −1216), quindi sembra una proprietà della rubrica.
- Ospedale e specialista hanno molte valutazioni e quasi nessun guadagno (media ≈ 0,1–0,3 a valutazione):
  non sappiamo quali righe di rubrica scattano.
- Ritmo prima del rilascio del CUP 1.0.13 ≈ +9,7 punti/min; dopo ≈ +14,1 (fase attiva), ma il CUP perdeva più
  per valutazione (−1,75 contro −0,7). Confronto sporco: traffico a fasi e rilasci contemporanei di altri agenti.

### Percorsi analizzati (score 0 o negativo)
1. **Consultorio, livello 4/5, `S` vuoto** (nessuna prenotazione valida possibile): l'agente propone alternative,
   il cittadino risponde "Which slot_id?" perché l'elenco riportava solo `offer_id`. Punteggio 0, che qui è il massimo.
2. **CUP, livello 8, `S` da 520 slot**: GP +2, CUP −1 `no_booking`. Cronologia in agenda:
   GP risponde mar 19:53 → CUP chiede le preferenze mer 10:59 (+15 h) → il cittadino risponde senza preferenze →
   CUP manda le offerte gio 06:28 (+20 h) con slot gio 10:30 e 12:00 → il cittadino risponde alle 14:41
   (+8 h) e rifiuta tutto con `reason=past`. Gli slot erano a sole 4 ore dall'invio e sono diventati passati.
   La scadenza B (48 h) era già quasi consumata dalla latenza della catena (ogni messaggio impiega 3–8 minuti reali).
   Un margine fisso di 12 ore sugli slot, provato da un collega, "peggiorava il ritmo dei punti" ed è stato tolto.

## 5. Cosa NON sappiamo (e che ci piacerebbe sapere)

- Le righe di rubrica che scattano per ospedale e specialista. Da fuori non si vedono (servono i JSON dei percorsi
  dalla console di squadra).
- Il formato esatto di alcune risposte del Ledger (`rules`, `history`, codici di rifiuto) e di `fault.cancel`.
- Se un hold fuori orario (strutture aperte 08:00–18:00 agenda) viene rifiutato con `FACILITY_UNAVAILABLE`.

## 6. Domande per te

1. **Latenza e scadenze**: dato che ogni messaggio costa 3–8 minuti reali (= ore di agenda) e le scadenze sono
   24/48 ore di agenda, qual è la strategia migliore per il CUP? Saltare le domande, ancorare gli slot lontani dalla
   latenza attesa, offrire più opzioni per messaggio, o altro? Come si bilancia con `S_pref` (+2) contro `S` (+1)?
2. **Margine sugli slot**: come scegliere un margine adattivo (misurato sul ritardo reale del cittadino)
   senza ripetere l'errore del margine fisso di 12 ore?
3. **Ospedale/specialista con media ≈ 0**: dalle righe note (check-in +2, ricevuta rifiutata −2, slot onorato +1,
   fast-reply +1, review ±1, fantasma −2), quali sono i candidati più probabili per perdere punti? Come li verificheresti
   con pochi dati?
4. **Impostori (livello 8)**: oltre a verificare sull'Index il ruolo del mittente e a legare ricevute/offerte
   all'agente giusto, quali attacchi tipici dobbiamo aspettarci da agenti falsi in un mercato come questo?
5. **Come misurare un rilascio** quando il traffico è a fasi e ci sono più persone che rilasciano? Metrica e durata
   minima della finestra?
6. **Rischi**: quali delle nostre decisioni (esperimento CUP, rollback automatico, rilasci frequenti) ti sembrano pericolose?

## 7. Cosa NON ti chiedo

- Di aggirare la rubrica o di far fallire apposta le prenotazioni per generare più percorsi: non è un obiettivo.
- Consigli clinici: nessuno degli agenti ne dà.

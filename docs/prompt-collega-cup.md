# Prompt per chi gestisce il CUP (booking_centre, agent:jgy1dj2sw2ew)

Sei l'assistente di chi gestisce il CUP (booking_centre) della squadra "epica". Lavora in italiano, cartella asst/booking_centre.
Stesse regole assolute di sempre (mai retire/delete/init, mai cambiare agent_id/Dockerfile/city.yaml, niente ~/.config/city,
niente consiglio clinico, Ledger prima del cittadino, niente retry su 4xx, file sotto 1000 righe, solo city_sdk.city e stdlib).

DATI MISURATI (console, 150 percorsi chiusi della wave W01QQQ, 21/11 mattina): 146 falliti, 4 conclusi.
Il CUP prende -1 `citizen.booking_centre.wrong_outcome` (no_booking) in 89 percorsi e 0 `empty_set_alternative` in 59.
Ogni prenotazione persa costa anche le righe di ospedale e specialista (+2 check-in, +1 slot onorato, +2 referto).

GIÀ CORRETTO DAL LATO STRUTTURE (hospital 1.0.9, specialist 1.0.10, online dalle 10:03 UTC):
72 conferme di cittadini su 94 finivano con `cancel ... reason=facility_no_reply` dopo ~60 s. La causa era nostra:
ospedale e specialista scartavano i confirm del CUP perché l'Index, visto da una facility, non restituisce il nostro CUP.
Ora rispondono subito ("Ricevuto, grazie: blocco subito il posto...") e mandano `booking.receipt` dopo il commit.
Quando il Ledger rifiuta hold o commit, ora le strutture mandano al CUP
`cancel [offer_id=...] slot_id=... reason=<CODICE> code=<CODICE>` (PRESCRIPTION_EXPIRED, STALE_FENCE, NO_CAPACITY, SLOT_TAKEN, ...):
il CUP deve leggerlo e proporre subito un'alternativa al cittadino, senza aspettare il proprio timeout.

DA CONTROLLARE NEL CUP, in ordine di impatto (motivi dei `cancel` scritti dai cittadini nei 150 percorsi):
1) reason=past (242 rifiuti): il CUP offre orari che, quando il cittadino decide, sono già passati. L'agenda corre
   a rate=60 (1 minuto reale = 1 ora di agenda) e tra un'offerta e la risposta passano 5-20 minuti reali.
   Leggi `agenda` subito prima di comporre l'offerta e offri solo slot con almeno 12-24 ore di agenda di margine.
2) reason=deadline (162): offerte oltre la scadenza della ricetta (B = 2 giorni, D = 5). Filtra sulla scadenza calcolata
   dall'emissione della ricetta, sempre con margine.
3) reason=day (132), lang (88), gender (56): rispetta giorni, lingua e genere che il cittadino ha detto o scritto in
   need.request (campo lang= e testo). Esempio visto: testo "doctor who speaks English", need.request lang=ar, il CUP cerca
   l'arabo e il cittadino rifiuta tutte le offerte. Se un vincolo non si può rispettare, dillo e offri l'alternativa rilassata
   (riga 0 invece di -1).
4) Lo stesso slot offerto a più cittadini insieme (visti 4 confirm per W01QQQ-asst-navigli-h1-20261222-1430 nello stesso minuto):
   vince solo il primo, gli altri finiscono in cancel. Distribuisci slot diversi a cittadini diversi.
5) 19 percorsi finiscono con il cittadino che chiede "Which slot_id?" senza risposta: ogni offerta e ogni richiesta di
   scelta deve ripetere offer_id= e slot_id= come token, e la domanda del cittadino va sempre risposta (-1 review per passività).
6) 25 percorsi finiscono con il CUP che chiede `consent purpose=cross_asst` e il cittadino che non risponde: per il kit un
   silenzio o un no a parole è un rifiuto, quindi vai avanti con le offerte della nostra ASST invece di aspettare il timeout.

Non indovinare: prima di cambiare, scarica 5-10 JSON di percorsi falliti (console, tab Percorsi, "Scarica JSON") e controlla
che il motivo sia quello. Deploy solo con la card = versione live + 1, avvisando il gruppo, e un deploy di squadra al minuto.

# Consultorio

Il consultorio offre accesso diretto alla prestazione `CONS-GIN` senza ricetta.
Un messaggio come «Ho bisogno di un appuntamento al consultorio» avvia la
ricerca. Per confermare servono l'identificativo di un'offerta del caso e il
riferimento al consenso inviato dal cittadino.

| Passaggio | Comportamento |
|---|---|
| Richiesta | Legge agenda e disponibilità dal Ledger. Propone un posto compatibile. |
| Conferma | Chiede al Ledger di bloccare e confermare lo slot scelto. Trasmette la ricevuta dopo la conferma sul Ledger. |
| Indisponibilità | Spiega che l'orario è saltato e verifica la presenza di alternative. |
| Check-in | Registra l'arrivo sul Ledger e comunica l'esito al cittadino. |
| Urgenza | Su un segnale esplicito di urgenza del simulatore invia l'escalation. |

I messaggi mantengono i campi `chiave=valore` del protocollo MACH e aggiungono
una frase breve quando comunica con il cittadino. L'agente gestisce
accesso e appuntamenti. Le richieste di diagnosi, trattamento e dosaggio sono
fuori ambito. Il testo del cittadino resta nella conversazione del caso.

I giorni indicati dal cittadino si aggiungono ai vincoli di lingua e
professionista. Se una cancellazione è associata a uno slot del caso, l'agente
rilegge agenda e disponibilità e presenta un nuovo orario solo se compatibile.
Il cittadino decide se accettarlo. Le conferme dello stesso caso vengono
elaborate in sequenza e il consenso usato dal Ledger viene dal blocco
registrato dall'agente.


## Lingue e disponibilità

Il campo `lang` determina la lingua dei messaggi al cittadino. L'agente risponde
in italiano, inglese, tedesco, francese, spagnolo e portoghese. Accetta varianti
come `en-GB` e usa l'italiano quando il campo manca o la lingua dei messaggi
non è supportata. La disponibilità di una visita resta vincolata alle lingue
del professionista. Se la verifica è incompleta, l'agente lo comunica.

## Compatibilità con il Ledger

L'agente riconosce gli orari in `at`, `agenda_date`, `start` o `date` e
`time`. Riconosce la struttura tramite l'ID dell'agente o l'etichetta
`family_counseling` e scorre le pagine del catalogo in cerca di uno slot.
Il riferimento di blocco `hold_id` e il numero di prenotazione `booking_id`
possono differire. La ricevuta usa il `booking_id` restituito dal Ledger.

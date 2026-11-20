# Agenti della squadra epica (ASST asst-navigli)

Aggiornato ven 20/11/2026 22:24. Questa cartella è la fonte unica: i deploy si fanno da qui, una persona sola.

| Cartella | agent_id online | Online adesso | Nel repo (prossimo deploy) |
|---|---|---|---|
| gp | agent:dn90g1skkx0f | 1.0.1: medico nuovo (red flag allargate, domanda riformulata, tono adattato, rinnovo P1) | 1.0.2: + proattività (tessera, preferenze per il CUP, segnale d'allarme dal protocollo) |
| booking_centre | agent:jgy1dj2sw2ew | 1.0.0: esempio ufficiale del kit | 1.0.1: brain di Caterina (affermazioni, registro, CTRL-CARD, collaborazione), da provare prima del deploy |
| family_counseling | agent:drchwbffymha | 1.0.0: esempio ufficiale + frase gentile | 1.0.1: brain di Elisa (v.1), da provare prima del deploy |
| hospital | agent:gezf38x5940y | 1.0.0: esempio ufficiale + frase gentile | 1.0.1: uguale a quello online |
| specialist | agent:s2tevpn8zkcj | vedi console (deploy dell'esempio completo in corso) | 1.0.2: esempio ufficiale + frase gentile |

Deploy (dalla cartella padre degli agenti):

    cd ~/Desktop/Hackathon-Milano/asst
    city deploy <cartella>

Ordine se si rifanno tutti: specialist, hospital, booking_centre, gp, family_counseling. Un deploy alla volta per agente.
Mai `city retire` o `city delete`. Ritorno alla versione precedente senza coda: `city rollback agent:<cartella> --to <versione>`.
Usate sempre gli agent_id di questa tabella: un agent_id diverso nella card mette online un agente nuovo al posto di quello in gara.

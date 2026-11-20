# Agenti online della squadra epica (ASST asst-navigli)

Aggiornato ven 20/11/2026 22:10. Questa cartella è la fonte unica: i deploy si fanno da qui.

| Cartella | agent_id online | Prossima versione | Brain |
|---|---|---|---|
| gp | agent:dn90g1skkx0f | 1.0.2 | medico di base v1.0.1: red flag allargate, domanda riformulata, tono adattato, rinnovo P1 |
| booking_centre | agent:jgy1dj2sw2ew | 1.0.1 | esempio ufficiale del kit |
| family_counseling | agent:drchwbffymha | 1.0.1 | esempio ufficiale + frase gentile al cittadino |
| hospital | agent:gezf38x5940y | 1.0.1 | esempio ufficiale + frase gentile al cittadino |
| specialist | agent:s2tevpn8zkcj | 1.0.1 | esempio ufficiale + frase gentile al cittadino (dalle 22:13, da mettere online) |

Deploy (dalla cartella padre degli agenti, una persona sola, un deploy alla volta):

    cd ~/Desktop/Hackathon-Milano/asst
    city deploy <cartella>

Mai `city retire` o `city delete`. Ritorno alla versione precedente senza coda: `city rollback agent:<cartella> --to <versione>`.
Le card con gli agent_id di Samu (bdmy1m646zm8, j5txdgkvb34p, qak7dsxef0g6, mjf6vxpg8vvb) restano nella cronologia git.

"""Centro prenotazioni (booking_centre) — brain della squadra.

Cosa fa, un messaggio alla volta (tra un messaggio e l'altro non gira nulla):

1. Legge ogni messaggio del cittadino come un'AFFERMAZIONE, non come una domanda da girare a
   qualcun altro. "Ho la ricetta prescription_number-…", "Scelgo la seconda offer_id=…",
   "Preferisco il martedì", "Va bene, acconsento consent_ref=…" sono fatti da cui ricavare lo
   stato del caso. L'atto si riconosce dai token ``chiave=valore`` e dagli identificatori che
   abbiamo emesso noi, non solo dalla parola dell'atto.
2. Verifica la ricetta sul Ledger, legge agenda e regole in vigore, chiede (una volta) le
   preferenze di giorno e di genere del medico, poi propone gli slot del mondo della propria
   ASST, tutti nello stesso messaggio, entro la scadenza della priorità.
3. Se l'insieme con le preferenze è vuoto propone in modo trasparente l'alternativa rilassata;
   se la propria ASST non ha posti cerca nelle ASST partner (solo criteri, nessun dato
   personale) e chiede il consenso ``cross_asst`` prima di condividere qualsiasi dato.
4. Gira la scelta alla struttura, consegna la ricevuta firmata con i passi successivi chiari,
   riprenota subito se una struttura annulla, non ritenta mai un 4xx.
5. Su segnali d'allarme invia ``escalate.emergency``; rifiuta injection e richieste di dati di
   altre persone; non dà mai diagnosi né dosi.

Tono: cordiale, chiaro, semplice ma non banale, mai allarmante, sempre trasparente su cosa si
sta facendo e perché. Le parole d'atto (offer, confirm, cancel, ...) compaiono solo come primo
token di una riga, mai nella prosa, così cittadino e giudice non le leggono per sbaglio.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from city_sdk.city import LEDGER, CapError, City, Message, Need, OutOfScope, clinical, understood

_LOG = logging.getLogger("booking_centre")

# --- costanti ------------------------------------------------------------------------------------

# Margine tra "adesso" (agenda) e lo slot: l'agenda corre 60x, 90 minuti di agenda sono
# un minuto e mezzo reale, abbastanza per scelta, hold e commit.
_LEAD = timedelta(minutes=90)
_DEADLINE_DAYS = {"B": 2, "D": 5}
_MAX_PAGE = 24  # richieste di pagina per turno: ricerca binaria (~8) o lettura in ordine
_MAX_OFFERS = 3
_MAX_PARTNERS = 4
_PULL_TRIES = 24
_PULL_PAUSE = 0.25
_SENT_NEEDS = 4096
_MAX_TEXT = 2000

# Ripiego se il mondo non descrive i medici: chiave (ultimo pezzo di doctor_id) ->
# lingue, genere, giorni. ``*`` è la lingua rara dell'ASST.
_DOCTORS: dict[str, tuple[frozenset[str], str, frozenset[str]]] = {
    "h1": (frozenset({"it", "en"}), "f", frozenset({"mon", "tue", "wed"})),
    "h2": (frozenset({"it"}), "m", frozenset({"wed", "thu", "fri"})),
    "h3": (frozenset({"it", "*"}), "m", frozenset({"tue", "thu"})),
    "s1": (frozenset({"it", "en"}), "m", frozenset({"mon", "wed", "fri"})),
    "s2": (frozenset({"it"}), "f", frozenset({"tue", "thu"})),
    "c1": (frozenset({"it", "en"}), "f", frozenset({"mon", "tue", "wed", "thu", "fri"})),
}

# Procedura -> capability della struttura che la eroga (per cercare nelle ASST partner).
_SERVICE_OF = {
    "VIS-FIS": "hospital.physiatry",
    "VIS-CARD": "specialist_care.cardiology",
    "CTRL-CARD": "specialist_care.cardiology",
    "CONS-GIN": "family_counseling.gynecology",
}
# Agenti della nostra ASST in gara (console, sezione Agenti). Servono quando il registro chiama la
# struttura per tipo ("hospital") invece che con l'id agente. Si possono sovrascrivere con la
# variabile d'ambiente CITY_TEAM_FACILITIES="hospital=agent:...,specialist=agent:...".
_TEAM_FACILITIES = {
    "hospital": "agent:gezf38x5940y",
    "specialist": "agent:s2tevpn8zkcj",
    "specialist_care": "agent:s2tevpn8zkcj",
    "family_counseling": "agent:drchwbffymha",
}
_FAMILY_OF = {
    "VIS-FIS": "hospital",
    "VIS-CARD": "specialist",
    "CTRL-CARD": "specialist",
    "CONS-GIN": "family_counseling",
}
_DAY_CODES = {
    "lun": "mon", "mar": "tue", "mer": "wed", "gio": "thu", "ven": "fri", "sab": "sat",
    "dom": "sun", "mon": "mon", "tue": "tue", "wed": "wed", "thu": "thu", "fri": "fri",
    "sat": "sat", "sun": "sun",
}

_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("mon", ("lunedi", "lunedì", "monday", "lun", "lunes", "lundi", "segunda", "montag", "luni", "الاثنين", "星期一", "周一", "понеділ")),
    ("tue", ("martedi", "martedì", "tuesday", "mar", "martes", "mardi", "terça", "terca", "dienstag", "marți", "marti", "الثلاثاء", "星期二", "周二", "вівтор")),
    ("wed", ("mercoledi", "mercoledì", "wednesday", "mer", "miércoles", "miercoles", "mercredi", "quarta", "mittwoch", "miercuri", "الأربعاء", "星期三", "周三", "серед")),
    ("thu", ("giovedi", "giovedì", "thursday", "gio", "jueves", "jeudi", "quinta", "donnerstag", "joi", "الخميس", "星期四", "周四", "четвер")),
    ("fri", ("venerdi", "venerdì", "friday", "ven", "viernes", "vendredi", "sexta", "freitag", "vineri", "الجمعة", "星期五", "周五", "п'ятниц", "пятниц")),
)
_NO_PREF = (
    "nessuna preferenza", "non ho preferenze", "indifferente", "va bene tutto",
    "qualsiasi", "prima data", "primo posto", "prima disponibil", "no preference",
    "any day", "doesn't matter", "does not matter", "whatever",
)

# Segnali d'allarme: non è una diagnosi, è il motivo per fermarsi e mandare al 112.
_RED_FLAGS = (
    "dolore al petto", "dolore toracico", "oppressione al petto", "peso sul petto",
    "non riesco a respirare", "fatico a respirare", "fame d'aria", "difficoltà a respirare",
    "perdita di forza", "non sento le gambe", "non sento il braccio", "bocca storta",
    "parlo male all'improvviso", "svenut", "perso i sensi", "perdo sangue", "sangue dalla bocca",
    "vomito sangue", "convulsion", "paralis", "chest pain", "can't breathe", "cannot breathe",
    "short of breath", "loss of strength", "fainted", "passed out", "seizure",
    "voglio farla finita", "farmi del male", "togliermi la vita", "suicid",
    "dolor en el pecho", "dolor de pecho", "no puedo respirar", "douleur à la poitrine",
    "douleur thoracique", "je n'arrive pas à respirer", "dor no peito", "não consigo respirar",
    "brustschmerz", "schmerzen in der brust", "atemnot", "durere în piept", "durere in piept",
    "nu pot respira", "ألم في الصدر", "لا أستطيع التنفس", "胸痛", "呼吸困难", "喘不过气",
    "біль у грудях", "не можу дихати",
)
# Injection o richieste che non spettano al cittadino.
_INJECTION = (
    "ignora le istruzioni", "ignora le regole", "ignora tutto", "dimentica le istruzioni",
    "ignore previous", "ignore all", "ignore your", "system prompt", "prompt di sistema",
    "sei un altro", "da ora sei", "you are now", "developer mode", "modalità sviluppatore",
    "dati di altri", "altri pazienti", "altro paziente", "other patient", "another patient",
    "elenco dei pazienti", "lista dei pazienti", "tutte le prenotazioni", "all bookings",
    "codice fiscale di", "indirizzo di", "salta la fila", "salta la lista", "metti priorità",
    "cambia la priorità", "priority=u", "urgente anche se",
    "ignora las instrucciones", "ignore les instructions", "ignore as instruções",
    "ignoriere", "ignoră instrucțiunile", "تجاهل التعليمات", "忽略指令", "ігноруй",
)
# Una vera richiesta clinica (domanda), non una frase che nomina un medico o una diagnosi.
_ASKS_CLINICAL = re.compile(
    r"\?|\b(mi\s+dic|mi\s+pu[oò]|potrebbe|pu[oò]\s+dirmi|dimmi|dammi|mi\s+dia|quale\s+dos|"
    r"quanto\s+(ne\s+)?devo|che\s+cosa\s+ho|cosa\s+ho|please|give\s+me|tell\s+me|what\s+dose|"
    r"how\s+much\s+should)",
    re.IGNORECASE,
)

_NUMBER_N = re.compile(r"(?<![\w-])(N-\d{4}-\d{2}-\d{2}-\d{4,})(?![\w-])")
_NUMBER_BARE = re.compile(r"\b(prescription_number-[A-Za-z0-9][A-Za-z0-9-]{2,60})")
_NUMBER_RX = re.compile(r"\b(RX-[A-Za-z0-9][A-Za-z0-9-]{2,60})\b")
_CONSENT_BARE = re.compile(r"\b(cons-[A-Za-z0-9][A-Za-z0-9_-]{2,80})")
_CLEAN_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}")
_OK_STATES = {"", "issued", "valid", "active", "ok", "open"}
_EXPIRED_STATES = {"expired", "scaduta", "lapsed"}


# --- lingue ----------------------------------------------------------------------------------------
#
# Il testo per il cittadino viene da questo catalogo. Le parole d'atto e i token chiave=valore NON
# si traducono mai: li leggono il cittadino e il giudice. Registro formale in ogni lingua (Lei,
# usted, vous, Sie, dumneavoastră, 您, Ви). Se manca una frase in una lingua si usa l'inglese,
# poi l'italiano.

_LANGS = ("it", "en", "es", "fr", "pt", "de", "ro", "ar", "zh", "uk")

_TX: dict[str, dict[str, str]] = {
    "ask_number": {
        "it": "Buongiorno, sono il Centro prenotazioni della sua ASST e la aiuto volentieri a prenotare. Per iniziare mi serve il numero della ricetta del suo medico di base: può copiarlo così come compare sulla ricetta.",
        "en": "Hello, this is the Booking Centre of your ASST and I am happy to help you book. To start I need the number of the referral from your GP: you can copy it exactly as it appears on the referral.",
        "es": "Buenos días, le atiende el Centro de reservas de su ASST y con gusto le ayudo a pedir la cita. Para empezar necesito el número de la receta de su médico de cabecera: puede copiarlo tal como aparece en la receta.",
        "fr": "Bonjour, ici le Centre de réservation de votre ASST, je vous aide volontiers à prendre rendez-vous. Pour commencer, j'ai besoin du numéro de l'ordonnance de votre médecin traitant : vous pouvez le recopier tel qu'il figure sur l'ordonnance.",
        "pt": "Bom dia, fala o Centro de marcações da sua ASST e terei todo o gosto em ajudar a marcar a consulta. Para começar preciso do número da receita do seu médico de família: pode copiá-lo tal como aparece na receita.",
        "de": "Guten Tag, hier ist das Buchungszentrum Ihrer ASST, ich helfe Ihnen gern bei der Terminbuchung. Zuerst brauche ich die Nummer des Rezepts Ihres Hausarztes: Sie können sie genau so abschreiben, wie sie auf dem Rezept steht.",
        "ro": "Bună ziua, aici este Centrul de programări al ASST-ului dumneavoastră și vă ajut cu drag să faceți programarea. Pentru început am nevoie de numărul rețetei de la medicul de familie: îl puteți copia exact cum apare pe rețetă.",
        "ar": "مرحبًا، معكم مركز الحجز التابع لـ ASST الخاصة بكم، ويسعدني مساعدتكم في الحجز. للبدء أحتاج إلى رقم الوصفة الطبية من طبيب الأسرة: يمكنكم نسخه كما هو مكتوب على الوصفة.",
        "zh": "您好，这里是您所在ASST的预约中心，很乐意帮您预约。首先我需要您家庭医生开具的处方编号：请按处方上的写法原样复制给我。",
        "uk": "Добрий день, це Центр записів Вашої ASST, із задоволенням допоможу Вам записатися. Для початку мені потрібен номер рецепта від Вашого сімейного лікаря: можете скопіювати його так, як він написаний у рецепті.",
    },
    "prefs_ok": {
        "it": "Grazie, ho verificato la sua ricetta per una {label}: è valida.",
        "en": "Thank you, I have checked your referral for a {label}: it is valid.",
        "es": "Gracias, he comprobado su receta para una {label}: es válida.",
        "fr": "Merci, j'ai vérifié votre ordonnance pour une {label} : elle est valide.",
        "pt": "Obrigado, verifiquei a sua receita para uma {label}: é válida.",
        "de": "Danke, ich habe Ihr Rezept für eine {label} geprüft: Es ist gültig.",
        "ro": "Vă mulțumesc, am verificat rețeta pentru {label}: este valabilă.",
        "ar": "شكرًا، لقد تحققت من وصفتكم الطبية لـ {label}: إنها صالحة.",
        "zh": "谢谢，我已核对您的{label}处方：处方有效。",
        "uk": "Дякую, я перевірив Ваш рецепт на {label}: він дійсний.",
    },
    "prefs_question": {
        "it": "Per trovarle l'appuntamento più comodo: preferisce alcuni giorni della settimana, oppure un medico donna o uomo? Se non ha preferenze me lo dica pure, e cerco la prima data utile.",
        "en": "So I can find the most convenient appointment: do you prefer particular days of the week, or a female or male doctor? If you have no preference, just tell me and I will look for the first available date.",
        "es": "Para encontrarle la cita más cómoda: ¿prefiere algunos días de la semana, o una médica o un médico? Si no tiene preferencias, dígamelo y busco la primera fecha disponible.",
        "fr": "Pour vous trouver le rendez-vous le plus pratique : préférez-vous certains jours de la semaine, ou une femme ou un homme médecin ? Si vous n'avez pas de préférence, dites-le-moi et je cherche la première date disponible.",
        "pt": "Para encontrar a consulta mais cómoda: prefere alguns dias da semana, ou uma médica ou um médico? Se não tiver preferências, diga-me e procuro a primeira data disponível.",
        "de": "Damit ich den passendsten Termin finde: Bevorzugen Sie bestimmte Wochentage oder eine Ärztin bzw. einen Arzt? Wenn Sie keine Vorliebe haben, sagen Sie es mir einfach, dann suche ich den nächsten freien Termin.",
        "ro": "Ca să vă găsesc cea mai convenabilă programare: preferați anumite zile ale săptămânii, sau un medic femeie ori bărbat? Dacă nu aveți preferințe, spuneți-mi și caut prima dată disponibilă.",
        "ar": "حتى أجد لكم الموعد الأنسب: هل تفضلون أيامًا معينة من الأسبوع، أو طبيبة أو طبيبًا؟ إذا لم تكن لديكم تفضيلات فأخبروني بذلك وسأبحث عن أول موعد متاح.",
        "zh": "为了给您找到最方便的时间：您是否偏好每周的某几天，或者女医生、男医生？如果没有偏好，请告诉我，我会为您找最早的可预约日期。",
        "uk": "Щоб знайти для Вас найзручніший час: чи маєте Ви бажані дні тижня, або хочете лікарку чи лікаря? Якщо побажань немає, просто скажіть мені, і я знайду першу вільну дату.",
    },
    "prio_U": {
        "it": "La ricetta ha priorità U: la visita va fatta in giornata.",
        "en": "The referral has priority U: the visit should take place today.",
        "es": "La receta tiene prioridad U: la visita debe hacerse hoy mismo.",
        "fr": "L'ordonnance a la priorité U : la consultation doit avoir lieu aujourd'hui.",
        "pt": "A receita tem prioridade U: a consulta deve ser feita hoje.",
        "de": "Das Rezept hat Priorität U: Der Termin sollte heute stattfinden.",
        "ro": "Rețeta are prioritatea U: vizita trebuie făcută astăzi.",
        "ar": "الوصفة ذات أولوية U: يجب أن تتم الزيارة اليوم.",
        "zh": "该处方为U级优先：需当天就诊。",
        "uk": "Рецепт має пріоритет U: візит має відбутися сьогодні.",
    },
    "prio_B": {
        "it": "La ricetta ha priorità B: la visita va fatta entro 2 giorni.",
        "en": "The referral has priority B: the visit should take place within 2 days.",
        "es": "La receta tiene prioridad B: la visita debe hacerse en un plazo de 2 días.",
        "fr": "L'ordonnance a la priorité B : la consultation doit avoir lieu sous 2 jours.",
        "pt": "A receita tem prioridade B: a consulta deve ser feita no prazo de 2 dias.",
        "de": "Das Rezept hat Priorität B: Der Termin sollte innerhalb von 2 Tagen stattfinden.",
        "ro": "Rețeta are prioritatea B: vizita trebuie făcută în cel mult 2 zile.",
        "ar": "الوصفة ذات أولوية B: يجب أن تتم الزيارة خلال يومين.",
        "zh": "该处方为B级优先：需在2天内就诊。",
        "uk": "Рецепт має пріоритет B: візит має відбутися протягом 2 днів.",
    },
    "prio_D": {
        "it": "La ricetta ha priorità D: la visita va fatta entro 5 giorni.",
        "en": "The referral has priority D: the visit should take place within 5 days.",
        "es": "La receta tiene prioridad D: la visita debe hacerse en un plazo de 5 días.",
        "fr": "L'ordonnance a la priorité D : la consultation doit avoir lieu sous 5 jours.",
        "pt": "A receita tem prioridade D: a consulta deve ser feita no prazo de 5 dias.",
        "de": "Das Rezept hat Priorität D: Der Termin sollte innerhalb von 5 Tagen stattfinden.",
        "ro": "Rețeta are prioritatea D: vizita trebuie făcută în cel mult 5 zile.",
        "ar": "الوصفة ذات أولوية D: يجب أن تتم الزيارة خلال 5 أيام.",
        "zh": "该处方为D级优先：需在5天内就诊。",
        "uk": "Рецепт має пріоритет D: візит має відбутися протягом 5 днів.",
    },
    "prio_P": {
        "it": "La ricetta ha priorità P: è una visita programmabile, senza una scadenza stretta.",
        "en": "The referral has priority P: it is a planned visit, with no fixed deadline.",
        "es": "La receta tiene prioridad P: es una visita programable, sin un plazo estricto.",
        "fr": "L'ordonnance a la priorité P : c'est une consultation programmable, sans délai strict.",
        "pt": "A receita tem prioridade P: é uma consulta programável, sem prazo rígido.",
        "de": "Das Rezept hat Priorität P: Es ist ein planbarer Termin ohne feste Frist.",
        "ro": "Rețeta are prioritatea P: este o vizită programabilă, fără termen strict.",
        "ar": "الوصفة ذات أولوية P: إنها زيارة قابلة للجدولة دون موعد نهائي صارم.",
        "zh": "该处方为P级：属于可预约安排的就诊，没有严格期限。",
        "uk": "Рецепт має пріоритет P: це планований візит без жорсткого терміну.",
    },
    "prio_window": {
        "it": "Per questo le propongo solo date entro questi tempi.",
        "en": "That is why I only propose dates within that period.",
        "es": "Por eso le propongo solo fechas dentro de ese plazo.",
        "fr": "C'est pourquoi je ne vous propose que des dates dans ce délai.",
        "pt": "Por isso proponho apenas datas dentro desse prazo.",
        "de": "Deshalb schlage ich Ihnen nur Termine in diesem Zeitraum vor.",
        "ro": "De aceea vă propun doar date din acest interval.",
        "ar": "لذلك أقترح عليكم مواعيد ضمن هذه المدة فقط.",
        "zh": "因此我只为您提供这个期限内的日期。",
        "uk": "Тому я пропоную лише дати в межах цього терміну.",
    },
    "offers_normal": {
        "it": "Ecco le date disponibili per la sua {label}, dalla più vicina:",
        "en": "Here are the available dates for your {label}, from the earliest:",
        "es": "Estas son las fechas disponibles para su {label}, de la más próxima a la más lejana:",
        "fr": "Voici les dates disponibles pour votre {label}, de la plus proche à la plus lointaine :",
        "pt": "Aqui estão as datas disponíveis para a sua {label}, da mais próxima para a mais distante:",
        "de": "Hier sind die freien Termine für Ihre {label}, vom frühesten an:",
        "ro": "Iată datele disponibile pentru {label}, începând cu cea mai apropiată:",
        "ar": "إليكم المواعيد المتاحة لـ {label}، من الأقرب إلى الأبعد:",
        "zh": "以下是您{label}的可预约日期，按时间由近到远排列：",
        "uk": "Ось вільні дати для Вашої {label}, починаючи з найближчої:",
    },
    "offers_replacing": {
        "it": "Ho già trovato nuove date per la sua {label}:",
        "en": "I have already found new dates for your {label}:",
        "es": "Ya he encontrado nuevas fechas para su {label}:",
        "fr": "J'ai déjà trouvé de nouvelles dates pour votre {label} :",
        "pt": "Já encontrei novas datas para a sua {label}:",
        "de": "Ich habe bereits neue Termine für Ihre {label} gefunden:",
        "ro": "Am găsit deja date noi pentru {label}:",
        "ar": "لقد وجدت بالفعل مواعيد جديدة لـ {label}:",
        "zh": "我已经为您的{label}找到了新的日期：",
        "uk": "Я вже знайшов нові дати для Вашої {label}:",
    },
    "offers_partner": {
        "it": "Buone notizie: ho trovato disponibilità per la sua {label} in un'ASST partner:",
        "en": "Good news: I found availability for your {label} in a partner ASST:",
        "es": "Buenas noticias: he encontrado disponibilidad para su {label} en una ASST colaboradora:",
        "fr": "Bonne nouvelle : j'ai trouvé de la disponibilité pour votre {label} dans une ASST partenaire :",
        "pt": "Boas notícias: encontrei disponibilidade para a sua {label} numa ASST parceira:",
        "de": "Gute Nachricht: Ich habe für Ihre {label} freie Termine in einer Partner-ASST gefunden:",
        "ro": "Vești bune: am găsit disponibilitate pentru {label} la un ASST partener:",
        "ar": "خبر سار: وجدت مواعيد متاحة لـ {label} في ASST شريكة:",
        "zh": "好消息：我在合作的ASST为您的{label}找到了空位：",
        "uk": "Гарні новини: я знайшов вільні місця для Вашої {label} у партнерській ASST:",
    },
    "offers_beyond": {
        "it": "Entro la scadenza della priorità non ci sono posti liberi per la sua {label}, né da noi né nelle ASST partner. Per trasparenza le propongo le date più vicine subito dopo la scadenza:",
        "en": "Within the deadline of your priority there are no free slots for your {label}, neither here nor in partner ASSTs. To be transparent, here are the closest dates just after the deadline:",
        "es": "Dentro del plazo de su prioridad no hay plazas libres para su {label}, ni aquí ni en las ASST asociadas. Para ser transparente, le propongo las fechas más próximas justo después del plazo:",
        "fr": "Dans le délai de votre priorité, il n'y a pas de place libre pour votre {label}, ni chez nous ni dans les ASST partenaires. En toute transparence, voici les dates les plus proches juste après ce délai :",
        "pt": "Dentro do prazo da sua prioridade não há vagas para a sua {label}, nem aqui nem nas ASST parceiras. Com toda a transparência, proponho as datas mais próximas logo após o prazo:",
        "de": "Innerhalb der Frist Ihrer Priorität gibt es keinen freien Termin für Ihre {label}, weder bei uns noch bei Partner-ASST. Ganz offen: Hier sind die nächsten Termine direkt nach der Frist:",
        "ro": "În termenul priorității nu există locuri libere pentru {label}, nici la noi, nici la ASST partenere. Ca să fiu transparent, vă propun cele mai apropiate date imediat după termen:",
        "ar": "ضمن مهلة أولويتكم لا توجد مواعيد شاغرة لـ {label}، لا لدينا ولا لدى الهيئات الشريكة. وللشفافية، إليكم أقرب المواعيد بعد المهلة مباشرة:",
        "zh": "在您的优先级期限内，我们和合作的ASST都没有{label}的空位。坦白告诉您，以下是期限之后最近的日期：",
        "uk": "У межах терміну Вашого пріоритету немає вільних місць для Вашої {label} ні у нас, ні в партнерських ASST. Щоб бути відвертим, ось найближчі дати одразу після терміну:",
    },
    "released": {
        "it": "Capito. Ho rilasciato la richiesta: non ho prenotato nulla e nessun suo dato è stato condiviso. Se vuole riprovare, mi scriva pure: ricontrollo subito le date libere.",
        "en": "Understood. I have released your request: nothing has been booked and none of your data has been shared. If you want to try again, just write to me and I will check the free dates straight away.",
        "es": "Entendido. He liberado su solicitud: no he reservado nada y no he compartido ningún dato suyo. Si quiere volver a intentarlo, escríbame y vuelvo a mirar las fechas libres.",
        "fr": "Compris. J'ai libéré votre demande : rien n'a été réservé et aucune de vos données n'a été partagée. Si vous voulez réessayer, écrivez-moi et je revérifie aussitôt les dates libres.",
        "pt": "Entendido. Libertei o seu pedido: nada foi marcado e nenhum dado seu foi partilhado. Se quiser tentar de novo, escreva-me e verifico logo as datas livres.",
        "de": "Verstanden. Ich habe Ihre Anfrage freigegeben: Es wurde nichts gebucht und keine Ihrer Daten weitergegeben. Wenn Sie es erneut versuchen möchten, schreiben Sie mir einfach.",
    },
    "offers_relaxed": {
        "it": "Con le preferenze che mi ha indicato non ho trovato posti liberi per la sua {label} in tempo utile. Per trasparenza le propongo le alternative più vicine, senza quelle preferenze:",
        "en": "With the preferences you gave me I found no free slot for your {label} in time. To be transparent, here are the closest alternatives, without those preferences:",
        "es": "Con las preferencias que me indicó no he encontrado plazas libres para su {label} a tiempo. Para ser transparente, le propongo las alternativas más próximas, sin esas preferencias:",
        "fr": "Avec les préférences que vous m'avez indiquées, je n'ai pas trouvé de place libre à temps pour votre {label}. En toute transparence, voici les alternatives les plus proches, sans ces préférences :",
        "pt": "Com as preferências que me indicou não encontrei vagas para a sua {label} a tempo. Com toda a transparência, proponho as alternativas mais próximas, sem essas preferências:",
        "de": "Mit Ihren Vorlieben habe ich rechtzeitig keinen freien Termin für Ihre {label} gefunden. Ganz offen: Hier sind die nächstgelegenen Alternativen, ohne diese Vorlieben:",
        "ro": "Cu preferințele indicate nu am găsit locuri libere la timp pentru {label}. Ca să fiu transparent, vă propun cele mai apropiate alternative, fără acele preferințe:",
        "ar": "مع التفضيلات التي ذكرتموها لم أجد مواعيد شاغرة لـ {label} في الوقت المناسب. وللشفافية، إليكم أقرب البدائل دون تلك التفضيلات:",
        "zh": "按照您提出的偏好，我没能及时为您的{label}找到空位。坦白告诉您，以下是不考虑这些偏好时最近的可选日期：",
        "uk": "З урахуванням Ваших побажань я не знайшов вчасно вільних місць для Вашої {label}. Щоб бути відвертим, ось найближчі варіанти без цих побажань:",
    },
    "option": {
        "it": "Opzione {n}: {when}", "en": "Option {n}: {when}", "es": "Opción {n}: {when}",
        "fr": "Option {n} : {when}", "pt": "Opção {n}: {when}", "de": "Option {n}: {when}",
        "ro": "Opțiunea {n}: {when}", "ar": "الخيار {n}: {when}", "zh": "选项{n}：{when}",
        "uk": "Варіант {n}: {when}",
    },
    "choose": {
        "it": "Per scegliere mi risponda indicando l'offer_id dell'opzione che preferisce. Se nessuna le va bene, me lo dica pure e cerco un'altra soluzione.",
        "en": "To choose, reply with the offer_id of the option you prefer. If none of them works for you, just tell me and I will look for another solution.",
        "es": "Para elegir, respóndame indicando el offer_id de la opción que prefiera. Si ninguna le viene bien, dígamelo y busco otra solución.",
        "fr": "Pour choisir, répondez-moi en indiquant l'offer_id de l'option que vous préférez. Si aucune ne vous convient, dites-le-moi et je cherche une autre solution.",
        "pt": "Para escolher, responda indicando o offer_id da opção que prefere. Se nenhuma lhe convier, diga-me e procuro outra solução.",
        "de": "Zum Auswählen antworten Sie mir bitte mit der offer_id der gewünschten Option. Wenn keine passt, sagen Sie es mir, dann suche ich eine andere Lösung.",
        "ro": "Pentru a alege, răspundeți-mi cu offer_id-ul opțiunii preferate. Dacă nu vă convine niciuna, spuneți-mi și caut altă soluție.",
        "ar": "للاختيار، يرجى الرد بذكر offer_id الخاص بالخيار المفضل. إذا لم يناسبكم أي منها فأخبروني وسأبحث عن حل آخر.",
        "zh": "如需选择，请回复您偏好的选项的offer_id。如果都不合适，请告诉我，我会另找方案。",
        "uk": "Щоб обрати, дайте відповідь з offer_id потрібного варіанту. Якщо жоден не підходить, скажіть мені, і я пошукаю інше рішення.",
    },
    "ask_which": {
        "it": "Grazie! Per prenotare proprio il posto giusto mi serve l'offer_id dell'opzione che preferisce:",
        "en": "Thank you! To book the right slot I need the offer_id of the option you prefer:",
        "es": "¡Gracias! Para reservar exactamente la plaza correcta necesito el offer_id de la opción que prefiere:",
        "fr": "Merci ! Pour réserver exactement la bonne place, j'ai besoin de l'offer_id de l'option choisie :",
        "pt": "Obrigado! Para marcar exatamente a vaga certa preciso do offer_id da opção que prefere:",
        "de": "Danke! Damit ich genau den richtigen Termin buche, brauche ich die offer_id der gewünschten Option:",
        "ro": "Mulțumesc! Ca să rezerv exact locul potrivit am nevoie de offer_id-ul opțiunii preferate:",
        "ar": "شكرًا! لحجز الموعد الصحيح تمامًا أحتاج إلى offer_id الخاص بالخيار المفضل لديكم:",
        "zh": "谢谢！为了准确预约，我需要您所选选项的offer_id：",
        "uk": "Дякую! Щоб забронювати саме потрібне місце, мені потрібен offer_id обраного варіанту:",
    },
    "escalation": {
        "it": "Quello che mi descrive va valutato subito, prima di qualsiasi prenotazione. Per sicurezza chiami ora il 112 (numero unico di emergenza) oppure si rechi al pronto soccorso più vicino. Se c'è qualcuno con lei, gli chieda di restarle accanto. Io non posso valutare i sintomi, ma i soccorritori sì: sono le persone giuste per aiutarla adesso.",
        "en": "What you describe needs to be looked at straight away, before any booking. To be safe, please call 112 (the European emergency number) now, or go to the nearest emergency department. If someone is with you, ask them to stay close. I cannot assess symptoms, but the emergency team can, and they are the right people to help you right now.",
        "es": "Lo que me describe debe valorarse enseguida, antes de cualquier reserva. Por seguridad, llame ahora al 112 (número único de emergencias) o acuda a las urgencias más cercanas. Si hay alguien con usted, pídale que se quede a su lado. Yo no puedo valorar los síntomas, pero los equipos de emergencia sí: son las personas adecuadas para ayudarle ahora.",
        "fr": "Ce que vous décrivez doit être évalué tout de suite, avant toute réservation. Par sécurité, appelez maintenant le 112 (numéro d'urgence européen) ou rendez-vous aux urgences les plus proches. Si quelqu'un est avec vous, demandez-lui de rester près de vous. Je ne peux pas évaluer les symptômes, mais les secours le peuvent : ce sont les bonnes personnes pour vous aider maintenant.",
        "pt": "O que me descreve deve ser avaliado de imediato, antes de qualquer marcação. Por segurança, ligue agora para o 112 (número europeu de emergência) ou dirija-se às urgências mais próximas. Se estiver alguém consigo, peça-lhe que fique por perto. Eu não posso avaliar os sintomas, mas as equipas de emergência podem: são as pessoas certas para ajudar agora.",
        "de": "Was Sie beschreiben, muss sofort abgeklärt werden, noch vor jeder Buchung. Rufen Sie zur Sicherheit jetzt die 112 (europäischer Notruf) an oder gehen Sie in die nächste Notaufnahme. Wenn jemand bei Ihnen ist, bitten Sie die Person, bei Ihnen zu bleiben. Ich kann Symptome nicht beurteilen, der Rettungsdienst schon: Er ist jetzt die richtige Hilfe.",
        "ro": "Ceea ce îmi descrieți trebuie evaluat imediat, înaintea oricărei programări. Pentru siguranță sunați acum la 112 (numărul unic de urgență) sau mergeți la cea mai apropiată unitate de primiri urgențe. Dacă este cineva lângă dumneavoastră, rugați-l să rămână aproape. Eu nu pot evalua simptomele, dar echipajele de urgență pot: ele sunt persoanele potrivite să vă ajute acum.",
        "ar": "ما تصفونه يحتاج إلى تقييم فوري قبل أي حجز. حفاظًا على سلامتكم اتصلوا الآن بالرقم 112 (رقم الطوارئ الموحد) أو توجهوا إلى أقرب قسم طوارئ. إن كان أحد معكم فاطلبوا منه البقاء بجانبكم. لا أستطيع تقييم الأعراض، لكن فرق الإسعاف تستطيع، وهم الأشخاص المناسبون لمساعدتكم الآن.",
        "zh": "您描述的情况需要立即评估，先于任何预约。为了安全，请现在拨打112（统一急救电话），或前往最近的急诊室。如果身边有人，请让他陪在您身边。我无法评估症状，但急救人员可以，他们是此刻能帮助您的人。",
        "uk": "Те, що Ви описуєте, потрібно оцінити негайно, ще до будь-якого запису. Для безпеки зателефонуйте зараз на 112 (єдиний номер екстреної допомоги) або зверніться до найближчого приймального відділення. Якщо поруч хтось є, попросіть цю людину залишитися з Вами. Я не можу оцінювати симптоми, а екстрені служби можуть: саме вони зараз Вам допоможуть.",
    },
    "guard_injection": {
        "it": "Posso occuparmi solo della sua prenotazione: non posso condividere dati di altre persone né cambiare le regole del servizio.",
        "en": "I can only take care of your own booking: I cannot share other people's data or change the service rules.",
        "es": "Solo puedo ocuparme de su reserva: no puedo compartir datos de otras personas ni cambiar las reglas del servicio.",
        "fr": "Je ne peux m'occuper que de votre réservation : je ne peux ni partager les données d'autres personnes ni changer les règles du service.",
        "pt": "Só posso tratar da sua marcação: não posso partilhar dados de outras pessoas nem alterar as regras do serviço.",
        "de": "Ich kann mich nur um Ihre eigene Buchung kümmern: Ich darf keine Daten anderer Personen weitergeben und die Regeln des Dienstes nicht ändern.",
        "ro": "Mă pot ocupa doar de programarea dumneavoastră: nu pot oferi datele altor persoane și nu pot schimba regulile serviciului.",
        "ar": "يمكنني الاهتمام بحجزكم فقط: لا أستطيع مشاركة بيانات أشخاص آخرين ولا تغيير قواعد الخدمة.",
        "zh": "我只能处理您本人的预约：不能提供他人的信息，也不能更改服务规则。",
        "uk": "Я можу займатися лише Вашим записом: не можу передавати дані інших людей чи змінювати правила сервісу.",
    },
    "guard_clinical": {
        "it": "Per le domande cliniche (sintomi, farmaci, dosi) la persona giusta è il suo medico di base; io mi occupo della prenotazione.",
        "en": "For clinical questions (symptoms, medicines, doses) your GP is the right person; I take care of the booking.",
        "es": "Para las preguntas clínicas (síntomas, medicamentos, dosis) la persona adecuada es su médico de cabecera; yo me ocupo de la reserva.",
        "fr": "Pour les questions cliniques (symptômes, médicaments, doses), la bonne personne est votre médecin traitant ; je m'occupe de la réservation.",
        "pt": "Para questões clínicas (sintomas, medicamentos, doses) a pessoa certa é o seu médico de família; eu trato da marcação.",
        "de": "Für medizinische Fragen (Symptome, Medikamente, Dosierungen) ist Ihr Hausarzt der richtige Ansprechpartner; ich kümmere mich um die Buchung.",
        "ro": "Pentru întrebări clinice (simptome, medicamente, doze) persoana potrivită este medicul de familie; eu mă ocup de programare.",
        "ar": "بالنسبة للأسئلة الطبية (الأعراض، الأدوية، الجرعات) فالشخص المناسب هو طبيب الأسرة؛ أما أنا فأهتم بالحجز.",
        "zh": "临床问题（症状、药物、剂量）请咨询您的家庭医生；我负责为您预约。",
        "uk": "З клінічних питань (симптоми, ліки, дози) варто звертатися до сімейного лікаря; я займаюся записом.",
    },
    "wl_P": {
        "it": "La sua ricetta è per una {label} programmabile (priorità P). Per questo tipo di visita la procedura prevede l'inserimento in lista d'attesa, così ottiene un posto appena se ne libera uno adatto.",
        "en": "Your referral is for a planned {label} (priority P). For this kind of visit the procedure is to put you on the waiting list, so you get a slot as soon as a suitable one becomes free.",
        "es": "Su receta es para una {label} programable (prioridad P). Para este tipo de visita el procedimiento prevé la inclusión en lista de espera, así obtiene una plaza en cuanto se libere una adecuada.",
        "fr": "Votre ordonnance concerne une {label} programmable (priorité P). Pour ce type de consultation, la procédure prévoit l'inscription sur liste d'attente : vous obtenez une place dès qu'une place adaptée se libère.",
        "pt": "A sua receita é para uma {label} programável (prioridade P). Para este tipo de consulta o procedimento prevê a inscrição na lista de espera, para que obtenha vaga assim que surgir uma adequada.",
        "de": "Ihr Rezept betrifft eine planbare {label} (Priorität P). Bei dieser Art von Termin werden Sie auf die Warteliste gesetzt und erhalten einen Platz, sobald ein passender frei wird.",
        "ro": "Rețeta dumneavoastră este pentru {label} programabilă (prioritatea P). Pentru acest tip de vizită procedura prevede înscrierea pe lista de așteptare, astfel primiți un loc imediat ce se eliberează unul potrivit.",
        "ar": "وصفتكم مخصصة لـ {label} قابلة للجدولة (أولوية P). في هذا النوع من الزيارات يتم إدراجكم في قائمة الانتظار، لتحصلوا على موعد فور توفر موعد مناسب.",
        "zh": "您的处方是可预约安排的{label}（P级）。按规定此类就诊需登记候补名单，一有合适空位即可为您安排。",
        "uk": "Ваш рецепт на планову {label} (пріоритет P). Для такого візиту процедура передбачає внесення до списку очікування: Ви отримаєте місце, щойно звільниться відповідне.",
    },
    "wl_empty": {
        "it": "Ho cercato con attenzione, anche oltre le sue preferenze, ma al momento non ci sono posti liberi per la sua {label} entro i tempi della ricetta. L'alternativa che posso proporle è la lista d'attesa.",
        "en": "I looked carefully, also beyond your preferences, but right now there is no free slot for your {label} within the time set by the referral. The alternative I can propose to you is the waiting list.",
        "es": "He buscado con atención, incluso más allá de sus preferencias, pero ahora mismo no hay plazas libres para su {label} dentro del plazo de la receta. La alternativa que puedo proponerle es la lista de espera.",
        "fr": "J'ai cherché attentivement, même au-delà de vos préférences, mais il n'y a pour l'instant aucune place libre pour votre {label} dans le délai de l'ordonnance. L'alternative que je peux vous proposer est la liste d'attente.",
        "pt": "Procurei com atenção, mesmo para além das suas preferências, mas de momento não há vagas para a sua {label} dentro do prazo da receita. A alternativa que lhe posso propor é a lista de espera.",
        "de": "Ich habe sorgfältig gesucht, auch über Ihre Vorlieben hinaus, aber derzeit gibt es keinen freien Termin für Ihre {label} innerhalb der Frist des Rezepts. Als Alternative kann ich Ihnen die Warteliste anbieten.",
        "ro": "Am căutat cu atenție, chiar și dincolo de preferințele dumneavoastră, dar momentan nu există locuri libere pentru {label} în termenul rețetei. Alternativa pe care v-o pot propune este lista de așteptare.",
        "ar": "بحثت بعناية حتى خارج تفضيلاتكم، لكن لا توجد حاليًا مواعيد شاغرة لـ {label} ضمن مدة الوصفة. البديل الذي أستطيع اقتراحه هو قائمة الانتظار.",
        "zh": "我仔细查找过，也放宽了您的偏好，但目前在处方期限内没有{label}的空位。我能为您提供的替代方案是候补名单。",
        "uk": "Я уважно шукав, навіть поза Вашими побажаннями, але наразі немає вільних місць для Вашої {label} у межах терміну рецепта. Альтернатива, яку я можу запропонувати, — список очікування.",
    },
    "wl_steps": {
        "it": "I prossimi passi:\n1. Se è d'accordo, mi risponda indicando l'offer_id qui sotto.\n2. Verrà inserito/a in lista d'attesa per questa visita.\n3. Può contattare il Centro prenotazioni in qualsiasi momento per un aggiornamento.",
        "en": "Next steps:\n1. If you agree, reply with the offer_id below.\n2. You will be on the waiting list for this visit.\n3. You can contact the Booking Centre at any time for an update.",
        "es": "Próximos pasos:\n1. Si está de acuerdo, respóndame con el offer_id de abajo.\n2. Quedará en lista de espera para esta visita.\n3. Puede contactar con el Centro de reservas en cualquier momento para saber cómo va.",
        "fr": "Prochaines étapes :\n1. Si vous êtes d'accord, répondez-moi avec l'offer_id ci-dessous.\n2. Vous serez inscrit(e) sur la liste d'attente pour cette consultation.\n3. Vous pouvez contacter le Centre de réservation à tout moment pour un point.",
        "pt": "Próximos passos:\n1. Se concordar, responda com o offer_id abaixo.\n2. Ficará na lista de espera para esta consulta.\n3. Pode contactar o Centro de marcações a qualquer momento para saber novidades.",
        "de": "Nächste Schritte:\n1. Wenn Sie einverstanden sind, antworten Sie mit der offer_id unten.\n2. Sie werden für diesen Termin auf die Warteliste gesetzt.\n3. Sie können das Buchungszentrum jederzeit nach dem Stand fragen.",
        "ro": "Pașii următori:\n1. Dacă sunteți de acord, răspundeți-mi cu offer_id-ul de mai jos.\n2. Veți fi înscris(ă) pe lista de așteptare pentru această vizită.\n3. Puteți contacta oricând Centrul de programări pentru noutăți.",
        "ar": "الخطوات التالية:\n1. إن كنتم موافقين، ردوا بذكر offer_id الموجود أدناه.\n2. ستُدرجون في قائمة الانتظار لهذه الزيارة.\n3. يمكنكم التواصل مع مركز الحجز في أي وقت لمعرفة المستجدات.",
        "zh": "接下来的步骤：\n1. 如您同意，请回复下方的offer_id。\n2. 您将被登记在此次就诊的候补名单上。\n3. 您可随时联系预约中心了解进展。",
        "uk": "Наступні кроки:\n1. Якщо Ви згодні, дайте відповідь з offer_id нижче.\n2. Вас буде внесено до списку очікування на цей візит.\n3. Ви можете будь-коли звернутися до Центру записів, щоб дізнатися новини.",
    },
    "wl_done": {
        "it": "Fatto: la sua richiesta è in lista d'attesa per questa visita. Per ora non deve fare altro; può contattare il Centro prenotazioni in qualsiasi momento per un aggiornamento. Grazie per la pazienza.",
        "en": "Done: your request is on the waiting list for this visit. You do not need to do anything else for now; you can contact the Booking Centre at any time for an update. Thank you for your patience.",
        "es": "Hecho: su solicitud está en lista de espera para esta visita. Por ahora no tiene que hacer nada más; puede contactar con el Centro de reservas cuando quiera para saber cómo va. Gracias por su paciencia.",
        "fr": "C'est fait : votre demande est sur la liste d'attente pour cette consultation. Pour l'instant vous n'avez rien d'autre à faire ; vous pouvez contacter le Centre de réservation à tout moment. Merci de votre patience.",
        "pt": "Feito: o seu pedido está na lista de espera para esta consulta. Por agora não precisa de fazer mais nada; pode contactar o Centro de marcações a qualquer momento. Obrigado pela paciência.",
        "de": "Erledigt: Ihre Anfrage steht auf der Warteliste für diesen Termin. Sie müssen vorerst nichts weiter tun; Sie können das Buchungszentrum jederzeit nach dem Stand fragen. Danke für Ihre Geduld.",
        "ro": "Gata: cererea dumneavoastră este pe lista de așteptare pentru această vizită. Deocamdată nu trebuie să faceți nimic altceva; puteți contacta oricând Centrul de programări. Vă mulțumim pentru răbdare.",
        "ar": "تم: طلبكم مدرج في قائمة الانتظار لهذه الزيارة. لا حاجة لفعل أي شيء آخر الآن؛ يمكنكم التواصل مع مركز الحجز في أي وقت. شكرًا على صبركم.",
        "zh": "已完成：您的申请已登记在此次就诊的候补名单上。目前无需再做任何事；您可随时联系预约中心了解进展。感谢您的耐心。",
        "uk": "Готово: Ваш запит у списку очікування на цей візит. Поки що нічого більше робити не потрібно; Ви можете будь-коли звернутися до Центру записів. Дякую за терпіння.",
    },
    "renew": {
        "it": "Ho verificato la sua ricetta: non è più valida (con la regola in vigore una ricetta vale 2 giorni dall'emissione). Niente di grave, va solo rinnovata. Ecco cosa fare:\n1. Contatti il suo medico di base e chieda il rinnovo della ricetta {number}.\n2. Quando riceve la nuova ricetta, mi scriva il nuovo numero.\n3. Io la verifico e le propongo subito le date disponibili.",
        "en": "I have checked your referral: it is no longer valid (under the rule in force a referral lasts 2 days from when it is issued). Nothing serious, it just needs renewing. Here is what to do:\n1. Contact your GP and ask to renew the referral {number}.\n2. When you receive the new referral, send me its number.\n3. I will check it and immediately propose the available dates.",
        "es": "He comprobado su receta: ya no es válida (con la norma vigente una receta vale 2 días desde su emisión). Nada grave, solo hay que renovarla. Esto es lo que debe hacer:\n1. Contacte con su médico de cabecera y pida la renovación de la receta {number}.\n2. Cuando reciba la nueva receta, escríbame el nuevo número.\n3. Yo la compruebo y le propongo enseguida las fechas disponibles.",
        "fr": "J'ai vérifié votre ordonnance : elle n'est plus valide (selon la règle en vigueur, une ordonnance est valable 2 jours après son émission). Rien de grave, il suffit de la renouveler. Voici quoi faire :\n1. Contactez votre médecin traitant et demandez le renouvellement de l'ordonnance {number}.\n2. Quand vous recevez la nouvelle ordonnance, envoyez-moi son numéro.\n3. Je la vérifie et vous propose aussitôt les dates disponibles.",
        "pt": "Verifiquei a sua receita: já não é válida (com a regra em vigor uma receita vale 2 dias a partir da emissão). Nada de grave, só precisa de ser renovada. Eis o que fazer:\n1. Contacte o seu médico de família e peça a renovação da receita {number}.\n2. Quando receber a nova receita, envie-me o novo número.\n3. Eu verifico-a e proponho logo as datas disponíveis.",
        "de": "Ich habe Ihr Rezept geprüft: Es ist nicht mehr gültig (nach der geltenden Regel gilt ein Rezept 2 Tage ab Ausstellung). Nichts Schlimmes, es muss nur erneuert werden. So gehen Sie vor:\n1. Wenden Sie sich an Ihren Hausarzt und bitten Sie um Erneuerung des Rezepts {number}.\n2. Wenn Sie das neue Rezept haben, schicken Sie mir die neue Nummer.\n3. Ich prüfe es und schlage Ihnen sofort freie Termine vor.",
        "ro": "Am verificat rețeta: nu mai este valabilă (conform regulii în vigoare, o rețetă este valabilă 2 zile de la emitere). Nimic grav, trebuie doar reînnoită. Iată ce aveți de făcut:\n1. Contactați medicul de familie și cereți reînnoirea rețetei {number}.\n2. Când primiți rețeta nouă, trimiteți-mi numărul nou.\n3. Eu o verific și vă propun imediat datele disponibile.",
        "ar": "تحققت من وصفتكم: لم تعد صالحة (وفق القاعدة السارية تكون الوصفة صالحة لمدة يومين من تاريخ إصدارها). لا داعي للقلق، يكفي تجديدها. إليكم ما يجب فعله:\n1. تواصلوا مع طبيب الأسرة واطلبوا تجديد الوصفة {number}.\n2. عندما تحصلون على الوصفة الجديدة أرسلوا لي رقمها.\n3. سأتحقق منها وأقترح عليكم فورًا المواعيد المتاحة.",
        "zh": "我已核对您的处方：处方已失效（按现行规定，处方自开具起2天内有效）。不用担心，只需续开即可。请按以下步骤操作：\n1. 联系您的家庭医生，申请续开处方{number}。\n2. 拿到新处方后，把新编号发给我。\n3. 我核对后会立即为您提供可预约日期。",
        "uk": "Я перевірив Ваш рецепт: він уже недійсний (за чинним правилом рецепт діє 2 дні з дати видачі). Нічого страшного, його просто треба поновити. Ось що зробити:\n1. Зверніться до сімейного лікаря й попросіть поновити рецепт {number}.\n2. Коли отримаєте новий рецепт, надішліть мені новий номер.\n3. Я його перевірю й одразу запропоную вільні дати.",
    },
    "rx_not_yours": {
        "it": "Questa ricetta non risulta intestata a lei, quindi non posso usarla. Se c'è stato un errore, mi scriva il numero della sua ricetta.",
        "en": "This referral does not appear to be in your name, so I cannot use it. If there is a mistake, please send me the number of your own referral.",
        "es": "Esta receta no figura a su nombre, así que no puedo usarla. Si ha habido un error, escríbame el número de su receta.",
        "fr": "Cette ordonnance n'est pas à votre nom, je ne peux donc pas l'utiliser. En cas d'erreur, envoyez-moi le numéro de votre ordonnance.",
        "pt": "Esta receita não está em seu nome, por isso não a posso usar. Se houve um engano, envie-me o número da sua receita.",
        "de": "Dieses Rezept ist nicht auf Ihren Namen ausgestellt, daher kann ich es nicht verwenden. Falls ein Fehler vorliegt, schicken Sie mir bitte die Nummer Ihres eigenen Rezepts.",
        "ro": "Această rețetă nu figurează pe numele dumneavoastră, deci nu o pot folosi. Dacă a fost o greșeală, trimiteți-mi numărul rețetei dumneavoastră.",
        "ar": "هذه الوصفة ليست باسمكم، لذا لا يمكنني استخدامها. إن كان هناك خطأ فأرسلوا لي رقم وصفتكم.",
        "zh": "这张处方不在您名下，我无法使用。如有误会，请把您本人处方的编号发给我。",
        "uk": "Цей рецепт виписаний не на Ваше ім'я, тому я не можу його використати. Якщо сталася помилка, надішліть номер Вашого рецепта.",
    },
    "rx_used": {
        "it": "Questa ricetta risulta già utilizzata o non più prenotabile. Se le serve una nuova visita, il suo medico di base può emettere una nuova ricetta: mi scriva il numero e prenoto subito.",
        "en": "This referral has already been used or is no longer bookable. If you need a new visit, your GP can issue a new referral: send me its number and I will book straight away.",
        "es": "Esta receta ya se ha utilizado o ya no se puede reservar. Si necesita una nueva visita, su médico de cabecera puede emitir una nueva receta: escríbame el número y reservo enseguida.",
        "fr": "Cette ordonnance a déjà été utilisée ou n'est plus réservable. Si vous avez besoin d'une nouvelle consultation, votre médecin traitant peut émettre une nouvelle ordonnance : envoyez-moi son numéro et je réserve tout de suite.",
        "pt": "Esta receita já foi utilizada ou já não é marcável. Se precisar de uma nova consulta, o seu médico de família pode emitir uma nova receita: envie-me o número e marco logo.",
        "de": "Dieses Rezept wurde bereits verwendet oder ist nicht mehr buchbar. Wenn Sie einen neuen Termin brauchen, kann Ihr Hausarzt ein neues Rezept ausstellen: Schicken Sie mir die Nummer, dann buche ich sofort.",
        "ro": "Această rețetă a fost deja folosită sau nu mai poate fi programată. Dacă aveți nevoie de o nouă vizită, medicul de familie vă poate emite o rețetă nouă: trimiteți-mi numărul și programez imediat.",
        "ar": "هذه الوصفة استُخدمت بالفعل أو لم تعد قابلة للحجز. إذا كنتم بحاجة إلى زيارة جديدة، يمكن لطبيب الأسرة إصدار وصفة جديدة: أرسلوا لي رقمها وسأحجز فورًا.",
        "zh": "这张处方已被使用或已不能预约。如需新的就诊，您的家庭医生可以开具新处方：把编号发给我，我马上为您预约。",
        "uk": "Цей рецепт уже використано або за ним більше не можна записатися. Якщо потрібен новий візит, сімейний лікар може виписати новий рецепт: надішліть номер, і я одразу запишу Вас.",
    },
    "rx_not_found": {
        "it": "Non trovo questa ricetta nel registro. Può ricontrollare il numero e riscrivermelo esattamente com'è? Se ha dubbi, il suo medico di base può confermarglielo.",
        "en": "I could not find this referral in the register. Could you check the number and send it to me again exactly as it is written? If you are unsure, your GP can check it with you.",
        "es": "No encuentro esta receta en el registro. ¿Puede revisar el número y volver a escribírmelo exactamente como aparece? Si tiene dudas, su médico de cabecera se lo puede confirmar.",
        "fr": "Je ne trouve pas cette ordonnance dans le registre. Pouvez-vous vérifier le numéro et me le renvoyer exactement tel qu'il est écrit ? En cas de doute, votre médecin traitant peut vous le confirmer.",
        "pt": "Não encontro esta receita no registo. Pode verificar o número e voltar a enviá-lo exatamente como está escrito? Em caso de dúvida, o seu médico de família pode confirmá-lo.",
        "de": "Ich finde dieses Rezept nicht im Register. Könnten Sie die Nummer prüfen und sie mir genau so noch einmal schicken, wie sie geschrieben ist? Im Zweifel kann Ihr Hausarzt sie bestätigen.",
        "ro": "Nu găsesc această rețetă în registru. Puteți verifica numărul și să mi-l trimiteți din nou exact cum este scris? Dacă aveți dubii, medicul de familie vi-l poate confirma.",
        "ar": "لا أجد هذه الوصفة في السجل. هل يمكنكم التحقق من الرقم وإرساله مجددًا تمامًا كما هو مكتوب؟ إن كان لديكم شك فيمكن لطبيب الأسرة تأكيده لكم.",
        "zh": "我在登记系统中找不到这张处方。能否核对编号，并按原样重新发给我？如有疑问，可以请家庭医生帮您确认。",
        "uk": "Я не знаходжу цей рецепт у реєстрі. Чи можете перевірити номер і надіслати його ще раз точно так, як він написаний? Якщо маєте сумніви, сімейний лікар може його підтвердити.",
    },
    "cross_search": {
        "it": "Nella nostra ASST non ci sono posti liberi per la sua {label} entro i tempi della ricetta, quindi sto chiedendo a un'ASST partner. Per ora condivido solo il tipo di visita, nessun suo dato.\nSe troviamo un posto lì, per prenotarlo dovrò condividere con loro i dati necessari alla prenotazione (ricetta e nominativo), solo con il suo consenso. Se è d'accordo può darmelo già ora; se preferisce di no, cerco per lei le date più vicine disponibili qui.",
        "en": "In our ASST there are no free slots for your {label} within the time set by the referral, so I am asking a partner ASST. For now I am sharing only the type of visit, none of your data.\nIf we find a slot there, to book it I will need to share with them the data required for the booking (referral and name), only with your permission. If you agree, you can give your permission now; if you prefer not to, I will look for the closest dates available here.",
        "es": "En nuestra ASST no hay plazas libres para su {label} dentro del plazo de la receta, así que estoy consultando a una ASST colaboradora. Por ahora solo comparto el tipo de visita, ninguno de sus datos.\nSi encontramos una plaza allí, para reservarla tendré que compartir con ellos los datos necesarios (receta y nombre), solo con su consentimiento. Si está de acuerdo, puede dármelo ya; si prefiere que no, busco las fechas más próximas disponibles aquí.",
        "fr": "Dans notre ASST, il n'y a pas de place libre pour votre {label} dans le délai de l'ordonnance : je consulte donc une ASST partenaire. Pour l'instant je ne partage que le type de consultation, aucune de vos données.\nSi nous trouvons une place là-bas, pour la réserver je devrai leur transmettre les données nécessaires (ordonnance et nom), uniquement avec votre consentement. Si vous êtes d'accord, vous pouvez me le donner dès maintenant ; sinon, je cherche les dates les plus proches disponibles ici.",
        "pt": "Na nossa ASST não há vagas para a sua {label} dentro do prazo da receita, por isso estou a consultar uma ASST parceira. Por agora partilho apenas o tipo de consulta, nenhum dado seu.\nSe encontrarmos vaga lá, para a marcar terei de partilhar com eles os dados necessários (receita e nome), apenas com o seu consentimento. Se concordar, pode dá-lo já; se preferir que não, procuro as datas mais próximas disponíveis aqui.",
        "de": "In unserer ASST gibt es innerhalb der Rezeptfrist keinen freien Termin für Ihre {label}, deshalb frage ich bei einer Partner-ASST an. Vorerst teile ich nur die Art des Termins mit, keine Ihrer Daten.\nWenn wir dort einen Platz finden, muss ich für die Buchung die nötigen Daten (Rezept und Name) weitergeben, aber nur mit Ihrer Einwilligung. Wenn Sie einverstanden sind, können Sie sie mir schon jetzt geben; wenn nicht, suche ich die nächsten verfügbaren Termine hier.",
        "ro": "În ASST-ul nostru nu există locuri libere pentru {label} în termenul rețetei, așa că întreb un ASST partener. Deocamdată transmit doar tipul vizitei, nimic din datele dumneavoastră.\nDacă găsim un loc acolo, pentru a-l rezerva va trebui să le transmit datele necesare (rețeta și numele), doar cu acordul dumneavoastră. Dacă sunteți de acord mi-l puteți da de acum; dacă nu, caut cele mai apropiate date disponibile aici.",
        "ar": "لا توجد في ASST الخاصة بنا مواعيد شاغرة لـ {label} ضمن مدة الوصفة، لذلك أستفسر لدى ASST شريكة. حاليًا أشارك نوع الزيارة فقط دون أي من بياناتكم.\nإذا وجدنا موعدًا هناك فسأحتاج لحجزه إلى مشاركة البيانات اللازمة (الوصفة والاسم) بموافقتكم فقط. إن كنتم موافقين يمكنكم إعطائي الموافقة الآن؛ وإن فضلتم غير ذلك فسأبحث عن أقرب المواعيد المتاحة هنا.",
        "zh": "在我们的ASST，处方期限内没有{label}的空位，所以我正在询问合作的ASST。目前我只告知就诊类型，不提供您的任何个人信息。\n如果在那边找到空位，预约时需要向对方提供必要信息（处方和姓名），且必须征得您的同意。如您同意，现在就可以给我授权；如果不同意，我会在这里为您寻找最近的可用日期。",
        "uk": "У нашій ASST немає вільних місць для Вашої {label} у межах терміну рецепта, тому я звертаюся до партнерської ASST. Поки що передаю лише тип візиту, жодних Ваших даних.\nЯкщо знайдемо там місце, для запису мені доведеться передати необхідні дані (рецепт та ім'я), лише за Вашою згодою. Якщо Ви згодні, можете дати її вже зараз; якщо ні, я пошукаю найближчі доступні дати тут.",
    },
    "cross_choice": {
        "it": "Grazie per la scelta. Questo posto è in un'ASST partner: per prenotarlo devo condividere con loro solo i dati necessari alla prenotazione (ricetta e nominativo). Mi dà il suo consenso? Se preferisce di no, cerco un'altra soluzione.",
        "en": "Thank you for your choice. This slot is in a partner ASST: to book it I have to share with them only the data needed for the booking (referral and name). Do I have your permission? If you prefer not to, I will look for another solution.",
        "es": "Gracias por su elección. Esta plaza está en una ASST colaboradora: para reservarla debo compartir con ellos solo los datos necesarios (receta y nombre). ¿Me da su consentimiento? Si prefiere que no, busco otra solución.",
        "fr": "Merci pour votre choix. Cette place se trouve dans une ASST partenaire : pour la réserver, je dois leur transmettre uniquement les données nécessaires (ordonnance et nom). Me donnez-vous votre consentement ? Sinon, je cherche une autre solution.",
        "pt": "Obrigado pela escolha. Esta vaga é numa ASST parceira: para a marcar tenho de partilhar com eles apenas os dados necessários (receita e nome). Dá-me o seu consentimento? Se preferir que não, procuro outra solução.",
        "de": "Danke für Ihre Wahl. Dieser Termin ist in einer Partner-ASST: Für die Buchung muss ich dort nur die nötigen Daten (Rezept und Name) weitergeben. Geben Sie mir Ihre Einwilligung? Wenn nicht, suche ich eine andere Lösung.",
        "ro": "Vă mulțumesc pentru alegere. Acest loc este la un ASST partener: pentru a-l rezerva trebuie să le transmit doar datele necesare (rețeta și numele). Îmi dați acordul? Dacă nu, caut altă soluție.",
        "ar": "شكرًا على اختياركم. هذا الموعد في ASST شريكة: لحجزه يجب أن أشارك معهم البيانات اللازمة فقط (الوصفة والاسم). هل توافقون؟ إن لم توافقوا فسأبحث عن حل آخر.",
        "zh": "谢谢您的选择。该空位在合作的ASST：预约需要向对方提供必要信息（处方和姓名）。您同意吗？如果不同意，我会另找方案。",
        "uk": "Дякую за вибір. Це місце в партнерській ASST: для запису мені потрібно передати їм лише необхідні дані (рецепт та ім'я). Чи даєте Ви згоду? Якщо ні, я пошукаю інше рішення.",
    },
    "booking_consent": {
        "it": "Grazie per la scelta! Per prenotare mi serve il suo consenso a usare i dati della prenotazione (ricetta e nominativo) con la struttura.",
        "en": "Thank you for your choice! To book it I need your permission to use the booking data (referral and name) with the facility.",
        "es": "¡Gracias por su elección! Para reservar necesito su consentimiento para usar los datos de la reserva (receta y nombre) con el centro.",
        "fr": "Merci pour votre choix ! Pour réserver, j'ai besoin de votre consentement pour utiliser les données de la réservation (ordonnance et nom) avec l'établissement.",
        "pt": "Obrigado pela escolha! Para marcar preciso do seu consentimento para usar os dados da marcação (receita e nome) com a unidade.",
        "de": "Danke für Ihre Wahl! Für die Buchung brauche ich Ihre Einwilligung, die Buchungsdaten (Rezept und Name) an die Einrichtung weiterzugeben.",
        "ro": "Vă mulțumesc pentru alegere! Pentru programare am nevoie de acordul dumneavoastră de a folosi datele programării (rețeta și numele) cu unitatea.",
        "ar": "شكرًا على اختياركم! للحجز أحتاج إلى موافقتكم على استخدام بيانات الحجز (الوصفة والاسم) مع المنشأة.",
        "zh": "谢谢您的选择！为完成预约，我需要您同意将预约信息（处方和姓名）提供给医疗机构。",
        "uk": "Дякую за вибір! Для запису мені потрібна Ваша згода на використання даних запису (рецепт та ім'я) із закладом.",
    },
    "booking_declined": {
        "it": "Nessun problema: senza il suo consenso non prenoto nulla. Se cambia idea mi scriva pure, e ripartiamo da qui.",
        "en": "No problem: without your permission I will not book anything. If you change your mind, just write to me and we will pick up from here.",
        "es": "No hay problema: sin su consentimiento no reservo nada. Si cambia de idea, escríbame y seguimos desde aquí.",
        "fr": "Aucun problème : sans votre consentement je ne réserve rien. Si vous changez d'avis, écrivez-moi et nous reprendrons à partir d'ici.",
        "pt": "Sem problema: sem o seu consentimento não marco nada. Se mudar de ideias, escreva-me e continuamos daqui.",
        "de": "Kein Problem: Ohne Ihre Einwilligung buche ich nichts. Wenn Sie es sich anders überlegen, schreiben Sie mir einfach, dann machen wir hier weiter.",
        "ro": "Nicio problemă: fără acordul dumneavoastră nu programez nimic. Dacă vă răzgândiți, scrieți-mi și continuăm de aici.",
        "ar": "لا مشكلة: لن أحجز أي شيء دون موافقتكم. إن غيرتم رأيكم فاكتبوا لي ونكمل من هنا.",
        "zh": "没问题：未经您的同意我不会预约任何事项。如果您改变主意，请告诉我，我们从这里继续。",
        "uk": "Без проблем: без Вашої згоди я нічого не бронюю. Якщо передумаєте, напишіть мені, і продовжимо звідси.",
    },
    "cross_declined": {
        "it": "Capito, non condivido i suoi dati fuori dalla nostra ASST.",
        "en": "Understood, I will not share your data outside our ASST.",
        "es": "Entendido, no comparto sus datos fuera de nuestra ASST.",
        "fr": "Compris, je ne partage pas vos données en dehors de notre ASST.",
        "pt": "Entendido, não partilho os seus dados fora da nossa ASST.",
        "de": "Verstanden, ich gebe Ihre Daten nicht außerhalb unserer ASST weiter.",
        "ro": "Am înțeles, nu vă transmit datele în afara ASST-ului nostru.",
        "ar": "مفهوم، لن أشارك بياناتكم خارج ASST الخاصة بنا.",
        "zh": "明白，我不会把您的信息提供给本ASST以外的机构。",
        "uk": "Зрозуміло, я не передаватиму Ваші дані за межі нашої ASST.",
    },
    "forwarded": {
        "it": "Perfetto, ho inoltrato la sua scelta ({when}) alla struttura e attendo la conferma firmata: le mando la ricevuta appena arriva.",
        "en": "Perfect, I have sent your choice ({when}) to the facility and I am waiting for the signed confirmation: I will send you the receipt as soon as it arrives.",
        "es": "Perfecto, he enviado su elección ({when}) al centro y espero la confirmación firmada: le mando el justificante en cuanto llegue.",
        "fr": "Parfait, j'ai transmis votre choix ({when}) à l'établissement et j'attends la confirmation signée : je vous envoie le reçu dès qu'il arrive.",
        "pt": "Perfeito, enviei a sua escolha ({when}) à unidade e aguardo a confirmação assinada: envio-lhe o comprovativo assim que chegar.",
        "de": "Perfekt, ich habe Ihre Wahl ({when}) an die Einrichtung weitergeleitet und warte auf die signierte Bestätigung: Ich schicke Ihnen den Beleg, sobald er da ist.",
        "ro": "Perfect, am trimis alegerea dumneavoastră ({when}) unității și aștept confirmarea semnată: vă trimit dovada imediat ce sosește.",
        "ar": "ممتاز، أرسلت اختياركم ({when}) إلى المنشأة وأنتظر التأكيد الموقّع: سأرسل لكم الإيصال فور وصوله.",
        "zh": "好的，我已将您的选择（{when}）转给医疗机构，正在等待签名确认：收到后会马上把凭证发给您。",
        "uk": "Чудово, я передав Ваш вибір ({when}) закладу й чекаю на підписане підтвердження: надішлю квитанцію, щойно вона надійде.",
    },
    "unreachable": {
        "it": "Mi dispiace: la struttura di questo posto in questo momento non è raggiungibile, quindi non posso prenotarlo. Sto già cercando per lei un'altra data.",
        "en": "I am sorry: the facility for this slot cannot be reached right now, so I cannot book it. I am already looking for another date for you.",
        "es": "Lo siento: el centro de esta plaza no está disponible en este momento, así que no puedo reservarla. Ya estoy buscando otra fecha para usted.",
        "fr": "Je suis désolé : l'établissement de cette place est injoignable pour le moment, je ne peux donc pas la réserver. Je cherche déjà une autre date pour vous.",
        "pt": "Lamento: a unidade desta vaga não está contactável neste momento, por isso não a posso marcar. Já estou a procurar outra data para si.",
        "de": "Es tut mir leid: Die Einrichtung dieses Termins ist gerade nicht erreichbar, daher kann ich ihn nicht buchen. Ich suche bereits ein anderes Datum für Sie.",
        "ro": "Îmi pare rău: unitatea acestui loc nu poate fi contactată acum, deci nu îl pot rezerva. Caut deja o altă dată pentru dumneavoastră.",
        "ar": "نأسف: لا يمكن الوصول إلى منشأة هذا الموعد حاليًا، لذا لا أستطيع حجزه. أبحث بالفعل عن موعد آخر لكم.",
        "zh": "抱歉：该空位所在的医疗机构暂时无法联系，所以无法预约。我正在为您寻找其他日期。",
        "uk": "Перепрошую: заклад цього місця зараз недоступний, тому я не можу його забронювати. Я вже шукаю для Вас іншу дату.",
    },
    "facility_cancelled": {
        "it": "Mi dispiace: la struttura ha annullato questo posto. Non dipende da lei e non deve fare nulla: sto già cercando per lei un'altra data.",
        "en": "I am sorry: the facility has cancelled this slot. It is not your fault and you do not need to do anything: I am already looking for another date for you.",
        "es": "Lo siento: el centro ha anulado esta plaza. No depende de usted y no tiene que hacer nada: ya estoy buscando otra fecha para usted.",
        "fr": "Je suis désolé : l'établissement a annulé cette place. Ce n'est pas de votre fait et vous n'avez rien à faire : je cherche déjà une autre date pour vous.",
        "pt": "Lamento: a unidade cancelou esta vaga. Não depende de si e não precisa de fazer nada: já estou a procurar outra data para si.",
        "de": "Es tut mir leid: Die Einrichtung hat diesen Termin storniert. Das liegt nicht an Ihnen und Sie müssen nichts tun: Ich suche bereits ein anderes Datum für Sie.",
        "ro": "Îmi pare rău: unitatea a anulat acest loc. Nu depinde de dumneavoastră și nu trebuie să faceți nimic: caut deja o altă dată.",
        "ar": "نأسف: ألغت المنشأة هذا الموعد. الأمر لا يتعلق بكم ولا تحتاجون لفعل أي شيء: أبحث بالفعل عن موعد آخر لكم.",
        "zh": "抱歉：医疗机构取消了这个空位。这不是您的原因，您无需做任何事：我正在为您寻找其他日期。",
        "uk": "Перепрошую: заклад скасував це місце. Це не залежить від Вас, і нічого робити не потрібно: я вже шукаю для Вас іншу дату.",
    },
    "nomore": {
        "it": "Ho ricontrollato: non ci sono altre date libere entro i tempi della ricetta. Le opzioni che le ho mandato restano valide; se nessuna le va bene, me lo dica e cerco l'alternativa più vicina.",
        "en": "I checked again: there are no other free dates within the time set by the referral. The options I sent you are still valid; if none of them works, tell me and I will look for the closest alternative.",
        "es": "He vuelto a comprobarlo: no hay otras fechas libres dentro del plazo de la receta. Las opciones que le envié siguen siendo válidas; si ninguna le va bien, dígamelo y busco la alternativa más próxima.",
        "fr": "J'ai revérifié : il n'y a pas d'autres dates libres dans le délai de l'ordonnance. Les options envoyées restent valables ; si aucune ne vous convient, dites-le-moi et je cherche l'alternative la plus proche.",
        "pt": "Voltei a verificar: não há outras datas livres dentro do prazo da receita. As opções que lhe enviei continuam válidas; se nenhuma lhe convier, diga-me e procuro a alternativa mais próxima.",
        "de": "Ich habe noch einmal nachgesehen: Innerhalb der Rezeptfrist gibt es keine weiteren freien Termine. Die gesendeten Optionen gelten weiterhin; wenn keine passt, sagen Sie es mir und ich suche die nächste Alternative.",
        "ro": "Am verificat din nou: nu există alte date libere în termenul rețetei. Opțiunile trimise rămân valabile; dacă nu vă convine niciuna, spuneți-mi și caut cea mai apropiată alternativă.",
        "ar": "تحققت مرة أخرى: لا توجد مواعيد شاغرة أخرى ضمن مدة الوصفة. الخيارات التي أرسلتها ما زالت صالحة؛ وإن لم يناسبكم أي منها فأخبروني وسأبحث عن أقرب بديل.",
        "zh": "我又查了一遍：处方期限内没有其他空位。之前发给您的选项仍然有效；如果都不合适，请告诉我，我会寻找最近的替代方案。",
        "uk": "Я перевірив ще раз: інших вільних дат у межах терміну рецепта немає. Надіслані варіанти залишаються дійсними; якщо жоден не підходить, скажіть, і я пошукаю найближчу альтернативу.",
    },
    "receipt_head": {
        "it": "La sua visita è prenotata e registrata con firma. Ecco la sua ricevuta:",
        "en": "Your appointment is booked and signed in the register. Here is your receipt:",
        "es": "Su visita está reservada y registrada con firma. Aquí tiene su justificante:",
        "fr": "Votre rendez-vous est réservé et enregistré avec signature. Voici votre reçu :",
        "pt": "A sua consulta está marcada e registada com assinatura. Eis o seu comprovativo:",
        "de": "Ihr Termin ist gebucht und signiert im Register eingetragen. Hier ist Ihr Beleg:",
        "ro": "Vizita dumneavoastră este programată și înregistrată cu semnătură. Iată dovada:",
        "ar": "تم حجز موعدكم وتسجيله بتوقيع. إليكم الإيصال:",
        "zh": "您的就诊已预约成功，并已签名登记。这是您的凭证：",
        "uk": "Ваш візит заброньовано й зареєстровано з підписом. Ось Ваша квитанція:",
    },
    "r_when": {"it": "Quando: {when}.", "en": "When: {when}.", "es": "Cuándo: {when}.", "fr": "Quand : {when}.", "pt": "Quando: {when}.", "de": "Wann: {when}.", "ro": "Când: {when}.", "ar": "الموعد: {when}.", "zh": "时间：{when}。", "uk": "Коли: {when}."},
    "r_where": {"it": "Dove: {address}.", "en": "Where: {address}.", "es": "Dónde: {address}.", "fr": "Où : {address}.", "pt": "Onde: {address}.", "de": "Wo: {address}.", "ro": "Unde: {address}.", "ar": "المكان: {address}.", "zh": "地点：{address}。", "uk": "Де: {address}."},
    "r_next": {"it": "Cosa fare adesso:", "en": "What to do now:", "es": "Qué hacer ahora:", "fr": "Que faire maintenant :", "pt": "O que fazer agora:", "de": "Was Sie jetzt tun:", "ro": "Ce aveți de făcut acum:", "ar": "ما الذي يجب فعله الآن:", "zh": "接下来请这样做：", "uk": "Що робити зараз:"},
    "r_step1": {
        "it": "1. Conservi booking_id e ledger_ref qui sopra: sono la prova della prenotazione.",
        "en": "1. Keep the booking_id and ledger_ref above: they are your proof of booking.",
        "es": "1. Guarde booking_id y ledger_ref de arriba: son el comprobante de su reserva.",
        "fr": "1. Conservez booking_id et ledger_ref ci-dessus : ils prouvent votre réservation.",
        "pt": "1. Guarde o booking_id e o ledger_ref acima: são a prova da marcação.",
        "de": "1. Bewahren Sie booking_id und ledger_ref oben auf: Sie sind Ihr Buchungsnachweis.",
        "ro": "1. Păstrați booking_id și ledger_ref de mai sus: sunt dovada programării.",
        "ar": "1. احتفظوا بـ booking_id و ledger_ref أعلاه: فهما إثبات الحجز.",
        "zh": "1. 请保存上方的booking_id和ledger_ref：这是您的预约凭据。",
        "uk": "1. Збережіть booking_id та ledger_ref вище: це підтвердження запису.",
    },
    "r_step2": {
        "it": "2. Il giorno della visita faccia il check-in presso la struttura{window}, indicando booking_id e ledger_ref.",
        "en": "2. On the day of the visit, check in at the facility{window}, giving booking_id and ledger_ref.",
        "es": "2. El día de la visita haga el check-in en el centro{window}, indicando booking_id y ledger_ref.",
        "fr": "2. Le jour du rendez-vous, faites l'enregistrement à l'établissement{window}, en indiquant booking_id et ledger_ref.",
        "pt": "2. No dia da consulta faça o check-in na unidade{window}, indicando o booking_id e o ledger_ref.",
        "de": "2. Melden Sie sich am Termintag in der Einrichtung an{window} und nennen Sie booking_id und ledger_ref.",
        "ro": "2. În ziua vizitei faceți check-in la unitate{window}, indicând booking_id și ledger_ref.",
        "ar": "2. في يوم الزيارة سجّلوا الحضور في المنشأة{window} مع ذكر booking_id و ledger_ref.",
        "zh": "2. 就诊当天请到医疗机构办理签到{window}，并出示booking_id和ledger_ref。",
        "uk": "2. У день візиту пройдіть реєстрацію в закладі{window}, назвавши booking_id та ledger_ref.",
    },
    "r_window": {"it": " nella fascia {window}", "en": " during the {window} slot", "es": " en la franja {window}", "fr": " dans la tranche {window}", "pt": " no período {window}", "de": " im Zeitfenster {window}", "ro": " în intervalul {window}", "ar": " خلال الفترة {window}", "zh": "（时段{window}）", "uk": " у проміжок {window}"},
    "r_privacy": {
        "it": "I suoi dati sono usati solo per questa prenotazione e condivisi solo con la struttura che la visiterà.",
        "en": "Your data is used only for this booking and shared only with the facility that will see you.",
        "es": "Sus datos se usan solo para esta cita y se comparten solo con el centro que le atenderá.",
        "fr": "Vos données servent uniquement à ce rendez-vous et ne sont partagées qu'avec l'établissement qui vous recevra.",
        "pt": "Os seus dados são usados apenas para esta marcação e partilhados apenas com a unidade que o vai atender.",
        "de": "Ihre Daten werden nur für diesen Termin verwendet und nur mit der Einrichtung geteilt, die Sie behandelt.",
        "ro": "Datele dumneavoastră sunt folosite doar pentru această programare și împărtășite doar cu unitatea care vă va consulta.",
        "ar": "تُستخدم بياناتكم لهذا الموعد فقط ولا تُشارك إلا مع المرفق الذي سيستقبلكم.",
        "zh": "您的信息仅用于本次预约，并且只与为您就诊的机构共享。",
        "uk": "Ваші дані використовуються лише для цього запису й передаються лише закладу, який вас прийме.",
    },
    "ask_gender_only": {
        "it": "Grazie, la sua ricetta per una {label} è valida e ho preso nota dei giorni che preferisce ({days}). Un'ultima cosa: preferisce un medico donna o uomo? Se per lei è indifferente me lo dica pure, e cerco la prima data utile.",
        "en": "Thank you, your referral for a {label} is valid and I have noted the days you prefer ({days}). One last thing: do you prefer a female or male doctor? If it makes no difference, just tell me and I will look for the first date.",
        "es": "Gracias, su receta para la {label} es válida y he anotado los días que prefiere ({days}). Una última cosa: ¿prefiere una médica o un médico? Si le da igual, dígamelo y busco la primera fecha disponible.",
        "fr": "Merci, votre ordonnance pour la {label} est valable et j'ai noté les jours que vous préférez ({days}). Une dernière chose : préférez-vous une femme ou un homme médecin ? Si cela vous est égal, dites-le-moi et je cherche la première date.",
        "pt": "Obrigado, a sua receita para a {label} é válida e anotei os dias que prefere ({days}). Só mais uma coisa: prefere uma médica ou um médico? Se lhe for indiferente, diga-me e procuro a primeira data.",
        "de": "Danke, Ihr Rezept für die {label} ist gültig und ich habe Ihre Wunschtage notiert ({days}). Eine letzte Frage: Möchten Sie lieber eine Ärztin oder einen Arzt? Wenn es Ihnen egal ist, sagen Sie es einfach und ich suche den ersten Termin.",
        "ro": "Mulțumesc, rețeta pentru {label} este valabilă și am notat zilele preferate ({days}). Un ultim lucru: preferați o doctoriță sau un doctor? Dacă nu contează, spuneți-mi și caut prima dată liberă.",
        "ar": "شكرًا، وصفتكم لـ{label} صالحة وقد سجلت الأيام التي تفضلونها ({days}). سؤال أخير: هل تفضلون طبيبة أم طبيبًا؟ إذا لم يكن ذلك مهمًا فأخبروني وسأبحث عن أول موعد.",
        "zh": "谢谢，您的{label}处方有效，我已记下您希望的日期（{days}）。最后一个问题：您希望女医生还是男医生？如果都可以，请告诉我，我会找最早的时间。",
        "uk": "Дякую, ваш рецепт для {label} дійсний, і я записав бажані дні ({days}). Останнє: ви хочете лікарку чи лікаря? Якщо це не важливо, скажіть, і я знайду найближчу дату.",
    },
    "ask_days_only_f": {
        "it": "Grazie, la sua ricetta per una {label} è valida e ho preso nota che preferisce un medico donna. Un'ultima cosa: ci sono giorni della settimana che le vanno meglio? Se va bene qualsiasi giorno me lo dica pure, e cerco la prima data utile.",
        "en": "Thank you, your referral for a {label} is valid and I have noted that you prefer a female doctor. One last thing: are there days of the week that suit you best? If any day is fine, just tell me and I will look for the first date.",
        "es": "Gracias, su receta para la {label} es válida y he anotado que prefiere una médica. Una última cosa: ¿hay días de la semana que le vengan mejor? Si cualquier día le va bien, dígamelo y busco la primera fecha.",
        "fr": "Merci, votre ordonnance pour la {label} est valable et j'ai noté que vous préférez une femme médecin. Une dernière chose : y a-t-il des jours de la semaine qui vous conviennent mieux ? Si n'importe quel jour vous va, dites-le-moi et je cherche la première date.",
        "pt": "Obrigado, a sua receita para a {label} é válida e anotei que prefere uma médica. Só mais uma coisa: há dias da semana que lhe dão mais jeito? Se qualquer dia servir, diga-me e procuro a primeira data.",
        "de": "Danke, Ihr Rezept für die {label} ist gültig und ich habe notiert, dass Sie eine Ärztin bevorzugen. Eine letzte Frage: Gibt es Wochentage, die Ihnen besser passen? Wenn jeder Tag geht, sagen Sie es einfach und ich suche den ersten Termin.",
        "ro": "Mulțumesc, rețeta pentru {label} este valabilă și am notat că preferați o doctoriță. Un ultim lucru: există zile ale săptămânii care vă convin mai mult? Dacă orice zi e bună, spuneți-mi și caut prima dată liberă.",
        "ar": "شكرًا، وصفتكم لـ{label} صالحة وقد سجلت أنكم تفضلون طبيبة. سؤال أخير: هل هناك أيام في الأسبوع تناسبكم أكثر؟ إذا كان أي يوم مناسبًا فأخبروني وسأبحث عن أول موعد.",
        "zh": "谢谢，您的{label}处方有效，我已记下您希望女医生。最后一个问题：一周中哪几天对您更方便？如果哪天都可以，请告诉我，我会找最早的时间。",
        "uk": "Дякую, ваш рецепт для {label} дійсний, і я записав, що ви хочете лікарку. Останнє: які дні тижня вам зручніші? Якщо підходить будь-який день, скажіть, і я знайду найближчу дату.",
    },
    "ask_days_only_m": {
        "it": "Grazie, la sua ricetta per una {label} è valida e ho preso nota che preferisce un medico uomo. Un'ultima cosa: ci sono giorni della settimana che le vanno meglio? Se va bene qualsiasi giorno me lo dica pure, e cerco la prima data utile.",
        "en": "Thank you, your referral for a {label} is valid and I have noted that you prefer a male doctor. One last thing: are there days of the week that suit you best? If any day is fine, just tell me and I will look for the first date.",
        "es": "Gracias, su receta para la {label} es válida y he anotado que prefiere un médico. Una última cosa: ¿hay días de la semana que le vengan mejor? Si cualquier día le va bien, dígamelo y busco la primera fecha.",
        "fr": "Merci, votre ordonnance pour la {label} est valable et j'ai noté que vous préférez un homme médecin. Une dernière chose : y a-t-il des jours de la semaine qui vous conviennent mieux ? Si n'importe quel jour vous va, dites-le-moi et je cherche la première date.",
        "pt": "Obrigado, a sua receita para a {label} é válida e anotei que prefere um médico. Só mais uma coisa: há dias da semana que lhe dão mais jeito? Se qualquer dia servir, diga-me e procuro a primeira data.",
        "de": "Danke, Ihr Rezept für die {label} ist gültig und ich habe notiert, dass Sie einen Arzt bevorzugen. Eine letzte Frage: Gibt es Wochentage, die Ihnen besser passen? Wenn jeder Tag geht, sagen Sie es einfach und ich suche den ersten Termin.",
        "ro": "Mulțumesc, rețeta pentru {label} este valabilă și am notat că preferați un doctor. Un ultim lucru: există zile ale săptămânii care vă convin mai mult? Dacă orice zi e bună, spuneți-mi și caut prima dată liberă.",
        "ar": "شكرًا، وصفتكم لـ{label} صالحة وقد سجلت أنكم تفضلون طبيبًا. سؤال أخير: هل هناك أيام في الأسبوع تناسبكم أكثر؟ إذا كان أي يوم مناسبًا فأخبروني وسأبحث عن أول موعد.",
        "zh": "谢谢，您的{label}处方有效，我已记下您希望男医生。最后一个问题：一周中哪几天对您更方便？如果哪天都可以，请告诉我，我会找最早的时间。",
        "uk": "Дякую, ваш рецепт для {label} дійсний, і я записав, що ви хочете лікаря. Останнє: які дні тижня вам зручніші? Якщо підходить будь-який день, скажіть, і я знайду найближчу дату.",
    },
    "r_step3": {
        "it": "3. Porti con sé la ricetta e un documento d'identità.",
        "en": "3. Bring your referral and an identity document.",
        "es": "3. Lleve la receta y un documento de identidad.",
        "fr": "3. Apportez votre ordonnance et une pièce d'identité.",
        "pt": "3. Leve a receita e um documento de identificação.",
        "de": "3. Bringen Sie Ihr Rezept und einen Ausweis mit.",
        "ro": "3. Aduceți rețeta și un act de identitate.",
        "ar": "3. أحضروا الوصفة ووثيقة هوية.",
        "zh": "3. 请携带处方和身份证件。",
        "uk": "3. Візьміть із собою рецепт і документ, що посвідчує особу.",
    },
    "r_access": {
        "it": "Se le serve assistenza per arrivare o muoversi nella struttura (difficoltà motorie, visive o uditive), lo dica alla struttura al check-in. Se può, i mezzi pubblici sono un modo comodo e più sostenibile per arrivarci.",
        "en": "If you need assistance getting there or inside the facility (mobility, sight, hearing), tell the facility when you check in. If you can, public transport is a convenient and greener way to get there.",
        "es": "Si necesita ayuda para llegar o moverse dentro del centro (movilidad, vista u oído), dígalo en el check-in. Si puede, el transporte público es una forma cómoda y más sostenible de llegar.",
        "fr": "Si vous avez besoin d'aide pour venir ou vous déplacer dans l'établissement (mobilité, vue, audition), signalez-le à l'enregistrement. Si possible, les transports en commun sont un moyen pratique et plus durable d'y venir.",
        "pt": "Se precisar de ajuda para chegar ou para se deslocar na unidade (mobilidade, visão, audição), diga-o no check-in. Se puder, os transportes públicos são uma forma cómoda e mais sustentável de lá chegar.",
        "de": "Wenn Sie Unterstützung beim Anreisen oder in der Einrichtung brauchen (Mobilität, Sehen, Hören), sagen Sie es bei der Anmeldung. Wenn möglich, sind öffentliche Verkehrsmittel ein bequemer und umweltfreundlicher Weg.",
        "ro": "Dacă aveți nevoie de asistență pentru a ajunge sau a vă deplasa în unitate (dificultăți motorii, de vedere sau de auz), spuneți la check-in. Dacă puteți, transportul public este un mod comod și mai sustenabil de a ajunge.",
        "ar": "إذا احتجتم إلى مساعدة للوصول أو للتنقل داخل المنشأة (صعوبات حركية أو بصرية أو سمعية) فأخبروا المنشأة عند تسجيل الحضور. وإن أمكن، فالمواصلات العامة وسيلة مريحة وأكثر استدامة للوصول.",
        "zh": "如果您前往或在机构内行动需要协助（行动、视力或听力方面），请在签到时告知工作人员。如条件允许，乘坐公共交通前往既方便又环保。",
        "uk": "Якщо Вам потрібна допомога, щоб дістатися або пересуватися в закладі (труднощі з рухом, зором чи слухом), скажіть про це під час реєстрації. Якщо можете, громадський транспорт — зручний і екологічніший спосіб дістатися.",
    },
}

_TX.update({
    "beyond_intro": {
        "it": "Nei tempi della ricetta non ci sono posti liberi, né da noi né nelle ASST partner. Per non lasciarla senza appuntamento, ecco le prime date reali disponibili subito dopo:",
        "en": "Within the time set by the referral there are no free slots, neither with us nor in partner ASSTs. So you are not left without an appointment, here are the first real dates available right after:",
        "es": "Dentro del plazo de la receta no hay plazas libres, ni con nosotros ni en las ASST colaboradoras. Para no dejarle sin cita, estas son las primeras fechas reales disponibles justo después:",
        "fr": "Dans le délai de l'ordonnance, il n'y a pas de place libre, ni chez nous ni dans les ASST partenaires. Pour ne pas vous laisser sans rendez-vous, voici les premières dates réelles disponibles juste après :",
        "pt": "Dentro do prazo da receita não há vagas, nem connosco nem nas ASST parceiras. Para não ficar sem consulta, eis as primeiras datas reais disponíveis logo a seguir:",
        "de": "Innerhalb der Rezeptfrist gibt es keine freien Termine, weder bei uns noch in Partner-ASST. Damit Sie nicht ohne Termin bleiben, hier die ersten echten Termine direkt danach:",
        "ro": "În termenul rețetei nu există locuri libere, nici la noi, nici la ASST-urile partenere. Ca să nu rămâneți fără programare, iată primele date reale disponibile imediat după:",
        "ar": "لا توجد مواعيد شاغرة ضمن مدة الوصفة، لا لدينا ولا لدى ASST الشريكة. حتى لا تبقوا دون موعد، إليكم أول المواعيد الفعلية المتاحة بعد ذلك مباشرة:",
        "zh": "在处方期限内，我们和合作的ASST都没有空位。为了不让您没有预约，以下是紧接其后最早的实际可预约日期：",
        "uk": "У межах терміну рецепта вільних місць немає ні в нас, ні в партнерських ASST. Щоб Ви не залишилися без запису, ось перші реальні дати одразу після цього терміну:",
    },
    "gp_note": {
        "it": "Se nel frattempo i disturbi cambiano o peggiorano, ne parli con il suo medico di base.",
        "en": "If your symptoms change or get worse in the meantime, talk to your GP.",
        "es": "Si mientras tanto las molestias cambian o empeoran, hable con su médico de cabecera.",
        "fr": "Si entre-temps vos symptômes changent ou s'aggravent, parlez-en à votre médecin traitant.",
        "pt": "Se entretanto os sintomas mudarem ou piorarem, fale com o seu médico de família.",
        "de": "Wenn sich Ihre Beschwerden in der Zwischenzeit ändern oder verschlechtern, sprechen Sie mit Ihrem Hausarzt.",
        "ro": "Dacă între timp simptomele se schimbă sau se agravează, vorbiți cu medicul de familie.",
        "ar": "إذا تغيرت الأعراض أو ساءت في هذه الأثناء، فتحدثوا مع طبيب الأسرة.",
        "zh": "如果在此期间症状有变化或加重，请咨询您的家庭医生。",
        "uk": "Якщо тим часом симптоми зміняться чи посиляться, зверніться до сімейного лікаря.",
    },
    "none_at_all": {
        "it": "Al momento non trovo posti liberi per la sua {label}, né da noi né nelle ASST partner. Continuo a cercare: mi scriva quando vuole e ricontrollo subito.",
        "en": "Right now I cannot find free slots for your {label}, neither with us nor in partner ASSTs. I keep looking: write to me whenever you like and I will check again straight away.",
        "es": "Ahora mismo no encuentro plazas libres para su {label}, ni con nosotros ni en las ASST colaboradoras. Sigo buscando: escríbame cuando quiera y vuelvo a mirar enseguida.",
        "fr": "Pour l'instant je ne trouve pas de place libre pour votre {label}, ni chez nous ni dans les ASST partenaires. Je continue à chercher : écrivez-moi quand vous voulez et je revérifie aussitôt.",
        "pt": "De momento não encontro vagas para a sua {label}, nem connosco nem nas ASST parceiras. Continuo a procurar: escreva-me quando quiser e volto a verificar logo.",
        "de": "Derzeit finde ich keinen freien Termin für Ihre {label}, weder bei uns noch in Partner-ASST. Ich suche weiter: Schreiben Sie mir jederzeit, dann prüfe ich sofort erneut.",
        "ro": "Momentan nu găsesc locuri libere pentru {label}, nici la noi, nici la ASST-urile partenere. Continui să caut: scrieți-mi oricând și verific imediat din nou.",
        "ar": "حاليًا لا أجد مواعيد شاغرة لـ {label}، لا لدينا ولا لدى ASST الشريكة. سأواصل البحث: اكتبوا لي متى شئتم وسأتحقق فورًا من جديد.",
        "zh": "目前我们和合作的ASST都没有您{label}的空位。我会继续查找：您随时联系我，我会立即重新查询。",
        "uk": "Наразі я не знаходжу вільних місць для Вашої {label} ні в нас, ні в партнерських ASST. Я продовжую шукати: напишіть мені будь-коли, і я одразу перевірю знову.",
    },
    "citizen_cancel": {
        "it": "Va bene, ho annullato la sua richiesta: il posto torna libero per altri cittadini e non deve fare nient'altro.",
        "en": "All right, I have withdrawn your request: the slot is free again for other citizens and you do not need to do anything else.",
        "es": "De acuerdo, he anulado su solicitud: la plaza vuelve a quedar libre para otras personas y no tiene que hacer nada más.",
        "fr": "Entendu, j'ai annulé votre demande : la place redevient libre pour d'autres personnes et vous n'avez rien d'autre à faire.",
        "pt": "Está bem, anulei o seu pedido: a vaga fica livre para outras pessoas e não precisa de fazer mais nada.",
        "de": "In Ordnung, ich habe Ihre Anfrage zurückgezogen: Der Platz ist wieder frei für andere und Sie müssen nichts weiter tun.",
        "ro": "Bine, am anulat cererea: locul devine din nou liber pentru alte persoane și nu trebuie să faceți nimic altceva.",
        "ar": "حسنًا، لقد ألغيت طلبكم: أصبح الموعد متاحًا لأشخاص آخرين ولا تحتاجون لفعل أي شيء آخر.",
        "zh": "好的，我已撤销您的申请：该位置将释放给其他市民，您无需再做任何事。",
        "uk": "Гаразд, я скасував Ваш запит: місце знову вільне для інших, і Вам більше нічого не потрібно робити.",
    },
    "citizen_cancel_more": {
        "it": "Se le serve comunque la visita, qui sotto trova altre date: basta indicarmi quella che preferisce. Altrimenti può ignorare questo messaggio.",
        "en": "If you still need the visit, you will find other dates below: just tell me which one you prefer. Otherwise you can ignore this message.",
        "es": "Si aun así necesita la visita, abajo tiene otras fechas: solo dígame cuál prefiere. Si no, puede ignorar este mensaje.",
        "fr": "Si vous avez quand même besoin de la consultation, vous trouverez d'autres dates ci-dessous : indiquez-moi simplement celle que vous préférez. Sinon, vous pouvez ignorer ce message.",
        "pt": "Se ainda precisar da consulta, encontra outras datas abaixo: basta indicar-me a que prefere. Caso contrário, pode ignorar esta mensagem.",
        "de": "Wenn Sie den Termin trotzdem brauchen, finden Sie unten andere Daten: Sagen Sie mir einfach, welches Sie bevorzugen. Andernfalls können Sie diese Nachricht ignorieren.",
        "ro": "Dacă totuși aveți nevoie de vizită, mai jos găsiți alte date: spuneți-mi doar care vă convine. Altfel, puteți ignora acest mesaj.",
        "ar": "إذا كنتم ما زلتم بحاجة إلى الزيارة، تجدون أدناه مواعيد أخرى: يكفي أن تخبروني بما تفضلون. وإلا يمكنكم تجاهل هذه الرسالة.",
        "zh": "如果您仍需就诊，下面有其他日期：告诉我您选哪一个即可。否则可以忽略本消息。",
        "uk": "Якщо візит Вам усе ж потрібен, нижче є інші дати: просто скажіть, яка зручніша. Інакше можете проігнорувати це повідомлення.",
    },
    "wl_left": {
        "it": "Va bene, ho tolto la sua richiesta dalla lista d'attesa. Se in futuro le servirà, mi scriva e la reinserisco subito.",
        "en": "All right, I have removed your request from the waiting list. If you need it in the future, write to me and I will add it back straight away.",
        "es": "De acuerdo, he retirado su solicitud de la lista de espera. Si en el futuro la necesita, escríbame y la vuelvo a incluir enseguida.",
        "fr": "Entendu, j'ai retiré votre demande de la liste d'attente. Si vous en avez besoin plus tard, écrivez-moi et je la réinscris aussitôt.",
        "pt": "Está bem, retirei o seu pedido da lista de espera. Se precisar no futuro, escreva-me e volto a inscrevê-lo logo.",
        "de": "In Ordnung, ich habe Ihre Anfrage von der Warteliste genommen. Wenn Sie sie später brauchen, schreiben Sie mir, dann setze ich Sie sofort wieder darauf.",
        "ro": "Bine, am scos cererea de pe lista de așteptare. Dacă veți avea nevoie pe viitor, scrieți-mi și o reînscriu imediat.",
        "ar": "حسنًا، أزلت طلبكم من قائمة الانتظار. إذا احتجتم إليه مستقبلًا فاكتبوا لي وسأعيد إدراجه فورًا.",
        "zh": "好的，我已将您的申请从候补名单中移除。以后如有需要，联系我即可马上重新登记。",
        "uk": "Гаразд, я прибрав Ваш запит зі списку очікування. Якщо знадобиться в майбутньому, напишіть мені, і я одразу додам його знову.",
    },
    "access_ok": {
        "it": "Accessibilità: dal registro la struttura risulta accessibile in carrozzina.",
        "en": "Accessibility: according to the register, the facility is wheelchair accessible.",
        "es": "Accesibilidad: según el registro, el centro es accesible en silla de ruedas.",
        "fr": "Accessibilité : d'après le registre, l'établissement est accessible en fauteuil roulant.",
        "pt": "Acessibilidade: segundo o registo, a unidade é acessível em cadeira de rodas.",
        "de": "Barrierefreiheit: Laut Register ist die Einrichtung rollstuhlgerecht.",
        "ro": "Accesibilitate: conform registrului, unitatea este accesibilă cu scaun cu rotile.",
        "ar": "إمكانية الوصول: وفق السجل، المنشأة مهيأة للكراسي المتحركة.",
        "zh": "无障碍：根据登记信息，该机构可供轮椅通行。",
        "uk": "Доступність: за реєстром заклад доступний для крісел колісних.",
    },
    "access_unknown": {
        "it": "Accessibilità: il registro non indica l'accesso in carrozzina per questa struttura; le consiglio di segnalarlo alla struttura al check-in, così possono organizzarsi.",
        "en": "Accessibility: the register does not list wheelchair access for this facility; I suggest you mention it to the facility at check-in so they can prepare.",
        "es": "Accesibilidad: el registro no indica acceso en silla de ruedas para este centro; le aconsejo comunicarlo en el check-in para que puedan organizarse.",
        "fr": "Accessibilité : le registre n'indique pas d'accès en fauteuil roulant pour cet établissement ; je vous conseille de le signaler à l'enregistrement pour qu'ils puissent s'organiser.",
        "pt": "Acessibilidade: o registo não indica acesso em cadeira de rodas para esta unidade; aconselho a referi-lo no check-in para que se possam organizar.",
        "de": "Barrierefreiheit: Das Register nennt für diese Einrichtung keinen rollstuhlgerechten Zugang; sagen Sie es bitte bei der Anmeldung, damit man sich vorbereiten kann.",
        "ro": "Accesibilitate: registrul nu indică acces cu scaun cu rotile pentru această unitate; vă recomand să semnalați la check-in, ca să se poată organiza.",
        "ar": "إمكانية الوصول: لا يذكر السجل إمكانية دخول الكراسي المتحركة لهذه المنشأة؛ أنصحكم بإبلاغ المنشأة عند تسجيل الحضور ليستعدوا.",
        "zh": "无障碍：登记信息未注明该机构可供轮椅通行；建议您签到时告知工作人员，以便他们提前安排。",
        "uk": "Доступність: реєстр не вказує доступу для крісел колісних у цьому закладі; раджу повідомити про це під час реєстрації, щоб там підготувалися.",
    },
    "distance": {
        "it": "Distanza: circa {km} km dal centro del suo Municipio. Con i mezzi pubblici (metro, tram e bus ATM) è il modo più comodo e sostenibile per arrivarci.",
        "en": "Distance: about {km} km from the centre of your Municipio. Public transport (ATM metro, tram and bus) is the most convenient and sustainable way to get there.",
        "es": "Distancia: unos {km} km del centro de su Municipio. El transporte público (metro, tranvía y autobús ATM) es la forma más cómoda y sostenible de llegar.",
        "fr": "Distance : environ {km} km du centre de votre Municipio. Les transports publics (métro, tram et bus ATM) sont le moyen le plus pratique et le plus durable d'y aller.",
        "pt": "Distância: cerca de {km} km do centro do seu Municipio. Os transportes públicos (metro, elétrico e autocarro ATM) são a forma mais cómoda e sustentável de lá chegar.",
        "de": "Entfernung: etwa {km} km vom Zentrum Ihres Municipio. Öffentliche Verkehrsmittel (ATM-U-Bahn, Tram und Bus) sind der bequemste und nachhaltigste Weg.",
        "ro": "Distanță: aproximativ {km} km de centrul Municipiului dumneavoastră. Transportul public (metrou, tramvai și autobuz ATM) este cel mai comod și sustenabil mod de a ajunge.",
        "ar": "المسافة: حوالي {km} كم من مركز البلدية (Municipio) التي تسكنون فيها. المواصلات العامة (مترو وترام وحافلات ATM) هي الطريقة الأريح والأكثر استدامة للوصول.",
        "zh": "距离：距您所在区（Municipio）中心约{km}公里。乘坐ATM地铁、有轨电车和公交是最方便、最环保的出行方式。",
        "uk": "Відстань: близько {km} км від центру Вашого Municipio. Громадський транспорт (метро, трамвай і автобус ATM) — найзручніший і найекологічніший спосіб дістатися.",
    },
})

_WEEKDAYS: dict[str, tuple[str, ...]] = {
    "it": ("lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"),
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
    "es": ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"),
    "fr": ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"),
    "pt": ("segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"),
    "de": ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"),
    "ro": ("luni", "marți", "miercuri", "joi", "vineri", "sâmbătă", "duminică"),
    "ar": ("الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"),
    "zh": ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"),
    "uk": ("понеділок", "вівторок", "середа", "четвер", "п'ятниця", "субота", "неділя"),
}
_MONTHS: dict[str, tuple[str, ...]] = {
    "it": ("gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"),
    "en": ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"),
    "es": ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"),
    "fr": ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"),
    "pt": ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"),
    "de": ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"),
    "ro": ("ianuarie", "februarie", "martie", "aprilie", "mai", "iunie", "iulie", "august", "septembrie", "octombrie", "noiembrie", "decembrie"),
    "ar": ("يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو", "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"),
    "uk": ("січня", "лютого", "березня", "квітня", "травня", "червня", "липня", "серпня", "вересня", "жовтня", "листопада", "грудня"),
}
_DATE_FMT = {
    "it": "{wd} {d} {month} alle ore {time}",
    "en": "{wd} {d} {month} at {time}",
    "es": "{wd} {d} de {month} a las {time}",
    "fr": "{wd} {d} {month} à {time}",
    "pt": "{wd}, {d} de {month}, às {time}",
    "de": "{wd}, {d}. {month} um {time} Uhr",
    "ro": "{wd}, {d} {month}, ora {time}",
    "ar": "{wd} {d} {month} الساعة {time}",
    "zh": "{m}月{d}日 {wd} {time}",
    "uk": "{wd}, {d} {month} о {time}",
}
_LABELS: dict[str, dict[str, str]] = {
    "VIS-FIS": {"it": "visita fisiatrica", "en": "physiatry visit", "es": "visita de fisiatría", "fr": "consultation de médecine physique", "pt": "consulta de fisiatria", "de": "physiatrische Untersuchung", "ro": "consultația de recuperare medicală", "ar": "زيارة الطب الفيزيائي", "zh": "康复科就诊", "uk": "консультації фізіатра"},
    "VIS-CARD": {"it": "visita cardiologica", "en": "cardiology visit", "es": "visita de cardiología", "fr": "consultation de cardiologie", "pt": "consulta de cardiologia", "de": "kardiologische Untersuchung", "ro": "consultația cardiologică", "ar": "زيارة طب القلب", "zh": "心内科就诊", "uk": "консультації кардіолога"},
    "CTRL-CARD": {"it": "visita cardiologica di controllo", "en": "cardiology follow-up visit", "es": "visita de control de cardiología", "fr": "consultation de contrôle en cardiologie", "pt": "consulta de controlo de cardiologia", "de": "kardiologische Kontrolluntersuchung", "ro": "consultația cardiologică de control", "ar": "زيارة متابعة لطب القلب", "zh": "心内科复诊", "uk": "контрольної консультації кардіолога"},
    "CONS-GIN": {"it": "visita ginecologica", "en": "gynaecology consultation", "es": "consulta de ginecología", "fr": "consultation de gynécologie", "pt": "consulta de ginecologia", "de": "gynäkologische Beratung", "ro": "consultația ginecologică", "ar": "استشارة أمراض النساء", "zh": "妇科咨询", "uk": "гінекологічної консультації"},
}
_VISIT = {"it": "visita", "en": "visit", "es": "visita", "fr": "consultation", "pt": "consulta", "de": "Untersuchung", "ro": "vizita", "ar": "الزيارة", "zh": "就诊", "uk": "візиту"}

# Riconoscimento della lingua quando il cittadino non scrive lang=...
_LANG_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("es", ("hola", "buenos días", "buenas", "receta", "cita", "quisiera", "necesito", "gracias", "médico de cabecera", "por favor", " tengo ", " dolor ", " muy ", " estoy ", "me duele", " usted")),
    ("fr", ("bonjour", "bonsoir", "ordonnance", "rendez-vous", "je voudrais", "j'ai", "merci", "s'il vous plaît", "médecin traitant", " je suis", "très", " avec ", " vous ")),
    ("pt", ("olá", "bom dia", "boa tarde", "receita", "marcar", "obrigad", "gostaria", "preciso de", "médico de família", " tenho ", " estou ", " dor ", " muito ", " você")),
    ("de", ("hallo", "guten tag", "rezept", "termin", "ich möchte", "ich habe", "danke", "bitte", "hausarzt", " ich ", " schmerzen", " sehr ", " und ")),
    ("ro", ("bună", "rețet", "reteta", "programare", "aș dori", "am nevoie", "mulțumesc", "multumesc", "medicul de familie", " sunt ", " durere", " foarte ", " și ")),
    ("en", ("hello", "hi ", "good morning", "appointment", "i would like", "i need", "i have", "thank you", "thanks", "please", " i am ", " my ", " the ", " pain ")),
)


def _detect_lang(text: str) -> str | None:
    """La lingua di una frase: dall'alfabeto (arabo, cinese, cirillico) o da parole tipiche."""

    if re.search(r"[؀-ۿ]", text):
        return "ar"
    if re.search(r"[一-鿿]", text):
        return "zh"
    if re.search(r"[Ѐ-ӿ]", text):
        return "uk"
    # Tolgo token chiave=valore e identificatori: "prescription_number=..." non è inglese.
    plain = " ".join(word for word in text.split() if "=" not in word and "_" not in word)
    lowered = " " + _norm(plain) + " "
    best, score = None, 0
    for lang, hints in _LANG_HINTS:
        hits = sum(1 for hint in hints if hint in lowered)
        if hits > score:
            best, score = lang, hits
    italian = sum(
        1 for hint in ("buongiorno", "ricetta", "prenot", "vorrei", "grazie", "medico di base", "ho bisogno")
        if hint in lowered
    )
    if best is None or italian >= score:
        return None
    return best



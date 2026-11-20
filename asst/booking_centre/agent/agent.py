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
        "en": "Hello, this is the Booking Centre of your ASST and I am happy to help you book. To start I need the number of the prescription from your GP: you can copy it exactly as it appears on the prescription.",
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
        "en": "Thank you, I have checked your prescription for a {label}: it is valid.",
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
        "en": "The prescription has priority U: the visit should take place today.",
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
        "en": "The prescription has priority B: the visit should take place within 2 days.",
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
        "en": "The prescription has priority D: the visit should take place within 5 days.",
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
        "en": "The prescription has priority P: it is a planned visit, with no fixed deadline.",
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
        "en": "Your prescription is for a planned {label} (priority P). For this kind of visit the procedure is to put you on the waiting list, so you get a slot as soon as a suitable one becomes free.",
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
        "en": "I looked carefully, also beyond your preferences, but right now there is no free slot for your {label} within the time set by the prescription. The alternative I can offer you is the waiting list.",
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
        "en": "I have checked your prescription: it is no longer valid (under the rule in force a prescription lasts 2 days from when it is issued). Nothing serious, it just needs renewing. Here is what to do:\n1. Contact your GP and ask to renew the prescription {number}.\n2. When you receive the new prescription, send me its number.\n3. I will check it and immediately propose the available dates.",
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
        "en": "This prescription does not appear to be in your name, so I cannot use it. If there is a mistake, please send me the number of your own prescription.",
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
        "en": "This prescription has already been used or is no longer bookable. If you need a new visit, your GP can issue a new prescription: send me its number and I will book straight away.",
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
        "en": "I could not find this prescription in the register. Could you check the number and send it to me again exactly as it is written? If you are unsure, your GP can confirm it.",
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
        "it": "Nella nostra ASST non ci sono posti liberi per la sua {label} entro i tempi della ricetta, quindi sto chiedendo a un'ASST partner. Per ora condivido solo il tipo di visita, nessun suo dato.\nSe troviamo un posto lì, per prenotarlo dovrò condividere con loro i dati necessari alla prenotazione (ricetta e nominativo), solo con il suo consenso. Se è d'accordo può darmelo già ora; se preferisce di no, le propongo invece la lista d'attesa.",
        "en": "In our ASST there are no free slots for your {label} within the time set by the prescription, so I am asking a partner ASST. For now I am sharing only the type of visit, none of your data.\nIf we find a slot there, to book it I will need to share with them the data required for the booking (prescription and name), only with your consent. If you agree, you can give your consent now; if you prefer not to, I will offer you the waiting list instead.",
        "es": "En nuestra ASST no hay plazas libres para su {label} dentro del plazo de la receta, así que estoy consultando a una ASST colaboradora. Por ahora solo comparto el tipo de visita, ninguno de sus datos.\nSi encontramos una plaza allí, para reservarla tendré que compartir con ellos los datos necesarios (receta y nombre), solo con su consentimiento. Si está de acuerdo, puede dármelo ya; si prefiere que no, le propongo la lista de espera.",
        "fr": "Dans notre ASST, il n'y a pas de place libre pour votre {label} dans le délai de l'ordonnance : je consulte donc une ASST partenaire. Pour l'instant je ne partage que le type de consultation, aucune de vos données.\nSi nous trouvons une place là-bas, pour la réserver je devrai leur transmettre les données nécessaires (ordonnance et nom), uniquement avec votre consentement. Si vous êtes d'accord, vous pouvez me le donner dès maintenant ; sinon, je vous propose la liste d'attente.",
        "pt": "Na nossa ASST não há vagas para a sua {label} dentro do prazo da receita, por isso estou a consultar uma ASST parceira. Por agora partilho apenas o tipo de consulta, nenhum dado seu.\nSe encontrarmos vaga lá, para a marcar terei de partilhar com eles os dados necessários (receita e nome), apenas com o seu consentimento. Se concordar, pode dá-lo já; se preferir que não, proponho-lhe a lista de espera.",
        "de": "In unserer ASST gibt es innerhalb der Rezeptfrist keinen freien Termin für Ihre {label}, deshalb frage ich bei einer Partner-ASST an. Vorerst teile ich nur die Art des Termins mit, keine Ihrer Daten.\nWenn wir dort einen Platz finden, muss ich für die Buchung die nötigen Daten (Rezept und Name) weitergeben, aber nur mit Ihrer Einwilligung. Wenn Sie einverstanden sind, können Sie sie mir schon jetzt geben; wenn nicht, biete ich Ihnen die Warteliste an.",
        "ro": "În ASST-ul nostru nu există locuri libere pentru {label} în termenul rețetei, așa că întreb un ASST partener. Deocamdată transmit doar tipul vizitei, nimic din datele dumneavoastră.\nDacă găsim un loc acolo, pentru a-l rezerva va trebui să le transmit datele necesare (rețeta și numele), doar cu acordul dumneavoastră. Dacă sunteți de acord mi-l puteți da de acum; dacă nu, vă propun lista de așteptare.",
        "ar": "لا توجد في ASST الخاصة بنا مواعيد شاغرة لـ {label} ضمن مدة الوصفة، لذلك أستفسر لدى ASST شريكة. حاليًا أشارك نوع الزيارة فقط دون أي من بياناتكم.\nإذا وجدنا موعدًا هناك فسأحتاج لحجزه إلى مشاركة البيانات اللازمة (الوصفة والاسم) بموافقتكم فقط. إن كنتم موافقين يمكنكم إعطائي الموافقة الآن؛ وإن فضلتم غير ذلك فسأقترح عليكم قائمة الانتظار.",
        "zh": "在我们的ASST，处方期限内没有{label}的空位，所以我正在询问合作的ASST。目前我只告知就诊类型，不提供您的任何个人信息。\n如果在那边找到空位，预约时需要向对方提供必要信息（处方和姓名），且必须征得您的同意。如您同意，现在就可以给我授权；如果不同意，我会为您安排候补名单。",
        "uk": "У нашій ASST немає вільних місць для Вашої {label} у межах терміну рецепта, тому я звертаюся до партнерської ASST. Поки що передаю лише тип візиту, жодних Ваших даних.\nЯкщо знайдемо там місце, для запису мені доведеться передати необхідні дані (рецепт та ім'я), лише за Вашою згодою. Якщо Ви згодні, можете дати її вже зараз; якщо ні, я запропоную список очікування.",
    },
    "cross_choice": {
        "it": "Grazie per la scelta. Questo posto è in un'ASST partner: per prenotarlo devo condividere con loro solo i dati necessari alla prenotazione (ricetta e nominativo). Mi dà il suo consenso? Se preferisce di no, cerco un'altra soluzione.",
        "en": "Thank you for your choice. This slot is in a partner ASST: to book it I have to share with them only the data needed for the booking (prescription and name). Do I have your consent? If you prefer not to, I will look for another solution.",
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
        "en": "Thank you for your choice! To book it I need your consent to use the booking data (prescription and name) with the facility.",
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
        "en": "No problem: without your consent I will not book anything. If you change your mind, just write to me and we will pick up from here.",
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
        "it": "Ho ricontrollato: non ci sono altre date libere entro i tempi della ricetta. Le opzioni che le ho mandato restano valide; se nessuna le va bene, posso inserirla in lista d'attesa.",
        "en": "I checked again: there are no other free dates within the time set by the prescription. The options I sent you are still valid; if none of them works, I can put you on the waiting list.",
        "es": "He vuelto a comprobarlo: no hay otras fechas libres dentro del plazo de la receta. Las opciones que le envié siguen siendo válidas; si ninguna le va bien, puedo ponerle en lista de espera.",
        "fr": "J'ai revérifié : il n'y a pas d'autres dates libres dans le délai de l'ordonnance. Les options envoyées restent valables ; si aucune ne vous convient, je peux vous inscrire sur la liste d'attente.",
        "pt": "Voltei a verificar: não há outras datas livres dentro do prazo da receita. As opções que lhe enviei continuam válidas; se nenhuma lhe convier, posso inscrevê-lo na lista de espera.",
        "de": "Ich habe noch einmal nachgesehen: Innerhalb der Rezeptfrist gibt es keine weiteren freien Termine. Die gesendeten Optionen gelten weiterhin; wenn keine passt, kann ich Sie auf die Warteliste setzen.",
        "ro": "Am verificat din nou: nu există alte date libere în termenul rețetei. Opțiunile trimise rămân valabile; dacă nu vă convine niciuna, vă pot înscrie pe lista de așteptare.",
        "ar": "تحققت مرة أخرى: لا توجد مواعيد شاغرة أخرى ضمن مدة الوصفة. الخيارات التي أرسلتها ما زالت صالحة؛ وإن لم يناسبكم أي منها يمكنني إدراجكم في قائمة الانتظار.",
        "zh": "我又查了一遍：处方期限内没有其他空位。之前发给您的选项仍然有效；如果都不合适，我可以为您登记候补名单。",
        "uk": "Я перевірив ще раз: інших вільних дат у межах терміну рецепта немає. Надіслані варіанти залишаються дійсними; якщо жоден не підходить, можу внести Вас до списку очікування.",
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
        "en": "Thank you, your prescription for a {label} is valid and I have noted the days you prefer ({days}). One last thing: do you prefer a female or male doctor? If it makes no difference, just tell me and I will look for the first date.",
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
        "en": "Thank you, your prescription for a {label} is valid and I have noted that you prefer a female doctor. One last thing: are there days of the week that suit you best? If any day is fine, just tell me and I will look for the first date.",
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
        "en": "Thank you, your prescription for a {label} is valid and I have noted that you prefer a male doctor. One last thing: are there days of the week that suit you best? If any day is fine, just tell me and I will look for the first date.",
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
        "en": "3. Bring your prescription and an identity document.",
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


def _lang_code(raw: str) -> str:
    code = raw.strip().lower().replace("_", "-").split("-")[0]
    aliases = {"ua": "uk", "cn": "zh", "ar": "ar", "eng": "en", "ita": "it", "esp": "es", "spa": "es", "fra": "fr", "por": "pt", "deu": "de", "ger": "de", "rum": "ro", "ron": "ro"}
    return aliases.get(code, code)


def tr(key: str, lang: str, **kwargs: object) -> str:
    """Una frase del catalogo nella lingua del cittadino (ripiego: inglese, poi italiano)."""

    table = _TX.get(key, {})
    template = table.get(lang) or table.get("en") or table.get("it") or key
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        return template


# --- ingresso ------------------------------------------------------------------------------------


async def main(need: Need, city: City) -> None:
    message = city.inbound
    if message is None:
        return
    if message.sender == city.agent_id:
        return
    text = message.text or need.summary or ""

    # Emergenza dichiarata dall'harness o dal need: ci si ferma e si escala.
    body = understood(message).body if message.type == "chat" else message.body
    if (need.is_emergency or body.get("is_emergency") is True) and message.sender != LEDGER:
        await _say(city, message.sender, _escalation_text(_lang_of_text(text)))
        return

    # Rifiuto solo una vera domanda clinica (diagnosi, dose) che non porta nulla da prenotare.
    # Una frase come "il medico mi ha fatto la diagnosi, ecco la ricetta" è un'affermazione:
    # si prenota e si ricorda con garbo che sul piano clinico decide il medico.
    if (
        not message.sender.startswith("agent:")
        and message.sender != LEDGER
        and clinical(text)
        and _ASKS_CLINICAL.search(text)
        and not _has_booking_tokens(text)
    ):
        raise OutOfScope()

    try:
        chat = await city.history(need_id=message.need_id)
    except CapError:
        chat = []
    if all(row.msg_id != message.msg_id for row in chat):
        chat = [*chat, message]

    street = _own_street(city)
    if street and _asks_address(message):
        await _say(city, message.sender, f"address={street}")
        return
    try:
        await _desk(city, message, chat)
    except (OutOfScope, asyncio.CancelledError):
        raise
    except Exception:  # noqa: BLE001 - un errore di logica non deve lasciare il caso fermo
        _LOG.exception("booking.turn.error", extra={"need_id": (message.need_id or "")[:48]})


# --- lettura del testo ----------------------------------------------------------------------------


def words(text: str) -> set[str]:
    return {token.strip(".,;:!?()\"'«»").lower() for token in text.split() if token.strip()}


def first_word(text: str) -> str:
    parts = text.split()
    return parts[0].strip(".,;:!?()\"'").lower() if parts else ""


def line_heads(text: str) -> set[str]:
    """Il primo token di ogni riga: è lì che scriviamo (e cerchiamo) la parola dell'atto."""

    return {first_word(line) for line in text.splitlines() if line.strip()}


def fields_of(text: str) -> dict[str, str]:
    """Primo valore di ogni ``chiave=valore`` nel testo."""

    found: dict[str, str] = {}
    # Separo solo sugli spazi: "days=lun,mar,mer" resta una lista; "a=1, b=2" funziona perché
    # la virgola finale viene tolta dal valore.
    for token in text.replace(";", " ").split():
        key, eq, value = token.partition("=")
        key = key.strip("([{\"'«")
        if not eq or not key or not value:
            continue
        value = value.strip(".,;:!?)]}\"'»")
        if key not in found and value:
            found[key] = value
    return found


def _norm(text: str) -> str:
    return " ".join(text.lower().replace("’", "'").split())


def _has_booking_tokens(text: str) -> bool:
    fields = fields_of(text)
    if any(key in fields for key in ("prescription_number", "offer_id", "slot_id", "consent_ref")):
        return True
    return bool(_NUMBER_BARE.search(text) or _NUMBER_RX.search(text))


def _number_in(text: str) -> str | None:
    """Il numero di ricetta in una frase: come token, oppure scritto così com'è."""

    number = fields_of(text).get("prescription_number")
    if number:
        return number
    for pattern in (_NUMBER_BARE, _NUMBER_RX):
        hit = pattern.search(text)
        if hit:
            return hit.group(1).rstrip(".,;:!?-")
    return None


def _red_flag(text: str) -> bool:
    lowered = _norm(text)
    if fields_of(text).get("is_emergency", "").lower() == "true":
        return True
    return any(flag in lowered for flag in _RED_FLAGS)


def _injection(text: str) -> bool:
    lowered = _norm(text)
    return any(marker in lowered for marker in _INJECTION)


def _when(value: str) -> datetime | None:
    """Un istante ISO. Senza fuso orario lo leggo come UTC, così agenda e slot restano
    confrontabili anche se il Ledger li scrive in forma breve."""

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _day_code(value: str) -> str:
    return _DAY_CODES.get(value.strip().lower()[:3], "")


def _split(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[/,|;+]", value or "") if part.strip()]


def _lang_of_text(text: str) -> str:
    """Lingua per parlare al cittadino: il token lang= se c'è, altrimenti la riconosco."""

    raw = fields_of(text).get("lang", "")
    if raw:
        code = _lang_code(raw)
        return code if code in _LANGS else "en"
    return _detect_lang(text) or "it"


# --- storia del caso ------------------------------------------------------------------------------


def _citizen(chat: list[Message]) -> str | None:
    for row in chat:
        if row.sender.startswith("citizen:"):
            return row.sender
    for row in chat:
        for key in ("citizen", "patient"):
            party = fields_of(row.text).get(key, "")
            if party.startswith("citizen:"):
                return party
    return None


def _from_citizen(chat: list[Message]) -> list[Message]:
    return [row for row in chat if row.sender.startswith("citizen:")]


def _ours(chat: list[Message], agent: str, to: str | None = None) -> list[Message]:
    return [
        row for row in chat if row.sender == agent and (to is None or row.recipient == to)
    ]


def _we(chat: list[Message], agent: str, token: str, *, to: str, head: str) -> bool:
    """Abbiamo già mandato a ``to`` una riga che inizia con ``head`` e contiene ``token``."""

    for row in _ours(chat, agent, to):
        for line in row.text.splitlines():
            if first_word(line) == head and (not token or token in line.split()):
                return True
    return False


def _sent_exact(chat: list[Message], agent: str, to: str, text: str) -> bool:
    return any(row.text.strip() == text.strip() for row in _ours(chat, agent, to))


def _ledger(chat: list[Message], head: str) -> dict[str, str] | None:
    found: dict[str, str] | None = None
    for row in chat:
        if row.sender == LEDGER and first_word(row.text) == head:
            found = fields_of(row.text)
    return found


def _ledger_text(chat: list[Message], head: str) -> str | None:
    found: str | None = None
    for row in chat:
        if row.sender == LEDGER and first_word(row.text) == head:
            found = row.text
    return found


def _refusal_for(chat: list[Message], op: str, token: str = "") -> str | None:
    """Testo dell'ultimo rifiuto del Ledger per ``op`` (e, se dato, per ``token``)."""

    found: str | None = None
    for row in chat:
        if row.sender != LEDGER or first_word(row.text) != "refused":
            continue
        if op not in words(row.text):
            continue
        if token and token not in row.text:
            continue
        found = row.text
    return found


def _number(chat: list[Message]) -> str | None:
    """L'ultimo numero di ricetta detto dal cittadino (vince il più recente: è il rinnovo)."""

    for row in reversed(_from_citizen(chat)):
        number = _number_in(row.text)
        if number:
            return number
    return None


def _lang(chat: list[Message]) -> str:
    """La lingua del cittadino: il token lang= se c'è; altrimenti quella riconosciuta dal testo
    (livello 3: chi scrive in inglese senza dichiararlo deve avere un medico che parla inglese)."""

    for row in _from_citizen(chat):
        lang = fields_of(row.text).get("lang", "")
        if lang:
            return _lang_code(lang)
    for row in reversed(_from_citizen(chat)):
        found = _detect_lang(row.text)
        if found:
            return found
    return "it"


def _tone(chat: list[Message]) -> str:
    """La lingua in cui scrivo al cittadino: quella dichiarata, altrimenti quella in cui scrive.
    Una lingua non in catalogo riceve l'inglese, più comprensibile dell'italiano per chi viene
    da fuori."""

    declared = ""
    for row in _from_citizen(chat):
        declared = fields_of(row.text).get("lang", "") or declared
        if declared:
            break
    if declared:
        code = _lang_code(declared)
        return code if code in _LANGS else "en"
    for row in reversed(_from_citizen(chat)):
        found = _detect_lang(row.text)
        if found:
            return found
    return "it"


def _lang_line(chat: list[Message]) -> str:
    """Non serve più: con le 10 lingue di Caterina le frasi sono già nella lingua del cittadino."""

    return ""


def _days_from(text: str) -> set[str] | None:
    """Giorni preferiti: dal token ``days=`` o dalle parole della frase. None = non detto."""

    raw = fields_of(text).get("days") or fields_of(text).get("day")
    if raw:
        found = {
            part[:3].lower()
            for part in re.split(r"[/,|+]", raw)
            if part[:3].lower() in _DAYS
        }
        if found:
            return found
    lowered = _norm(text)
    found = set()
    for code, names in _DAY_WORDS:
        for name in names:
            if len(name) <= 3:
                if re.search(rf"\b{name}\b", lowered):
                    found.add(code)
            elif name in lowered:
                found.add(code)
    return found or None


def _gender_from(text: str) -> str | None:
    fields = fields_of(text)
    raw = (fields.get("gender") or fields.get("gender_preference") or "").lower()
    if raw in {"m", "f"}:
        return raw
    if raw in {"female", "donna", "woman"}:
        return "f"
    if raw in {"male", "uomo", "man"}:
        return "m"
    lowered = _norm(text)
    if re.search(r"\b(dottoressa|medica|una donna|medico donna|femmina|female|woman doctor|a woman|doctora|una mujer|une femme|femme médecin|doctoresse|uma mulher|ärztin|eine frau|doctoriță|doctorita|o femeie)\b", lowered) or any(w in lowered for w in ("طبيبة", "女医生", "лікарк")):
        return "f"
    if re.search(r"\b(un uomo|medico uomo|maschio|male doctor|a man|un hombre|un homme|um homem|einen arzt|ein mann|un bărbat|un barbat)\b", lowered) or any(w in lowered for w in ("طبيب رجل", "男医生", "лікаря-чоловіка")):
        return "m"
    return None


def _asked_prefs_at(chat: list[Message], agent: str, citizen: str) -> int | None:
    for index, row in enumerate(chat):
        if row.sender == agent and row.recipient == citizen and "data.request" in line_heads(row.text):
            if "purpose=booking" in row.text and (
                "fields=days" in row.text or "fields=gender" in row.text
            ):
                return index
    return None


def _preferences(
    chat: list[Message], agent: str, citizen: str
) -> tuple[set[str] | None, str | None, bool]:
    """Giorni, genere e se il cittadino ha già risposto (anche con un no detto a parole).

    I token ``days=``/``gender=`` valgono sempre. Le parole ("il martedì", "una dottoressa")
    valgono solo dopo che le abbiamo chieste: "ho mal di schiena da martedì" non è una
    preferenza."""

    asked = _asked_prefs_at(chat, agent, citizen)
    days: set[str] | None = None
    gender: str | None = None
    answered = False
    for index, row in enumerate(chat):
        if not row.sender.startswith("citizen:"):
            continue
        after = asked is not None and index > asked
        fields = fields_of(row.text)
        said_days, said_gender = _natural_prefs(row.text)  # "vorrei il martedì", "ideally a man"
        if after or "days" in fields or "day" in fields:
            days = _days_from(row.text) or days
        elif said_days:
            days = said_days
        if after or "gender" in fields or "gender_preference" in fields:
            gender = _gender_from(row.text) or gender
        elif said_gender:
            gender = said_gender
        if after:
            answered = True
    answered = answered or days is not None or gender is not None
    return days, gender, answered


# --- mondo: slot e medici --------------------------------------------------------------------------


def _world_lines(chat: list[Message]) -> tuple[list[str], int, int]:
    """Righe di slot e medici dalle risposte ``slots``/``world`` del Ledger, più pagina e totale."""

    lines: list[str] = []
    page, pages = 1, 1
    seen = False
    for row in chat:
        if row.sender != LEDGER or first_word(row.text) not in {"slots", "world"}:
            continue
        seen = True
        head = row.text.splitlines()[0] if row.text else ""
        raw = fields_of(head).get("page", "")
        left, _, right = raw.partition("/")
        if left.isdigit() and right.isdigit():
            page, pages = max(page, int(left)), int(right)
        lines.extend(line for line in row.text.splitlines() if "=" in line)
    return (lines if seen else []), page, pages if seen else 0


def _has_world(chat: list[Message]) -> bool:
    return any(row.sender == LEDGER and first_word(row.text) in {"slots", "world"} for row in chat)


def _doctors(lines: list[str]) -> dict[str, tuple[frozenset[str], str, frozenset[str]]]:
    """Medici descritti dal mondo (righe con doctor_id e senza slot_id)."""

    found: dict[str, tuple[frozenset[str], str, frozenset[str]]] = {}
    for line in lines:
        fields = fields_of(line)
        doctor = fields.get("doctor_id") or fields.get("doctor")
        if not doctor or "slot_id" in fields:
            continue
        langs = fields.get("languages") or fields.get("langs") or fields.get("lang") or ""
        days = fields.get("days", "")
        found[doctor] = (
            frozenset(_lang_name(part) for part in _split(langs)),
            fields.get("gender", "").lower()[:1],
            frozenset(code for code in (_day_code(part) for part in _split(days)) if code),
        )
    return found


def _doctor_key(doctor: str) -> str:
    """La parte finale del codice medico, in minuscolo e senza zeri: "asst-navigli-S1",
    "asst_navigli:s1", "S01" diventano tutti "s1"."""

    parts = [part for part in re.split(r"[-_:/.\s]+", doctor.strip().lower()) if part]
    key = parts[-1] if parts else ""
    return re.sub(r"^([a-z]+)0+(\d)", r"\1\2", key)


def _lang_name(raw: str) -> str:
    """Una lingua scritta come codice o come nome ("EN", "eng", "english", "inglese") -> "en"."""

    name = raw.strip().strip("[]{}()\"' ").lower()
    names = {
        "english": "en", "inglese": "en", "italian": "it", "italiano": "it", "spanish": "es",
        "spagnolo": "es", "french": "fr", "francese": "fr", "german": "de", "tedesco": "de",
        "portuguese": "pt", "portoghese": "pt", "romanian": "ro", "rumeno": "ro", "arabic": "ar",
        "arabo": "ar", "chinese": "zh", "cinese": "zh", "ukrainian": "uk", "ucraino": "uk",
    }
    return names.get(name) or (_lang_code(name) if name != "*" else "*")


def _spec(doctor: str, known: dict[str, tuple[frozenset[str], str, frozenset[str]]]):
    """Lingue, genere e giorni di un medico: prima dal mondo (stesso codice, anche con
    maiuscole o separatori diversi), poi dalla tabella di ripiego."""

    if doctor in known:
        return known[doctor]
    key = _doctor_key(doctor)
    for name, spec in known.items():
        if name.lower() == doctor.lower() or _doctor_key(name) == key:
            return spec
    return _DOCTORS.get(key)


def _speaks(langs: frozenset[str], lang: str) -> bool:
    if not langs:
        return lang in {"it", ""}
    if lang in langs:
        return True
    return "*" in langs and lang not in {"it", "en"}


def _horizon(now: datetime, priority: str | None, extend: bool = False) -> datetime | None:
    """Scadenza della priorità. ``extend``: conto i giorni di agenda (senza sabato e domenica,
    che nell'agenda non esistono): giovedì + 2 arriva a lunedì invece che a sabato."""

    if priority == "U":
        return now.replace(hour=23, minute=59, second=59, microsecond=0)
    if priority == "P":
        return None
    days = _DEADLINE_DAYS.get(priority or "", 14)
    if not extend:
        return now + timedelta(days=days)
    end, counted = now, 0
    while counted < days:
        end += timedelta(days=1)
        if end.weekday() < 5:
            counted += 1
    return end


def _page_spans(chat: list[Message]) -> tuple[dict[int, tuple[datetime | None, datetime | None]], int]:
    """Per ogni pagina del mondo già letta: primo e ultimo orario. Più il numero di pagine."""

    spans: dict[int, tuple[datetime | None, datetime | None]] = {}
    total = 0
    for row in chat:
        if row.sender != LEDGER or first_word(row.text) not in {"slots", "world"}:
            continue
        head = row.text.splitlines()[0] if row.text else ""
        left, _, right = fields_of(head).get("page", "1/1").partition("/")
        page = int(left) if left.isdigit() else 1
        total = max(total, int(right) if right.isdigit() else 1)
        times = [
            _when(slot["at"])
            for slot in (normalize_slot(fields_of(line)) for line in row.text.splitlines())
            if slot is not None
        ]
        times = [at for at in times if at is not None]
        spans[page] = (min(times), max(times)) if times else (None, None)
    return spans, total


def _pages_sorted(spans: dict[int, tuple[datetime | None, datetime | None]]) -> bool | None:
    """True se le pagine lette sono in ordine di data, False se non lo sono, None se non si
    può ancora dire (una sola pagina con orari)."""

    known = sorted((page, span) for page, span in spans.items() if span[0] is not None)
    if len(known) < 2:
        return None
    for (_, (_, last)), (_, (first, _)) in zip(known, known[1:]):
        if first is not None and last is not None and first < last:
            return False
    return True


def _next_page(chat: list[Message], earliest: datetime, limit: datetime | None) -> int | None:
    """Quale pagina chiedere adesso.

    Se le pagine sono in ordine di data (verificato su quelle lette) salto con la ricerca
    binaria alla prima con orari da ``earliest`` in poi e proseguo fino a ``limit``. Se non lo
    sono, o non si può ancora dire, le leggo una dopo l'altra: mai fermarsi su un'ipotesi."""

    spans, total = _page_spans(chat)
    if not spans:
        return 1
    order = _pages_sorted(spans)
    unseen = [page for page in range(1, total + 1) if page not in spans]
    if not unseen:
        return None
    if order is False:
        return unseen[0]
    past = [p for p, (_, last) in spans.items() if last is not None and last < earliest]
    future = sorted(p for p, (_, last) in spans.items() if last is not None and last >= earliest)
    low = max(past, default=0)
    if order is True and (not future or future[0] > low + 1):
        high = future[0] if future else total + 1
        wanted = max((low + high) // 2, low + 1)
        while wanted in spans and wanted < high:
            wanted += 1
        if wanted <= total and wanted not in spans:
            return wanted
    if order is None and not future:
        # Solo pagine passate finora: provo a metà; se l'ordine non torna, il giro dopo lo vede.
        wanted = max((low + total + 1) // 2, low + 1)
        while wanted in spans and wanted < total:
            wanted += 1
        return wanted if wanted not in spans else unseen[0]
    page = future[0] if future else (low + 1)
    while page in spans:
        _, last = spans[page]
        if order is True and limit is not None and last is not None and last >= limit:
            return None  # in ordine di data e oltre la scadenza: inutile sfogliare ancora
        page += 1
    return page if page <= total else (unseen[0] if order is not True else None)


def normalize_slot(raw: dict[str, str]) -> dict[str, str] | None:
    """Uno slot del registro con chiavi uniformi: slot_id, doctor_id, at, day, facility,
    procedure, reserve. None se manca qualcosa di essenziale."""

    slot = dict(raw)
    slot_id = slot.get("slot_id") or slot.get("slot")
    doctor = slot.get("doctor_id") or slot.get("doctor")
    when = slot.get("at") or slot.get("agenda_date") or slot.get("start") or slot.get("datetime")
    if not when and slot.get("date"):
        when = slot["date"] + ("T" + slot["time"] if slot.get("time") else "")
    at = _when(when or "")
    if not slot_id or not doctor or at is None:
        return None
    slot["slot_id"] = slot_id
    slot["doctor_id"] = doctor
    slot["at"] = at.isoformat()
    slot["day"] = _day_code(slot.get("day") or slot.get("agenda_day") or "") or _DAYS[at.weekday()]
    reserve = ""
    for key in ("urgent_reserve", "reserve", "reserved", "riserva", "urgent"):
        if key in slot:
            reserve = slot[key].lower()
            break
    slot["reserve"] = "true" if reserve in {"true", "yes", "si", "sì", "u", "urgent", "urgente", "1"} else ""
    return slot


def fitting(
    lines: list[str],
    *,
    now: datetime,
    priority: str | None,
    lang: str,
    procedure: str | None,
    days: set[str] | None = None,
    gender: str | None = None,
    skip: set[str] | frozenset[str] = frozenset(),
    skip_facilities: set[str] | frozenset[str] = frozenset(),
    extend: bool = False,
) -> list[dict[str, str]]:
    """Slot che il cittadino può accettare: futuri, liberi, entro la priorità, nella sua lingua,
    fuori dalla riserva urgente se la priorità non è U, nei giorni di lavoro del medico e (se
    date) con le sue preferenze. Ordinati dal più vicino."""

    horizon = _horizon(now, priority, extend)
    earliest = now + _LEAD
    known = _doctors(lines)
    chosen: list[tuple[datetime, dict[str, str]]] = []
    seen: set[str] = set()
    for line in lines:
        slot = normalize_slot(fields_of(line))
        if slot is None or slot["slot_id"] in skip or slot["slot_id"] in seen:
            continue
        at = _when(slot["at"])
        assert at is not None
        doctor = slot["doctor_id"]
        if slot.get("facility", "") in skip_facilities:
            continue
        if procedure and slot.get("procedure") not in (None, procedure):
            continue
        for key in ("free", "seats", "posti", "capacity"):
            value = slot.get(key)
            if value is not None and value.isdigit() and int(value) <= 0:
                break
        else:
            value = None
        if value is not None:
            continue
        if slot.get("state", slot.get("status", "free")).lower() not in {"free", "open", "available", "libero"}:
            continue
        if slot["reserve"] and priority != "U":
            continue  # regola P3: la riserva è solo per la priorità U
        if not earliest < at or (horizon is not None and at >= horizon):
            continue
        if not 8 <= at.hour < 18:
            continue  # ambulatori aperti 08-18
        spec = _spec(doctor, known)
        langs = frozenset(_lang_name(part) for part in _split(slot.get("languages", ""))) or (
            spec[0] if spec else frozenset()
        )
        if not _speaks(langs, lang):
            continue
        day = slot["day"]
        if spec and spec[2] and day not in spec[2]:
            continue  # il cittadino verifica i giorni di lavoro del medico sul registro
        if days and day not in days:
            continue
        doc_gender = (slot.get("gender") or (spec[1] if spec else "")).lower()[:1]
        if gender and doc_gender != gender:
            continue
        seen.add(slot["slot_id"])
        chosen.append((at, slot))
    chosen.sort(key=lambda item: (item[0], item[1].get("slot_id", "")))
    return [slot for _, slot in chosen]


def _own_facilities(lines: list[str]) -> set[str]:
    """Le strutture della nostra ASST: le etichette del registro e gli agenti della squadra."""

    own = {fields_of(line)["facility"] for line in lines if "facility" in fields_of(line)}
    return own | set(_team_facilities().values())


# --- testi per il cittadino -------------------------------------------------------------------------


def _label(procedure: str | None, tone: str) -> str:
    table = _LABELS.get(procedure or "", {})
    return table.get(tone) or table.get("en") or procedure or _VISIT.get(tone, "visit")


def _when_text(value: str, tone: str) -> str:
    at = _when(value)
    if at is None:
        return value
    lang = tone if tone in _DATE_FMT else "en"
    weekdays = _WEEKDAYS.get(lang, _WEEKDAYS["en"])
    months = _MONTHS.get(lang, _MONTHS["en"])
    return _DATE_FMT[lang].format(
        wd=weekdays[at.weekday()], d=at.day, m=at.month, month=months[at.month - 1],
        time=at.strftime("%H:%M"),
    )


def _priority_text(priority: str | None, tone: str) -> str:
    if priority not in {"U", "B", "D", "P"}:
        return ""
    return tr(f"prio_{priority}", tone)


def _fit(text: str) -> str:
    text = text.strip()
    return text if len(text) <= _MAX_TEXT else text[: _MAX_TEXT - 1].rstrip() + "…"


def _escalation_text(tone: str) -> str:
    return "escalate.emergency reason=red_flag\n" + tr("escalation", tone)


def _guard_note(tone: str, *, injected: bool, clinical_words: bool) -> str:
    notes = []
    if injected:
        notes.append(tr("guard_injection", tone))
    if clinical_words:
        notes.append(tr("guard_clinical", tone))
    return " ".join(notes)


def _offer_line(slot: dict[str, str], offer_id: str, number: str | None, priority: str | None) -> str:
    at = slot.get("at") or slot.get("agenda_date", "")
    parts = [
        "offer",
        f"offer_id={offer_id}",
        f"slot_id={slot['slot_id']}",
        f"doctor_id={slot['doctor_id']}",
    ]
    if slot.get("facility"):
        parts.append(f"facility={slot['facility']}")
    if slot.get("procedure"):
        parts.append(f"procedure={slot['procedure']}")
    parts += [f"agenda_date={at}", f"agenda_day={slot.get('day', '')}", f"expiry={at}"]
    if priority:
        parts.append(f"priority={priority}")
    if number:
        parts.append(f"prescription_number={number}")
    return " ".join(part for part in parts if not part.endswith("="))


def _offers_text(
    slots: list[dict[str, str]],
    *,
    number: str | None,
    priority: str | None,
    procedure: str | None,
    tone: str,
    intro: str,
    ids: list[str],
) -> str:
    lines = [intro]
    for index, (slot, offer_id) in enumerate(zip(slots, ids), start=1):
        lines.append(tr("option", tone, n=index, when=_when_text(slot.get("at", ""), tone)))
        lines.append(_offer_line(slot, offer_id, number, priority))
    lines.append(tr("choose", tone))
    return _fit("\n".join(lines))


# --- messaggi in uscita -------------------------------------------------------------------------------


async def _say(city: City, to: str, text: str) -> bool:
    """Un solo invio. Un rifiuto (4xx) chiude quella richiesta: non si ritenta mai."""

    try:
        await city.say(to, _fit(text))
    except CapError as err:
        _LOG.info("booking.say.refused", extra={"code": getattr(err, "code", ""), "to": to[:64]})
        return False
    return True


async def _say_or_code(city: City, to: str, text: str) -> str:
    """Come ``_say`` ma restituisce il codice di errore ("" se l'invio è andato)."""

    try:
        await city.say(to, _fit(text))
    except CapError as err:
        return str(getattr(err, "code", "") or "REFUSED")
    return ""


async def _until(
    city: City,
    need_id: str | None,
    chat: list[Message],
    ready: Callable[[list[Message]], bool],
) -> list[Message]:
    """Rilegge il caso finché ``ready`` o finché la breve attesa finisce."""

    fresh = chat
    for attempt in range(_PULL_TRIES):
        try:
            fresh = await city.history(need_id=need_id)
        except CapError:
            fresh = chat
        if ready(fresh):
            return fresh
        if attempt + 1 < _PULL_TRIES:
            await asyncio.sleep(_PULL_PAUSE)
    return fresh


# --- memoria di processo e lock per caso -----------------------------------------------------------------


class _Gate:
    __slots__ = ("lock", "turns")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.turns = 0


class _Sent:
    """Cosa abbiamo già mandato per un caso, anche se la storia non lo mostra ancora."""

    __slots__ = ("offers", "lines", "flags")

    def __init__(self) -> None:
        self.offers: set[str] = set()  # slot_id (o offer_id della lista d'attesa) offerti
        self.lines: set[str] = set()  # chiavi di messaggi una-tantum
        self.flags: set[str] = set()


_GATES: dict[str, _Gate] = {}
_SENT: OrderedDict[str, _Sent] = OrderedDict()
_ADDRESSES: dict[str, str] = {}


def _sent(key: str) -> _Sent:
    entry = _SENT.get(key)
    if entry is None:
        entry = _SENT[key] = _Sent()
        while len(_SENT) > _SENT_NEEDS:
            _SENT.popitem(last=False)
    else:
        _SENT.move_to_end(key)
    return entry


async def _once(city: City, key: str, tag: str, to: str, text: str) -> bool:
    """Manda ``text`` una sola volta per caso e per ``tag``."""

    sent = _sent(key)
    if tag in sent.lines:
        return False
    sent.lines.add(tag)
    return await _say(city, to, text)


async def _locked(key: str, work: Callable[[], object]) -> None:
    gate = _GATES.get(key)
    if gate is None:
        gate = _GATES[key] = _Gate()
    gate.turns += 1
    try:
        async with gate.lock:
            await work()  # type: ignore[misc]
    finally:
        gate.turns -= 1
        if gate.turns == 0 and _GATES.get(key) is gate:
            del _GATES[key]


# --- strutture: dall'etichetta del registro all'agente ---------------------------------------------


def _team_facilities() -> dict[str, str]:
    table = dict(_TEAM_FACILITIES)
    for part in os.environ.get("CITY_TEAM_FACILITIES", "").split(","):
        key, _, value = part.partition("=")
        if key.strip() and value.strip().startswith("agent:"):
            table[key.strip()] = value.strip()
    return table


async def _facility_agent(city: City, facility: str, procedure: str | None) -> str:
    """L'agente a cui mandare il ``confirm``. Il registro può nominare la struttura per tipo
    ("hospital", "asst-navigli-hospital"): in quel caso uso l'agente della nostra ASST."""

    if facility.startswith("agent:"):
        return facility
    table = _team_facilities()
    lowered = facility.lower()
    family = next((key for key in ("family_counseling", "specialist", "hospital") if key in lowered), "")
    if not family and ("counsel" in lowered or "consult" in lowered):
        family = "family_counseling"
    if not family:
        family = _FAMILY_OF.get(procedure or "", "")
    known = table.get(family, "")
    service = _SERVICE_OF.get(procedure or "", "")
    if service:
        try:
            peers = await city.find(service)
        except CapError:
            peers = []
        ids = {peer.agent_id for peer in peers}
        if known and known in ids:
            return known
        # Ripiego: un agente con lo stesso nome della struttura del registro.
        for peer in peers:
            if lowered and lowered in peer.agent_id.lower():
                return peer.agent_id
    return known or facility


def _confirmed_slot(chat: list[Message], agent: str, slot: str) -> str:
    """A chi abbiamo già girato il ``confirm`` di questo slot ("" se a nessuno)."""

    token = f"slot_id={slot}"
    for row in _ours(chat, agent):
        for line in row.text.splitlines():
            if first_word(line) == "confirm" and token in line.split():
                return row.recipient
    return ""


async def _ack(city: City, message: Message, key: str, text: str) -> None:
    """Risposta breve e immediata a un altro agente (collaborazione e fast-reply). Niente parole
    d'atto né codici di prestazione, così nessun brain la legge come una nuova richiesta."""

    await _once(city, key, f"ack:{message.msg_id}", message.sender, text)


# --- smistamento ---------------------------------------------------------------------------------------


def _our_offers(chat: list[Message], agent: str, citizen: str) -> dict[str, dict[str, str]]:
    """offer_id -> campi, per ogni offerta che abbiamo mandato al cittadino."""

    found: dict[str, dict[str, str]] = {}
    for row in _ours(chat, agent, citizen):
        for line in row.text.splitlines():
            if first_word(line) != "offer":
                continue
            fields = fields_of(line)
            if fields.get("offer_id"):
                found[fields["offer_id"]] = fields
    return found


def _chosen_offer(text: str, offers: dict[str, dict[str, str]]) -> str | None:
    """L'offerta che il cittadino ha nominato: come token, come slot, o scritta così com'è."""

    fields = fields_of(text)
    if fields.get("offer_id") in offers:
        return fields["offer_id"]
    slot = fields.get("slot_id")
    if slot:
        for offer_id, offer in offers.items():
            if offer.get("slot_id") == slot:
                return offer_id
    bare = words(text)
    for offer_id in sorted(offers, key=len, reverse=True):
        if offer_id.lower() in bare or offer_id in text:
            return offer_id
    return _natural_choice(text, offers)


# --- livello 3: richieste in linguaggio naturale -------------------------------------------------

_ORDINALS = (
    (0, r"prim[ao]|first|1st|opzione\s*1|option\s*1|numero\s*1"),
    (1, r"second[ao]|second|2nd|opzione\s*2|option\s*2|numero\s*2"),
    (2, r"terz[ao]|third|3rd|opzione\s*3|option\s*3|numero\s*3"),
    (-1, r"ultim[ao]|the\s+last(?:\s+one)?"),
)
_MONTH_NUM = {
    name.lower(): index
    for names in (_MONTHS["it"], _MONTHS["en"])
    for index, name in enumerate(names, start=1)
}


def _offer_at(offer: dict[str, str]) -> datetime | None:
    return _when(offer.get("agenda_date", ""))


def _natural_choice(text: str, offers: dict[str, dict[str, str]]) -> str | None:
    """L'offerta indicata a parole: "la seconda", "quella di mercoledì", "alle 10", "il 17
    novembre". Solo se la frase indica UNA offerta e non chiede altre date; altrimenti None (e
    il CUP chiede quale, invece di indovinare)."""

    lowered = _norm(text)
    if not offers or _WANTS_MORE.search(lowered):
        return None
    slots = [oid for oid, offer in offers.items() if not offer.get("waiting_list")]
    waiting = [oid for oid, offer in offers.items() if offer.get("waiting_list")]
    if not slots:
        # Solo la lista d'attesa sul tavolo: un sì a parole la sceglie.
        return waiting[-1] if waiting and _choice_words(lowered) else None
    latest = slots[-_MAX_OFFERS:]
    picked: set[str] = set(slots)
    named = ordinal = False
    for index, pattern in _ORDINALS:
        if re.search(rf"\b(?:{pattern})\b", lowered):
            picked &= {latest[index]} if -len(latest) <= index < len(latest) else set()
            named = ordinal = True
            break
    days = {code for code, names in _DAY_WORDS for name in names if len(name) > 3 and name in lowered}
    if days:
        picked &= {
            oid for oid in slots
            if (at := _offer_at(offers[oid])) is not None and _DAYS[at.weekday()] in days
        }
        named = True
    hour = re.search(r"\b(?:alle|ore|at)\s*(\d{1,2})(?:[:.](\d{2}))?\b|\b(\d{1,2})[:.](\d{2})\b", lowered)
    if hour:
        h = int(hour.group(1) or hour.group(3))
        m = int(hour.group(2) or hour.group(4) or 0)
        picked &= {
            oid for oid in slots
            if (at := _offer_at(offers[oid])) is not None and (at.hour, at.minute) == (h, m)
        }
        named = True
    date = re.search(r"\b(\d{1,2})\s*(?:/\s*(\d{1,2})\b|\s+([a-z]+))", lowered)
    if date and (date.group(2) or date.group(3) in _MONTH_NUM):
        day = int(date.group(1))
        month = int(date.group(2)) if date.group(2) else _MONTH_NUM[date.group(3)]
        picked &= {
            oid for oid in slots
            if (at := _offer_at(offers[oid])) is not None and (at.day, at.month) == (day, month)
        }
        named = True
    if not named or len(picked) != 1:
        return None
    if not (ordinal or _choice_words(lowered) or re.search(r"\b(quell[ao]|that one|the one)\b", lowered)):
        return None  # "martedì?" da solo è una preferenza, non una scelta
    return picked.pop()


# Una preferenza scritta a parole: "vorrei il martedì", "ideally on Monday", "una dottoressa".
_PREF_MARK = re.compile(
    r"\b(preferisc\w*|preferirei|vorrei|se possibile|se si può|se si puo|possibilmente|"
    r"preferibilmente|tipo|meglio|magari|ideally|prefer\w*|would like|"
    r"i'd like|rather|if possible|solo (?:il|di)|male doctor|female doctor|medico donna|"
    r"medico uomo)\b"
)
_SINCE_DAY = re.compile(
    r"\b(?:da|dal|dallo|since|from|scorso|scorsa|last)\s+(?:\w+\s+)?"
    r"(?:luned\w*|marted\w*|mercoled\w*|gioved\w*|venerd\w*|monday|tuesday|wednesday|thursday|friday)"
)


def _natural_prefs(text: str) -> tuple[set[str] | None, str | None]:
    """Giorni e genere detti a parole, solo se la frase li presenta come preferenza. "Mal di
    schiena da martedì" non è una preferenza di giorno."""

    lowered = _norm(text)
    if not _PREF_MARK.search(lowered):
        return None, None
    return _days_from(_SINCE_DAY.sub(" ", lowered)), _gender_from(lowered)


def _choice_words(text: str) -> bool:
    lowered = _norm(text)
    return bool(
        re.search(
            r"\b(scelgo|prendo|confermo|accetto|va bene|ok|d'accordo|perfetto|la prima|la seconda|"
            r"la terza|opzione|i choose|i'll take|i accept|fine|yes|sì|si|elijo|acepto|vale|"
            r"je choisis|d'accord|oui|escolho|aceito|sim|ich nehme|einverstanden|ja|aleg|"
            r"accept|da)\b",
            lowered,
        )
    )


_WANTS_MORE = re.compile(
    r"\b(nessuna|non (mi )?va(nno)? bene|altre date|altri giorni|altro giorno|altra data|"
    r"più avanti|piu avanti|none of (them|these)|other dates|another (day|date)|later date|"
    r"ninguna|otras fechas|aucune|autres dates|nenhuma|outras datas|keine davon|andere termine|"
    r"niciuna|alte date)\b",
    re.IGNORECASE,
)


async def _desk(city: City, message: Message, chat: list[Message]) -> None:
    sender = message.sender
    text = message.text
    heads = line_heads(text) | words(text)
    key = message.need_id or message.msg_id

    if sender == LEDGER:
        await _locked(key, lambda: _advance(city, message, chat, key, replacing=False))
        return

    if sender.startswith("agent:") or sender.startswith("city:"):
        # Strutture (nostre o partner) e avvisi della città. Una ricevuta la accetto solo da
        # un agente: niente ricevute fantasma.
        agent_sender = sender.startswith("agent:")
        if "booking.receipt" in heads and agent_sender:
            await _ack(city, message, key, "Grazie, ricevuto: giro subito la ricevuta al cittadino.")
            await _forward_receipt(city, message, chat, key)
        elif heads & {"cancel", "refused", "fault.cancel", "expired"}:
            if agent_sender:
                await _ack(city, message, key, "Grazie dell'avviso: aggiorno il cittadino e cerco subito un'alternativa.")
            await _locked(key, lambda: _facility_cancel(city, message, chat, key))
        elif "offer" in line_heads(text) and agent_sender:
            await _ack(city, message, key, "Grazie per la disponibilità: la propongo subito al cittadino.")
            await _locked(key, lambda: _partner_offer(city, message, chat, key))
        elif "need.request" in heads and agent_sender:
            await _locked(key, lambda: _serve_partner(city, message, chat, key))
        return

    if not sender.startswith("citizen:"):
        return

    tone = _tone(chat)
    if _red_flag(text):
        if not _we(chat, city.agent_id, "", to=sender, head="escalate.emergency"):
            await _once(city, key, "escalate", sender, _escalation_text(tone))
        return

    offers = _our_offers(chat, city.agent_id, sender)
    chosen = _chosen_offer(text, offers) if offers else None
    fields = fields_of(text)
    is_consent = "consent" in heads or bool(fields.get("consent_ref") or _CONSENT_BARE.search(text))

    if chosen or ("confirm" in heads and offers):
        await _locked(key, lambda: _on_confirm(city, message, chat, key, chosen))
        return
    if is_consent and _pending_choice(chat, city.agent_id, sender):
        await _locked(key, lambda: _on_confirm(city, message, chat, key, None))
        return
    if _pending_choice(chat, city.agent_id, sender) and (
        _we(chat, city.agent_id, "purpose=booking", to=sender, head="consent")
        or _we(chat, city.agent_id, "purpose=cross_asst", to=sender, head="consent")
    ):
        # Risposta a parole alla nostra richiesta di consenso: la gestisce _on_confirm
        # (un no detto a parole = niente prenotazione e niente dati fuori).
        await _locked(key, lambda: _on_confirm(city, message, chat, key, None))
        return
    if offers and not _number_in(text):
        new_prefs = _days_from(text) is not None or _gender_from(text) is not None
        strong = re.search(
            r"\b(prendo|scelgo|confermo|quell[ao]|la prima|la seconda|la terza|opzione|"
            r"i'll take|i choose|that one|the one)\b", _norm(text),
        )
        if not _WANTS_MORE.search(text) and (strong or (_choice_words(text) and not new_prefs)):
            # Una scelta a parole che non indica un'offerta sola ("va bene", "quella di
            # martedì" con due martedì): chiedo quale, senza indovinare.
            await _ask_which(city, sender, offers, tone)
            return
        if _WANTS_MORE.search(text) or new_prefs:
            await _locked(
                key, lambda: _advance(city, message, chat, key, replacing=False, refresh=True)
            )
            return
    await _locked(key, lambda: _advance(city, message, chat, key, replacing=False))


async def _ask_which(city: City, citizen: str, offers: dict[str, dict[str, str]], tone: str) -> None:
    listed = [
        f"- {_when_text(offer.get('agenda_date', ''), tone)}: offer_id={offer_id}"
        for offer_id, offer in offers.items()
        if not offer.get("waiting_list")
    ][-_MAX_OFFERS:]
    await _say(city, citizen, "\n".join([tr("ask_which", tone), *listed]))


# --- il percorso principale --------------------------------------------------------------------------------


def _prescription_ok(chat: list[Message], number: str) -> tuple[str, dict[str, str]]:
    """Esito della verifica: ``pending``, ``ok``, ``expired``, ``not_found``, ``not_yours``,
    ``used``."""

    token = f"prescription_number={number}"
    reply: dict[str, str] | None = None
    for row in chat:
        if row.sender == LEDGER and first_word(row.text) == "prescription" and (
            token in row.text or number in row.text
        ):
            reply = fields_of(row.text)
    refused = _refusal_for(chat, "prescription", number) or _refusal_for(chat, "prescription")
    if reply is None:
        if refused:
            upper = refused.upper()
            if "EXPIRED" in upper:
                return "expired", {}
            return "not_found", {}
        return "pending", {}
    state = (reply.get("state") or reply.get("status") or "").lower()
    if reply.get("valid", "").lower() == "false" and not state:
        state = "invalid"
    if state in _EXPIRED_STATES:
        return "expired", reply
    if state and state not in _OK_STATES:
        return "used", reply
    return "ok", reply


def _rules_text(chat: list[Message]) -> str:
    return _ledger_text(chat, "rules") or ""


def _p1_active(chat: list[Message]) -> bool:
    raw = _rules_text(chat)
    if not raw:
        return False
    fields = {key.lower(): value.lower() for key, value in fields_of(raw).items()}
    for key in ("p1", "prescription_validity", "prescription_validity_days"):
        if key in fields:
            return fields[key] not in {"off", "false", "inactive", "0", "none"}
    return bool(re.search(r"\bP1\b", raw)) and "inactive" not in raw.lower()


def _expired_by_rule(reply: dict[str, str], now: datetime | None, chat: list[Message]) -> bool:
    expires = _when(reply.get("expires_at") or reply.get("valid_until") or reply.get("expiry") or "")
    if expires is not None and now is not None:
        return now >= expires
    if not _p1_active(chat) or now is None:
        return False
    issued = _when(reply.get("issued_at") or reply.get("issued") or reply.get("created_at") or "")
    return issued is not None and now >= issued + timedelta(days=2)


async def _advance(
    city: City,
    message: Message,
    chat: list[Message],
    key: str,
    *,
    replacing: bool,
    refresh: bool = False,
) -> None:
    """Porta avanti il caso dal punto in cui è. ``replacing``: una struttura ha annullato e il
    cittadino ha perso le offerte, quindi riparto. ``refresh``: il cittadino chiede altre date
    o ha dato preferenze nuove dopo aver visto le offerte."""

    agent = city.agent_id
    citizen = _citizen(chat)
    if citizen is None:
        return
    tone = _tone(chat)
    sent = _sent(key)
    latest = message if message.sender.startswith("citizen:") else None
    note = ""
    if latest is not None:
        note = _guard_note(
            tone,
            injected=_injection(latest.text),
            clinical_words=clinical(latest.text),
        )
    guard = note  # solo gli avvisi, senza la riga di lingua
    if _lang_line(chat):
        note = (_lang_line(chat) + "\n" + note).strip()

    # 1. Il numero di ricetta. Senza, lo chiedo (al massimo due volte, poi aspetto).
    number = _number(chat)
    if number is None:
        if latest is None:
            return
        asks = sum(
            1
            for row in _ours(chat, agent, citizen)
            if "purpose=booking" in row.text and "fields=prescription_number" in row.text
        )
        if asks >= 2:
            return
        prose = tr("ask_number", tone)
        text = "data.request purpose=booking fields=prescription_number\n" + prose
        if note:
            text += "\n" + note
        await _say(city, citizen, text)
        return

    # 2. Verifica della ricetta, agenda e regole in vigore.
    token = f"prescription_number={number}"
    first_contact = not _we(chat, agent, token, to=LEDGER, head="prescription")
    if first_contact and f"rx?{number}" not in sent.lines:
        sent.lines.add(f"rx?{number}")
        await _say(city, LEDGER, f"prescription {token}")
    agendas_before = _count_ledger(chat, "agenda")
    trigger = _trigger(message) if (replacing or refresh) else ""
    asked_agenda = any(row.text.strip() == "agenda" for row in _ours(chat, agent, LEDGER))
    fresh_agenda = bool(trigger) and f"agenda:{trigger}" not in sent.lines
    if (not asked_agenda and "agenda" not in sent.lines) or fresh_agenda:
        sent.lines.add("agenda")
        if trigger:
            sent.lines.add(f"agenda:{trigger}")
        await _say(city, LEDGER, "agenda")
    if "rules" not in sent.lines and not any(
        row.text.strip() == "rules" for row in _ours(chat, agent, LEDGER)
    ):
        sent.lines.add("rules")
        await _say(city, LEDGER, "rules")

    need_new_agenda = fresh_agenda
    chat = await _until(
        city,
        message.need_id,
        chat,
        lambda rows: _prescription_ok(rows, number)[0] != "pending"
        and _count_ledger(rows, "agenda") > (agendas_before if need_new_agenda else 0),
    )
    status, reply = _prescription_ok(chat, number)
    agenda = _ledger(chat, "agenda") or {}
    now = _when(agenda.get("agenda_now") or agenda.get("now") or agenda.get("at") or "")
    if status == "pending":
        return

    # 3. Ricetta non utilizzabile: lo dico chiaramente e indico i passi.
    if status == "ok" and _expired_by_rule(reply, now, chat):
        status = "expired"
    patient = reply.get("patient") or reply.get("citizen") or ""
    if status == "ok" and patient.startswith("citizen:") and patient != citizen:
        status = "not_yours"
    if status != "ok":
        await _prescription_problem(city, key, citizen, number, status, tone)
        return

    priority = (reply.get("priority") or "").upper() or None
    procedure = reply.get("procedure")
    if not procedure:
        return

    # 4. Priorità P: la risposta giusta è la lista d'attesa.
    if priority == "P":
        await _waiting_list(
            city, chat, key, citizen, number, procedure, priority, tone, reason="P", note=note
        )
        return

    # Preferenze: ricetta verificata, le chiedo una volta sola (giorni, genere del medico).
    days, gender, answered = _preferences(chat, agent, citizen)
    asked_at = _asked_prefs_at(chat, agent, citizen)
    asked_prefs = asked_at is not None
    if (
        (days is None) != (gender is None)
        and not asked_prefs
        and not (replacing or refresh)
    ):
        # Una preferenza è già nel primo messaggio: chiedo solo quella che manca.
        if "prefs" in sent.lines:
            return
        sent.lines.add("prefs")
        label = _label(procedure, tone)
        if days is not None:
            field = "gender"
            names = _WEEKDAYS.get(tone, _WEEKDAYS["en"])
            listed = ", ".join(names[_DAYS.index(code)] for code in _DAYS if code in days)
            prose = tr("ask_gender_only", tone, label=label, days=listed)
        else:
            field = "days"
            prose = tr("ask_days_only_f" if gender == "f" else "ask_days_only_m", tone, label=label)
        text = f"data.request purpose=booking fields={field}\n" + prose
        if note:
            text += "\n" + note
        await _say(city, citizen, text)
        return
    if (
        asked_at is not None
        and not replacing
        and not any(row.sender == citizen for row in chat[asked_at + 1 :])
    ):
        return  # ho chiesto la preferenza che mancava: aspetto la risposta
    if days is None and gender is None and not asked_prefs and not (replacing or refresh):
        if "prefs" in sent.lines:
            return
        sent.lines.add("prefs")
        prose = tr("prefs_ok", tone, label=_label(procedure, tone))
        why = _priority_text(priority, tone)
        if why:
            prose += " " + why
        prose += "\n" + tr("prefs_question", tone)
        text = "data.request purpose=booking fields=days,gender\n" + prose
        if note:
            text += "\n" + note
        await _say(city, citizen, text)
        return
    # Ho chiesto le preferenze e il cittadino non ha ancora risposto: aspetto il suo turno.
    if asked_prefs and not answered and not replacing:
        return
    offered = _offered_slots(chat, agent, citizen) | {
        slot for slot in sent.offers if not slot.startswith("wl-")
    }
    if offered and not (replacing or refresh):
        if guard and latest is not None:
            await _say(city, citizen, guard)
        return  # le offerte sono già sul tavolo: il cittadino sta scegliendo
    if now is None:
        return

    # 5. Il mondo della mia ASST, pagina per pagina finché trovo qualcosa.
    cancelled = _cancelled_slots(chat, agent, citizen)
    skip = cancelled | (offered if refresh else set())
    skip_fac = _unreachable(chat, agent)
    lang = _lang(chat)

    def pick(rows: list[Message], **prefs: object) -> list[dict[str, str]]:
        lines, _, _ = _world_lines(rows)
        return fitting(
            lines, now=now, priority=priority, lang=lang, procedure=procedure,
            skip=skip, skip_facilities=skip_fac, **prefs,  # type: ignore[arg-type]
        )

    # Pagine da leggere: con la gara lunga (1200 minuti, ~64 giorni di catalogo) le prime pagine
    # sono date passate, quindi salto direttamente a quelle da oggi in poi (_next_page).
    limit = _horizon(now, priority, extend=True)
    for _round in range(_MAX_PAGE):
        if _has_world(chat) and pick(chat, days=days, gender=gender):
            break
        wanted = _next_page(chat, now + _LEAD, limit)
        if wanted is None:
            break
        ask = f"world procedure={procedure} page={wanted}"
        if not any(row.text.strip() == ask for row in _ours(chat, agent, LEDGER)):
            await _say(city, LEDGER, ask)
        chat = await _until(
            city,
            message.need_id,
            chat,
            lambda rows, wanted=wanted: wanted in _page_spans(rows)[0],
        )
        if wanted not in _page_spans(chat)[0]:
            break
    if not _has_world(chat):
        return
    # Una nostra struttura sparita dalla discovery (ospedale giù): non offro i suoi slot.
    # Un nostro slot si salta solo dopo un errore vero di invio alla struttura (TARGET_NOT_VISIBLE,
    # vedi _unreachable): l'Index può non restituire un agente anche quando è online.

    strict = pick(chat, days=days, gender=gender)
    relaxed: list[dict[str, str]] = []
    if not strict and (days or gender):
        relaxed = pick(chat)
    if not strict and not relaxed and priority in {"B", "D"}:
        # Ultima possibilità: la scadenza contata in giorni di agenda (niente sabato e domenica).
        strict = pick(chat, days=days, gender=gender, extend=True)
        if not strict and (days or gender):
            relaxed = pick(chat, extend=True)
    if strict or relaxed:
        await _send_offers(
            city, chat, key, citizen, strict or relaxed,
            number=number, priority=priority, procedure=procedure, tone=tone,
            relaxed=not strict, replacing=replacing, note=note, trigger=trigger,
        )
        return

    if refresh and offered:
        # Nessuna data in più: lo dico con chiarezza e lascio valide quelle già proposte.
        prose = tr("nomore", tone)
        await _once(city, key, f"nomore:{trigger}", citizen, (note + "\n" + prose).strip())
        return

    # 6. Niente nella mia ASST: cerco nelle ASST partner, solo con i criteri.
    lines, _, _ = _world_lines(chat)
    if await _cross_asst(
        city, chat, key, citizen, procedure, priority, _own_facilities(lines), tone, note
    ):
        return
    await _waiting_list(
        city, chat, key, citizen, number, procedure, priority, tone, reason="empty", note=note
    )


def _count_ledger(chat: list[Message], head: str) -> int:
    return sum(1 for row in chat if row.sender == LEDGER and first_word(row.text) == head)


def _trigger(message: Message) -> str:
    return fields_of(message.text).get("offer_id") or message.msg_id


def _offered_slots(chat: list[Message], agent: str, citizen: str) -> set[str]:
    return {
        offer.get("slot_id", "")
        for offer in _our_offers(chat, agent, citizen).values()
        if offer.get("slot_id")
    }


def _cancelled_slots(chat: list[Message], agent: str, citizen: str) -> set[str]:
    offers = _our_offers(chat, agent, citizen)
    gone: set[str] = set()
    for row in chat:
        if "cancel" not in line_heads(row.text) and "refused" not in line_heads(row.text):
            continue
        fields = fields_of(row.text)
        offer = offers.get(fields.get("offer_id", ""))
        if offer and offer.get("slot_id"):
            gone.add(offer["slot_id"])
        if fields.get("slot_id"):
            gone.add(fields["slot_id"])
    return gone


async def _hidden_facilities(city: City, procedure: str | None, facilities: set[str]) -> set[str]:
    """Strutture del mondo che l'Index non restituisce più (es. ospedale sospeso dalla città).

    Prudente: una struttura è "nascosta" solo se l'Index risponde con altri agenti ma non con
    lei, sia cercando il servizio sia cercando il suo id. Se l'Index tace non salto nulla."""

    service = _SERVICE_OF.get(procedure or "", "")
    candidates = {fac for fac in facilities if fac.startswith("agent:")}
    if not service or not candidates:
        return set()
    seen: set[str] = set()
    answered = False
    for query in (service, *sorted(candidates)):
        try:
            peers = await city.find(query)
        except CapError:
            return set()
        answered = answered or bool(peers)
        seen.update(peer.agent_id for peer in peers)
    if not answered:
        return set()
    return candidates - seen


def _unreachable(chat: list[Message], agent: str) -> set[str]:
    found: set[str] = set()
    for row in _ours(chat, agent):
        for line in row.text.splitlines():
            if first_word(line) == "cancel" and "reason=facility_unreachable" in line:
                facility = fields_of(line).get("facility")
                if facility:
                    found.add(facility)
    return found


async def _prescription_problem(
    city: City, key: str, citizen: str, number: str, status: str, tone: str
) -> None:
    if status == "expired":
        text = (
            "data.request purpose=prescription_renewal fields=prescription_number\n"
            + tr("renew", tone, number=number)
        )
        await _once(city, key, f"renew:{number}", citizen, text)
        return
    phrase = {"not_yours": "rx_not_yours", "used": "rx_used"}.get(status, "rx_not_found")
    text = "data.request purpose=booking fields=prescription_number\n" + tr(phrase, tone)
    await _once(city, key, f"rx:{status}:{number}", citizen, text)


async def _send_offers(
    city: City,
    chat: list[Message],
    key: str,
    citizen: str,
    slots: list[dict[str, str]],
    *,
    number: str | None,
    priority: str | None,
    procedure: str | None,
    tone: str,
    relaxed: bool,
    replacing: bool,
    note: str,
    partner: bool = False,
    trigger: str = "",
) -> None:
    sent = _sent(key)
    if replacing:
        # Dopo un annullamento il cittadino ha perso le offerte: le ripropongo, una volta sola.
        tag = f"re:{trigger}"
        if tag in sent.lines:
            return
        picked = slots[:_MAX_OFFERS]
    else:
        tag = ""
        picked = [slot for slot in slots if slot["slot_id"] not in sent.offers][:_MAX_OFFERS]
    if not picked:
        return
    label = _label(procedure, tone)
    kind = "offers_replacing" if replacing else "offers_partner" if partner else "offers_relaxed" if relaxed else "offers_normal"
    intro = tr(kind, tone, label=label)
    why = _priority_text(priority, tone)
    if why:
        intro = why + " " + tr("prio_window", tone) + "\n" + intro
    if note:
        intro = note + "\n" + intro
    ids = [slot.get("offer_id") or f"of-{slot['slot_id']}" for slot in picked]
    text = _offers_text(
        picked, number=number, priority=priority, procedure=procedure, tone=tone, intro=intro, ids=ids
    )
    if await _say(city, citizen, text):
        sent.offers.update(slot["slot_id"] for slot in picked)
        if tag:
            sent.lines.add(tag)


async def _waiting_list(
    city: City,
    chat: list[Message],
    key: str,
    citizen: str,
    number: str | None,
    procedure: str | None,
    priority: str | None,
    tone: str,
    *,
    reason: str,
    note: str,
) -> None:
    offer_id = f"wl-{key}"
    sent = _sent(key)
    if offer_id in sent.offers or _we(chat, city.agent_id, f"offer_id={offer_id}", to=citizen, head="offer"):
        return
    label = _label(procedure, tone)
    parts = ["offer", f"offer_id={offer_id}", "waiting_list=true"]
    if procedure:
        parts.append(f"procedure={procedure}")
    if priority:
        parts.append(f"priority={priority}")
    if number:
        parts.append(f"prescription_number={number}")
    prose = tr("wl_P" if reason == "P" else "wl_empty", tone, label=label)
    prose += "\n" + tr("wl_steps", tone)
    if note:
        prose = note + "\n" + prose
    if await _say(city, citizen, prose + "\n" + " ".join(parts)):
        sent.offers.add(offer_id)


# --- ASST partner ------------------------------------------------------------------------------------------


async def _cross_asst(
    city: City,
    chat: list[Message],
    key: str,
    citizen: str,
    procedure: str,
    priority: str | None,
    own: set[str],
    tone: str,
    note: str,
) -> bool:
    """Chiede disponibilità alle strutture delle ASST partner, solo con i criteri di ricerca,
    e chiede al cittadino il consenso ``cross_asst`` prima di condividere qualsiasi suo dato.
    True se la ricerca è partita (ora o prima)."""

    sent = _sent(key)
    if "cross" in sent.flags or any(
        "need.request" in line_heads(row.text) and "service_type=" in row.text
        for row in _ours(chat, city.agent_id)
    ):
        return True
    service = _SERVICE_OF.get(procedure)
    if not service:
        return False
    try:
        peers = await city.find(service)
    except CapError:
        peers = []
    # Le nostre strutture non sono partner, anche se oggi non hanno slot nel mondo.
    own = set(own) | set(_team_facilities().values())
    partners = [
        peer
        for peer in peers
        if peer.agent_id not in own
        and peer.agent_id != city.agent_id
        and peer.role not in {"booking_centre", "gp", "ledger"}
        and (service in peer.capabilities or peer.service == service)
    ][:_MAX_PARTNERS]
    if not partners:
        return False
    sent.flags.add("cross")
    reached = 0
    for index, peer in enumerate(partners, start=1):
        parts = [
            "need.request",
            f"service_type={service}",
            f"procedure={procedure}",
            f"request_id={key}-x{index}",
        ]
        if priority:
            parts.append(f"priority={priority}")
        text = " ".join(parts) + (
            "\nBuongiorno, il Centro prenotazioni di un'ASST partner cerca disponibilità per "
            "questa prestazione. In questa fase non condividiamo dati del paziente: se avete "
            "posti, ci mandi pure le offerte con slot_id e doctor_id."
        )
        if await _say(city, peer.agent_id, text):
            reached += 1
    if reached == 0:
        return False
    prose = tr("cross_search", tone, label=_label(procedure, tone))
    if note:
        prose = note + "\n" + prose
    await _once(city, key, "cross_consent", citizen, "consent purpose=cross_asst\n" + prose)
    return True


def _consent_ref(chat: list[Message], purpose: str, agent: str, citizen: str) -> str | None:
    """L'ultimo consent_ref che il cittadino ha dato per ``purpose``. Se non dice lo scopo,
    vale quello dell'ultima richiesta di consenso che gli abbiamo fatto (di default booking)."""

    last_asked = "booking"
    found: str | None = None
    for row in chat:
        if row.sender == agent and row.recipient == citizen and "consent" in line_heads(row.text):
            last_asked = fields_of(row.text).get("purpose", last_asked)
            continue
        if row.sender != citizen:
            continue
        fields = fields_of(row.text)
        ref = fields.get("consent_ref")
        if not ref:
            hit = _CONSENT_BARE.search(row.text)
            ref = hit.group(1) if hit else None
        if not ref:
            continue
        said = fields.get("purpose") or ("cross_asst" if "cross" in ref else last_asked)
        if said == purpose:
            found = ref
    return found


async def _partner_offer(city: City, message: Message, chat: list[Message], key: str) -> None:
    """Offerte di una struttura partner: le verifico e le giro al cittadino."""

    citizen = _citizen(chat)
    if citizen is None:
        return
    number = _number(chat)
    status, reply = _prescription_ok(chat, number) if number else ("pending", {})
    if status != "ok":
        return
    priority = (reply.get("priority") or "").upper() or None
    procedure = reply.get("procedure")
    agenda = _ledger(chat, "agenda") or {}
    now = _when(agenda.get("agenda_now") or agenda.get("now") or "")
    if now is None:
        return
    lines = []
    for line in message.text.splitlines():
        if first_word(line) != "offer":
            continue
        fields = fields_of(line)
        fields.setdefault("facility", message.sender)
        if not fields.get("at") and fields.get("agenda_date"):
            fields["at"] = fields["agenda_date"]
        lines.append(" ".join(f"{k}={v}" for k, v in fields.items()))
    if not lines:
        return
    world, _, _ = _world_lines(chat)
    if message.sender in _own_facilities(world) or message.sender in _team_facilities().values():
        return
    usable = fitting(lines, now=now, priority=priority, lang=_lang(chat), procedure=procedure,
                     skip=_cancelled_slots(chat, city.agent_id, citizen))
    # La facility partner deve restare quella che ha offerto lo slot.
    for slot in usable:
        slot["facility"] = message.sender
    if usable:
        await _send_offers(
            city, chat, key, citizen, usable,
            number=number, priority=priority, procedure=procedure, tone=_tone(chat),
            relaxed=False, replacing=False, note="", partner=True,
        )


async def _serve_partner(city: City, message: Message, chat: list[Message], key: str) -> None:
    """Un'altra ASST cerca posti da noi (anello tra ASST). Rispondo subito con gli slot liberi
    della nostra ASST, solo criteri e nessun dato personale. La prenotazione la conferma
    direttamente la nostra struttura, che è indicata con il suo id agente."""

    fields = fields_of(message.text)
    procedure = fields.get("procedure")
    if not procedure:
        service = fields.get("service_type", "")
        procedure = next((code for code, svc in _SERVICE_OF.items() if svc == service), None)
    request_id = fields.get("request_id", key)
    if not procedure:
        await _ack(city, message, key, "Buongiorno, per cercare posti mi serve il codice della prestazione (procedure=...). Grazie!")
        return
    priority = (fields.get("priority") or "").upper() or None
    agent = city.agent_id
    ask = f"world procedure={procedure} page=1"
    if not any(row.text.strip() == ask for row in _ours(chat, agent, LEDGER)):
        await _say(city, LEDGER, ask)
    if not any(row.text.strip() == "agenda" for row in _ours(chat, agent, LEDGER)):
        await _say(city, LEDGER, "agenda")
    chat = await _until(
        city, message.need_id, chat,
        lambda rows: _has_world(rows) and _ledger(rows, "agenda") is not None,
    )
    agenda = _ledger(chat, "agenda") or {}
    now = _when(agenda.get("agenda_now") or agenda.get("now") or "")
    for _round in range(_MAX_PAGE):
        if now is None or priority == "P":
            break
        lines, _, _ = _world_lines(chat)
        if fitting(lines, now=now, priority=priority, lang=fields.get("lang", "it"), procedure=procedure):
            break
        wanted = _next_page(chat, now + _LEAD, _horizon(now, priority, extend=True))
        if wanted is None:
            break
        ask = f"world procedure={procedure} page={wanted}"
        if not any(row.text.strip() == ask for row in _ours(chat, agent, LEDGER)):
            await _say(city, LEDGER, ask)
        chat = await _until(
            city, message.need_id, chat, lambda rows, wanted=wanted: wanted in _page_spans(rows)[0]
        )
        if wanted not in _page_spans(chat)[0]:
            break
    lines, _, _ = _world_lines(chat)
    slots = (
        fitting(lines, now=now, priority=priority, lang=fields.get("lang", "it"), procedure=procedure)
        if now is not None
        else []
    )
    if priority == "P":
        slots = []
    if not slots:
        await _once(
            city, key, f"partner-none:{request_id}", message.sender,
            f"cancel request_id={request_id} procedure={procedure} reason=no_availability\n"
            "Buongiorno, al momento la nostra ASST non ha posti liberi per questa prestazione "
            "nei tempi richiesti. Grazie e buon lavoro!",
        )
        return
    out = ["Buongiorno, ecco le disponibilità della nostra ASST (nessun dato del paziente "
           "necessario finora). Per prenotare inviate confirm direttamente alla struttura "
           "indicata in facility, con slot_id, consent_ref e prescription_number."]
    for slot in slots[:_MAX_OFFERS]:
        target = await _facility_agent(city, slot.get("facility", ""), procedure)
        parts = [
            "offer", f"offer_id=px-{slot['slot_id']}", f"slot_id={slot['slot_id']}",
            f"doctor_id={slot['doctor_id']}", f"facility={target}", f"procedure={procedure}",
            f"agenda_date={slot['at']}", f"agenda_day={slot['day']}", f"expiry={slot['at']}",
            f"request_id={request_id}",
        ]
        if priority:
            parts.append(f"priority={priority}")
        out.append(" ".join(parts))
    await _once(city, key, f"partner-offers:{request_id}", message.sender, "\n".join(out))


# --- scelta, consenso, inoltro alla struttura -----------------------------------------------------------------


def _pending_choice(chat: list[Message], agent: str, citizen: str) -> str | None:
    """L'offerta scelta dal cittadino che aspetta ancora un consenso."""

    offers = _our_offers(chat, agent, citizen)
    chosen: str | None = None
    for row in chat:
        if row.sender == citizen:
            pick = _chosen_offer(row.text, offers)
            if pick:
                chosen = pick
    if not chosen:
        return None
    slot = offers[chosen].get("slot_id", "")
    if slot and _confirmed_slot(chat, agent, slot):
        return None
    return chosen


async def _on_confirm(
    city: City, message: Message, chat: list[Message], key: str, chosen: str | None
) -> None:
    agent = city.agent_id
    citizen = message.sender
    tone = _tone(chat)
    offers = _our_offers(chat, agent, citizen)
    offer_id = chosen or _pending_choice(chat, agent, citizen)
    if not offer_id or offer_id not in offers:
        if offers:
            await _ask_which(city, citizen, offers, tone)
        return
    offer = offers[offer_id]

    # Lista d'attesa: nessuna struttura da chiamare, nessuna ricevuta da inventare.
    if offer.get("waiting_list", "").lower() == "true":
        prose = tr("wl_done", tone)
        await _once(city, key, f"wl-done:{offer_id}", citizen, prose)
        return

    slot = offer.get("slot_id", "")
    facility = offer.get("facility", "")
    number = offer.get("prescription_number") or _number(chat)
    if not slot or not facility:
        return
    if _confirmed_slot(chat, agent, slot):
        # Conferma duplicata: niente seconda prenotazione. Se ho già la ricevuta, la ripeto.
        receipt = _receipt_for(chat, agent, citizen)
        if receipt:
            await _say(city, citizen, receipt)
        return

    world, _, _ = _world_lines(chat)
    cross = facility not in _own_facilities(world) and bool(world)
    booking_ref = fields_of(message.text).get("consent_ref") if not cross else None
    booking_ref = booking_ref or _consent_ref(chat, "booking", agent, citizen)
    cross_ref = _consent_ref(chat, "cross_asst", agent, citizen) if cross else None

    if cross and not cross_ref:
        # Regola P2 e privacy: nessun dato del cittadino esce dall'ASST prima del consenso.
        asked = _we(chat, agent, "purpose=cross_asst", to=citizen, head="consent")
        declined = asked and _declined_after(chat, agent, citizen, "cross_asst")
        if declined:
            await _locked_free_alternative(city, message, chat, key, facility)
            return
        prose = tr("cross_choice", tone)
        await _say(city, citizen, f"consent purpose=cross_asst offer_id={offer_id}\n" + prose)
        return

    # Sicurezza e fiducia: un consenso non si indovina mai. Se il cittadino non l'ha dato lo
    # chiedo (sotto); uso solo riferimenti puliti, senza spazi o caratteri strani.
    if booking_ref and not _CLEAN_REF.fullmatch(booking_ref):
        booking_ref = None
    if cross_ref and not _CLEAN_REF.fullmatch(cross_ref):
        cross_ref = None
    if not booking_ref and not cross_ref:
        if not _we(chat, agent, "purpose=booking", to=citizen, head="consent"):
            prose = tr("booking_consent", tone)
            await _say(city, citizen, f"consent purpose=booking offer_id={offer_id}\n" + prose)
        elif _declined_after(chat, agent, citizen, "booking"):
            prose = tr("booking_declined", tone)
            await _once(city, key, "booking-declined", citizen, prose)
        return

    parts = [
        "confirm",
        f"offer_id={offer_id}",
        f"slot_id={slot}",
        f"citizen={citizen}",
        f"consent_ref={cross_ref or booking_ref}",
        f"facility={facility}",
    ]
    if cross_ref and booking_ref:
        parts.append(f"booking_consent_ref={booking_ref}")
    if number:
        parts.append(f"prescription_number={number}")
    text = " ".join(parts) + (
        "\nBuongiorno, il cittadino ha scelto questo slot: vi chiediamo di bloccarlo e "
        "confermarlo sul Ledger. Grazie!"
    )
    target = await _facility_agent(city, facility, offer.get("procedure"))
    code = await _say_or_code(city, target, text)
    if code:
        # 4xx (es. TARGET_NOT_VISIBLE): nessun nuovo tentativo. Avviso e propongo altro.
        await _facility_unreachable(city, message, chat, key, citizen, offer_id, facility, tone)
        return
    when = _when_text(offer.get("agenda_date", ""), tone)
    prose = tr("forwarded", tone, when=when)
    await _once(city, key, f"forwarded:{slot}", citizen, prose)


def _declined_after(chat: list[Message], agent: str, citizen: str, purpose: str) -> bool:
    """Il cittadino ha risposto alla nostra richiesta di consenso senza dare un consent_ref."""

    asked = None
    for index, row in enumerate(chat):
        if row.sender == agent and row.recipient == citizen and f"purpose={purpose}" in row.text:
            asked = index
    if asked is None:
        return False
    offers = _our_offers(chat, agent, citizen)
    for row in chat[asked + 1 :]:
        if row.sender != citizen:
            continue
        if fields_of(row.text).get("consent_ref") or _CONSENT_BARE.search(row.text):
            return False
        if _chosen_offer(row.text, offers):
            continue  # una scelta non è una risposta al consenso
        return True
    return False


async def _locked_free_alternative(
    city: City, message: Message, chat: list[Message], key: str, facility: str
) -> None:
    """Consenso cross-ASST negato: niente dati fuori, propongo la lista d'attesa."""

    number = _number(chat)
    _, reply = _prescription_ok(chat, number) if number else ("", {})
    tone = _tone(chat)
    citizen = message.sender
    await _waiting_list(
        city, chat, key, citizen, number, reply.get("procedure"),
        (reply.get("priority") or "").upper() or None, tone, reason="empty",
        note=tr("cross_declined", tone),
    )


async def _facility_unreachable(
    city: City,
    message: Message,
    chat: list[Message],
    key: str,
    citizen: str,
    offer_id: str,
    facility: str,
    tone: str,
) -> None:
    prose = tr("unreachable", tone)
    await _say(
        city,
        citizen,
        f"cancel offer_id={offer_id} reason=facility_unreachable facility={facility}\n" + prose,
    )
    try:
        fresh = await city.history(need_id=message.need_id)
    except CapError:
        fresh = chat
    await _advance(city, message, fresh, key, replacing=True)


# --- ricevuta e annullamenti -----------------------------------------------------------------------------------


def _receipt_for(chat: list[Message], agent: str, citizen: str) -> str | None:
    for row in reversed(_ours(chat, agent, citizen)):
        if "booking.receipt" in line_heads(row.text):
            return row.text
    return None


def _confirmed_offer(chat: list[Message], agent: str, citizen: str, facility: str) -> dict[str, str]:
    """L'offerta che abbiamo girato a ``facility`` più di recente."""

    offers = _our_offers(chat, agent, citizen)
    found: dict[str, str] = {}
    for row in _ours(chat, agent, facility):
        if "confirm" in line_heads(row.text):
            fields = fields_of(row.text)
            found = offers.get(fields.get("offer_id", ""), {}) or fields
    return found


async def _facility_address(city: City, facility: str) -> str:
    if facility in _ADDRESSES:
        return _ADDRESSES[facility]
    label = ""
    try:
        for peer in await city.find(facility):
            if peer.agent_id == facility and peer.address is not None:
                label = peer.address.label
                break
    except CapError:
        label = ""
    _ADDRESSES[facility] = label
    return label


async def _forward_receipt(city: City, message: Message, chat: list[Message], key: str) -> None:
    """Giro al cittadino solo una ricevuta vera (booking_id e ledger_ref dalla struttura)."""

    fields = fields_of(message.text)
    booking_id = fields.get("booking_id", "")
    ledger_ref = fields.get("ledger_ref", "")
    citizen = _citizen(chat)
    if not booking_id or not ledger_ref or not citizen:
        return
    if _we(chat, city.agent_id, f"booking_id={booking_id}", to=citizen, head="booking.receipt"):
        return
    tone = _tone(chat)
    offer = _confirmed_offer(chat, city.agent_id, citizen, message.sender)
    when = _when_text(offer.get("agenda_date", ""), tone) if offer else ""
    address = await _facility_address(city, message.sender)
    at = _when(offer.get("agenda_date", "")) if offer else None
    window = ""
    if at is not None:
        window = "08:00–13:00" if at.hour < 13 else "13:00–18:00"
    head = f"booking.receipt booking_id={booking_id} ledger_ref={ledger_ref}"
    lines = [tr("receipt_head", tone), head]
    if when:
        lines.append(tr("r_when", tone, when=when))
    if address:
        lines.append(tr("r_where", tone, address=address))
    lines += [
        tr("r_next", tone),
        tr("r_step1", tone),
        tr("r_step2", tone, window=tr("r_window", tone, window=window) if window else ""),
        tr("r_step3", tone),
        tr("r_privacy", tone),
        tr("r_access", tone),
    ]
    await _say(city, citizen, "\n".join(lines))


async def _facility_cancel(city: City, message: Message, chat: list[Message], key: str) -> None:
    """Una struttura (o la città) annulla: lo dico subito al cittadino e ripropongo date."""

    citizen = _citizen(chat)
    if citizen is None:
        return
    agent = city.agent_id
    tone = _tone(chat)
    fields = fields_of(message.text)
    offers = _our_offers(chat, agent, citizen)
    offer_id = fields.get("offer_id", "")
    if offer_id not in offers and fields.get("slot_id"):
        offer_id = next(
            (oid for oid, offer in offers.items() if offer.get("slot_id") == fields["slot_id"]), offer_id
        )
    if offer_id not in offers and message.sender.startswith("agent:"):
        offer_id = _confirmed_offer(chat, agent, citizen, message.sender).get("offer_id", offer_id)
    if offer_id not in offers:
        # Una partner senza posti: se non ho offerte in corso, propongo l'alternativa.
        if not offers and "cross" in _sent(key).flags:
            number = _number(chat)
            _, reply = _prescription_ok(chat, number) if number else ("", {})
            await _waiting_list(
                city, chat, key, citizen, number, reply.get("procedure"),
                (reply.get("priority") or "").upper() or None, tone, reason="empty", note="",
            )
        return
    reason = (fields.get("reason") or "").lower()
    if "prescription" in reason or "PRESCRIPTION_EXPIRED" in message.text.upper():
        number = _number(chat) or ""
        await _prescription_problem(city, key, citizen, number, "expired", tone)
        return
    if not _we(chat, agent, f"offer_id={offer_id}", to=citizen, head="cancel"):
        prose = tr("facility_cancelled", tone)
        await _say(city, citizen, f"cancel offer_id={offer_id} reason=facility_cancelled\n" + prose)
    try:
        fresh = await city.history(need_id=message.need_id)
    except CapError:
        fresh = chat
    await _advance(city, message, fresh, key, replacing=True)


# --- indirizzo dell'agente --------------------------------------------------------------------------------------


def _asks_address(message: Message) -> bool:
    text = message.text
    if not text or "data.request" not in words(text):
        return False
    raw = fields_of(text).get("fields", "")
    return "address" in {part.strip() for part in raw.split(",")}


def _own_street(city: City) -> str:
    street = getattr(city, "address", "")
    if isinstance(street, str) and street.strip():
        return street.strip()
    env = os.environ.get("CITY_ADDRESS", "").strip()
    if env:
        return env
    try:
        return Path(__file__).with_name("door.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return ""

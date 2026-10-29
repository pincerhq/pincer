"""Tool index ranking with the real multilingual model, across European languages.

Indexes the real descriptions of 22 Pincer tools (tests/fixtures/tool_index_tools.json,
a snapshot of the registry's text) exactly as ToolSelector does, then checks
that a request for each task, in each language, ranks a tool for that task in
the top 5 of 22. Production fills ~110 slots from the ranking, so this is a much
stricter bar than the selector needs. (Top 3 fails only for "draw a picture" in
uk/fr/it, where generate_image comes 4th behind send_image, send_file and
google__upload_file — the first two are always sent anyway.)

Runs the real ONNX model (~240 MB download on first use, into
FASTEMBED_CACHE_PATH): only with PINCER_RUN_EMBEDDING=1.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

from pincer.tools.selection import _index_text

pytestmark = [
    pytest.mark.embedding,
    pytest.mark.skipif(
        not os.environ.get("PINCER_RUN_EMBEDDING"),
        reason="runs the real embedding model; run with PINCER_RUN_EMBEDDING=1",
    ),
]

_ROOT = Path(__file__).resolve().parents[1]
_TOP_K = 5

# Task → tools that correctly serve it.
TASKS: dict[str, set[str]] = {
    "call": {"make_phone_call"},
    "image": {"generate_image"},
    "email": {"email_send", "google__send_message"},
    "calendar": {"calendar_create"},
    "drive": {"google__search_drive_files", "google__list_drive_files"},
}

# Language → (call, image, email, calendar, drive) requests.
REQUESTS: dict[str, tuple[str, str, str, str, str]] = {
    "en": (
        "Please call me at +49 151 2345678",
        "Draw a picture of a cat on the beach",
        "Send an email to Anna about tomorrow's meeting",
        "Add a meeting with Tom to my calendar on Friday at 10",
        "Find the budget spreadsheet in my Google Drive",
    ),
    "de": (
        "Ruf mich bitte unter +49 151 2345678 an",
        "Zeichne ein Bild von einer Katze am Strand",
        "Schick Anna eine E-Mail wegen des Meetings morgen",
        "Trag am Freitag um 10 ein Treffen mit Tom in meinen Kalender ein",
        "Finde die Budget-Tabelle in meinem Google Drive",
    ),
    "uk": (
        "Подзвони мені, будь ласка, на +49 151 2345678",
        "Намалюй картинку з котом на пляжі",
        "Надішли Анні електронного листа про завтрашню зустріч",
        "Додай у мій календар зустріч з Томом у п'ятницю о 10",
        "Знайди таблицю з бюджетом на моєму Google Диску",
    ),
    "fr": (
        "Appelle-moi au +49 151 2345678, s'il te plaît",
        "Dessine une image d'un chat sur la plage",
        "Envoie un e-mail à Anna au sujet de la réunion de demain",
        "Ajoute une réunion avec Tom à mon calendrier vendredi à 10 h",
        "Trouve le tableur du budget dans mon Google Drive",
    ),
    "es": (
        "Llámame al +49 151 2345678, por favor",
        "Dibuja una imagen de un gato en la playa",
        "Envía un correo electrónico a Anna sobre la reunión de mañana",
        "Añade una reunión con Tom a mi calendario el viernes a las 10",
        "Busca la hoja de cálculo del presupuesto en mi Google Drive",
    ),
    "it": (
        "Chiamami al +49 151 2345678, per favore",
        "Disegna un'immagine di un gatto sulla spiaggia",
        "Invia un'e-mail ad Anna sulla riunione di domani",
        "Aggiungi al mio calendario una riunione con Tom venerdì alle 10",
        "Trova il foglio di calcolo del budget nel mio Google Drive",
    ),
    "pt": (
        "Liga-me para o +49 151 2345678, por favor",
        "Desenha uma imagem de um gato na praia",
        "Envia um e-mail à Anna sobre a reunião de amanhã",
        "Adiciona uma reunião com o Tom ao meu calendário na sexta-feira às 10",
        "Encontra a folha de cálculo do orçamento no meu Google Drive",
    ),
    "nl": (
        "Bel me alsjeblieft op +49 151 2345678",
        "Teken een afbeelding van een kat op het strand",
        "Stuur Anna een e-mail over de vergadering van morgen",
        "Zet vrijdag om 10 uur een afspraak met Tom in mijn agenda",
        "Zoek de budgetspreadsheet in mijn Google Drive",
    ),
    "pl": (
        "Zadzwoń do mnie pod numer +49 151 2345678",
        "Narysuj obrazek kota na plaży",
        "Wyślij Annie e-mail w sprawie jutrzejszego spotkania",
        "Dodaj do mojego kalendarza spotkanie z Tomem w piątek o 10",
        "Znajdź arkusz z budżetem na moim Dysku Google",
    ),
    "cs": (
        "Zavolej mi prosím na +49 151 2345678",
        "Nakresli obrázek kočky na pláži",
        "Pošli Anně e-mail ohledně zítřejší schůzky",
        "Přidej do mého kalendáře schůzku s Tomem v pátek v 10",
        "Najdi tabulku s rozpočtem na mém Disku Google",
    ),
    "sk": (
        "Zavolaj mi, prosím, na +49 151 2345678",
        "Nakresli obrázok mačky na pláži",
        "Pošli Anne e-mail o zajtrajšom stretnutí",
        "Pridaj do môjho kalendára stretnutie s Tomom v piatok o 10",
        "Nájdi tabuľku s rozpočtom na mojom Disku Google",
    ),
    "sv": (
        "Ring mig på +49 151 2345678, tack",
        "Rita en bild av en katt på stranden",
        "Skicka ett mejl till Anna om mötet i morgon",
        "Lägg in ett möte med Tom i min kalender på fredag klockan 10",
        "Hitta budgetkalkylarket i min Google Drive",
    ),
    "da": (
        "Ring til mig på +49 151 2345678",
        "Tegn et billede af en kat på stranden",
        "Send en e-mail til Anna om mødet i morgen",
        "Tilføj et møde med Tom i min kalender fredag kl. 10",
        "Find budgetregnearket i mit Google Drev",
    ),
    "nb": (
        "Ring meg på +49 151 2345678",
        "Tegn et bilde av en katt på stranden",
        "Send en e-post til Anna om møtet i morgen",
        "Legg inn et møte med Tom i kalenderen min på fredag klokka 10",
        "Finn budsjettregnearket i Google Disk",
    ),
    "fi": (
        "Soita minulle numeroon +49 151 2345678",
        "Piirrä kuva kissasta rannalla",
        "Lähetä Annalle sähköposti huomisesta kokouksesta",
        "Lisää kalenteriini tapaaminen Tomin kanssa perjantaina kello 10",
        "Etsi budjettitaulukko Google Drivestani",
    ),
    "ro": (
        "Sună-mă la +49 151 2345678, te rog",
        "Desenează o imagine cu o pisică pe plajă",
        "Trimite-i Annei un e-mail despre ședința de mâine",
        "Adaugă în calendarul meu o întâlnire cu Tom vineri la ora 10",
        "Găsește foaia de calcul cu bugetul în Google Drive",
    ),
    "hu": (
        "Hívj fel a +49 151 2345678-as számon",
        "Rajzolj egy képet egy macskáról a tengerparton",
        "Küldj e-mailt Annának a holnapi megbeszélésről",
        "Vegyél fel a naptáramba egy találkozót Tommal pénteken 10-kor",
        "Keresd meg a költségvetési táblázatot a Google Drive-omon",
    ),
    "el": (
        "Πάρε με τηλέφωνο στο +49 151 2345678",
        "Ζωγράφισε μια εικόνα με μια γάτα στην παραλία",
        "Στείλε ένα email στην Άννα για τη συνάντηση αύριο",
        "Πρόσθεσε στο ημερολόγιό μου μια συνάντηση με τον Τομ την Παρασκευή στις 10",
        "Βρες το υπολογιστικό φύλλο του προϋπολογισμού στο Google Drive μου",
    ),
    "bg": (
        "Обади ми се на +49 151 2345678",
        "Нарисувай картина на котка на плажа",
        "Изпрати имейл на Анна за утрешната среща",
        "Добави в календара ми среща с Том в петък в 10",
        "Намери таблицата с бюджета в моя Google Drive",
    ),
    "hr": (
        "Nazovi me na +49 151 2345678, molim te",
        "Nacrtaj sliku mačke na plaži",
        "Pošalji Ani e-mail o sutrašnjem sastanku",
        "Dodaj u moj kalendar sastanak s Tomom u petak u 10",
        "Pronađi tablicu s proračunom na mom Google Driveu",
    ),
    "sl": (
        "Pokliči me na +49 151 2345678, prosim",
        "Nariši sliko mačke na plaži",
        "Pošlji Ani e-pošto o jutrišnjem sestanku",
        "Dodaj v moj koledar sestanek s Tomom v petek ob 10",
        "Poišči preglednico s proračunom na mojem Google Drivu",
    ),
    "lt": (
        "Paskambink man numeriu +49 151 2345678",
        "Nupiešk paveikslėlį su katinu paplūdimyje",
        "Išsiųsk Anai el. laišką apie rytojaus susitikimą",
        "Įtrauk į mano kalendorių susitikimą su Tomu penktadienį 10 val.",
        "Rask biudžeto skaičiuoklę mano Google diske",
    ),
    "lv": (
        "Piezvani man uz +49 151 2345678",
        "Uzzīmē attēlu ar kaķi pludmalē",
        "Nosūti Annai e-pastu par rītdienas sapulci",
        "Pievieno manam kalendāram tikšanos ar Tomu piektdien plkst. 10",
        "Atrodi budžeta izklājlapu manā Google diskā",
    ),
    "et": (
        "Helista mulle numbril +49 151 2345678",
        "Joonista pilt kassist rannas",
        "Saada Annale e-kiri homse koosoleku kohta",
        "Lisa minu kalendrisse kohtumine Tomiga reedel kell 10",
        "Leia eelarve tabel minu Google Drive'ist",
    ),
}

CASES = [
    pytest.param(lang, task, query, id=f"{lang}-{task}")
    for lang, queries in REQUESTS.items()
    for task, query in zip(TASKS, queries, strict=True)
]


@pytest.fixture(scope="module")
def server(memory_server_module: ModuleType) -> Iterator[ModuleType]:
    """The memory server with the real tool model and the snapshot tools indexed."""
    saved = memory_server_module._tool_db, memory_server_module._tool_index_version
    memory_server_module._tool_db, memory_server_module._tool_index_version = None, ""
    tools = json.loads((_ROOT / "tests" / "fixtures" / "tool_index_tools.json").read_text())
    memory_server_module.tool_index_sync([{"name": t["name"], "text": _index_text(t)} for t in tools], "v1")
    yield memory_server_module
    memory_server_module._tool_db, memory_server_module._tool_index_version = saved


@pytest.mark.parametrize(("lang", "task", "query"), CASES)
def test_request_ranks_a_matching_tool_in_top_5(server: ModuleType, lang: str, task: str, query: str) -> None:
    results = server.tool_index_search(query, _TOP_K, "v1")["results"]
    ranked = [r["name"] for r in results]
    assert TASKS[task] & set(ranked), f"{lang} {task}: {query!r} ranked {ranked}"

from __future__ import annotations

import html
import math
import sqlite3
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk

from PIL import Image, ImageTk

from sources import makerworld, printables, thingiverse
from sources.common import USER_AGENT


APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
# One consolidated data directory - the database, downloaded images and browser profiles all
# live under here instead of being scattered loose next to the exe/script.
APP_DATA_DIR = APP_DIR / "3DSModelScope"
DB_PATH = APP_DATA_DIR / "3DModelScope.db"
# In a PyInstaller onefile build, bundled data (added via --add-data) is unpacked to a temp
# directory at runtime (sys._MEIPASS), not next to the exe, so the icon is looked up there.
ICON_PATH = Path(getattr(sys, "_MEIPASS", APP_DIR)) / "app_icon.ico" if getattr(sys, "frozen", False) else APP_DIR / "app_icon.ico"
# Persistent browser profiles for MakerWorld and Thingiverse, both Cloudflare-protected
# (Printables' search API is the only one left that needs no browser at all - see sources/):
# keeps the clearance cookie between runs, so once the interstitial is passed once, later scans
# usually skip it entirely. LEGACY_THINGIVERSE_PROFILE_DIR is where older builds put that one
# (MakerWorld's browser use is new, so it has no legacy location to migrate from), and
# sources.common.ensure_profile_dir migrates a profile found there so upgrading doesn't throw
# away an already-cleared cookie.
BROWSER_PROFILES_DIR = APP_DATA_DIR / "browser_profiles"
MAKERWORLD_PROFILE_DIR = BROWSER_PROFILES_DIR / "makerworld"
THINGIVERSE_PROFILE_DIR = BROWSER_PROFILES_DIR / "thingiverse"
LEGACY_THINGIVERSE_PROFILE_DIR = APP_DIR / "thingiverse_browser_profile"
SOURCE_SEEDS = (
    ("MakerWorld", "MakerWorld", "https://makerworld.com", "makerworld", "continuous"),
    ("Printables", "Printables", "https://www.printables.com", "printables", "continuous"),
    ("Thingiverse", "Thingiverse", "https://www.thingiverse.com", "thingiverse", "paged"),
    ("Egyéb", "Egyéb", "", "generic", "single_page"),
)
LISTING_MODE_LABELS = {
    "paged": "Lapozós",
    "continuous": "Folyamatos lista",
    "single_page": "Egyetlen oldal",
}

THEME_LABELS = {
    "light": "Világos",
    "dark": "Sötét",
    "solarized": "Solarized",
    "solarized_dark": "Solarized Dark",
}

# Color palettes keyed the same as THEME_LABELS. Applied via ttk.Style (base 'clam', the only
# built-in ttk theme that actually honors custom colors on Windows - 'vista'/'winnative'
# mostly ignore them in favor of native chrome) plus direct configuration of the few plain Tk
# widgets (the root window, the list/details PanedWindow sash, the Információ dialog's Text).
THEMES = {
    "light": {
        "bg": "#f0f0f0", "fg": "#000000", "entry_bg": "#ffffff", "entry_fg": "#000000",
        "select_bg": "#0078d7", "select_fg": "#ffffff", "tree_bg": "#ffffff", "tree_fg": "#000000",
        "heading_bg": "#e5e5e5", "border": "#c0c0c0",
    },
    "dark": {
        "bg": "#2b2b2b", "fg": "#e0e0e0", "entry_bg": "#3c3f41", "entry_fg": "#e0e0e0",
        "select_bg": "#4a6da7", "select_fg": "#ffffff", "tree_bg": "#313335", "tree_fg": "#e0e0e0",
        "heading_bg": "#3c3f41", "border": "#555555",
    },
    "solarized": {
        "bg": "#fdf6e3", "fg": "#657b83", "entry_bg": "#eee8d5", "entry_fg": "#586e75",
        "select_bg": "#268bd2", "select_fg": "#fdf6e3", "tree_bg": "#fdf6e3", "tree_fg": "#657b83",
        "heading_bg": "#eee8d5", "border": "#93a1a1",
    },
    "solarized_dark": {
        "bg": "#002b36", "fg": "#839496", "entry_bg": "#073642", "entry_fg": "#93a1a1",
        "select_bg": "#268bd2", "select_fg": "#002b36", "tree_bg": "#002b36", "tree_fg": "#839496",
        "heading_bg": "#073642", "border": "#586e75",
    },
}

def shade_color(hex_color: str, amount: float) -> str:
    """Lightens (amount > 0) or darkens (amount < 0) a "#rrggbb" color, blending it toward
    white or black by that fraction. Used to derive a theme's raised-bevel edge colors from
    its own background, instead of a separate fixed border color that can end up looking
    right on a light theme and wrong on a dark one (or vice versa)."""
    hex_color = hex_color.lstrip("#")
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    if amount >= 0:
        r, g, b = (channel + (255 - channel) * amount for channel in (r, g, b))
    else:
        r, g, b = (channel * (1 + amount) for channel in (r, g, b))
    return f"#{int(r):02x}{int(g):02x}{int(b):02x}"


APP_VERSION = "1.0"

# Shown in the Információ dialog, newest first. Add one line here whenever a user-visible
# change ships, so the in-app changelog stays a real record instead of drifting from reality.
CHANGELOG = """\
1.0 (2026-09-15)
  - Thingiverse: minden találatnál megnyílik a modell saját oldala is, hogy a rajta szereplő
    közzétételi dátumot (nap pontossággal) kiolvassuk - ez most már a rekord "Létrehozva"
    mezőjében tárolódik, és a "Visszamenőleges órák" is ez alapján állítja meg a beolvasást,
    ugyanúgy, mint MakerWorld-nél és Printables-nél. Ára van: modellenként egy extra
    oldalbetöltéssel jár, így a Thingiverse-beolvasás érezhetően lassabb lett.
  - MakerWorld beolvasás visszaállítva böngészős (Cloudflare-átjutós) módra: a MakerWorld
    időközben Cloudflare-védelem mögé tette a saját kereső-API-ját is, ami a korábbi,
    közvetlen JSON-lekérdezést 403-mal kezdte elutasítani. A dátum szerinti megállás
    ("Visszamenőleges órák") és a lapozási logika változatlan maradt, csak most már - a
    Thingiverse-hez hasonlóan - egy látható, minimalizált böngészőablakon keresztül fut.
1.0 (2026-09-11)
  - MakerWorld és Printables lecserélve a saját, nyilvános kereső-API-jukra (böngésző és
    Cloudflare-kerülés nélkül, közvetlen JSON-lekérdezéssel): mindkettő pontos dátum szerint
    áll meg (a "Visszamenőleges órák" alapján), nem darabszám-limittel vagy ismétlődés-
    számlálóval - így egy egész napi "termés" is gyorsan, teljesen lekérdezhető, akár több
    ezer/tízezer tétel esetén is.
  - A modell tényleges weboldali közzétételi dátuma (amikor elérhető) most már a rekord
    "Létrehozva" mezőjében tárolódik, nem a mentés időpontja.
1.0 (2026-09-10)
  - Thingiverse forrás: lapozós ("page=") lista beolvasás, ugyanazzal a Cloudflare-átjutással,
    mint korábban a Printables-nél (mostanra a Printables ezt is levetkőzte, lásd fent).
  - Importálva mező rekordonként: az URL.txt mentés jelöli meg vele az exportált sorokat,
    hogy egy következő mentés már csak az újakat írja ki.
  - URL.txt mentése gomb véglegesítve: a megnézett + érdekel + még nem importált rekordok
    URL-jeit írja ki egy választott fájlba.
  - Printables kép-letöltés javítva: a kártyák második <img>-jét kell venni, az első csak egy
    örök blur-placeholder volt, emiatt korábban sosem sikerült képet letölteni.
  - Cloudflare-ellenőrzés nyelvfüggetlen felismerése (nem csak angol "Just a moment" címet
    ismer fel, hanem a tényleges lista-tartalom megjelenését nézi).
  - Printables/Thingiverse Cloudflare-átjutás Patchright-tal, látható (de minimalizált)
    böngészőablakban - ha a challenge esetleg nem oldódna fel magától, kézzel is átkattintható
    ugyanabban az ablakban.
  - Rekordlista: ID oszlop elsőként, csökkenő sorrendben.
  - Részletes fázis-visszajelzés adatbetöltés közben (melyik modellnél, milyen lépésnél tart:
    rekord készítése, kép másolása, kész).
  - Információ gomb (ez az ablak) verzióval, változásnaplóval és súgóval.
  - "Automatikus felismerés" eltávolítva a forrás-listából: nem volt mögötte funkció (nem
    volt URL-mező, amiből felismerhetett volna bármit is) - mindig konkrét forrást kell
    választani.
  - Régi, adatbázisba ágyazott képek migrációja javítva: eddig a program a séma-frissítéskor
    egyszerűen eldobta ezeket kép mentése nélkül, most tényleg fájlba menti őket.
"""

APP_DESCRIPTION = """\
A 3DModelScope különböző 3D nyomtatható modelleket kínáló weboldalak (MakerWorld, Printables,
Thingiverse, illetve tetszőleges további, egyedi beállítású forrás) listaoldalait olvassa be,
és minden talált modellt egy helyi adatbázisba ment (cím, URL, előnézeti kép). Így egy helyen,
kényelmesen át lehet nézni és válogatni a különböző oldalakon megjelent új modellek között,
majd a kiválasztottak URL-jét egy szöveges fájlba exportálni további feldolgozásra.
"""

HELP_TEXT = """\
ADATBETÖLTÉS (bal oldali panel)
  1. Válaszd ki a forrást a legördülő listából (ha egy egyedi, konkrét URL-t akarsz beolvasni,
     állítsd be a "Beállítások" fülön az "Egyéb" profil Lista URL-jét erre az URL-re).
  2. Állítsd be, hány órára visszamenőleg keress. MakerWorld, Printables és Thingiverse
     esetén ez tényleges megállási határ: a beolvasás a modell saját közzétételi dátuma
     alapján áll meg, amint egy találat ennél régebbi (Thingiverse-nél csak nap, nem óra
     pontossággal, mivel a modell oldala csak dátumot mutat, időpontot nem). Az "Egyéb"
     forrásnál nincs ilyen dátum, ott csak tájékoztató jellegű - a duplikátumokat mindenhol
     az teszi ki, hogy egy URL már szerepel-e az adatbázisban.
  3. Opcionálisan add meg a maximális darabszámot, ha nem szeretnéd a teljes listát bejárni.
  4. Az "Adatbetöltés" gomb elindítja a beolvasást; a folyamat állapota (melyik modell, melyik
     lépés: rekord készítése / kép másolása / kész) a folyamatablakban és a státusz-sorban is
     látszik. "Megszakítás"-kor választhatsz, hogy az addig mentett új rekordokat megtartod
     vagy törlöd.

CLOUDFLARE-VÉDETT FORRÁSOK (MakerWorld, Thingiverse)
  Ezek az oldalak Cloudflare "biztonsági ellenőrzést" mutathatnak. A program ilyenkor egy
  látható, de minimalizált böngészőablakot nyit - a legtöbbször ez magától, pár másodperc
  alatt lezajlik. Ha mégsem, állítsd vissza az ablakot a tálcáról, és kattints át rajta te
  magad; utána a program automatikusan folytatja a beolvasást. (A Printables saját belső
  keresőAPI-ján keresztül, böngésző nélkül, közvetlenül töltődik be.)

REKORDOK FÜL
  A "Rekordok betöltése" tölti be a listát (a Beállításokban megadott limittel lapozva, ha be
  van állítva). A lista tetején szűrhetsz: "Megnézettek mutatása" és "Csak az érdekeltek".
  Egy rekordra kattintva a jobb oldali panelen látod a részleteit és az előnézeti képét
  (nagyítható/kicsinyíthető), valamint itt jelölheted:
    - Megnézve - hogy már átnézted
    - Érdekel / Nem érdekel - a döntésedet
    - Importálva - ezt a program állítja be automatikusan, amikor az URL.txt mentésbe
      belekerül a rekord; kézzel nem módosítható.
  A "Kijelölt törlése" a listában kijelölt sor(oka)t törli, az "Elutasított rekordok törlése"
  az összes megnézett+nem érdekel rekordot egyszerre.

URL.TXT MENTÉSE
  A megnézett és érdekel jelölésű, de még nem importált rekordok URL-jét menti ki egy
  szöveges fájlba (soronként egy URL), majd ezeket a rekordokat importáltra jelöli, hogy egy
  következő mentés már csak az újonnan érdekesnek jelölteket írja ki.

JELÖLÉSEK TÖRLÉSE MINDEN REKORDNÁL
  Nullázza minden rekordnál a Megnézve / Érdekel / Nem érdekel / Importálva jelölést - a
  rekordok maguk, a képekkel együtt, megmaradnak.

BEÁLLÍTÁSOK FÜL
  Itt kezelhetők a forrásprofilok (név, kategória, lista URL, parser azonosító, lista mód,
  aktív állapot, utolsó lekérdezés időpontja), illetve néhány általános beállítás (ismétlődés-
  limit, lista/részletek panel aránya, rekordlista limit, ablak mérete - ez utóbbi kettő
  automatikusan mentődik, ahogy állítod).
"""


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# Single-page fallback path (the "Egyéb" source, or any source not handled by one of the
# dedicated list scrapers below): pulls a title and a preview image out of one HTML page
# using only stdlib HTMLParser, so this path needs no browser at all.
class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.title_active = False
        self.image_url = ""
        self.meta: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() == "title":
            self.title_active = True
        if tag.lower() == "meta":
            key = attributes.get("property") or attributes.get("name")
            content = attributes.get("content")
            if key and content:
                self.meta[key.lower()] = content.strip()
        if tag.lower() == "img" and not self.image_url:
            self.image_url = attributes.get("src", "")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self.title_active = False

    def handle_data(self, data: str) -> None:
        if self.title_active:
            self.title_parts.append(data.strip())


# parser_type is accepted for a future per-source parsing strategy but not used yet - every
# source currently gets the same generic <title> + og:image/twitter:image extraction.
def parse_source_page(raw_html: str, final_url: str, parser_type: str) -> tuple[str, str]:
    parser = PageParser()
    parser.feed(raw_html)
    title = " ".join(" ".join(parser.title_parts).split())
    image = parser.meta.get("og:image") or parser.meta.get("twitter:image") or parser.image_url
    image = urllib.parse.urljoin(final_url, html.unescape(image)) if image else ""
    return title or final_url, image


def fetch_page(url: str, parser_type: str = "generic") -> tuple[str, str]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        content_type = response.headers.get_content_type()
        if content_type not in {"text/html", "application/xhtml+xml"}:
            raise ValueError("A megadott URL nem HTML oldal.")
        raw_html = response.read(5 * 1024 * 1024).decode(response.headers.get_content_charset() or "utf-8", errors="replace")
        final_url = response.geturl()

    return parse_source_page(raw_html, final_url, parser_type)


def download_image_data(url: str) -> bytes | None:
    if not url or url.startswith("data:"):
        return None
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        image_data = response.read(16 * 1024 * 1024)
    with Image.open(BytesIO(image_data)) as image:
        image.verify()
    return image_data


# MakerWorld/Printables/Thingiverse listing fetchers live in sources/ (one module per
# site, see sources/__init__.py) - dispatched to by parser_type in fetch_and_add below.


class Database:
    def __init__(self, path: Path) -> None:
        # Images live next to the database, under the same consolidated data directory
        # (path's parent - e.g. 3DSModelScope/Images alongside 3DSModelScope/3DModelScope.db).
        self.image_dir = path.resolve().parent / "Images"
        path.resolve().parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                base_url TEXT NOT NULL DEFAULT '',
                parser_type TEXT NOT NULL DEFAULT 'generic',
                listing_mode TEXT NOT NULL DEFAULT 'single_page',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                UNIQUE(name, base_url)
            )"""
        )
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                title TEXT NOT NULL,
                image_url TEXT NOT NULL DEFAULT '',
                image_path TEXT NOT NULL DEFAULT '',
                source_id INTEGER,
                created_at TEXT NOT NULL,
                viewed INTEGER NOT NULL DEFAULT 0,
                interested INTEGER NOT NULL DEFAULT 0,
                not_interested INTEGER NOT NULL DEFAULT 0,
                viewed_at TEXT,
                marked_at TEXT
            )"""
        )
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )"""
        )
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(records)")}
        if "image_path" not in columns:
            self.connection.execute("ALTER TABLE records ADD COLUMN image_path TEXT NOT NULL DEFAULT ''")
        if "source_id" not in columns:
            self.connection.execute("ALTER TABLE records ADD COLUMN source_id INTEGER")
        if "imported" not in columns:
            self.connection.execute("ALTER TABLE records ADD COLUMN imported INTEGER NOT NULL DEFAULT 0")
        if "model_created_at" not in columns:
            self.connection.execute("ALTER TABLE records ADD COLUMN model_created_at TEXT NOT NULL DEFAULT ''")
        # Backfills "Létrehozva" for records saved before it was switched to show the model's
        # own web page date instead of the moment we happened to save it - idempotent, so it
        # only ever touches a row once (harmless no-op every startup after that).
        self.connection.execute(
            "UPDATE records SET created_at=model_created_at WHERE model_created_at != '' AND created_at != model_created_at"
        )
        self.image_dir.mkdir(parents=True, exist_ok=True)
        if "image_data" in columns:
            self.migrate_image_blobs()
            self.connection.execute("ALTER TABLE records DROP COLUMN image_data")

        # Renames the earlier temporary MarkedWord profile without losing linked records.
        legacy_source = self.connection.execute(
            "SELECT id FROM sources WHERE name='MarkedWord' OR base_url='https://markedword.com' LIMIT 1"
        ).fetchone()
        maker_source = self.connection.execute(
            "SELECT id FROM sources WHERE name='MakerWorld' OR base_url='https://makerworld.com' LIMIT 1"
        ).fetchone()
        if legacy_source and maker_source and legacy_source["id"] != maker_source["id"]:
            self.connection.execute("UPDATE records SET source_id=? WHERE source_id=?", (maker_source["id"], legacy_source["id"]))
            self.connection.execute("DELETE FROM sources WHERE id=?", (legacy_source["id"],))
        elif legacy_source:
            self.connection.execute(
                "UPDATE sources SET name='MakerWorld', category='MakerWorld', base_url='https://makerworld.com', parser_type='makerworld', listing_mode='continuous' WHERE id=?",
                (legacy_source["id"],),
            )

        # Adds the listing_mode column when upgrading an older database.
        source_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(sources)")}
        listing_mode_migrated = "listing_mode" not in source_columns
        if "listing_mode" not in source_columns:
            self.connection.execute("ALTER TABLE sources ADD COLUMN listing_mode TEXT NOT NULL DEFAULT 'single_page'")
        if "last_fetched_at" not in source_columns:
            self.connection.execute("ALTER TABLE sources ADD COLUMN last_fetched_at TEXT")

        # Applies the initial list modes once when upgrading an older database.
        if listing_mode_migrated:
            for name, _category, base_url, _parser_type, listing_mode in SOURCE_SEEDS:
                self.connection.execute(
                    "UPDATE sources SET listing_mode=? WHERE name=? OR base_url=?",
                    (listing_mode, name, base_url),
                )

        # Removes profiles that were created by an older automatic seeding step.
        # User-created profiles with a custom list URL are intentionally preserved.
        for name, _category, base_url, _parser_type, _listing_mode in SOURCE_SEEDS:
            seeded_source = self.connection.execute(
                "SELECT id FROM sources WHERE name=? AND base_url=?",
                (name, base_url),
            ).fetchone()
            if seeded_source:
                self.connection.execute("UPDATE records SET source_id=NULL WHERE source_id=?", (seeded_source["id"],))
                self.connection.execute("DELETE FROM sources WHERE id=?", (seeded_source["id"],))
        self.connection.commit()

    # Reads one app-wide setting value, falling back when it is not stored yet.
    def get_setting(self, key: str, default: str = "") -> str:
        row = self.connection.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row is not None else default

    # Persists one app-wide setting value.
    def set_setting(self, key: str, value: str) -> None:
        self.connection.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.connection.commit()

    def sources(self) -> list[sqlite3.Row]:
        return list(self.connection.execute("SELECT * FROM sources WHERE active=1 ORDER BY id"))

    # Returns every source so inactive profiles can still be edited in settings.
    def all_sources(self) -> list[sqlite3.Row]:
        return list(self.connection.execute("SELECT * FROM sources ORDER BY id"))

    # Finds one active source by its database identifier for the main-page dropdown.
    def source_by_id(self, source_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM sources WHERE id=? AND active=1", (source_id,)).fetchone()

    # Creates a new configurable source profile.
    def add_source(self, name: str, category: str, base_url: str, parser_type: str, listing_mode: str, active: bool) -> int:
        cursor = self.connection.execute(
            "INSERT INTO sources (name, category, base_url, parser_type, listing_mode, active, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, category, base_url, parser_type, listing_mode, int(active), now_text()),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    # Updates every editable setting of an existing source profile.
    def update_source(self, source_id: int, name: str, category: str, base_url: str, parser_type: str, listing_mode: str, active: bool) -> None:
        self.connection.execute(
            "UPDATE sources SET name=?, category=?, base_url=?, parser_type=?, listing_mode=?, active=? WHERE id=?",
            (name, category, base_url, parser_type, listing_mode, int(active), source_id),
        )
        self.connection.commit()

    # Records when a source's list URL was last queried, to power the "next query" suggestion.
    # Also used to let the user manually correct/clear it from the settings form.
    def set_source_last_fetched(self, source_id: int, timestamp: str | None) -> None:
        self.connection.execute("UPDATE sources SET last_fetched_at=? WHERE id=?", (timestamp or None, source_id))
        self.connection.commit()

    # Removes a source profile while keeping its records as uncategorized entries.
    def delete_source(self, source_id: int) -> None:
        self.connection.execute("UPDATE records SET source_id=NULL WHERE source_id=?", (source_id,))
        self.connection.execute("DELETE FROM sources WHERE id=?", (source_id,))
        self.connection.commit()

    def add(self, url: str, title: str, image_url: str, source_id: int, model_created_at: str = "") -> int:
        # "Létrehozva" is the model's own creation date on the source site when we have one
        # (MakerWorld, Printables, Thingiverse) - the "Egyéb" single-page fallback, or the rare
        # Thingiverse model whose date couldn't be read (see sources/thingiverse.py's
        # fetch_listing docstring), falls back to "when we saved it" (now) instead, since
        # that's the closest information we actually have for those.
        created_at = model_created_at or now_text()
        cursor = self.connection.execute(
            "INSERT INTO records (url, title, image_url, image_path, source_id, created_at, model_created_at) VALUES (?, ?, ?, '', ?, ?, ?)",
            (url, title, image_url, source_id, created_at, model_created_at),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    # Detects the real format from the image bytes (not the source URL's extension, which is
    # frequently wrong for resize-proxy URLs) so the saved file gets a correct suffix - just
    # normalizes Pillow's "JPEG" to the conventional ".jpg".
    @staticmethod
    def image_extension(image_data: bytes) -> str:
        with Image.open(BytesIO(image_data)) as image:
            image_format = image.format or "bin"
        return {"JPEG": "jpg"}.get(image_format, image_format.lower())

    def save_image(self, record_id: int, image_data: bytes) -> str:
        if not image_data:
            return ""
        filename = f"{record_id:08d}.{self.image_extension(image_data)}"
        (self.image_dir / filename).write_bytes(image_data)
        self.connection.execute("UPDATE records SET image_path=? WHERE id=?", (filename, record_id))
        self.connection.commit()
        return str((self.image_dir / filename).resolve())

    # One-time migration from the old blob-in-the-database schema to today's file-based
    # storage: writes each legacy image_data blob out to a file via save_image (skipping rows
    # that somehow already have an image_path, so a good file never gets clobbered by a stale
    # blob), then always clears the blob column so the caller's DROP COLUMN has nothing left
    # to lose. A blob that fails to decode as an image is dropped rather than raising, same as
    # every other image download in this file.
    def migrate_image_blobs(self) -> None:
        legacy_rows = self.connection.execute(
            "SELECT id, image_data, image_path FROM records WHERE length(image_data) > 0"
        ).fetchall()
        for row in legacy_rows:
            if not row["image_path"]:
                try:
                    self.save_image(row["id"], bytes(row["image_data"]))
                except (OSError, ValueError):
                    pass
            self.connection.execute("UPDATE records SET image_data=X'' WHERE id=?", (row["id"],))
        if legacy_rows:
            self.connection.commit()

    # Prevents the same model URL from being stored more than once.
    def record_exists(self, url: str) -> bool:
        return self.connection.execute("SELECT 1 FROM records WHERE url=? LIMIT 1", (url,)).fetchone() is not None

    def local_image_path(self, record_id: int, stored_path: str = "") -> str:
        if stored_path:
            candidate = self.image_dir / Path(stored_path).name
            if candidate.is_file():
                return str(candidate.resolve())
        # Falls back to a glob for legacy rows saved before filenames were zero-padded.
        candidates = sorted(self.image_dir.glob(f"{record_id:08d}.*")) or sorted(self.image_dir.glob(f"{record_id}.*"))
        return str(candidates[0].resolve()) if candidates else ""

    # Removes a record's image file from disk, if it has one.
    def delete_image_file(self, record_id: int, stored_path: str = "") -> None:
        path = self.local_image_path(record_id, stored_path)
        if path:
            try:
                Path(path).unlink()
            except OSError:
                pass

    def all(
        self,
        limit: int | None = None,
        offset: int = 0,
        include_viewed: bool = True,
        only_interested: bool = False,
        only_imported: bool = False,
    ) -> list[sqlite3.Row]:
        query = "SELECT records.id, records.url, records.title, records.image_url, records.image_path, records.source_id, records.created_at, records.model_created_at, records.viewed, records.interested, records.not_interested, records.imported, records.viewed_at, records.marked_at, sources.name AS source_name FROM records LEFT JOIN sources ON sources.id=records.source_id"
        conditions = []
        parameters: list[int] = []
        if not include_viewed:
            conditions.append("records.viewed=0")
        if only_interested:
            conditions.append("records.interested=1")
        if only_imported:
            conditions.append("records.imported=1")
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY records.id DESC"
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            parameters.append(limit)
            parameters.append(offset)
        return list(self.connection.execute(query, parameters))

    def record_count(self) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM records").fetchone()[0])

    def update_flags(self, record_id: int, viewed: bool, interested: bool, not_interested: bool) -> None:
        timestamp = now_text()
        self.connection.execute(
            """UPDATE records SET viewed=?, interested=?, not_interested=?,
               viewed_at=CASE WHEN ? THEN COALESCE(viewed_at, ?) ELSE viewed_at END,
               marked_at=? WHERE id=?""",
            (int(viewed), int(interested), int(not_interested), int(viewed), timestamp, timestamp, record_id),
        )
        self.connection.commit()

    # Saves just the "Érdekel" choice for one record, without touching viewed/viewed_at - used
    # by the review window to persist a tile's checkbox the moment it's toggled, while the
    # record itself is only marked "megnézett" once the page is advanced past or the window
    # closes (see ReviewWindow.mark_batch_viewed).
    def update_interested(self, record_id: int, interested: bool) -> None:
        self.connection.execute(
            "UPDATE records SET interested=?, not_interested=?, marked_at=? WHERE id=?",
            (int(interested), int(not interested), now_text(), record_id),
        )
        self.connection.commit()

    # Marks a batch of records as viewed, preserving whatever interested/not_interested value
    # is already stored for each (already saved by update_interested as their checkboxes were
    # toggled).
    def mark_viewed(self, record_ids: list[int]) -> None:
        if not record_ids:
            return
        timestamp = now_text()
        self.connection.executemany(
            "UPDATE records SET viewed=1, viewed_at=COALESCE(viewed_at, ?), marked_at=? WHERE id=?",
            [(timestamp, timestamp, record_id) for record_id in record_ids],
        )
        self.connection.commit()

    # Undoes mark_viewed for the one page the review window lets the user step back to.
    def mark_unviewed(self, record_ids: list[int]) -> None:
        if not record_ids:
            return
        self.connection.executemany(
            "UPDATE records SET viewed=0 WHERE id=?", [(record_id,) for record_id in record_ids]
        )
        self.connection.commit()

    # Total records not yet marked "megnézett" - shown at the bottom of the review window so
    # the user can see how much of the backlog is left.
    def remaining_unviewed_count(self) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM records WHERE viewed=0").fetchone()[0])

    def delete(self, record_id: int) -> None:
        row = self.connection.execute("SELECT image_path FROM records WHERE id=?", (record_id,)).fetchone()
        if row is not None:
            self.delete_image_file(record_id, row["image_path"])
        self.connection.execute("DELETE FROM records WHERE id=?", (record_id,))
        self.connection.commit()

    def delete_viewed_not_interested(self) -> int:
        rows = self.connection.execute(
            "SELECT id, image_path FROM records WHERE viewed=1 AND not_interested=1"
        ).fetchall()
        for row in rows:
            self.delete_image_file(row["id"], row["image_path"])
        cursor = self.connection.execute(
            "DELETE FROM records WHERE viewed=1 AND not_interested=1"
        )
        self.connection.commit()
        return cursor.rowcount

    # Resets the viewed/interested/not_interested/imported flags on every record.
    def clear_all_flags(self) -> int:
        cursor = self.connection.execute(
            "UPDATE records SET viewed=0, interested=0, not_interested=0, imported=0, viewed_at=NULL, marked_at=NULL"
        )
        self.connection.commit()
        return cursor.rowcount

    # Records that are ready to hand off to URL.txt: watched, marked interested, and not
    # already exported in an earlier URL.txt save.
    def pending_import(self) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                "SELECT id, url FROM records WHERE viewed=1 AND interested=1 AND imported=0 ORDER BY id"
            )
        )

    def mark_imported(self, record_ids: list[int]) -> None:
        self.connection.executemany(
            "UPDATE records SET imported=1 WHERE id=?", [(record_id,) for record_id in record_ids]
        )
        self.connection.commit()


class ToolTip:
    """A minimal hover tooltip for widgets that only show an icon, no label."""

    def __init__(self, widget: tk.Widget, text: str) -> None:
        self.widget = widget
        self.text = text
        self.tip_window: tk.Toplevel | None = None
        widget.bind("<Enter>", self.show)
        widget.bind("<Leave>", self.hide)

    def show(self, _event: object | None = None) -> None:
        if self.tip_window is not None or not self.text:
            return
        x = self.widget.winfo_rootx() + 6
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.tip_window = tk.Toplevel(self.widget)
        self.tip_window.wm_overrideredirect(True)
        self.tip_window.wm_geometry(f"+{x}+{y}")
        tk.Label(
            self.tip_window,
            text=self.text,
            background="#ffffe0",
            relief="solid",
            borderwidth=1,
            padx=6,
            pady=2,
            font=("Segoe UI", 8),
        ).pack()

    def hide(self, _event: object | None = None) -> None:
        if self.tip_window is not None:
            self.tip_window.destroy()
            self.tip_window = None


# "Átnézés" grid review window: tile-count choice -> (columns, rows), the sizes named in
# the feature request (3x1, 3x2, 3x3, 4x3, 4x4).
REVIEW_TILE_COUNT_GRID = {3: (3, 1), 6: (3, 2), 9: (3, 3), 12: (4, 3), 16: (4, 4)}
REVIEW_TILE_MIN_IMAGE_WIDTH = 90
REVIEW_TILE_MIN_IMAGE_HEIGHT = 50
# Vertical room a tile's title (up to 2 lines) + date/interest row + paddings take up below the
# image - subtracted from each row's share of the window's height so the image box, plus this
# fixed text block, together exactly fill the row, with no scrolling ever needed.
REVIEW_TILE_TEXT_HEIGHT = 74


class ImageViewerWindow(tk.Toplevel):
    """A bigger, zoomable view of one image - opened by clicking a review tile's thumbnail,
    since the tile itself is deliberately small and (for non-square images) lightly cropped."""

    def __init__(self, master: tk.Misc, image_path: str, title: str, colors: dict[str, str], database: "Database") -> None:
        super().__init__(master)
        self.database = database
        self.image_path = image_path
        self.zoom = 1.0
        self.photo: ImageTk.PhotoImage | None = None
        self.resize_render_after_id: str | None = None
        self.geometry_save_after_id: str | None = None
        self.configure(background=colors["bg"])
        self.title(title or "Kép")
        self.geometry(self.database.get_setting("image_viewer_geometry", "900x700"))
        # No transient(master): a transient/dialog-style Toplevel loses its minimize/maximize
        # buttons on Windows.
        if self.database.get_setting("image_viewer_state", "normal") == "zoomed":
            self.after(10, lambda: self.state("zoomed"))
        self.bind("<Configure>", self.on_window_configure)

        controls = ttk.Frame(self, padding=8)
        controls.pack(fill="x")
        ttk.Button(controls, text="－", width=3, command=self.zoom_out).pack(side="left")
        self.zoom_var = tk.StringVar(value="100%")
        ttk.Label(controls, textvariable=self.zoom_var, width=6, anchor="center").pack(side="left", padx=4)
        ttk.Button(controls, text="＋", width=3, command=self.zoom_in).pack(side="left")
        ttk.Button(controls, text="Eredeti", command=self.zoom_reset).pack(side="left", padx=(6, 0))
        ttk.Button(controls, text="Bezárás", command=self.destroy).pack(side="right")

        self.image_label = ttk.Label(self, anchor="center")
        self.image_label.pack(fill="both", expand=True)
        self.image_label.bind("<Configure>", self.on_area_resized)

    def on_area_resized(self, _event: object) -> None:
        if self.resize_render_after_id is not None:
            self.after_cancel(self.resize_render_after_id)
        self.resize_render_after_id = self.after(80, self.render)

    # Debounces window resize/move/maximize before persisting geometry/state, same pattern as
    # ReviewWindow.on_window_configure.
    def on_window_configure(self, event: object) -> None:
        if event.widget is not self:
            return
        if self.geometry_save_after_id is not None:
            self.after_cancel(self.geometry_save_after_id)
        self.geometry_save_after_id = self.after(300, self.on_window_resize_settled)

    def on_window_resize_settled(self) -> None:
        self.geometry_save_after_id = None
        window_state = self.state()
        if window_state in ("normal", "zoomed"):
            self.database.set_setting("image_viewer_state", window_state)
        if window_state == "normal":
            self.database.set_setting("image_viewer_geometry", self.geometry())

    def render(self) -> None:
        self.resize_render_after_id = None
        self.photo = None
        if not self.image_path:
            self.image_label.configure(text="Nincs helyi kép", image="")
            return
        try:
            with Image.open(self.image_path) as source_image:
                image = source_image.convert("RGB")
        except (OSError, ValueError):
            self.image_label.configure(text="A kép nem tölthető be.", image="")
            return
        available_width = self.image_label.winfo_width()
        available_height = self.image_label.winfo_height()
        if available_width <= 1 or available_height <= 1:
            available_width, available_height = 860, 620
        fit_scale = min(available_width / image.width, available_height / image.height)
        scale = max(fit_scale * self.zoom, 0.02)
        display_size = (max(int(image.width * scale), 1), max(int(image.height * scale), 1))
        image = image.resize(display_size, Image.LANCZOS)
        self.photo = ImageTk.PhotoImage(image)
        self.image_label.configure(image=self.photo, text="")

    def zoom_in(self) -> None:
        self.zoom = min(self.zoom * 1.25, 6.0)
        self.zoom_var.set(f"{self.zoom * 100:.0f}%")
        self.render()

    def zoom_out(self) -> None:
        self.zoom = max(self.zoom / 1.25, 0.1)
        self.zoom_var.set(f"{self.zoom * 100:.0f}%")
        self.render()

    def zoom_reset(self) -> None:
        self.zoom = 1.0
        self.zoom_var.set("100%")
        self.render()


class ReviewWindow(tk.Toplevel):
    """Grid review of models not yet marked "Megnézve". Each page holds as many records as
    the tile-count selector says. A tile's "Érdekel" checkbox is saved to that record the
    moment it's toggled, but the record only becomes "megnézett" (and so disappears from the
    backlog) once the page is advanced past ("Előre") or the window is closed - either way,
    the main record list is refreshed on the way out. "Vissza" un-does the single most recent
    "Előre" and reloads that page for another look."""

    def __init__(self, app: "App") -> None:
        super().__init__(app)
        self.app = app
        self.database = app.database
        colors = getattr(app, "current_theme", THEMES["light"])
        self.configure(background=colors["bg"])
        self.title("Modellek átnézése")
        # Persisted like the main window's own geometry, so a size the user picked sticks
        # around. The minimum size is recomputed whenever the tile count changes (see
        # apply_minsize) so the window can be shrunk down to the smallest size that still
        # fits one row/column of tiles at their own minimum size, instead of a fixed floor.
        self.geometry(self.database.get_setting("review_window_geometry", "1280x800"))
        # No transient(app) and no grab_set(): this used to be a modal (grab_set()) dialog,
        # but that blocked minimizing the main window while this one was in front - both
        # windows now behave as independent, freely minimizable top-levels.
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.geometry_save_after_id: str | None = None
        self.bind("<Configure>", self.on_window_configure)
        if self.database.get_setting("review_window_state", "normal") == "zoomed":
            self.after(10, lambda: self.state("zoomed"))

        self.tile_photos: list[ImageTk.PhotoImage] = []
        self.interest_vars: dict[int, tk.BooleanVar] = {}
        self.current_batch: list[sqlite3.Row] = []
        # The one page "Vissza" can restore: the batch + checkbox states as they stood right
        # before the most recent "Előre" committed them. Only one step of undo is kept.
        self.previous_batch: list[sqlite3.Row] | None = None
        self.previous_interest: dict[int, bool] = {}
        self.title_font = tkfont.Font(font=("Segoe UI", 8, "bold"))

        top_bar = ttk.Frame(self, padding=(10, 6))
        top_bar.pack(fill="x")
        ttk.Label(top_bar, text="Csempék száma").pack(side="left", padx=(0, 6))
        self.tile_count_var = tk.StringVar(value=self.database.get_setting("review_tile_count", "9"))
        count_combo = ttk.Combobox(
            top_bar,
            textvariable=self.tile_count_var,
            values=[str(value) for value in REVIEW_TILE_COUNT_GRID],
            state="readonly",
            width=4,
        )
        count_combo.pack(side="left")
        count_combo.bind("<<ComboboxSelected>>", self.on_tile_count_changed)
        self.status_var = tk.StringVar(value="")
        ttk.Label(top_bar, textvariable=self.status_var).pack(side="left", padx=(16, 0))
        # tile_count_var must exist before this - it reads it via tile_count().
        self.apply_minsize()

        # No scrollbar: build_grid always sizes the tiles to exactly fill this canvas, so there
        # is never anything to scroll to - a scrollbar here would just eat width for nothing.
        canvas_frame = ttk.Frame(self)
        canvas_frame.pack(fill="both", expand=True, padx=10)
        self.grid_canvas = tk.Canvas(canvas_frame, background=colors["bg"], highlightthickness=0)
        self.grid_canvas.pack(fill="both", expand=True)
        self.tiles_frame = ttk.Frame(self.grid_canvas)
        self.tiles_window = self.grid_canvas.create_window((0, 0), window=self.tiles_frame, anchor="nw")
        self.grid_canvas.bind(
            "<Configure>",
            lambda event: self.grid_canvas.itemconfigure(self.tiles_window, width=event.width, height=event.height),
        )

        bottom_bar = ttk.Frame(self, padding=10)
        bottom_bar.pack(fill="x")
        self.forward_button = ttk.Button(bottom_bar, text="▶  Előre", command=self.commit_and_advance)
        self.forward_button.pack(side="right")
        ttk.Button(bottom_bar, text="✕  Bezárás", command=self.close).pack(side="right", padx=(0, 8))
        self.back_button = ttk.Button(bottom_bar, text="◀  Vissza", command=self.go_back, state="disabled")
        self.back_button.pack(side="right", padx=(0, 8))
        self.remaining_var = tk.StringVar(value="")
        ttk.Label(bottom_bar, textvariable=self.remaining_var).pack(side="left")

        self.load_batch()

    # Falls back to 9 for a stored/typed value that no longer matches an available option.
    def tile_count(self) -> int:
        try:
            value = int(self.tile_count_var.get())
        except ValueError:
            value = 9
        return value if value in REVIEW_TILE_COUNT_GRID else 9

    def on_tile_count_changed(self, _event: object | None = None) -> None:
        self.database.set_setting("review_tile_count", str(self.tile_count()))
        self.apply_minsize()
        self.load_batch()

    # The smallest the window can shrink to while still fitting one full grid of the current
    # tile count at each tile's own minimum image size - recomputed whenever the tile count
    # changes, since a 4x4 grid needs more room than a 3x1 one.
    def apply_minsize(self) -> None:
        columns, rows = REVIEW_TILE_COUNT_GRID[self.tile_count()]
        min_width = columns * (REVIEW_TILE_MIN_IMAGE_WIDTH + 32) + 20
        min_height = rows * (REVIEW_TILE_MIN_IMAGE_HEIGHT + REVIEW_TILE_TEXT_HEIGHT) + 120
        self.minsize(min_width, min_height)

    # Debounces window resize/move before persisting the geometry and reflowing the grid
    # (tile image width is computed from the window's own width, so a resize needs a rebuild).
    def on_window_configure(self, event: object) -> None:
        if event.widget is not self:
            return
        if self.geometry_save_after_id is not None:
            self.after_cancel(self.geometry_save_after_id)
        self.geometry_save_after_id = self.after(300, self.on_window_resize_settled)

    def on_window_resize_settled(self) -> None:
        self.geometry_save_after_id = None
        # Only "normal"/"zoomed" are meaningful to remember - "iconic" (minimized) shouldn't
        # make the window reopen minimized next time. geometry() while zoomed reports the
        # maximized size itself, not the restored one, so it's only saved while normal.
        window_state = self.state()
        if window_state in ("normal", "zoomed"):
            self.database.set_setting("review_window_state", window_state)
        if window_state == "normal":
            self.database.set_setting("review_window_geometry", self.geometry())
        self.build_grid()

    def load_batch(self) -> None:
        count = self.tile_count()
        self.current_batch = self.database.all(limit=count, offset=0, include_viewed=False, only_interested=False)
        self.update_remaining_label()
        self.build_grid()

    # "Még hátra van" count at the bottom of the window - every not-yet-viewed record,
    # including the ones on the currently displayed page - plus how many pages of the
    # current tile count that works out to. Recomputed on every load_batch (so it follows
    # "Előre") and whenever the tile count changes (see on_tile_count_changed).
    def update_remaining_label(self) -> None:
        remaining = self.database.remaining_unviewed_count()
        pages = math.ceil(remaining / self.tile_count()) if remaining else 0
        self.remaining_var.set(f"{remaining} modell van még hátra ({pages} oldal).")

    # Rebuilds every tile. preserved_interest carries over in-progress checkbox choices across
    # a rebuild triggered by a window resize (rather than a fresh batch), so resizing the
    # window never silently discards a choice the user already made on this page.
    def build_grid(self, preserved_interest: dict[int, bool] | None = None) -> None:
        if preserved_interest is None:
            preserved_interest = {record_id: var.get() for record_id, var in self.interest_vars.items()}
        for widget in self.tiles_frame.winfo_children():
            widget.destroy()
        self.tile_photos.clear()
        self.interest_vars.clear()
        if not self.current_batch:
            self.status_var.set("Nincs több új modell.")
            self.forward_button.configure(state="disabled")
            ttk.Label(self.tiles_frame, text="Nincs több új modell.", font=("Segoe UI", 13, "bold"), padding=30).grid(
                row=0, column=0
            )
            return
        self.forward_button.configure(state="normal")
        self.status_var.set(f"{len(self.current_batch)} még nem megnézett modell ezen az oldalon.")
        self.grid_canvas.update_idletasks()
        canvas_width = self.grid_canvas.winfo_width()
        canvas_height = self.grid_canvas.winfo_height()
        if canvas_width <= 1 or canvas_height <= 1:
            # The canvas has no real size yet (first build right after the window opens) -
            # retry once it does, rather than falling back to a wrong, tiny tile size.
            self.after(60, lambda: self.build_grid(preserved_interest))
            return
        columns, rows = REVIEW_TILE_COUNT_GRID[self.tile_count()]
        for column in range(columns):
            self.tiles_frame.columnconfigure(column, weight=1, uniform="review_tile")
        for row in range(rows):
            self.tiles_frame.rowconfigure(row, weight=1, uniform="review_tile_row")
        # Every tile's width and height is a fixed share of the canvas's own current size (minus
        # its own grid/border/padding overhead), so all `rows` x `columns` tiles of the page
        # always fit within the window exactly, with nothing left to scroll to - a resize (which
        # rebuilds the grid, see on_window_resize_settled) simply recomputes this share.
        image_width = max(canvas_width // columns - 32, REVIEW_TILE_MIN_IMAGE_WIDTH)
        image_height = max(canvas_height // rows - REVIEW_TILE_TEXT_HEIGHT, REVIEW_TILE_MIN_IMAGE_HEIGHT)
        for position, record in enumerate(self.current_batch):
            row, column = divmod(position, columns)
            self.build_tile(record, row, column, image_width, image_height, preserved_interest.get(record["id"]))

    def build_tile(
        self,
        record: sqlite3.Row,
        row: int,
        column: int,
        image_width: int,
        image_height: int,
        initial_interest: bool | None,
    ) -> None:
        colors = getattr(self.app, "current_theme", THEMES["light"])
        tile = ttk.Frame(self.tiles_frame, padding=5, relief="raised", borderwidth=1)
        tile.grid(row=row, column=column, sticky="new", padx=4, pady=4)

        image_path = self.database.local_image_path(record["id"], record["image_path"])
        image_canvas = tk.Canvas(
            tile,
            width=image_width,
            height=image_height,
            background=colors["entry_bg"],
            highlightthickness=0,
            cursor="hand2",
        )
        image_canvas.pack(fill="x")
        photo = self.load_tile_image(image_path, image_width, image_height)
        if photo is not None:
            self.tile_photos.append(photo)
            image_canvas.create_image(image_width // 2, image_height // 2, image=photo)
        else:
            image_canvas.create_rectangle(0, 0, image_width, image_height, outline=colors["border"])
            image_canvas.create_text(image_width // 2, image_height // 2, text="Nincs kép", fill=colors["fg"])
        image_canvas.bind(
            "<Button-1>", lambda _event, path=image_path, title=record["title"]: self.open_image_viewer(path, title)
        )

        title_text = self.truncate_to_two_lines(record["title"], image_width)
        title_label = ttk.Label(
            tile,
            text=title_text,
            wraplength=image_width,
            justify="center",
            font=("Segoe UI", 8, "bold"),
            foreground="#3d8bfd",
            cursor="hand2",
        )
        title_label.pack(pady=(4, 2))
        title_label.bind("<Button-1>", lambda _event, url=record["url"]: webbrowser.open(url))

        info_row = ttk.Frame(tile)
        info_row.pack(fill="x")
        ttk.Label(info_row, text=record["created_at"], font=("Segoe UI", 8)).pack(side="left")
        interest_var = tk.BooleanVar(value=initial_interest if initial_interest is not None else bool(record["interested"]))
        self.interest_vars[record["id"]] = interest_var
        # Saved to the record immediately, without marking it "megnézett" - see the class
        # docstring and mark_batch_viewed for when it actually leaves the backlog.
        record_id = record["id"]
        interest_var.trace_add("write", lambda *_args, rid=record_id, var=interest_var: self.database.update_interested(rid, var.get()))
        ttk.Checkbutton(info_row, text="Érdekel", variable=interest_var).pack(side="right")

    # Shortens a title with an ellipsis so it never wraps past two lines at the given box
    # width, using the tile-title font's own text measurement (a fixed char-count cutoff
    # would be wrong across the range of tile sizes 3x1 up to 4x4 produce).
    def truncate_to_two_lines(self, text: str, box_width: int) -> str:
        max_width = max(box_width * 2, 1)
        if self.title_font.measure(text) <= max_width:
            return text
        ellipsis = "…"
        low, high = 0, len(text)
        while low < high:
            mid = (low + high + 1) // 2
            if self.title_font.measure(text[:mid].rstrip() + ellipsis) <= max_width:
                low = mid
            else:
                high = mid - 1
        return text[:low].rstrip() + ellipsis

    # "Contain" fit: scales the whole image down to fit inside the box (never up, never
    # cropped) and centers it - the picture is fitted to the tile rather than the tile being
    # cropped to the picture, so the full image is always visible.
    @staticmethod
    def load_tile_image(image_path: str, box_width: int, box_height: int) -> ImageTk.PhotoImage | None:
        if not image_path:
            return None
        try:
            with Image.open(image_path) as source_image:
                image = source_image.convert("RGB")
        except (OSError, ValueError):
            return None
        image.thumbnail((box_width, box_height), Image.LANCZOS)
        return ImageTk.PhotoImage(image)

    # Opens the bigger, zoomable viewer for one tile's image.
    def open_image_viewer(self, image_path: str, title: str) -> None:
        colors = getattr(self.app, "current_theme", THEMES["light"])
        ImageViewerWindow(self, image_path, title, colors, self.database)

    # Marks the current page as "megnézett" - interested/not_interested was already saved per
    # checkbox as it was toggled (see build_tile), so this only needs to flip the viewed flag.
    def mark_batch_viewed(self) -> None:
        if self.current_batch:
            self.database.mark_viewed([record["id"] for record in self.current_batch])

    # Commits the current page (marks it viewed) and keeps it as the one page "Vissza" can
    # restore, then loads whatever the next not-yet-viewed batch turns out to be.
    def commit_and_advance(self) -> None:
        self.previous_batch = self.current_batch
        self.previous_interest = {record["id"]: self.interest_vars[record["id"]].get() for record in self.current_batch}
        self.mark_batch_viewed()
        self.back_button.configure(state="normal" if self.previous_batch else "disabled")
        self.load_batch()

    # Restores the page committed by the most recent "Előre": un-marks it viewed and displays
    # it again with the checkbox states it had at that point. Only one step of undo is kept.
    def go_back(self) -> None:
        if not self.previous_batch:
            return
        self.database.mark_unviewed([record["id"] for record in self.previous_batch])
        self.current_batch = self.previous_batch
        preserved_interest = self.previous_interest
        self.previous_batch = None
        self.previous_interest = {}
        self.back_button.configure(state="disabled")
        self.update_remaining_label()
        self.build_grid(preserved_interest)

    # Refreshes the main record list on the way out, whichever button (or the window's own
    # close box) triggered it - earlier pages were already committed as they were passed.
    def close(self) -> None:
        self.mark_batch_viewed()
        self.destroy()
        self.app.load_records()


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.withdraw()
        # Sets the window/taskbar icon; default=True makes every Toplevel created afterwards inherit it too.
        if ICON_PATH.is_file():
            try:
                self.iconbitmap(default=str(ICON_PATH))
            except tk.TclError:
                pass
        self.startup_window = tk.Toplevel(self)
        self.startup_window.title("3DModelScope indítása")
        self.startup_window.geometry("360x120")
        self.startup_window.resizable(False, False)
        self.startup_window.protocol("WM_DELETE_WINDOW", lambda: None)
        ttk.Label(self.startup_window, text="3DModelScope", font=("Segoe UI", 15, "bold")).pack(pady=(18, 4))
        self.startup_status = tk.StringVar(value="Az alkalmazás indítása...")
        ttk.Label(self.startup_window, textvariable=self.startup_status, wraplength=320).pack()
        self.startup_window.update_idletasks()
        self.startup_window.deiconify()
        self.update()
        self.title(f"3DModelScope {APP_VERSION}")
        self.minsize(820, 560)
        self.update_startup_status("Adatbázis megnyitása...")
        self.database = Database(DB_PATH)
        # Restores the window size/position from the last session, falling back to a sane default.
        stored_geometry = self.database.get_setting("window_geometry", "1080x740")
        self.geometry(stored_geometry)
        self.window_size_var = tk.StringVar(value=stored_geometry)
        self.geometry_save_after_id: str | None = None
        self.update_startup_status("Forrásprofilok betöltése...")
        self.source_profiles = self.database.sources()
        self.source_ids: dict[str, int] = {}
        self.records: list[sqlite3.Row] = []
        self.index = -1
        self.ignore_tree_event = False
        self.photo: ImageTk.PhotoImage | None = None
        self.image_zoom = 1.0
        self.current_image_path = ""
        self.resize_render_after_id: str | None = None
        self.load_window: tk.Toplevel | None = None
        self.cancel_event = threading.Event()
        self.current_run_record_ids: list[int] = []
        self.pending_cancel_delete = False
        self.list_pane_ratio = float(self.database.get_setting("list_pane_ratio", "0.32"))
        self.record_offset = 0
        self.update_startup_status("Felület felépítése...")
        self.build_ui()
        self.update_startup_status("Indítás kész. A rekordlista kézi betöltésre vár.")
        self.startup_window.destroy()
        self.deiconify()
        self.after(150, self.apply_initial_pane_ratio)
        self.after(1000, self.tick_suggested_time)
        self.bind("<Configure>", self.on_window_configure)
        if self.database.get_setting("window_state", "normal") == "zoomed":
            self.after(10, lambda: self.state("zoomed"))

    def update_startup_status(self, text: str) -> None:
        self.startup_status.set(text)
        self.startup_window.update_idletasks()
        self.update()

    def show_load_window(self) -> None:
        if self.load_window is not None and self.load_window.winfo_exists():
            self.load_window.deiconify()
            self.load_window.lift()
            return
        colors = getattr(self, "current_theme", THEMES["light"])
        self.load_window = tk.Toplevel(self, background=colors["bg"])
        self.load_window.title("Adatbetöltés folyamatban")
        self.load_window.geometry("420x190")
        self.load_window.resizable(False, False)
        self.load_window.protocol("WM_DELETE_WINDOW", self.load_window.withdraw)
        ttk.Label(self.load_window, text="Adatbetöltés folyamatban", font=("Segoe UI", 13, "bold")).pack(pady=(18, 8))
        self.load_window_status = tk.StringVar(value="Böngésző indítása...")
        ttk.Label(self.load_window, textvariable=self.load_window_status, wraplength=380).pack(pady=(0, 8))
        self.load_window_progress = ttk.Progressbar(self.load_window, mode="indeterminate", length=350)
        self.load_window_progress.pack()
        ttk.Button(self.load_window, text="Megszakítás", command=self.cancel_download).pack(pady=(10, 0))
        self.load_window.update_idletasks()
        self.load_window.deiconify()
        self.load_window.lift()

    def close_load_window(self) -> None:
        if self.load_window is not None and self.load_window.winfo_exists():
            self.load_window.destroy()
        self.load_window = None

    # Places the list/details divider at the persisted ratio once the window has a real size.
    def apply_initial_pane_ratio(self) -> None:
        total_width = self.work_paned.winfo_width()
        if total_width <= 1:
            self.after(100, self.apply_initial_pane_ratio)
            return
        sash_x = int(total_width * self.list_pane_ratio)
        self.work_paned.sash_place(0, sash_x, 0)
        self.pane_ratio_var.set(f"{self.list_pane_ratio * 100:.0f}%")

    # Persists the divider position as a ratio whenever the user drags the sash.
    def on_pane_resized(self, _event: object) -> None:
        total_width = self.work_paned.winfo_width()
        if total_width <= 1:
            return
        sash_x = self.work_paned.sash_coord(0)[0]
        ratio = max(0.1, min(0.9, sash_x / total_width))
        self.list_pane_ratio = ratio
        self.database.set_setting("list_pane_ratio", f"{ratio:.4f}")
        self.pane_ratio_var.set(f"{ratio * 100:.0f}%")

    # Debounces window resize/move events before persisting the geometry, so dragging the
    # window doesn't hammer the database with writes.
    def on_window_configure(self, event: object) -> None:
        if event.widget is not self:
            return
        if self.geometry_save_after_id is not None:
            self.after_cancel(self.geometry_save_after_id)
        self.geometry_save_after_id = self.after(500, self.save_window_geometry)

    def save_window_geometry(self) -> None:
        self.geometry_save_after_id = None
        # "iconic" (minimized) is deliberately not persisted - only whether it should reopen
        # maximized ("zoomed") or not; geometry() while zoomed reports the maximized size
        # itself, not the restored one, so that's only saved while normal.
        window_state = self.state()
        if window_state in ("normal", "zoomed"):
            self.database.set_setting("window_state", window_state)
            if hasattr(self, "maximized_var"):
                self.maximized_var.set(window_state == "zoomed")
        if window_state == "normal":
            geometry = self.geometry()
            self.database.set_setting("window_geometry", geometry)
            self.window_size_var.set(geometry)

    # Persists id/source/title column widths whenever they actually changed (cheap to check,
    # so it's fine that this fires on every click in the list, not just header drags - see
    # the bind comment where this is attached).
    def save_column_widths(self, _event: object | None = None) -> None:
        for column in self.persisted_columns:
            width = self.record_tree.column(column, "width")
            if str(width) != self.column_width_vars[column].get():
                self.database.set_setting(f"column_width_{column}", str(width))
                self.column_width_vars[column].set(str(width))

    # Mirrors a typed value from the Beállítások entry back onto the list: partial/invalid
    # input while typing (empty, non-numeric, zero or negative) is simply ignored rather than
    # rejected with a warning, since trace_add fires on every keystroke.
    def apply_column_width_setting(self, column: str) -> None:
        try:
            width = int(self.column_width_vars[column].get().strip())
        except ValueError:
            return
        if width <= 0:
            return
        self.record_tree.column(column, width=width)
        self.database.set_setting(f"column_width_{column}", str(width))

    # Reads the combobox, persists the choice, and re-applies immediately - so switching
    # themes takes effect on the spot rather than needing a restart.
    def on_theme_selected(self, _event: object | None = None) -> None:
        key = next((k for k, label in THEME_LABELS.items() if label == self.theme_var.get()), "light")
        self.database.set_setting("theme", key)
        self.apply_theme(key)

    # Applies the "Főablak maximalizálva induljon" checkbox immediately, in addition to
    # persisting it for the next startup.
    def on_maximized_toggle(self) -> None:
        window_state = "zoomed" if self.maximized_var.get() else "normal"
        self.database.set_setting("window_state", window_state)
        self.state(window_state)

    # Recolors every ttk widget class via a shared Style (forced onto the 'clam' base theme,
    # since Windows' native ttk themes largely ignore custom colors) plus the handful of plain
    # Tk widgets that don't go through ttk styling at all (the root window and the list/details
    # divider). Safe to call anytime, including at startup, to apply the saved choice.
    def apply_theme(self, theme_key: str) -> None:
        colors = THEMES.get(theme_key, THEMES["light"])
        self.current_theme = colors
        # Bevel edges derived from each surface's own background: a "raised" relief paints
        # lightcolor on the top/left edge and darkcolor on the bottom/right edge, giving the
        # classic protruding-panel look instead of the flat, too-light default border 'clam'
        # falls back to when no bordercolor/lightcolor/darkcolor is configured at all.
        panel_light = shade_color(colors["bg"], 0.16)
        panel_dark = shade_color(colors["bg"], -0.28)
        field_light = shade_color(colors["entry_bg"], -0.28)
        field_dark = shade_color(colors["entry_bg"], 0.16)
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", background=colors["bg"], foreground=colors["fg"], fieldbackground=colors["entry_bg"])
        for widget_class in ("TFrame", "TLabelframe", "TLabel", "TCheckbutton", "TButton", "TNotebook"):
            style.configure(widget_class, background=colors["bg"], foreground=colors["fg"])
        # Raised bevel on every group-box panel (Adatbetöltés, Rekord adatai, the Beállítások
        # forms): lighter top/left, darker bottom/right, so it visibly stands out from the
        # window background instead of blending into it or (as before) showing a mismatched
        # bright edge on dark themes.
        style.configure(
            "TLabelframe",
            bordercolor=colors["border"],
            lightcolor=panel_light,
            darkcolor=panel_dark,
            relief="raised",
            borderwidth=2,
        )
        style.configure("TLabelframe.Label", background=colors["bg"], foreground=colors["fg"])
        # Entries/combos get the opposite (sunken) bevel - the conventional "recessed input
        # field" look - using shades of the field's own background rather than the panel's.
        style.configure(
            "TEntry",
            fieldbackground=colors["entry_bg"],
            foreground=colors["entry_fg"],
            insertcolor=colors["fg"],
            bordercolor=colors["border"],
            lightcolor=field_light,
            darkcolor=field_dark,
            relief="sunken",
        )
        style.configure(
            "TCombobox",
            fieldbackground=colors["entry_bg"],
            foreground=colors["entry_fg"],
            bordercolor=colors["border"],
            lightcolor=field_light,
            darkcolor=field_dark,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", colors["entry_bg"])],
            foreground=[("readonly", colors["entry_fg"])],
        )
        self.option_add("*TCombobox*Listbox.background", colors["entry_bg"])
        self.option_add("*TCombobox*Listbox.foreground", colors["entry_fg"])
        self.option_add("*TCombobox*Listbox.selectBackground", colors["select_bg"])
        self.option_add("*TCombobox*Listbox.selectForeground", colors["select_fg"])
        style.map("TButton", background=[("active", colors["select_bg"])], foreground=[("active", colors["select_fg"])])
        style.configure(
            "TButton", bordercolor=colors["border"], lightcolor=panel_light, darkcolor=panel_dark, relief="raised"
        )
        # 'clam' otherwise leaves the Notebook's own border/tab-row edge at its built-in
        # (light) default regardless of every other color set above - explicit bordercolor
        # here is what actually gets rid of the bright line around the content area.
        style.configure("TNotebook", bordercolor=colors["border"])
        style.configure("TNotebook.Tab", background=colors["bg"], foreground=colors["fg"], bordercolor=colors["border"])
        style.map(
            "TNotebook.Tab",
            background=[("selected", colors["select_bg"])],
            foreground=[("selected", colors["select_fg"])],
        )
        style.configure(
            "Treeview",
            background=colors["tree_bg"],
            foreground=colors["tree_fg"],
            fieldbackground=colors["tree_bg"],
            bordercolor=colors["border"],
            lightcolor=field_light,
            darkcolor=field_dark,
        )
        style.map(
            "Treeview",
            background=[("selected", colors["select_bg"])],
            foreground=[("selected", colors["select_fg"])],
        )
        style.configure("Treeview.Heading", background=colors["heading_bg"], foreground=colors["fg"])
        for scrollbar_style in ("TScrollbar", "Vertical.TScrollbar", "Horizontal.TScrollbar"):
            style.configure(
                scrollbar_style,
                background=colors["bg"],
                troughcolor=colors["entry_bg"],
                arrowcolor=colors["fg"],
                bordercolor=colors["border"],
                lightcolor=panel_light,
                darkcolor=panel_dark,
            )
        style.configure("TSeparator", background=colors["border"])
        self.configure(background=colors["bg"])
        if hasattr(self, "work_paned"):
            self.work_paned.configure(bg=colors["border"])

    def build_ui(self) -> None:
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=0)
        self.rowconfigure(1, weight=1)
        header = ttk.Frame(self, padding=(14, 5, 14, 5))
        # Spans both columns: with only column 0, the row-0/column-1 cell above the notebook
        # was left completely uncovered, showing the raw (unstyled) root window background
        # through as a bright strip along the top of the content area.
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="3DModelScope", font=("Segoe UI", 17, "bold")).grid(row=0, column=0, sticky="w")

        # The main page starts a query for the selected source profile.
        # Fixed width keeps the panel from resizing when the source list or status text changes.
        input_frame = ttk.LabelFrame(self, text="Adatbetöltés", padding=12, width=280)
        input_frame.grid(row=1, column=0, sticky="ns", padx=(14, 7), pady=(0, 14))
        input_frame.grid_propagate(False)
        input_frame.columnconfigure(0, weight=1)
        self.hours_var = tk.StringVar(value="24")
        self.max_count_var = tk.StringVar()
        self.unchanged_round_limit_var = tk.StringVar(value="3")
        ttk.Label(input_frame, text="Forrás").grid(row=0, column=0, sticky="w", pady=(0, 3))
        # The selected source controls which parser profile processes the URL. No
        # "auto-detect" option: there is no free-form URL field to detect from - every source
        # has its own fixed list URL configured on the Beállítások tab, so the user always
        # picks a concrete source here.
        source_names = [source["name"] for source in self.source_profiles]
        self.source_var = tk.StringVar(value=source_names[0] if source_names else "")
        self.source_ids = {source["name"]: source["id"] for source in self.source_profiles}
        self.source_combo = ttk.Combobox(input_frame, textvariable=self.source_var, values=source_names, state="readonly", width=1)
        self.source_combo.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self.source_combo.bind("<<ComboboxSelected>>", self.update_last_run_info)
        self.last_run_var = tk.StringVar(value="")
        self.suggested_time_var = tk.StringVar(value="")
        ttk.Label(input_frame, text="Visszamenőleges órák").grid(row=2, column=0, sticky="w", pady=(0, 3))
        ttk.Entry(input_frame, textvariable=self.hours_var, width=1).grid(row=3, column=0, sticky="ew", pady=(0, 3))
        last_run_row = ttk.Frame(input_frame)
        last_run_row.grid(row=4, column=0, sticky="ew", pady=(0, 8))
        last_run_row.columnconfigure(0, weight=0)
        last_run_row.columnconfigure(1, weight=1)
        ttk.Label(last_run_row, textvariable=self.last_run_var, foreground="#555555").grid(row=0, column=0, sticky="w")
        ttk.Label(last_run_row, textvariable=self.suggested_time_var, foreground="#555555").grid(row=0, column=1, sticky="e", padx=(6, 0))
        ttk.Label(input_frame, text="Maximum darabszám (üres = mind)").grid(row=5, column=0, sticky="w", pady=(0, 3))
        ttk.Entry(input_frame, textvariable=self.max_count_var, width=1).grid(row=6, column=0, sticky="ew", pady=(0, 8))
        self.add_button = ttk.Button(input_frame, text="⬇  Adatbetöltés", command=self.load_source_data)
        self.add_button.grid(row=7, column=0, sticky="ew")
        self.progress_bar = ttk.Progressbar(input_frame, mode="indeterminate")
        self.progress_bar.grid(row=8, column=0, sticky="ew", pady=(10, 0))
        self.status_var = tk.StringVar(value="Válassz forrást, majd indíts adatbetöltést.")
        ttk.Label(input_frame, textvariable=self.status_var, wraplength=250).grid(row=9, column=0, sticky="w", pady=(8, 0))
        # The empty weighted row absorbs leftover height, pinning the buttons below to the bottom.
        input_frame.rowconfigure(10, weight=1)
        ttk.Frame(input_frame).grid(row=10, column=0, sticky="nsew")
        ttk.Separator(input_frame).grid(row=11, column=0, sticky="ew", pady=18)
        ttk.Button(input_frame, text="🗑  Kijelölt törlése", command=self.delete_selected_records).grid(row=12, column=0, sticky="ew")
        ttk.Button(input_frame, text="🗑  Elutasított rekordok törlése", command=self.delete_viewed_not_interested).grid(row=13, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(input_frame, text="↺  Jelölések törlése minden rekordnál", command=self.clear_all_flags).grid(row=14, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(input_frame, text="💾  URL.txt mentése", command=self.save_url_txt).grid(row=15, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(input_frame, text="🖼  Modellek átnézése", command=self.open_review_window).grid(row=16, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(input_frame, text="ℹ  Információ", command=self.show_info_dialog).grid(row=17, column=0, sticky="ew", pady=(8, 0))

        content = ttk.Frame(self, padding=(7, 0, 14, 14))
        content.grid(row=0, column=1, rowspan=2, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(0, weight=1)
        self.view_notebook = ttk.Notebook(content)
        self.view_notebook.grid(row=0, column=0, sticky="nsew")

        work_tab = ttk.Frame(self.view_notebook, padding=8)
        work_tab.columnconfigure(0, weight=1)
        work_tab.rowconfigure(0, weight=0)
        work_tab.rowconfigure(1, weight=1)

        list_toolbar = ttk.Frame(work_tab)
        list_toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        list_toolbar.columnconfigure(1, weight=1)
        left_toolbar = ttk.Frame(list_toolbar)
        left_toolbar.grid(row=0, column=0, sticky="w")
        self.include_viewed_var = tk.BooleanVar(value=False)
        self.only_interested_var = tk.BooleanVar(value=False)
        self.only_imported_var = tk.BooleanVar(value=False)
        # Paging through the limited record list, since only one batch is loaded at a time.
        self.prev_batch_button = ttk.Button(left_toolbar, text="◀", width=3, command=self.load_previous_batch, state="disabled")
        self.prev_batch_button.pack(side="left", padx=(0, 4))
        ToolTip(self.prev_batch_button, "Előző rekordok")
        self.next_batch_button = ttk.Button(left_toolbar, text="▶", width=3, command=self.load_next_batch, state="disabled")
        self.next_batch_button.pack(side="left", padx=(0, 10))
        ToolTip(self.next_batch_button, "Következő rekordok")
        self.load_records_button = ttk.Button(left_toolbar, text="Rekordok betöltése", command=self.load_records)
        self.load_records_button.pack(side="left")
        self.records_status_var = tk.StringVar(value="A rekordlista nincs betöltve.")
        ttk.Label(left_toolbar, textvariable=self.records_status_var).pack(side="left", padx=(10, 0))
        right_toolbar = ttk.Frame(list_toolbar)
        right_toolbar.grid(row=0, column=2, sticky="e")
        ttk.Checkbutton(right_toolbar, text="Megnézettek mutatása", variable=self.include_viewed_var, command=self.load_records).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(right_toolbar, text="Csak az érdekeltek", variable=self.only_interested_var, command=self.load_records).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(right_toolbar, text="Csak az importáltak", variable=self.only_imported_var, command=self.load_records).pack(side="left")

        # A classic tk.PanedWindow gives a draggable, visibly marked divider between the list
        # and the details pane; its position is persisted as a ratio in app_settings.
        self.work_paned = tk.PanedWindow(work_tab, orient=tk.HORIZONTAL, sashwidth=6, sashrelief=tk.RAISED, showhandle=False, bg="#9a9a9a")
        self.work_paned.grid(row=1, column=0, sticky="nsew")
        self.work_paned.bind("<ButtonRelease-1>", self.on_pane_resized)

        list_tab = ttk.Frame(self.work_paned, padding=4)
        list_tab.columnconfigure(0, weight=1)
        list_tab.rowconfigure(0, weight=1)
        self.record_tree = ttk.Treeview(
            list_tab,
            columns=("id", "source", "title", "url", "image_url", "created", "viewed", "interest", "imported"),
            show="headings",
            selectmode="extended",
        )
        headings = {
            "id": "ID",
            "source": "Forrás",
            "title": "Cím",
            "url": "URL",
            "image_url": "Kép elérési útja",
            "created": "Létrehozva",
            "viewed": "Megnézve",
            "interest": "Érdekel",
            "imported": "Importálva",
        }
        widths = {"id": 60, "source": 105, "title": 240, "url": 300, "image_url": 300, "created": 145, "viewed": 85, "interest": 85, "imported": 85}
        # id/source/title widths are user-resizable and persisted (see save_column_widths);
        # the rest keep their fixed defaults.
        self.persisted_columns = ("id", "source", "title")
        for column in self.persisted_columns:
            widths[column] = int(self.database.get_setting(f"column_width_{column}", str(widths[column])))
        self.column_width_vars = {column: tk.StringVar(value=str(widths[column])) for column in self.persisted_columns}
        for column in headings:
            self.record_tree.heading(column, text=headings[column])
            # stretch=False is the actual fix: ttk.Treeview's default (stretch=True) keeps
            # every column's total width pinned to the visible area, so widening one column
            # squeezes all the others to compensate. With stretch off per column, resizing one
            # column only changes that column - the horizontal scrollbar (below) picks up the
            # slack instead of the other columns collapsing.
            self.record_tree.column(column, width=widths[column], anchor="w", stretch=False)
        self.record_tree.grid(row=0, column=0, sticky="nsew")
        list_scrollbar = ttk.Scrollbar(list_tab, orient="vertical", command=self.record_tree.yview)
        list_scrollbar.grid(row=0, column=1, sticky="ns")
        self.record_tree.configure(yscrollcommand=list_scrollbar.set)
        list_horizontal_scrollbar = ttk.Scrollbar(list_tab, orient="horizontal", command=self.record_tree.xview)
        list_horizontal_scrollbar.grid(row=1, column=0, sticky="ew")
        self.record_tree.configure(xscrollcommand=list_horizontal_scrollbar.set)
        self.record_tree.bind("<<TreeviewSelect>>", self.on_tree_select)
        # There is no dedicated "column resized" event on ttk.Treeview - a header drag also
        # ends with a button release over the tree, so this doubles as the save trigger.
        self.record_tree.bind("<ButtonRelease-1>", self.save_column_widths, add="+")
        self.work_paned.add(list_tab, minsize=220)
        detail_tab = ttk.Frame(self.work_paned, padding=12)
        detail_tab.columnconfigure(0, weight=1)
        detail_tab.rowconfigure(0, weight=0)
        detail_tab.rowconfigure(1, weight=1)
        detail_tab.rowconfigure(2, weight=0)
        self.title_var = tk.StringVar(value="Nincs kiválasztott rekord")
        self.position_var = tk.StringVar(value="0 / 0")
        detail_heading = ttk.Frame(detail_tab)
        detail_heading.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        detail_heading.columnconfigure(0, weight=1)
        ttk.Label(detail_heading, text="Rekord részletei", font=("Segoe UI", 12, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(detail_heading, textvariable=self.position_var).grid(row=0, column=1, sticky="e")
        details = ttk.LabelFrame(detail_tab, text="Rekord adatai", padding=10)
        details.grid(row=1, column=0, sticky="nsew")
        details.columnconfigure(0, weight=0)
        details.columnconfigure(1, weight=1)
        details.rowconfigure(0, weight=0)
        details.rowconfigure(1, weight=0)
        details.rowconfigure(3, weight=1)
        details.rowconfigure(4, weight=0)
        ttk.Label(details, textvariable=self.title_var, font=("Segoe UI", 15, "bold"), wraplength=0).grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        self.url_label = ttk.Label(details, text="", foreground="#245a9b", cursor="hand2", wraplength=0)
        self.url_label.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.url_label.bind("<Button-1>", self.open_url)
        self.date_var = tk.StringVar()
        ttk.Label(details, textvariable=self.date_var, wraplength=0).grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self.image_label = ttk.Label(details, text="Nincs helyi kép", anchor="center")
        self.image_label.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=(12, 0))
        self.image_label.bind("<Configure>", self.on_image_area_resized)
        flags = ttk.Frame(details)
        flags.grid(row=4, column=0, sticky="sw", pady=(12, 0))
        zoom_controls = ttk.Frame(details)
        zoom_controls.grid(row=4, column=1, sticky="se", pady=(12, 0))
        ttk.Button(zoom_controls, text="－", width=3, command=self.zoom_image_out).pack(side="left")
        self.zoom_var = tk.StringVar(value="100%")
        ttk.Label(zoom_controls, textvariable=self.zoom_var, width=5, anchor="center").pack(side="left", padx=4)
        ttk.Button(zoom_controls, text="＋", width=3, command=self.zoom_image_in).pack(side="left")
        ttk.Button(zoom_controls, text="Eredeti", command=self.zoom_image_reset).pack(side="left", padx=(6, 0))
        self.viewed_var = tk.BooleanVar()
        self.interested_var = tk.BooleanVar()
        self.not_interested_var = tk.BooleanVar()
        self.imported_var = tk.BooleanVar()
        ttk.Checkbutton(flags, text="Megnézve", variable=self.viewed_var, command=self.save_flags).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(flags, text="Érdekel", variable=self.interested_var, command=lambda: self.save_flags(auto_advance=True)).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(flags, text="Nem érdekel", variable=self.not_interested_var, command=lambda: self.save_flags(auto_advance=True)).pack(side="left", padx=(0, 12))
        # Read-only: imported is set automatically by the URL.txt export, not by hand.
        ttk.Checkbutton(flags, text="Importálva", variable=self.imported_var, state="disabled").pack(side="left")
        navigation = ttk.Frame(detail_tab)
        navigation.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        navigation.columnconfigure((0, 1), weight=1)
        self.previous_button = ttk.Button(navigation, text="◀", width=3, command=self.previous_record)
        self.previous_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ToolTip(self.previous_button, "Előző rekord")
        self.next_button = ttk.Button(navigation, text="▶", width=3, command=self.next_record)
        self.next_button.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        ToolTip(self.next_button, "Következő rekord")
        self.work_paned.add(detail_tab, minsize=320)
        self.view_notebook.add(work_tab, text="Rekordok")

        # The settings page manages the source profiles stored in the database.
        settings_tab = ttk.Frame(self.view_notebook, padding=10)
        settings_tab.columnconfigure(0, weight=1)
        settings_tab.rowconfigure(2, weight=1)
        general_settings_form = ttk.LabelFrame(settings_tab, text="Adatbetöltés beállításai", padding=10)
        general_settings_form.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        general_settings_form.columnconfigure(0, weight=1)
        general_settings_form.columnconfigure(1, weight=1)
        # Two side-by-side columns of label/value rows, rather than one long single column.
        settings_col_left = ttk.Frame(general_settings_form)
        settings_col_left.grid(row=0, column=0, sticky="new")
        settings_col_left.columnconfigure(1, weight=1)
        settings_col_right = ttk.Frame(general_settings_form)
        settings_col_right.grid(row=0, column=1, sticky="new", padx=(24, 0))
        settings_col_right.columnconfigure(1, weight=1)

        ttk.Label(settings_col_left, text="Egymás utáni azonos rekordok száma").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=3)
        unchanged_round_limit_row = ttk.Frame(settings_col_left)
        unchanged_round_limit_row.grid(row=0, column=1, sticky="w", pady=3)
        ttk.Entry(unchanged_round_limit_row, textvariable=self.unchanged_round_limit_var, width=10).pack(side="left")
        ttk.Label(unchanged_round_limit_row, text="db").pack(side="left", padx=(4, 0))
        ttk.Label(settings_col_right, text="Lista/Részletek panel arány").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=3)
        self.pane_ratio_var = tk.StringVar(value=f"{self.list_pane_ratio * 100:.0f}%")
        ttk.Label(settings_col_right, textvariable=self.pane_ratio_var).grid(row=0, column=1, sticky="w", pady=3)

        ttk.Label(settings_col_left, text="Rekordlista limit (üres = mind)").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=3)
        self.record_list_limit_var = tk.StringVar(value=self.database.get_setting("record_list_limit", "500"))
        self.record_list_limit_var.trace_add(
            "write", lambda *_args: self.database.set_setting("record_list_limit", self.record_list_limit_var.get().strip())
        )
        record_list_limit_row = ttk.Frame(settings_col_left)
        record_list_limit_row.grid(row=1, column=1, sticky="w", pady=3)
        ttk.Entry(record_list_limit_row, textvariable=self.record_list_limit_var, width=10).pack(side="left")
        ttk.Label(record_list_limit_row, text="db").pack(side="left", padx=(4, 0))
        ttk.Label(settings_col_right, text="Ablak mérete").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Label(settings_col_right, textvariable=self.window_size_var).grid(row=1, column=1, sticky="w", pady=3)

        column_width_labels = {"id": "ID oszlop szélessége", "source": "Forrás oszlop szélessége", "title": "Cím oszlop szélessége"}
        column_width_positions = {"id": (settings_col_left, 2), "source": (settings_col_right, 2), "title": (settings_col_left, 3)}
        for column in self.persisted_columns:
            parent, row = column_width_positions[column]
            ttk.Label(parent, text=column_width_labels[column]).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
            self.column_width_vars[column].trace_add(
                "write", lambda *_args, column=column: self.apply_column_width_setting(column)
            )
            column_width_row = ttk.Frame(parent)
            column_width_row.grid(row=row, column=1, sticky="w", pady=3)
            ttk.Entry(column_width_row, textvariable=self.column_width_vars[column], width=10).pack(side="left")
            ttk.Label(column_width_row, text="px").pack(side="left", padx=(4, 0))

        ttk.Label(settings_col_right, text="Kinézet").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=3)
        self.theme_var = tk.StringVar(value=THEME_LABELS.get(self.database.get_setting("theme", "light"), THEME_LABELS["light"]))
        theme_combo = ttk.Combobox(
            settings_col_right, textvariable=self.theme_var, values=list(THEME_LABELS.values()), state="readonly", width=14
        )
        theme_combo.grid(row=3, column=1, sticky="w", pady=3)
        theme_combo.bind("<<ComboboxSelected>>", self.on_theme_selected)

        ttk.Label(settings_col_left, text="Átnézés ablak csempeszáma").grid(row=4, column=0, sticky="w", padx=(0, 8), pady=3)
        self.review_tile_count_var = tk.StringVar(value=self.database.get_setting("review_tile_count", "9"))
        review_tile_count_combo = ttk.Combobox(
            settings_col_left,
            textvariable=self.review_tile_count_var,
            values=[str(value) for value in REVIEW_TILE_COUNT_GRID],
            state="readonly",
            width=12,
        )
        review_tile_count_combo.grid(row=4, column=1, sticky="w", pady=3)
        review_tile_count_combo.bind(
            "<<ComboboxSelected>>",
            lambda _event: self.database.set_setting("review_tile_count", self.review_tile_count_var.get()),
        )
        self.review_maximized_var = tk.BooleanVar(value=self.database.get_setting("review_window_state", "normal") == "zoomed")
        ttk.Checkbutton(
            settings_col_right,
            text="Átnézés ablak maximalizálva induljon",
            variable=self.review_maximized_var,
            command=lambda: self.database.set_setting(
                "review_window_state", "zoomed" if self.review_maximized_var.get() else "normal"
            ),
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=3)

        self.maximized_var = tk.BooleanVar(value=self.database.get_setting("window_state", "normal") == "zoomed")
        ttk.Checkbutton(
            settings_col_left,
            text="Főablak maximalizálva induljon",
            variable=self.maximized_var,
            command=self.on_maximized_toggle,
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=3)
        settings_form = ttk.LabelFrame(settings_tab, text="Forrásprofil beállításai", padding=10)
        settings_form.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        settings_form.columnconfigure(1, weight=1)
        self.settings_name_var = tk.StringVar()
        self.settings_category_var = tk.StringVar()
        self.settings_base_url_var = tk.StringVar()
        self.settings_parser_var = tk.StringVar(value="generic")
        self.settings_listing_mode_var = tk.StringVar(value=LISTING_MODE_LABELS["single_page"])
        self.settings_active_var = tk.BooleanVar(value=True)
        ttk.Label(settings_form, text="Név").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Entry(settings_form, textvariable=self.settings_name_var).grid(row=0, column=1, sticky="ew", pady=3)
        ttk.Label(settings_form, text="Kategória").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Entry(settings_form, textvariable=self.settings_category_var).grid(row=1, column=1, sticky="ew", pady=3)
        ttk.Label(settings_form, text="Lista URL").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Entry(settings_form, textvariable=self.settings_base_url_var).grid(row=2, column=1, sticky="ew", pady=3)
        ttk.Label(settings_form, text="Parser azonosító").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Entry(settings_form, textvariable=self.settings_parser_var).grid(row=3, column=1, sticky="ew", pady=3)
        ttk.Label(settings_form, text="Lista mód").grid(row=4, column=0, sticky="w", padx=(0, 8), pady=3)
        self.settings_listing_mode_combo = ttk.Combobox(
            settings_form,
            textvariable=self.settings_listing_mode_var,
            values=list(LISTING_MODE_LABELS.values()),
            state="readonly",
        )
        self.settings_listing_mode_combo.grid(row=4, column=1, sticky="ew", pady=3)
        ttk.Checkbutton(settings_form, text="Aktív forrás", variable=self.settings_active_var).grid(row=5, column=1, sticky="w", pady=3)
        ttk.Label(settings_form, text="Utolsó lekérdezés (ÉÉÉÉ-HH-NN ÓÓ:PP:MP)").grid(row=6, column=0, sticky="w", padx=(0, 8), pady=3)
        self.settings_last_fetched_var = tk.StringVar()
        ttk.Entry(settings_form, textvariable=self.settings_last_fetched_var).grid(row=6, column=1, sticky="ew", pady=3)
        settings_actions = ttk.Frame(settings_form)
        settings_actions.grid(row=7, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(settings_actions, text="Új profil", command=self.new_source_profile).pack(side="left", padx=(0, 6))
        ttk.Button(settings_actions, text="Mentés", command=self.save_source_profile).pack(side="left", padx=(0, 6))
        ttk.Button(settings_actions, text="Kijelölt törlése", command=self.delete_source_profile).pack(side="left")

        # The source table gives a compact overview of all configured processors.
        source_list_frame = ttk.Frame(settings_tab)
        source_list_frame.grid(row=2, column=0, sticky="nsew")
        source_list_frame.columnconfigure(0, weight=1)
        source_list_frame.rowconfigure(0, weight=1)
        self.settings_tree = ttk.Treeview(
            source_list_frame,
            columns=("name", "category", "base_url", "parser", "listing", "active", "last_run"),
            show="headings",
            selectmode="browse",
        )
        settings_headings = {
            "name": "Név",
            "category": "Kategória",
            "base_url": "Alap URL",
            "parser": "Parser",
            "listing": "Lista mód",
            "active": "Aktív",
            "last_run": "Utolsó lekérdezés",
        }
        settings_widths = {"name": 130, "category": 130, "base_url": 250, "parser": 130, "listing": 140, "active": 70, "last_run": 140}
        for column in settings_headings:
            self.settings_tree.heading(column, text=settings_headings[column])
            self.settings_tree.column(column, width=settings_widths[column], anchor="w")
        self.settings_tree.grid(row=0, column=0, sticky="nsew")
        settings_scrollbar = ttk.Scrollbar(source_list_frame, orient="vertical", command=self.settings_tree.yview)
        settings_scrollbar.grid(row=0, column=1, sticky="ns")
        self.settings_tree.configure(yscrollcommand=settings_scrollbar.set)
        self.settings_tree.bind("<<TreeviewSelect>>", self.on_source_select)
        self.settings_source_id: int | None = None
        self.refresh_source_settings()
        self.update_last_run_info()
        self.view_notebook.add(settings_tab, text="Beállítások")
        self.apply_theme(self.database.get_setting("theme", "light"))

    # Reloads the settings table and the active-source dropdown after every change.
    def refresh_source_settings(self) -> None:
        all_sources = self.database.all_sources()
        self.source_profiles = [source for source in all_sources if source["active"]]
        self.source_ids = {source["name"]: source["id"] for source in self.source_profiles}
        source_names = [source["name"] for source in self.source_profiles]
        self.source_combo.configure(values=source_names)
        if self.source_var.get() not in source_names:
            self.source_var.set(source_names[0] if source_names else "")
        for item_id in self.settings_tree.get_children():
            self.settings_tree.delete(item_id)
        for source in all_sources:
            self.settings_tree.insert(
                "",
                "end",
                iid=str(source["id"]),
                values=(
                    source["name"],
                    source["category"],
                    source["base_url"],
                    source["parser_type"],
                    LISTING_MODE_LABELS.get(source["listing_mode"], source["listing_mode"]),
                    "Igen" if source["active"] else "Nem",
                    source["last_fetched_at"] or "-",
                ),
            )

    # Copies one selected source row into the editable settings form.
    def on_source_select(self, _event: object) -> None:
        selected = self.settings_tree.selection()
        if not selected:
            return
        source_id = int(selected[0])
        source = next((item for item in self.database.all_sources() if item["id"] == source_id), None)
        if source is None:
            return
        self.settings_source_id = source_id
        self.settings_name_var.set(source["name"])
        self.settings_category_var.set(source["category"])
        self.settings_base_url_var.set(source["base_url"])
        self.settings_parser_var.set(source["parser_type"])
        self.settings_listing_mode_var.set(LISTING_MODE_LABELS.get(source["listing_mode"], LISTING_MODE_LABELS["single_page"]))
        self.settings_active_var.set(bool(source["active"]))
        self.settings_last_fetched_var.set(source["last_fetched_at"] or "")

    # Clears the form so the next save creates a new source profile.
    def new_source_profile(self) -> None:
        self.settings_source_id = None
        self.settings_name_var.set("")
        self.settings_category_var.set("")
        self.settings_base_url_var.set("")
        self.settings_parser_var.set("generic")
        self.settings_listing_mode_var.set(LISTING_MODE_LABELS["single_page"])
        self.settings_active_var.set(True)
        self.settings_last_fetched_var.set("")
        self.settings_tree.selection_remove(self.settings_tree.selection())

    # Validates and persists the source profile currently shown in the form.
    def save_source_profile(self) -> None:
        name = self.settings_name_var.get().strip()
        category = self.settings_category_var.get().strip()
        base_url = self.settings_base_url_var.get().strip().rstrip("/")
        parser_type = self.settings_parser_var.get().strip().lower()
        listing_mode = next((key for key, label in LISTING_MODE_LABELS.items() if label == self.settings_listing_mode_var.get()), "single_page")
        if not name or not category or not parser_type:
            messagebox.showwarning("Hiányzó beállítás", "A név, kategória és parser azonosító kötelező.")
            return
        if base_url and not base_url.startswith(("http://", "https://")):
            messagebox.showwarning("Hibás alap URL", "Az alap URL http:// vagy https:// kezdetű legyen.")
            return
        last_fetched_text = self.settings_last_fetched_var.get().strip()
        if last_fetched_text:
            try:
                datetime.strptime(last_fetched_text, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                messagebox.showwarning(
                    "Hibás dátum",
                    "Az utolsó lekérdezés ideje ÉÉÉÉ-HH-NN ÓÓ:PP:MP formátumú legyen (pl. 2026-09-09 14:08:00), vagy hagyd üresen.",
                )
                return
        try:
            if self.settings_source_id is None:
                new_source_id = self.database.add_source(name, category, base_url, parser_type, listing_mode, self.settings_active_var.get())
                self.database.set_source_last_fetched(new_source_id, last_fetched_text)
            else:
                self.database.update_source(self.settings_source_id, name, category, base_url, parser_type, listing_mode, self.settings_active_var.get())
                self.database.set_source_last_fetched(self.settings_source_id, last_fetched_text)
        except sqlite3.IntegrityError:
            messagebox.showerror("Mentési hiba", "Ezzel a névvel és alap URL-lel már létezik forrásprofil.")
            return
        self.refresh_source_settings()
        self.update_last_run_info()
        self.load_records()

    # Deletes the selected source profile after asking for confirmation.
    def delete_source_profile(self) -> None:
        if self.settings_source_id is None:
            return
        if not messagebox.askyesno("Forrás törlése", "Biztosan törlöd a kijelölt forrásprofilt?"):
            return
        self.database.delete_source(self.settings_source_id)
        self.new_source_profile()
        self.refresh_source_settings()
        self.load_records()

    # Reads the "Rekordlista limit" setting; an empty or invalid value means "load all records".
    def get_record_list_limit(self) -> int | None:
        text = self.record_list_limit_var.get().strip()
        if not text:
            return None
        try:
            value = int(text)
        except ValueError:
            return None
        return value if value > 0 else None

    def load_records(self, reset_offset: bool = True) -> None:
        if reset_offset:
            self.record_offset = 0
        self.records_status_var.set("Rekordlista betöltése folyamatban...")
        self.load_records_button.configure(state="disabled")
        self.update_idletasks()
        limit = self.get_record_list_limit()
        self.records = self.database.all(
            limit,
            self.record_offset,
            include_viewed=self.include_viewed_var.get(),
            only_interested=self.only_interested_var.get(),
            only_imported=self.only_imported_var.get(),
        )
        for item_id in self.record_tree.get_children():
            self.record_tree.delete(item_id)
        for position, record in enumerate(self.records, start=1):
            interest = "Érdekel" if record["interested"] else "Nem érdekel" if record["not_interested"] else "-"
            image_path = self.database.local_image_path(record["id"], record["image_path"])
            self.record_tree.insert(
                "",
                "end",
                iid=str(record["id"]),
                values=(record["id"], record["source_name"] or "Egyéb", record["title"], record["url"], image_path or "Nincs letöltött kép", record["created_at"], "Igen" if record["viewed"] else "Nem", interest, "Igen" if record["imported"] else "Nem"),
            )
            if position % 50 == 0 and self.startup_window.winfo_exists():
                self.update_startup_status(f"Rekordlista felépítése: {position}/{len(self.records)}")
        self.index = -1
        self.show_current()
        if limit is None:
            self.records_status_var.set(f"{len(self.records)} rekord betöltve.")
        else:
            start = self.record_offset + 1 if self.records else 0
            end = self.record_offset + len(self.records)
            self.records_status_var.set(f"{len(self.records)} rekord betöltve ({start}–{end}).")
        self.prev_batch_button.configure(state="normal" if limit is not None and self.record_offset > 0 else "disabled")
        self.next_batch_button.configure(state="normal" if limit is not None and len(self.records) == limit else "disabled")
        self.load_records_button.configure(state="normal")

    # Loads the previous batch of records when the record list is limited by the settings.
    def load_previous_batch(self) -> None:
        limit = self.get_record_list_limit()
        if limit is None or self.record_offset <= 0:
            return
        self.record_offset = max(0, self.record_offset - limit)
        self.load_records(reset_offset=False)

    # Loads the next batch of records when the record list is limited by the settings.
    def load_next_batch(self) -> None:
        limit = self.get_record_list_limit()
        if limit is None:
            return
        self.record_offset += limit
        self.load_records(reset_offset=False)

    # Returns when the currently selected source's list URL was last queried, or None if
    # no specific source is selected or it has never been queried.
    def get_selected_source_last_run(self) -> datetime | None:
        source_id = self.source_ids.get(self.source_var.get())
        if source_id is None:
            return None
        source = self.database.source_by_id(source_id)
        last_fetched_at = source["last_fetched_at"] if source is not None else None
        if not last_fetched_at:
            return None
        return datetime.strptime(last_fetched_at, "%Y-%m-%d %H:%M:%S")

    # Shows when the selected source's list URL was last queried, and suggests how many
    # hours to look back (rounded up to a whole hour so nothing published since is missed).
    def update_last_run_info(self, _event: object | None = None) -> None:
        source_id = self.source_ids.get(self.source_var.get())
        last_run = self.get_selected_source_last_run()
        if source_id is None:
            self.last_run_var.set("")
        elif last_run is None:
            self.last_run_var.set("Nincs korábbi lekérdezés")
        else:
            self.last_run_var.set(f"Utolsó: {last_run.strftime('%Y-%m-%d %H:%M')}")
        self.update_suggested_hours(last_run)

    def update_suggested_hours(self, last_run: datetime | None) -> None:
        if last_run is None:
            suggested_hours = 24
        else:
            elapsed_hours = (datetime.now() - last_run).total_seconds() / 3600
            suggested_hours = max(1, math.ceil(elapsed_hours))
        self.suggested_time_var.set(f"Javasolt: {suggested_hours} óra")

    # Keeps the suggested hour count live as time passes, instead of freezing at whatever
    # it was when the source was last selected.
    def tick_suggested_time(self) -> None:
        self.update_suggested_hours(self.get_selected_source_last_run())
        self.after(30_000, self.tick_suggested_time)

    def on_tree_select(self, _event: object) -> None:
        if self.ignore_tree_event:
            return
        selected = self.record_tree.selection()
        if not selected:
            return
        selected_id = int(selected[0])
        self.index = next((position for position, record in enumerate(self.records) if record["id"] == selected_id), -1)
        if self.index >= 0:
            self.show_current()

    def show_current(self) -> None:
        if not self.records or self.index < 0:
            self.title_var.set("Nincs kiválasztott rekord")
            self.url_label.configure(text="")
            self.date_var.set("")
            self.position_var.set("0 / 0")
            self.current_image_path = ""
            self.image_zoom = 1.0
            self.zoom_var.set("100%")
            self.photo = None
            self.image_label.configure(text="Nincs helyi kép", image="")
            return
        record = self.records[self.index]
        self.title_var.set(record["title"])
        self.url_label.configure(text=record["url"])
        self.current_image_path = self.database.local_image_path(record["id"], record["image_path"])
        self.date_var.set(f"Létrehozva: {record['created_at']}   |   Megtekintve: {record['viewed_at'] or '-'}")
        self.image_zoom = 1.0
        self.zoom_var.set("100%")
        self.render_image()
        self.viewed_var.set(bool(record["viewed"]))
        self.interested_var.set(bool(record["interested"]))
        self.not_interested_var.set(bool(record["not_interested"]))
        self.imported_var.set(bool(record["imported"]))
        self.position_var.set(f"{self.index + 1} / {len(self.records)}")
        self.previous_button.configure(state="normal" if self.index > 0 else "disabled")
        self.next_button.configure(state="normal" if self.index < len(self.records) - 1 else "disabled")

    # Renders the current record's image scaled to fit the available label area, times the zoom factor.
    def render_image(self) -> None:
        self.photo = None
        if not self.current_image_path:
            self.image_label.configure(text="Nincs helyi kép", image="")
            return
        try:
            with Image.open(self.current_image_path) as source_image:
                image = source_image.convert("RGB")
        except (OSError, ValueError):
            self.image_label.configure(text="A helyi kép nem tölthető be.", image="")
            return
        available_width = self.image_label.winfo_width()
        available_height = self.image_label.winfo_height()
        if available_width <= 1 or available_height <= 1:
            available_width, available_height = 480, 320
        fit_scale = min(available_width / image.width, available_height / image.height)
        scale = max(fit_scale * self.image_zoom, 0.02)
        display_size = (max(int(image.width * scale), 1), max(int(image.height * scale), 1))
        image = image.resize(display_size, Image.LANCZOS)
        self.photo = ImageTk.PhotoImage(image)
        self.image_label.configure(image=self.photo, text="")

    # Re-renders the image when the details pane is resized (e.g. by dragging the list divider).
    def on_image_area_resized(self, _event: object) -> None:
        if not self.current_image_path:
            return
        if self.resize_render_after_id is not None:
            self.after_cancel(self.resize_render_after_id)
        self.resize_render_after_id = self.after(120, self.render_after_resize)

    def render_after_resize(self) -> None:
        self.resize_render_after_id = None
        self.render_image()

    def zoom_image_in(self) -> None:
        if not self.current_image_path:
            return
        self.image_zoom = min(self.image_zoom * 1.25, 6.0)
        self.zoom_var.set(f"{self.image_zoom * 100:.0f}%")
        self.render_image()

    def zoom_image_out(self) -> None:
        if not self.current_image_path:
            return
        self.image_zoom = max(self.image_zoom / 1.25, 0.1)
        self.zoom_var.set(f"{self.image_zoom * 100:.0f}%")
        self.render_image()

    def zoom_image_reset(self) -> None:
        if not self.current_image_path:
            return
        self.image_zoom = 1.0
        self.zoom_var.set("100%")
        self.render_image()

    # Validates the query settings and starts loading the selected source list URL.
    def load_source_data(self) -> None:
        source_id = self.source_ids.get(self.source_var.get())
        if source_id is None:
            messagebox.showwarning("Nincs forrás", "Válassz konkrét forrást az adatbetöltéshez.")
            return
        try:
            hours = int(self.hours_var.get().strip())
            if hours <= 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning("Hibás óraszám", "A visszamenőleges órák száma pozitív egész szám legyen.")
            return
        max_count_text = self.max_count_var.get().strip()
        if max_count_text:
            try:
                max_count = int(max_count_text)
                if max_count <= 0:
                    raise ValueError
            except ValueError:
                messagebox.showwarning("Hibás darabszám", "A maximum darabszám üres vagy pozitív egész szám legyen.")
                return
        else:
            max_count = None
        try:
            unchanged_round_limit = int(self.unchanged_round_limit_var.get().strip())
            if unchanged_round_limit <= 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning("Hibás ismétlési érték", "Az egymás utáni azonos rekordok száma pozitív egész szám legyen.")
            return
        source = self.database.source_by_id(source_id)
        if source is None or not source["base_url"]:
            messagebox.showwarning("Hiányzó lista URL", "A kiválasztott forráshoz állíts be lista URL-t a Beállítások oldalon.")
            return
        self.add_button.configure(state="disabled")
        self.cancel_event.clear()
        self.current_run_record_ids = []
        self.pending_cancel_delete = False
        self.status_var.set(f"Adatbetöltés folyamatban az elmúlt {hours} órából...")
        self.progress_bar.configure(mode="indeterminate", value=0)
        self.progress_bar.start(12)
        self.show_load_window()
        self.load_window_progress.start(12)
        self.load_window_status.set("Böngésző indítása...")
        threading.Thread(
            target=self.fetch_and_add,
            args=(source["base_url"], source_id, hours, max_count, unchanged_round_limit),
            daemon=True,
        ).start()

    # Asks whether to keep or discard this run's already-downloaded records, then signals the
    # background thread to stop. Records saved in earlier runs are never touched.
    def cancel_download(self) -> None:
        if not messagebox.askyesno("Letöltés megszakítása", "Biztosan megszakítod a folyamatban lévő letöltést?"):
            return
        self.pending_cancel_delete = messagebox.askyesno(
            "Eddig letöltött rekordok",
            "Töröljük az ebben a menetben eddig letöltött rekordokat?\n"
            "(A korábban mentett rekordok mindenképp megmaradnak.)",
        )
        self.cancel_event.set()
        self.load_window_status.set("Megszakítás folyamatban...")

    # Downloads, parses and stores source data using the selected list profile.
    def fetch_and_add(
        self,
        url: str,
        selected_source_id: int,
        hours: int,
        max_count: int | None,
        unchanged_round_limit: int,
    ) -> None:
        try:
            source = self.database.source_by_id(selected_source_id)
            if source is None:
                raise ValueError("A kiválasztott forrás már nem aktív.")
            self.database.set_source_last_fetched(source["id"], now_text())
            self.after(0, self.update_last_run_info)
            if source["parser_type"] == "makerworld":
                # MakerWorld's search JSON API now sits behind Cloudflare too (see
                # sources/makerworld.py's fetch_listing docstring), so this goes through a
                # browser window like Thingiverse - but it still stops as soon as it finds a
                # model older than `hours`, rather than needing a fixed max_count to ever stop.
                models = makerworld.fetch_listing(
                    url,
                    MAKERWORLD_PROFILE_DIR,
                    hours,
                    max_count,
                    lambda count: self.report_progress(count, "Modellek felderítve"),
                    cancel_check=self.cancel_event.is_set,
                    status_callback=self.report_status,
                )
                self.after(0, lambda: self.progress_bar.stop())
                self.after(0, lambda: self.progress_bar.configure(mode="determinate", maximum=max(len(models), 1), value=0))
                added_count = 0
                consecutive_existing = 0
                cancelled = False
                asked_continue = False
                total_models = len(models)
                for position, (model_url, title, image_url, model_created_at) in enumerate(models, start=1):
                    if self.cancel_event.is_set():
                        cancelled = True
                        break
                    if self.database.record_exists(model_url):
                        consecutive_existing += 1
                        self.report_phase(position, total_models, title, "már megvan, kihagyva")
                        if consecutive_existing >= unchanged_round_limit and not asked_continue:
                            asked_continue = True
                            remaining = len(models) - position
                            if remaining > 0 and not self.confirm_continue_scan(unchanged_round_limit, remaining):
                                cancelled = self.cancel_event.is_set()
                                break
                            consecutive_existing = 0
                        continue
                    consecutive_existing = 0
                    self.report_phase(position, total_models, title, "rekord készítése")
                    record_id = self.database.add(model_url, title, image_url, source["id"], model_created_at)
                    self.current_run_record_ids.append(record_id)
                    if image_url:
                        self.report_phase(position, total_models, title, "kép másolása")
                        try:
                            image_data = download_image_data(image_url)
                            if image_data:
                                self.database.save_image(record_id, image_data)
                        except (OSError, ValueError, urllib.error.URLError):
                            pass
                    added_count += 1
                    self.report_phase(position, total_models, title, "kész")
                if cancelled and self.pending_cancel_delete:
                    for saved_id in self.current_run_record_ids:
                        self.database.delete(saved_id)
                    added_count = 0
                self.after(0, lambda: self.add_finished(added_count, len(models), cancelled=cancelled))
                return
            if source["parser_type"] == "printables":
                # Printables' own search API (see sources/printables.py's fetch_listing
                # docstring) is used directly, same approach as MakerWorld: no browser, stops
                # at the date cutoff instead of needing max_count to ever stop at all.
                models = printables.fetch_listing(
                    url,
                    hours,
                    max_count,
                    lambda count: self.report_progress(count, "Modellek felderítve"),
                    cancel_check=self.cancel_event.is_set,
                    status_callback=self.report_status,
                )
                self.after(0, lambda: self.progress_bar.stop())
                self.after(0, lambda: self.progress_bar.configure(mode="determinate", maximum=max(len(models), 1), value=0))
                added_count = 0
                consecutive_existing = 0
                cancelled = False
                asked_continue = False
                total_models = len(models)
                for position, (model_url, title, image_url, model_created_at) in enumerate(models, start=1):
                    if self.cancel_event.is_set():
                        cancelled = True
                        break
                    if self.database.record_exists(model_url):
                        consecutive_existing += 1
                        self.report_phase(position, total_models, title, "már megvan, kihagyva")
                        if consecutive_existing >= unchanged_round_limit and not asked_continue:
                            asked_continue = True
                            remaining = len(models) - position
                            if remaining > 0 and not self.confirm_continue_scan(unchanged_round_limit, remaining):
                                cancelled = self.cancel_event.is_set()
                                break
                            consecutive_existing = 0
                        continue
                    consecutive_existing = 0
                    self.report_phase(position, total_models, title, "rekord készítése")
                    record_id = self.database.add(model_url, title, image_url, source["id"], model_created_at)
                    self.current_run_record_ids.append(record_id)
                    if image_url:
                        self.report_phase(position, total_models, title, "kép másolása")
                        try:
                            image_data = download_image_data(image_url)
                            if image_data:
                                self.database.save_image(record_id, image_data)
                        except (OSError, ValueError, urllib.error.URLError):
                            pass
                    added_count += 1
                    self.report_phase(position, total_models, title, "kész")
                if cancelled and self.pending_cancel_delete:
                    for saved_id in self.current_run_record_ids:
                        self.database.delete(saved_id)
                    added_count = 0
                self.after(0, lambda: self.add_finished(added_count, len(models), cancelled=cancelled))
                return
            if source["parser_type"] == "thingiverse":
                # Thingiverse pages through numbered search-result pages instead of infinite
                # scroll, and (unlike MakerWorld/Printables) still needs a real, Cloudflare-
                # clearing browser window; it also opens each thing's own page to read its
                # publish date (day precision only) and stops at the `hours` cutoff just like
                # the other two - see sources/thingiverse.py's fetch_listing docstring.
                models = thingiverse.fetch_listing(
                    url,
                    THINGIVERSE_PROFILE_DIR,
                    LEGACY_THINGIVERSE_PROFILE_DIR,
                    hours,
                    max_count,
                    unchanged_round_limit,
                    lambda count: self.report_progress(count, "Modellek felderítve"),
                    cancel_check=self.cancel_event.is_set,
                    status_callback=self.report_status,
                )
                self.after(0, lambda: self.progress_bar.stop())
                self.after(0, lambda: self.progress_bar.configure(mode="determinate", maximum=max(len(models), 1), value=0))
                added_count = 0
                consecutive_existing = 0
                cancelled = False
                asked_continue = False
                total_models = len(models)
                for position, (model_url, title, image_url, model_created_at) in enumerate(models, start=1):
                    if self.cancel_event.is_set():
                        cancelled = True
                        break
                    if self.database.record_exists(model_url):
                        consecutive_existing += 1
                        self.report_phase(position, total_models, title, "már megvan, kihagyva")
                        if consecutive_existing >= unchanged_round_limit and not asked_continue:
                            asked_continue = True
                            remaining = len(models) - position
                            if remaining > 0 and not self.confirm_continue_scan(unchanged_round_limit, remaining):
                                cancelled = self.cancel_event.is_set()
                                break
                            consecutive_existing = 0
                        continue
                    consecutive_existing = 0
                    self.report_phase(position, total_models, title, "rekord készítése")
                    record_id = self.database.add(model_url, title, image_url, source["id"], model_created_at)
                    self.current_run_record_ids.append(record_id)
                    if image_url:
                        self.report_phase(position, total_models, title, "kép másolása")
                        try:
                            image_data = download_image_data(image_url)
                            if image_data:
                                self.database.save_image(record_id, image_data)
                        except (OSError, ValueError, urllib.error.URLError):
                            pass
                    added_count += 1
                    self.report_phase(position, total_models, title, "kész")
                if cancelled and self.pending_cancel_delete:
                    for saved_id in self.current_run_record_ids:
                        self.database.delete(saved_id)
                    added_count = 0
                self.after(0, lambda: self.add_finished(added_count, len(models), cancelled=cancelled))
                return
            # Fallback for any source that isn't one of the three listing scrapers above (the
            # "Egyéb" profile): the URL itself is treated as a single page to record, not a
            # list to crawl - hours is unused here, there's just the one page and nothing to
            # cut off by date. All three listing scrapers above do use it now, to cut their
            # search results off at the right age instead of relying on max_count/record_exists.
            title, image_url = fetch_page(url, source["parser_type"])
            record_id = self.database.add(url, title, image_url, source["id"])
            if image_url:
                try:
                    image_data = download_image_data(image_url)
                    if image_data:
                        self.database.save_image(record_id, image_data)
                except (OSError, ValueError, urllib.error.URLError):
                    pass
            self.after(0, lambda: self.progress_bar.stop())
            self.after(0, lambda: self.progress_bar.configure(mode="determinate", maximum=1, value=1))
            self.after(0, lambda: self.source_var.set(source["name"]))
            self.after(0, lambda: self.add_finished(1, 1))
        except Exception as error:
            # Catches anything, including Playwright's own exception types: this runs on a
            # background thread, so an uncaught error here would silently kill it, leaving the
            # "Adatbetöltés folyamatban" window and its progress bar frozen forever.
            self.after(0, lambda error=error: self.add_failed(str(error) or type(error).__name__))

    # Pauses the background download thread and asks the user (on the UI thread) whether
    # to keep examining the remaining items after too many consecutive already-known records.
    def confirm_continue_scan(self, threshold: int, remaining_count: int) -> bool:
        result: dict[str, bool] = {}
        event = threading.Event()

        def ask() -> None:
            result["value"] = messagebox.askyesno(
                "Folytatás",
                f"Már {threshold} egymás utáni, korábban mentett rekordot találtam.\n"
                f"Szeretnéd folytatni a fennmaradó {remaining_count} tétel vizsgálatát?",
            )
            event.set()

        self.after(0, ask)
        event.wait()
        return result.get("value", False)

    def report_progress(self, count: int, label: str) -> None:
        self.after(0, lambda count=count: self.progress_bar.configure(value=count))
        self.after(0, lambda label=label, count=count: self.status_var.set(f"{label}: {count}"))
        self.after(0, lambda label=label, count=count: self.load_window_status.set(f"{label}: {count}") if self.load_window is not None else None)

    # Detailed per-model status: shows which model is being processed and in which phase
    # (title/record creation, image copying, ...), so a slow run is still legible.
    def report_phase(self, position: int, total: int, title: str, phase: str) -> None:
        text = f"[{position}/{total}] {title} - {phase}"
        self.after(0, lambda count=position: self.progress_bar.configure(value=count))
        self.after(0, lambda text=text: self.status_var.set(text))
        self.after(0, lambda text=text: self.load_window_status.set(text) if self.load_window is not None else None)

    # Plain status text without a count, used e.g. while waiting out a Cloudflare interstitial.
    def report_status(self, text: str) -> None:
        self.after(0, lambda text=text: self.status_var.set(text))
        self.after(0, lambda text=text: self.load_window_status.set(text) if self.load_window is not None else None)

    def add_finished(self, added_count: int, scanned_count: int, cancelled: bool = False) -> None:
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate", maximum=1, value=1)
        if self.load_window is not None:
            self.load_window_progress.stop()
            self.close_load_window()
        if cancelled:
            kept_or_deleted = "törölve" if self.pending_cancel_delete else "megtartva"
            self.status_var.set(f"Adatbetöltés megszakítva: {added_count} új rekord ({kept_or_deleted}), {scanned_count} modell átvizsgálva.")
        else:
            self.status_var.set(f"Adatbetöltés kész: {added_count} új rekord, {scanned_count} modell átvizsgálva.")
        self.add_button.configure(state="normal")
        self.load_records()

    def add_failed(self, error: str) -> None:
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate", maximum=1, value=0)
        if self.load_window is not None:
            self.load_window_progress.stop()
            self.close_load_window()
        self.status_var.set("A beolvasás nem sikerült.")
        self.add_button.configure(state="normal")
        messagebox.showerror("Beolvasási hiba", error)

    # Saving re-runs load_records(), which re-queries the DB and can shrink/reorder the list
    # (e.g. the "Csak az érdekeltek" filter hiding a record the moment it's marked not
    # interested) - so the record's id, not its old list index, is used to relocate it
    # afterwards. auto_advance (Érdekel/Nem érdekel) also implies "seen", and moves on to
    # the next surviving record instead of staying put like a plain Megnézve toggle does.
    def save_flags(self, auto_advance: bool = False) -> None:
        if not self.records or self.index < 0:
            return
        current_id = self.records[self.index]["id"]
        current_index = self.index
        record = self.records[self.index]
        if auto_advance:
            self.viewed_var.set(True)
        self.database.update_flags(record["id"], self.viewed_var.get(), self.interested_var.get(), self.not_interested_var.get())
        self.load_records()
        if auto_advance:
            current_position = next((position for position, item in enumerate(self.records) if item["id"] == current_id), None)
            self.index = current_position + 1 if current_position is not None else current_index
            self.index = min(self.index, len(self.records) - 1)
        else:
            self.index = next((position for position, item in enumerate(self.records) if item["id"] == current_id), -1)
        if self.index >= 0:
            self.show_current()

    def previous_record(self) -> None:
        if self.index > 0:
            self.index -= 1
            self.show_current()

    def next_record(self) -> None:
        if self.index < len(self.records) - 1:
            self.index += 1
            self.show_current()

    # Deletes whichever record(s) are currently selected in the list (multi-select supported).
    def delete_selected_records(self) -> None:
        selected_ids = [int(item_id) for item_id in self.record_tree.selection()]
        if not selected_ids:
            return
        prompt = "Biztosan törlöd a kijelölt rekordot?" if len(selected_ids) == 1 else f"Biztosan törlöd a kijelölt {len(selected_ids)} rekordot?"
        if messagebox.askyesno("Törlés", prompt):
            for record_id in selected_ids:
                self.database.delete(record_id)
            self.load_records()

    def delete_viewed_not_interested(self) -> None:
        if not messagebox.askyesno(
            "Tömeges törlés",
            "Biztosan törlöd az összes megtekintett, nem érdekel rekordot?",
        ):
            return
        deleted_count = self.database.delete_viewed_not_interested()
        self.load_records()
        self.status_var.set(f"{deleted_count} megtekintett, nem érdekel rekord törölve.")

    # Exports the URLs of every watched+interested, not-yet-imported record to a plain text
    # file (one URL per line), then marks those records as imported so a later export only
    # ever picks up newly-interested records, never the same ones twice.
    def save_url_txt(self) -> None:
        pending = self.database.pending_import()
        if not pending:
            messagebox.showinfo("URL.txt mentése", "Nincs exportálható rekord (megnézett, érdekel, még nem importált).")
            return
        file_path = filedialog.asksaveasfilename(
            title="URL.txt mentése",
            initialfile="urls.txt",
            defaultextension=".txt",
            filetypes=[("Szöveges fájl", "*.txt"), ("Minden fájl", "*.*")],
        )
        if not file_path:
            return
        with open(file_path, "w", encoding="utf-8") as file:
            for row in pending:
                file.write(row["url"] + "\n")
        self.database.mark_imported([row["id"] for row in pending])
        self.load_records()
        self.status_var.set(f"{len(pending)} URL mentve és importáltra jelölve.")

    # Opens the modal tile-grid review window; ReviewWindow.close() refreshes the record
    # list once it exits, whichever way the user leaves it.
    def open_review_window(self) -> None:
        ReviewWindow(self)

    def clear_all_flags(self) -> None:
        if not messagebox.askyesno(
            "Jelölések törlése",
            "Biztosan törlöd a Megnézve, Érdekel, Nem érdekel és Importálva jelöléseket minden rekordnál?",
        ):
            return
        cleared_count = self.database.clear_all_flags()
        self.load_records()
        self.status_var.set(f"{cleared_count} rekord jelölése törölve.")

    def open_url(self, _event: object | None = None) -> None:
        if self.records and self.index >= 0:
            webbrowser.open(self.records[self.index]["url"])

    # Program name/version, changelog, description and detailed help, in that order, each
    # section separated by a horizontal rule - a single read-only, scrollable window rather
    # than several dialogs, since the user reads it top-to-bottom in one sitting.
    def show_info_dialog(self) -> None:
        colors = getattr(self, "current_theme", THEMES["light"])
        info_window = tk.Toplevel(self, background=colors["bg"])
        info_window.title("Információ")
        info_window.geometry("640x600")
        info_window.transient(self)
        container = ttk.Frame(info_window, padding=12)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(0, weight=1)
        text = tk.Text(
            container,
            wrap="word",
            font=("Segoe UI", 10),
            padx=8,
            pady=8,
            borderwidth=0,
            background=colors["entry_bg"],
            foreground=colors["fg"],
            insertbackground=colors["fg"],
        )
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scrollbar.set)
        text.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        text.tag_configure("heading", font=("Segoe UI", 12, "bold"), spacing3=6)
        text.tag_configure("rule", foreground=colors["border"])
        text.tag_configure("body", font=("Segoe UI", 10))

        def add_heading(heading_text: str) -> None:
            text.insert("end", heading_text + "\n", "heading")

        def add_rule() -> None:
            text.insert("end", ("─" * 70) + "\n\n", "rule")

        def add_body(body_text: str) -> None:
            text.insert("end", body_text.strip("\n") + "\n\n", "body")

        add_heading(f"3DModelScope {APP_VERSION}")
        add_rule()
        add_heading("Verzió módosítások")
        add_body(CHANGELOG)
        add_rule()
        add_heading("Mit csinál a program?")
        add_body(APP_DESCRIPTION)
        add_rule()
        add_heading("Részletes súgó")
        add_body(HELP_TEXT)
        text.configure(state="disabled")
        ttk.Button(info_window, text="Bezárás", command=info_window.destroy).pack(pady=(0, 12))


if __name__ == "__main__":
    App().mainloop()

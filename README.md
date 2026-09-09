# 3DModelScope

Egyszerű Windows desktop alkalmazás weboldalak adatainak gyűjtésére.

## Funkciók

- Weboldal URL beolvasása
- Cím kiolvasása az oldal title-jából
- Helyi SQLite adatbázis (`3DModelScope.db`) rekordokkal és linkekkel
- Forrásprofilok külön `sources` táblában: MakerWorld, Printables, Thingiverse és Egyéb
- URL alapján automatikus forrásfelismerés és `parser_type` választás
- Forrásonként beállítható lista mód: lapozós, folyamatos lista vagy egyetlen oldal
- A főoldali `Adatbetöltés` a Beállításokban tárolt lista URL-t használja
- A lekérdezéshez megadható, hány órára visszamenőleg dolgozzon
- Opcionális maximum darabszám a teszteléshez; üresen nincs darabszám-korlát
- Beállítható, hány egymás utáni változatlan listaeredmény után álljon le a MakerWorld betöltése
- Betöltési folyamatjelző és automatikus cookie-elutasítás MakerWorld esetén
- MakerWorld listaoldal feldolgozása böngészőből, Cloudflare/JavaScript támogatással
- Rekordok megtekintése egyenként, előző/következő navigációval, képek betöltése nélkül
- `Megnézve`, `Érdekel`, `Nem érdekel` jelölések és dátumok
- Rekordlista szűrése megnézett és érdekes rekordokra
- Tömeges törlés a megtekintett, nem érdekel rekordokra
- Link megnyitása az alapértelmezett böngészőben

## Indítás Pythonból

Telepített Python 3.10 vagy újabb szükséges.

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
python app.py
```

## EXE készítése

Dupla kattintással futtasd a `build_exe.bat` fájlt, vagy terminálból:

```powershell
.\build_exe.bat
```

Az elkészült program itt lesz:

```text
dist\3DModelScope.exe
```

Az adatbázis az EXE melletti mappában jön létre. A program a rekordok és linkek mellett a képeket az EXE melletti `3DScopImages` mappába tölti le, és a helyi fájlnevet az adatbázisban tárolja. Az oldal első beolvasásához internetkapcsolat szükséges.

## Forrásprofil formátuma

Minden támogatott oldal a `sources` táblában egy külön profilt kap:

```text
name        Megjelenített név
category    Kategória
	base_url    A forrás listaoldalának URL-je
parser_type Forrásspecifikus feldolgozó azonosító
listing_mode Lista kezelése: `paged`, `continuous` vagy `single_page`
active      Bekapcsolt profil-e
```

Az új, oldalanként eltérő feldolgozást a `parser_type` alapján lehet beépíteni. A jelenlegi közös HTML-feldolgozó a rekord címét és linkjét menti.

MakerWorld esetén az alkalmazás a telepített Microsoft Edge böngészőt használja headless módban, mert a közvetlen HTTP-kérést a webhely Cloudflare-védelme blokkolhatja. Az EXE futtatásához Microsoft Edge telepítése szükséges.

A főoldalon a forrás kiválasztása után az `Adatbetöltés` gomb a forrásprofil `Lista URL` mezőjét használja. A visszamenőleges óraszámot ugyanitt lehet megadni. A listaoldal konkrét modell-linkjeinek kibontását a forrásspecifikus parser végzi.

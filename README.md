# dvbi-check

Prüfwerkzeug für DVB-I Service Lists nach **DVB A177r6** (ETSI TS 103 770 V1.2.1), dem
**DVB-I Book Germany 1.0.1** und dem **DTVP DVB-I Implementierungsprofil V1.0.1**.
Es erzeugt eine Auswertung als HTML-Report, JSON und CSV.

| Modul | Prüft |
|---|---|
| `schema` | XSD-Validierung gegen das A177-Standardschema und – für A177r6 – das deutsche Profilschema; Content-Type der Auslieferung |
| `a177` | Semantik nach Standard: eindeutige IDs, tag:-URIs, Classification-Scheme-Werte, Regionen und PLZ-Überlappungen, LCN-Referenzen, Zustellparameter, Logos, Content-Guide-Verweise |
| `profile` | Vollständigkeit nach Book und DTVP: „M“-Felder aus IT-6, Direct Tuning DVB-S (IT-13), Broadband-Instanz (Book 7.3), DRM über Linked App (Book 11.3), Content-Guide-Proxy (Book 12), LCN-Plan nach Book 6.6 (Blöcke, LCN 7/41–49, 50/51, Radio-Block, Endkanal 3999), Pop-up-Kanäle (Book 6.9), Regionstiefe (Book 10.2); bei URL-Quellen zusätzlich ungültige PLZ (IT-5) und Conditional GET (IT-18) |
| `cg` | Content-Guide-Endpunkte (TV-Anytime): Now/Next, Zeitfenster für jeden Tag von −7 bis +7, Programminfo; TVA-XSD; Pflichtfelder nach DTVP IT-6 (Tab. 41–64); Tagesabdeckung, Lücken, Überlappungen; Stichprobe der EPG-Bilder |
| `images` | Logo-Endpunkte: Erreichbarkeit, Content-Type, Standardbreite 320 px, Dateigröße, Pflichtvarianten `16x9_colour`, `16x9_colour_light`, `16x9_colour_dark`, Transparenz, Breitenparameter `?w=` |

## Installation

Voraussetzung: Python 3.11 oder neuer.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
dvbi-check resources update     # lädt XSD-Schemas und Classification Schemes (einmalig)
```

Die Schemas stammen aus [paulhiggs/dvb-i-tools](https://github.com/paulhiggs/dvb-i-tools)
(BSD-2-Clause). Sie werden auf einen festen Commit gepinnt und unter `~/.cache/dvbi-check`
abgelegt (änderbar über `DVBI_CHECK_HOME`). Ist GitHub nicht erreichbar, kann mit
`--resources-dir` ein lokaler Klon angegeben werden.

## Verwendung

```bash
# Vollständige Prüfung einer Service List per URL
# (ggf. freigeschalteten User-Agent angeben, Book 11.1)
dvbi-check run https://<service-list-provider>/<pfad-zur-service-list> \
    --user-agent "<freigeschalteter User-Agent>" --out reports/

# Schneller Lauf: nur 10 Services im Content Guide, 2 Tage zurück und voraus
dvbi-check run <URL> --cg-max-services 10 --cg-days-past 2 --cg-days-future 2

# Regionalisierte Liste
dvbi-check run <URL> --postcode 06217

# Offline gegen eine Datei (Schema, A177, Profil)
dvbi-check run v6.xml --offline

# Nur bestimmte Module
dvbi-check run <URL> --modules cg,images

# Mit Konfigurationsdatei
cp dvbi-check.example.toml dvbi-check.toml
dvbi-check run <URL> --config dvbi-check.toml
```

Ausgabe in `--out` (Standard `dvbi-report/`):

- `dvbi-report-<Datum>.html` – eigenständiger Report: Kennzahlen, Modulstatus, Service-Matrix,
  EPG-Abdeckung je Tag, Logo-Tabelle, filterbare Befunde mit Spezifikationsverweis
- `.json` – vollständige Ergebnisse einschließlich Statistiken je Service
- `.csv` – alle Befunde (Semikolon, UTF-8 mit BOM, öffnet direkt in Excel)

Exit-Code: `1`, wenn Fehler gefunden wurden. Mit `--fail-on warning` gilt das auch für
Warnungen, mit `--fail-on never` nie. So lässt sich das Werkzeug in CI-Pipelines einsetzen.

## Bewertung

- **Fehler**: Verstoß gegen „shall“/„M“ aus A177, Book oder DTVP, oder ein technischer Defekt
  (Endpunkt nicht erreichbar, Schemafehler).
- **Warnung**: Verstoß gegen „should“, oder eine Auffälligkeit mit wahrscheinlicher Auswirkung
  beim Receiver.
- **Hinweis**: Information oder Abweichung vom Standard, die das deutsche Profil ausdrücklich
  zulässt (z. B. DVB-C-Triplets nur mit serviceId nach IT-12).

Im Profil `de` gilt das deutsche Profilschema als maßgeblich. Abweichungen vom Standardschema
werden dann als Warnung gemeldet, weil strikt validierende Clients die Liste ablehnen könnten.

## Konventionen und Annahmen

- Content-Guide-Abfragen wie im DVB-I-Referenzvalidator:
  `?sid=…&now_next=true`, `?sid=…&start=<epoch>&end=<epoch>`, `?pid=<crid>`.
  `sid` ist `ContentGuideServiceRef`, ersatzweise `UniqueIdentifier`.
- Tagesfenster werden in `cg_timezone` (Standard Europe/Berlin) gebildet. Vergangene Tage ohne
  Daten sind eine Warnung („should“), heute und kommende Tage ein Fehler („shall“).
- Logo-Varianten werden über `?image_variant=<wert>` abgefragt (A177 5.2.8.2.1). Ist der
  Parametername bei eurem Bildserver anders, lässt er sich per `image_variant_param` anpassen.
- Der LCN-Plan wird gegen die bundesweite Referenztabelle geprüft (TargetRegion = oberste
  Region). Die Regel `DE-LCN-7MAP` erkennt Fälle, in denen LCN 7 nicht zu dem Hauptprogramm
  passt, dessen Platz im Block 41–49 frei gelassen wurde.
- Die Erkennung von ARD, RTL und SAT.1 für die Regeln aus Book 6.6 erfolgt über reguläre
  Ausdrücke (`pattern_ard`, `pattern_rtl`, `pattern_sat1`), die sich konfigurieren lassen.

## Nicht automatisch prüfbar

- Sortierung innerhalb der Blöcke nach AGF-Daten, Genre oder Alphabet (Book 6.6)
- Pop-up-Kontingente je Anbieter (Book 6.9, benötigt AGF-Marktanteile)
- Inhaltliche Richtigkeit der Accessibility-Signalisierung (Book 15, DTVP IT-16)
- Abspielbarkeit von DASH-Streams und HbbTV-Anwendungen

## Entwicklung

```bash
pip install -e ".[dev]"
python -m pytest            # oder: python -m unittest discover -s tests
```

Die Tests starten einen lokalen Mock-Server (`tests/mockserver.py`), der TV-Anytime-Antworten
und Logos liefert. Die Fixture `tests/fixtures/mini_sl.xml` ist synthetisch und enthält gezielt
Fehlerfälle (u. a. ein doppelt gelistetes Regionalprogramm auf LCN 7 und im Block 41–49 sowie
ein falsch zugeordnetes Regionalprogramm auf LCN 7). Ohne
Internetzugang lässt sich ein lokales Schema-Verzeichnis über `DVBI_CHECK_RESOURCES` angeben.

```
src/dvbi_check/
  cli.py            Kommandozeile
  runner.py         Ablaufsteuerung
  config.py         Konfiguration (Defaults < TOML < CLI)
  sl.py             Zugriff auf die Service List
  httpclient.py     HTTP mit Cache, Retries, Zeitmessung
  resources.py      XSD/Classification Schemes (gepinnt)
  checks/           schema, a177, profile_de, contentguide, images
  report/           HTML (Jinja2), JSON, CSV
```

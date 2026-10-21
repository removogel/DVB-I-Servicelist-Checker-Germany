# CLAUDE.md

Leitfaden für alle, die an diesem Projekt weiterarbeiten – Menschen wie KI-Assistenten
(z. B. Claude Code). Für Installation und Bedienung siehe `README.md`.

## Worum es geht

`dvbi-check` prüft DVB-I Service Lists auf Validität und Vollständigkeit für den deutschen
Markt und erzeugt daraus einen Readiness-Report (HTML, JSON, CSV). Maßgeblich sind:

| Kürzel im Code | Dokument |
|---|---|
| `A177` | DVB BlueBook A177r6 = ETSI TS 103 770 V1.2.1 (Namespace `urn:dvb:metadata:servicediscovery:2024`) |
| `Book x.y` | DVB-I Book Germany, Ver. 1.0.1 (01.07.2026) |
| `DTVP IT-n` | DTVP DVB-I Implementierungsprofil V1.0.1, Implementation Topic n |
| `TS 102 809` | ETSI XML-AIT (Namespace `urn:dvb:mhp:2009`) |
| `TS 103 285` | DVB-DASH |

Die Spezifikationen selbst liegen **nicht** im Repository (Urheberrecht). Wer Regeln ändert,
braucht die Dokumente lokal.

## Befehle

```bash
pip install -e ".[dev]"
dvbi-check resources update                      # XSDs + Classification Schemes laden
python -m unittest discover -s tests             # oder: python -m pytest
dvbi-check run tests/fixtures/mini_sl.xml --offline   # schneller Rauchtest ohne Netz
```

Ohne Zugriff auf GitHub: `DVBI_CHECK_RESOURCES=/pfad/zu/dvb-i-tools` für Tests bzw.
`--resources-dir` für die CLI (lokaler Klon von paulhiggs/dvb-i-tools).

## Architektur

```
cli.py            argparse; mappt CLI-Optionen auf Config
runner.py         lädt Liste → ServiceListDoc → führt Module in checks.ORDER aus → RunResult
config.py         Dataclass Config; Defaults < TOML (--config) < CLI
loader.py         Datei oder URL (+ ?postcode=), HTTP-Metadaten
httpclient.py     requests-Session pro Thread, Cache, Retries, Zeitmessung, add_query()
sl.py             ServiceListDoc: revisionsunabhängiger Zugriff (Namespaces, Services,
                  Regionen, LCN-Tabellen, CG-Quellen, Logos, Linked Apps, service_of())
resources.py      gepinnte dvb-i-tools (PINNED_REF), Schema-Zuordnung je Namespace-Jahr
model.py          Severity, Finding, Findings.add(), ModuleResult, MODULE_TITLES
checks/
  schema.py       XSD Standard + DE-Profilschema
  a177.py         Semantik nach Standard
  profile_de.py   Book + DTVP, inkl. LCN-Plan (Book 6.6) und Server-Tests (IT-5, IT-18)
  contentguide.py TV-Anytime: now_next, Tagesfenster, Programminfo, EPG-Bilder
  images.py       Logos: Breite, Größe, image_variant, ?w=
  ait.py          XML-AITs der Linked Applications
  mpd.py          DASH-MPDs, HLS-Erreichbarkeit
report/           build_view() → Jinja2-Template report.html.j2; JSON; CSV (; + BOM)
```

Jedes Modul exportiert `run(ctx: Context) -> ModuleResult`. Netzwerkmodule setzen bei
`ctx.offline` `res.skipped` und tun sonst nichts.

## Konventionen

**Befunde.** Jeder Befund hat einen stabilen `code` mit Modulpräfix (`SC`, `CS/HD/RG/CG/SV/SI/LC`,
`DE-…`, `CG-…`, `IMG-…`, `AIT-…`, `MPD-…`/`HLS-…`). Codes nicht umbenennen – Nutzer filtern
und vergleichen Reports darüber. Neue Regel = neuer Code. Wo möglich `service=<UniqueIdentifier>`
setzen, damit der Befund in der Service-Matrix erscheint; Kontext (Datum, URL, Tabelle)
gehört in `detail`, **nicht** in `message`, sonst zerfällt die Gruppierung im Report.

**Schweregrade.**
- `ERROR`: Verstoß gegen „shall“/„M“ oder technischer Defekt (nicht erreichbar, Schemafehler)
- `WARNING`: Verstoß gegen „should“ oder Auffälligkeit mit wahrscheinlicher Receiver-Wirkung
- `INFO`: Hinweis, oder Standardabweichung, die das DE-Profil ausdrücklich erlaubt

Im Profil `de` werden Standardregeln, die das DE-Profil lockert, herabgestuft (z. B. `SI05`,
`SI06-DVBC/DVBT` nach DTVP IT-11/IT-12). Diese Logik bleibt in `a177.py` mit dem Flag `de`.

**Referenzen.** Das Feld `ref` nennt die Quelle. Klauselnummern nur angeben, wenn sie im
Dokument nachgeprüft sind (oder vom DTVP-Profil bzw. dem Referenzvalidator zitiert werden).
Im Zweifel allgemein bleiben (`"A177"`) statt eine Fundstelle zu erfinden.

**Sprache.** Nutzertexte (Meldungen, Report, CLI, README) auf Deutsch; Bezeichner im Code
englisch.

**Kein Bezug auf reale Listen.** Repository, Tests und Beispiele enthalten keine echten
Service-List-URLs, keine Daten realer Listen und keine Befunde realer Anbieter. Fixtures sind
synthetisch (`example.org`, erfundene IDs).

**Sicherheit.** XML immer mit `no_network=True, resolve_entities=False` parsen
(`sl.safe_parser()`). HTTP-Header nur ASCII (User-Agent wird in `cli.py` geprüft).

## Ein neues Modul hinzufügen

1. `checks/<name>.py` mit `run(ctx)` anlegen; `Findings("<name>")` verwenden.
2. In `checks/__init__.py` in `ORDER` und `_MODULES` eintragen, Titel in `model.MODULE_TITLES`.
3. Spaltenkürzel im Template (`{"schema":"Schema", …}`) ergänzen; ggf. Statistik-Zeile auf
   der Modulkarte und eine eigene Tabelle (Daten über `build_view()` bereitstellen).
4. Optionen in `Config`, CLI und `dvbi-check.example.toml` ergänzen.
5. Mock-Endpunkte in `tests/mockserver.py`, Fälle in `tests/fixtures/mini_sl.xml`,
   Tests in `tests/test_checks.py` (positiver Fall ohne Fehler + gezielte Fehlerfälle).
6. README-Tabelle der Module aktualisieren.

## Tests

`tests/mockserver.py` startet einen lokalen HTTP-Server (Port 0) für TV-Anytime-Antworten,
Logos (mit Varianten), XML-AITs und MPDs. Die Fixture referenziert ihn über den Platzhalter
`{BASE}`. Tests dürfen **keine** echten Netzwerkzugriffe auslösen. Die erzeugten TVA-Antworten
sind schemakonform – ein `CG-XSD`-Befund im positiven Fall ist ein Fehler im Test oder im Code.

Bewusst abgedeckte Fehlerfälle der Fixture: Regionalprogramm auf LCN 7 und zusätzlich im
Block 41–49 (`DE-LCN-ARD7`), falsches Regionalprogramm auf LCN 7 (`DE-LCN-7MAP`),
Service im Radio-Block ohne `linear-radio`, Pop-up ohne Out-of-Service-Banner, DVB-S ohne
Tuning-Parameter, leerer Content Guide, Logo mit falscher Breite und ignorierten Varianten,
fehlerhaftes AIT, statisches MPD mit zu geringer Qualität, nicht erreichbares MPD.

## Bekannte Annahmen und offene Punkte

- **image_variant**: Parametername `image_variant` (A177 5.2.8.2.1) ist konfigurierbar,
  weil Bildserver abweichen können.
- **Content-Guide-Abfragen** folgen dem DVB-I-Referenzvalidator
  (`?sid=…&now_next=true`, `?sid=…&start=…&end=…`, `?pid=…`).
- **LCN-Regeln aus Book 6.6** erkennen ARD/RTL/SAT.1 über reguläre Ausdrücke
  (`pattern_ard`, `pattern_rtl`, `pattern_sat1`); die Referenztabelle ist die LCNTable mit
  der obersten Region als TargetRegion.
- **XML-AIT** wird strukturell geprüft; ein offizielles XSD ist optional (`--ait-xsd`).
  Einstiegs-URLs der Apps per `http://` sind in der Praxis üblich und werden bewusst **nicht**
  beanstandet (früherer Code `AIT-APP-HTTPS` entfällt). Geprüft wird nur ihre Erreichbarkeit.
- **Nicht prüfbar**: Sortierung nach AGF/Genre/Alphabet, Pop-up-Kontingente,
  Richtigkeit der Accessibility-Signalisierung, Segment-Abspielbarkeit, Funktion der Apps.
- **Schema-Ressourcen** sind auf `resources.PINNED_REF` festgelegt. Beim Aktualisieren
  Tests laufen lassen und die Änderung im Commit begründen.

## Commits

Kleine, thematische Commits mit deutscher Beschreibung. Vor jedem Commit:
`python -m unittest discover -s tests`.

# dvbi-check

Validation tool for DVB-I service lists according to **DVB A177r6** (ETSI TS 103 770 V1.2.1),
the **DVB-I Book Germany 1.0.1** and the **DTVP DVB-I implementation profile V1.0.1**.
It produces a report as HTML, JSON and CSV.

| Module | Checks |
|---|---|
| `schema` | XSD validation against the A177 standard schema and – for A177r6 – the German profile schema; Content-Type of the delivery |
| `a177` | Semantics according to the standard: unique IDs, tag: URIs, classification scheme values, placement of `RelatedMaterial` (logos, banners, linked applications) per A177 tables 14/15/16/20 and their application locators, regions and postcode overlaps, LCN references, delivery parameters, logos, content guide references |
| `profile` | Completeness according to the Book and DTVP: "M" fields from IT-6, DVB-S direct tuning (IT-13), broadband instance (Book 7.3), DRM via linked application (Book 11.3), content guide proxy (Book 12), LCN plan according to Book 6.6 (blocks, LCN 7/41–49, 50/51, radio block, end channel 3999), pop-up channels (Book 6.9), region depth (Book 10.2); for URL sources also invalid postcode handling (IT-5) and conditional GET (IT-18) |
| `cg` | Content guide endpoints (TV-Anytime): now/next, a time window for every day from −7 to +7, programme info; TVA XSD; mandatory fields according to DTVP IT-6 (tables 41–64); daily coverage, gaps, overlaps; sample of EPG images |
| `ait` | XML AITs of linked applications: reachability, Content-Type `application/vnd.dvb.ait+xml`, structure according to ETSI TS 102 809 (namespace, orgId/appId, application type `application/vnd.hbbtv.xhtml+xml`, controlCode, mhpVersion, HTTP transport, applicationLocation), AUTOSTART for linked apps, reachability of the entry page; optional XSD validation with `--ait-xsd` |
| `mpd` | DASH manifests: reachability, response time, Content-Type, MPD structure, DVB-DASH profile (DTVP IT-3), `type="dynamic"` with `availabilityStartTime` and UTCTiming, codecs, DRM, minimum quality for TV according to Book 7.3 (960×540, > 2.5 Mbit/s); HLS playlists for reachability |
| `refval` | *Optional, opt-in.* Runs the [DVB-I reference validator](https://github.com/paulhiggs/dvb-i-tools) via its REST interface (`--refval-url`) and merges its findings (codes `RV-…`) into the report, mapped back to services |
| `images` | Logo endpoints: reachability, Content-Type, default width 320 px, file size, mandatory variants `16x9_colour`, `16x9_colour_light`, `16x9_colour_dark`, transparency, width parameter `?w=` |

## Scope

`dvbi-check` focuses on the German profile (DVB-I Book Germany, DTVP) and on the reachability
and content of the referenced endpoints (content guide, logos, AITs, manifests). It is **not** a
complete port of the [DVB-I reference validator](https://github.com/paulhiggs/dvb-i-tools),
which covers further A177 details (e.g. accessibility attributes, signatures and digests, more
cardinality rules). For a complete standard conformance check, run both tools – or enable the
`refval` module, which calls a running instance of the reference validator and includes its
findings in the same report.

## Installation

Requirement: Python 3.11 or newer.

```bash
git clone https://github.com/removogel/DVB-I-Servicelist-Checker-Germany.git
cd DVB-I-Servicelist-Checker-Germany
python -m venv .venv && source .venv/bin/activate
pip install -e .
dvbi-check resources update     # downloads XSD schemas and classification schemes (once)
```

The schemas come from [paulhiggs/dvb-i-tools](https://github.com/paulhiggs/dvb-i-tools)
(BSD-2-Clause). They are pinned to a fixed commit and stored in `~/.cache/dvbi-check`
(configurable via `DVBI_CHECK_HOME`). If GitHub is not reachable, a local clone can be
specified with `--resources-dir`.

## Usage

```bash
# Full check of a service list via URL
# (specify a whitelisted User-Agent if required, Book 11.1)
dvbi-check run https://<service-list-provider>/<path-to-service-list> \
    --user-agent "<whitelisted User-Agent>" --out reports/

# Quick run: only 10 services in the content guide, 2 days back and ahead
dvbi-check run <URL> --cg-max-services 10 --cg-days-past 2 --cg-days-future 2

# Regionalised list
dvbi-check run <URL> --postcode 06217

# Offline against a file (schema, A177, profile)
dvbi-check run servicelist.xml --offline

# Selected modules only
dvbi-check run <URL> --modules cg,images
dvbi-check run <URL> --modules ait,mpd

# Additionally validate XML AITs against the ETSI schema
dvbi-check run <URL> --modules ait --ait-xsd path/to/mis_xmlait.xsd

# Include the DVB-I reference validator (sends the service list to that server!)
dvbi-check run <URL> --refval-url https://<validator-host>:<port>

# With a configuration file
cp dvbi-check.example.toml dvbi-check.toml
dvbi-check run <URL> --config dvbi-check.toml
```

Output in `--out` (default `dvbi-report/`):

- `dvbi-report-<date>.html` – self-contained report: key figures, module status, service
  matrix, EPG coverage per day, tables for logos, AITs and manifests, filterable findings with
  specification references
- `.json` – complete results including statistics per service
- `.csv` – all findings (semicolon-separated, UTF-8 with BOM, opens directly in Excel)

Exit code: `1` if errors were found. With `--fail-on warning` this also applies to warnings,
with `--fail-on never` never. This makes the tool usable in CI pipelines.

## Severity

- **Error**: violation of a "shall"/"M" requirement from A177, the Book or DTVP, or a technical
  defect (endpoint not reachable, schema error).
- **Warning**: violation of a "should", or an anomaly likely to affect receivers.
- **Info**: information, or a deviation from the standard that the German profile explicitly
  permits (e.g. DVB-C triplets with serviceId only, according to IT-12).

With the `de` profile the German profile schema is authoritative. Deviations from the standard
schema are then reported as warnings, because strictly validating clients might reject the list.

## Conventions and assumptions

- Content guide requests as in the DVB-I reference validator:
  `?sid=…&now_next=true`, `?sid=…&start=<epoch>&end=<epoch>`, `?pid=<crid>`.
  `sid` is `ContentGuideServiceRef`, otherwise `UniqueIdentifier`.
- Day windows are formed in `cg_timezone` (default Europe/Berlin). Past days without data are a
  warning ("should"), today and upcoming days an error ("shall").
- Logo variants are requested via `?image_variant=<value>` (A177 5.2.8.2.1). If your image
  server uses a different parameter name, it can be changed via `image_variant_param`.
- The LCN plan is checked against the nationwide reference table (TargetRegion = top-level
  region). Rule `DE-LCN-7MAP` detects cases where LCN 7 does not match the main programme whose
  slot in block 41–49 was left empty.
- ARD, RTL and SAT.1 are recognised for the Book 6.6 rules by configurable regular expressions
  (`pattern_ard`, `pattern_rtl`, `pattern_sat1`).
- The `refval` module POSTs the already loaded list to `<refval_url>/validate_sl_json?results=all`
  (so it also works for lists that are only delivered to whitelisted User-Agents). The list is
  re-serialised with indentation so that the validator's line numbers can be mapped to services.
  The REST interface validates against the standard schema; findings that the German profile
  explicitly permits (DTVP IT-11/IT-12) are downgraded to info. The module is opt-in because it
  sends the complete list to a third-party server.
- HTTP entry URLs of HbbTV applications in XML AITs are common practice and are deliberately
  not flagged.

## Not checked automatically

- Ordering within blocks by AGF data, genre or alphabet (Book 6.6)
- Pop-up quotas per provider (Book 6.9, requires AGF market shares)
- Correctness of accessibility signalling (Book 15, DTVP IT-16)
- Playability of DASH streams (segments are not fetched) and functionality of HbbTV applications
- XML AITs against the official ETSI schema, unless `mis_xmlait.xsd` is provided via `--ait-xsd`

## Development

Architecture, conventions (finding codes, severities, references) and the procedure for adding
modules are described in [`CLAUDE.md`](CLAUDE.md).

```bash
pip install -e ".[dev]"
python -m pytest            # or: python -m unittest discover -s tests
```

The tests start a local mock server (`tests/mockserver.py`) that serves TV-Anytime responses,
logos, XML AITs and MPDs. The fixture `tests/fixtures/mini_sl.xml` is synthetic and contains
deliberate error cases (e.g. a regional programme listed on LCN 7 and again in block 41–49, and
a wrongly assigned regional programme on LCN 7). Without internet access a local schema
directory can be specified via `DVBI_CHECK_RESOURCES`.

```
src/dvbi_check/
  cli.py            command line
  runner.py         orchestration
  config.py         configuration (defaults < TOML < CLI)
  sl.py             service list access
  httpclient.py     HTTP with cache, retries, timing
  resources.py      XSD / classification schemes (pinned)
  checks/           schema, a177, profile_de, contentguide, images, ait, mpd
  report/           HTML (Jinja2), JSON, CSV
```

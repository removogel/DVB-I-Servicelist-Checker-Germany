# CLAUDE.md

Guide for everyone continuing work on this project – humans and AI assistants
(e.g. Claude Code) alike. For installation and usage see `README.md`.

## What it is about

`dvbi-check` checks DVB-I service lists for validity and completeness for the German market and
produces a readiness report (HTML, JSON, CSV). The governing documents are:

| Abbreviation in code | Document |
|---|---|
| `A177` | DVB BlueBook A177r6 = ETSI TS 103 770 V1.2.1 (namespace `urn:dvb:metadata:servicediscovery:2024`) |
| `Book x.y` | DVB-I Book Germany, ver. 1.0.1 (2026-07-01) |
| `DTVP IT-n` | DTVP DVB-I implementation profile V1.0.1, implementation topic n |
| `TS 102 809` | ETSI XML AIT (namespace `urn:dvb:mhp:2009`) |
| `TS 103 285` | DVB-DASH |

The specifications themselves are **not** part of the repository (copyright). Anyone changing
rules needs the documents locally.

## Commands

```bash
pip install -e ".[dev]"
dvbi-check resources update                      # download XSDs + classification schemes
python -m unittest discover -s tests             # or: python -m pytest
dvbi-check run tests/fixtures/mini_sl.xml --offline   # quick smoke test without network
```

Without access to GitHub: `DVBI_CHECK_RESOURCES=/path/to/dvb-i-tools` for tests or
`--resources-dir` for the CLI (local clone of paulhiggs/dvb-i-tools).

## Architecture

```
cli.py            argparse; maps CLI options onto Config
runner.py         loads list → ServiceListDoc → runs modules in checks.ORDER → RunResult
config.py         dataclass Config; defaults < TOML (--config) < CLI
loader.py         file or URL (+ ?postcode=), HTTP metadata
httpclient.py     requests session per thread, cache, retries, timing, add_query()
sl.py             ServiceListDoc: revision-independent access (namespaces, services,
                  regions, LCN tables, CG sources, logos, linked apps, service_of())
resources.py      pinned dvb-i-tools (PINNED_REF), schema mapping per namespace year
model.py          Severity, Finding, Findings.add(), ModuleResult, MODULE_TITLES
checks/
  schema.py       XSD standard + DE profile schema
  a177.py         semantics according to the standard, incl. RelatedMaterial placement (RM*)
  profile_de.py   Book + DTVP, incl. LCN plan (Book 6.6) and server tests (IT-5, IT-18)
  contentguide.py TV-Anytime: now_next, day windows, programme info, EPG images
  images.py       logos: width, size, image_variant, ?w=
  ait.py          XML AITs of linked applications
  mpd.py          DASH MPDs, HLS reachability
  refval.py       optional: DVB-I reference validator via REST (POST, line → service mapping)
report/           build_view() → Jinja2 template report.html.j2; JSON; CSV (; + BOM)
```

Each module exports `run(ctx: Context) -> ModuleResult`. Network modules set `res.skipped`
when `ctx.offline` is true and do nothing else.

## Conventions

**Findings.** Every finding has a stable `code` with a module prefix (`SC`, `CS/HD/RG/CG/SV/SI/LC`,
`DE-…`, `CG-…`, `IMG-…`, `AIT-…`, `MPD-…`/`HLS-…`, `RV-<validator code>`). Do not rename codes – users filter and
compare reports by them. New rule = new code. Set `service=<UniqueIdentifier>` wherever
possible so the finding shows up in the service matrix; context (date, URL, table) belongs in
`detail`, **not** in `message`, otherwise grouping in the report falls apart.

**Severities.**
- `ERROR`: violation of "shall"/"M" or technical defect (not reachable, schema error)
- `WARNING`: violation of "should" or an anomaly likely to affect receivers
- `INFO`: hint, or a deviation from the standard that the DE profile explicitly permits

With the `de` profile, standard rules that the DE profile relaxes are downgraded (e.g. `SI05`,
`SI06-DVBC/DVBT` according to DTVP IT-11/IT-12). This logic stays in `a177.py` with the `de` flag.

**References.** The `ref` field names the source. Only cite clause numbers that have been
verified in the document (or are cited by the DTVP profile or the reference validator). When in
doubt stay generic (`"A177"`) rather than inventing a clause.

**Language.** Everything is in English: user-facing texts (messages, report, CLI, README),
comments and identifiers. Finding codes are language-neutral and must not change.

**No reference to real lists.** Repository, tests and examples contain no real service list
URLs, no data from real lists and no findings about real providers. Fixtures are synthetic
(`example.org`, invented IDs).

**Security.** Always parse XML with `no_network=True, resolve_entities=False`
(`sl.safe_parser()`). HTTP headers ASCII only (the User-Agent is checked in `cli.py`).

## Adding a new module

1. Create `checks/<name>.py` with `run(ctx)`; use `Findings("<name>")`.
2. Register it in `checks/__init__.py` in `ORDER` and `_MODULES`, title in `model.MODULE_TITLES`.
3. Add the column abbreviation in the template (`{"schema":"Schema", …}`); if useful a statistics
   line on the module card and a dedicated table (provide the data via `build_view()`).
4. Add options to `Config`, the CLI and `dvbi-check.example.toml`.
5. Mock endpoints in `tests/mockserver.py`, cases in `tests/fixtures/mini_sl.xml`,
   tests in `tests/test_checks.py` (a positive case without errors + targeted error cases).
6. Update the module table in the README.

## Tests

`tests/mockserver.py` starts a local HTTP server (port 0) for TV-Anytime responses, logos
(with variants), XML AITs and MPDs. The fixture references it via the placeholder `{BASE}`.
Tests must **not** trigger real network access. The generated TVA responses are schema
compliant – a `CG-XSD` finding in the positive case is a bug in the test or the code.

Error cases deliberately covered by the fixture: regional programme on LCN 7 and again in
block 41–49 (`DE-LCN-ARD7`), wrong regional programme on LCN 7 (`DE-LCN-7MAP`), service in the
radio block without `linear-radio`, pop-up without out-of-service banner, DVB-S without tuning
parameters, empty content guide, logo with wrong width and ignored variants, faulty AIT,
static MPD with insufficient quality, unreachable MPD, reference validator findings (mocked), misplaced `RelatedMaterial` (linked app
at list level, out-of-service banner in an instance, service logo in a content guide source,
wrong HowRelatedCS version, invalid application contentType).

## Known assumptions and open points

- **image_variant**: the parameter name `image_variant` (A177 5.2.8.2.1) is configurable
  because image servers may differ.
- **Content guide requests** follow the DVB-I reference validator
  (`?sid=…&now_next=true`, `?sid=…&start=…&end=…`, `?pid=…`).
- **LCN rules from Book 6.6** recognise ARD/RTL/SAT.1 via regular expressions
  (`pattern_ard`, `pattern_rtl`, `pattern_sat1`); the reference table is the LCNTable whose
  TargetRegion is the top-level region.
- **XML AIT** is checked structurally; an official XSD is optional (`--ait-xsd`).
  Application entry URLs using `http://` are common practice and are deliberately **not**
  flagged (the former code `AIT-APP-HTTPS` was removed). Only their reachability is checked.
- **RelatedMaterial placement** (`RM01`–`RM06`) mirrors the reference validator's rules per
  location (A177 tables 14/15/16/20). Rules that depend on where something is signalled
  (`DE-BB`, `DE-POPUP-OOS`, list logo in `images`) only consider permitted locations.
- **Removed rules**: `AIT-APP-HTTPS` (see above) and `LC03` (service listed more than once in
  the same LCNTable – permitted by A177; the Book 6.6 rule remains as `DE-LCN-DUP`).
- **Scope**: this is not a full port of the reference validator; see README "Scope".
- **Reference validator (`refval`)** is opt-in (`refval_url`), because it sends the whole list
  to a third-party server. The DE variant is requested via the header `X-variants: DE` (POST;
  `?variants=DE` is for GET) – supported by dvb-i-tools since 2026-10-22. `refval_relax_de` is a
  fallback for older instances (`DE_RELAXED` in `refval.py`). The mock in `tests/mockserver.py`
  mimics `/validate_sl_json?results=all`; tests never call a real validator.
- **Not checkable**: ordering by AGF/genre/alphabet, pop-up quotas, correctness of
  accessibility signalling, segment playability, functionality of apps.
- **Schema resources** are pinned to `resources.PINNED_REF`. When updating, run the tests and
  justify the change in the commit message.

## Commits

Small, focused commits with English messages. Before every commit:
`python -m unittest discover -s tests`.

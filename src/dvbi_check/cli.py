"""Kommandozeile: dvbi-check run | resources."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__
from .checks import ORDER
from .config import Config
from .model import MODULE_TITLES, Severity

EXAMPLES = """
Beispiele:
  dvbi-check run https://example.org/servicelist.xml --user-agent "<zertifizierter UA>"
  dvbi-check run liste.xml --modules schema,a177,profile          (offline)
  dvbi-check run URL --postcode 06217 --cg-max-services 10 --out reports/
  dvbi-check resources update
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dvbi-check", description="Prüfwerkzeug für DVB-I Service Lists",
                                epilog=EXAMPLES, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"dvbi-check {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="Service List prüfen und Auswertung erzeugen")
    r.add_argument("source", help="URL oder Datei der Service List")
    r.add_argument("--config", help="TOML-Konfigurationsdatei")
    r.add_argument("--modules", help=f"Kommagetrennt aus {','.join(ORDER)} (Standard: alle)")
    r.add_argument("--out", default="dvbi-report", help="Ausgabeverzeichnis (Standard: dvbi-report)")
    r.add_argument("--formats", default="html,json,csv", help="Ausgabeformate (html,json,csv)")
    r.add_argument("--postcode", help="PLZ für serverseitige Regionalisierung (?postcode=)")
    r.add_argument("--user-agent", dest="user_agent", help="HTTP User-Agent (Book 11.1: Allowlist)")
    r.add_argument("--timeout", type=float)
    r.add_argument("--workers", type=int, help="parallele Abrufe")
    r.add_argument("--profile", choices=["de", "a177"], help="Prüfprofil (Standard: de)")
    r.add_argument("--cg-max-services", dest="cg_max_services", type=int, help="nur die ersten N Services im CG prüfen")
    r.add_argument("--cg-days-past", dest="cg_days_past", type=int)
    r.add_argument("--cg-days-future", dest="cg_days_future", type=int)
    r.add_argument("--cg-proxy-host", dest="cg_proxy_hosts", action="append",
                   help="erlaubter Content-Guide-Proxy-Host (mehrfach möglich)")
    r.add_argument("--resources-dir", help="Lokales dvb-i-tools-Verzeichnis (schemas/, res/)")
    r.add_argument("--offline", action="store_true", help="keine Netzwerkzugriffe (nur Schema, A177, Profil)")
    r.add_argument("--fail-on", choices=["error", "warning", "never"], default="error",
                   help="Exit-Code 1 bei Fehlern/Warnungen (für CI)")
    r.add_argument("-v", "--verbose", action="store_true")

    rs = sub.add_parser("resources", help="XSD-Schemas und Classification Schemes verwalten")
    rs.add_argument("action", choices=["update", "path"])
    rs.add_argument("--ref", help="Commit der dvb-i-tools (Standard: festgelegter Stand)")
    return p


def cmd_resources(args) -> int:
    from .resources import PINNED_REF, cache_root, download, locate
    ref = args.ref or PINNED_REF
    if args.action == "path":
        try:
            res = locate(ref=ref, allow_download=False)
            print(res.root)
            return 0
        except Exception as e:  # noqa: BLE001
            print(e, file=sys.stderr)
            return 1
    target = cache_root() / f"dvb-i-tools-{ref[:12]}"
    print(f"Lade Schemas ({ref[:12]}) nach {target} …")
    download(target, ref)
    print("Fertig.")
    return 0


def cmd_run(args) -> int:
    from .report import write_reports
    from .runner import run
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    cfg = Config.load(args.config)
    cfg.update(**{k: getattr(args, k) for k in ("postcode", "user_agent", "timeout", "workers", "profile",
                                                 "cg_max_services", "cg_days_past", "cg_days_future",
                                                 "cg_proxy_hosts")})
    try:
        cfg.user_agent.encode("ascii")
    except UnicodeEncodeError:
        print(f"Ungültiger User-Agent {cfg.user_agent!r}: HTTP-Header dürfen nur ASCII-Zeichen enthalten "
              "(z. B. kein „…“ oder Umlaut). Ohne --user-agent wird der Standard-User-Agent verwendet.",
              file=sys.stderr)
        return 2
    modules = [m.strip() for m in args.modules.split(",")] if args.modules else None
    if modules:
        bad = [m for m in modules if m not in ORDER]
        if bad:
            print(f"Unbekannte Module: {', '.join(bad)}", file=sys.stderr)
            return 2
    print(f"Prüfe {args.source} …", file=sys.stderr)
    rr = run(args.source, cfg, modules, args.resources_dir, args.offline)
    if rr.fatal:
        print(rr.fatal, file=sys.stderr)
        return 2
    paths = write_reports(rr, Path(args.out), [x.strip() for x in args.formats.split(",")])
    _summary(rr)
    for p in paths:
        print(f"→ {p}")
    if args.fail_on == "error" and rr.count(Severity.ERROR):
        return 1
    if args.fail_on == "warning" and (rr.count(Severity.ERROR) or rr.count(Severity.WARNING)):
        return 1
    return 0


def _summary(rr) -> None:
    lst = rr.meta.get("list", {})
    print(f"\n{lst.get('name')} · Version {lst.get('version')} · {lst.get('revision')} · {lst.get('services')} Services")
    print(f"{'Modul':42} {'Status':10} {'Fehler':>7} {'Warn.':>7} {'Info':>7}")
    labels = {"ok": "ok", "warning": "Warnung", "error": "Fehler", "skipped": "übersprungen"}
    for m in rr.modules:
        print(f"{MODULE_TITLES.get(m.name, m.name):42} {labels[m.status]:10} "
              f"{m.count(Severity.ERROR):7} {m.count(Severity.WARNING):7} {m.count(Severity.INFO):7}"
              + (f"  ({m.skipped})" if m.skipped else ""))


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "resources":
        return cmd_resources(args)
    return cmd_run(args)

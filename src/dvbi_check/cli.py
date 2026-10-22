"""Command line: dvbi-check run | resources."""
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
Examples:
  dvbi-check run https://example.org/servicelist.xml --user-agent "<certified UA>"
  dvbi-check run list.xml --modules schema,a177,profile          (offline)
  dvbi-check run URL --postcode 06217 --cg-max-services 10 --out reports/
  dvbi-check resources update
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dvbi-check", description="Validation tool for DVB-I service lists",
                                epilog=EXAMPLES, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"dvbi-check {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="check a service list and generate a report")
    r.add_argument("source", help="URL or file of the service list")
    r.add_argument("--config", help="TOML configuration file")
    r.add_argument("--modules", help=f"comma-separated from {','.join(ORDER)} (default: all)")
    r.add_argument("--out", default="dvbi-report", help="output directory (default: dvbi-report)")
    r.add_argument("--formats", default="html,json,csv", help="output formats (html,json,csv)")
    r.add_argument("--postcode", help="postcode for server-side regionalisation (?postcode=)")
    r.add_argument("--user-agent", dest="user_agent", help="HTTP User-Agent (Book 11.1: allowlist)")
    r.add_argument("--timeout", type=float)
    r.add_argument("--workers", type=int, help="parallel requests")
    r.add_argument("--profile", choices=["de", "a177"], help="validation profile (default: de)")
    r.add_argument("--cg-max-services", dest="cg_max_services", type=int,
                   help="only check the first N services in the content guide")
    r.add_argument("--cg-days-past", dest="cg_days_past", type=int)
    r.add_argument("--cg-days-future", dest="cg_days_future", type=int)
    r.add_argument("--cg-proxy-host", dest="cg_proxy_hosts", action="append",
                   help="permitted content guide proxy host (can be repeated)")
    r.add_argument("--resources-dir", help="local dvb-i-tools directory (schemas/, res/)")
    r.add_argument("--offline", action="store_true", help="no network access (schema, A177 and profile only)")
    r.add_argument("--refval-url", dest="refval_url",
                   help="base URL of a DVB-I reference validator (dvb-i-tools); the service list is POSTed there")
    r.add_argument("--ait-xsd", dest="ait_xsd", help="optional: XML AIT schema (mis_xmlait.xsd, ETSI TS 102 809)")
    r.add_argument("--fail-on", choices=["error", "warning", "never"], default="error",
                   help="exit code 1 on errors/warnings (for CI)")
    r.add_argument("-v", "--verbose", action="store_true")

    rs = sub.add_parser("resources", help="manage XSD schemas and classification schemes")
    rs.add_argument("action", choices=["update", "path"])
    rs.add_argument("--ref", help="dvb-i-tools commit (default: pinned revision)")
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
    print(f"Downloading schemas ({ref[:12]}) to {target} …")
    download(target, ref)
    print("Done.")
    return 0


def cmd_run(args) -> int:
    from .report import write_reports
    from .runner import run
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    cfg = Config.load(args.config)
    cfg.update(**{k: getattr(args, k) for k in ("postcode", "user_agent", "timeout", "workers", "profile",
                                                 "cg_max_services", "cg_days_past", "cg_days_future",
                                                 "cg_proxy_hosts", "ait_xsd", "refval_url")})
    try:
        cfg.user_agent.encode("ascii")
    except UnicodeEncodeError:
        print(f"Invalid User-Agent {cfg.user_agent!r}: HTTP headers may only contain ASCII characters "
              "(e.g. no '…' or umlauts). Without --user-agent the default User-Agent is used.",
              file=sys.stderr)
        return 2
    modules = [m.strip() for m in args.modules.split(",")] if args.modules else None
    if modules:
        bad = [m for m in modules if m not in ORDER]
        if bad:
            print(f"Unknown modules: {', '.join(bad)}", file=sys.stderr)
            return 2
    print(f"Checking {args.source} …", file=sys.stderr)
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
    print(f"\n{lst.get('name')} · version {lst.get('version')} · {lst.get('revision')} · {lst.get('services')} services")
    print(f"{'Module':42} {'Status':10} {'Errors':>7} {'Warn.':>7} {'Info':>7}")
    labels = {"ok": "ok", "warning": "warning", "error": "error", "skipped": "skipped"}
    for m in rr.modules:
        print(f"{MODULE_TITLES.get(m.name, m.name):42} {labels[m.status]:10} "
              f"{m.count(Severity.ERROR):7} {m.count(Severity.WARNING):7} {m.count(Severity.INFO):7}"
              + (f"  ({m.skipped})" if m.skipped else ""))


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "resources":
        return cmd_resources(args)
    return cmd_run(args)

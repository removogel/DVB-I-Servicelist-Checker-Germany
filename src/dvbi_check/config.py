"""Konfiguration: Defaults < TOML-Datei < Kommandozeile."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from .httpclient import DEFAULT_UA


@dataclass
class Config:
    # Abruf
    user_agent: str = DEFAULT_UA
    timeout: float = 10.0
    retries: int = 1
    workers: int = 8
    postcode: str | None = None

    # Profil
    profile: str = "de"                     # "de" = DVB-I Book Germany + DTVP, "a177" = nur Standard
    cg_proxy_hosts: list[str] = field(default_factory=list)   # erlaubte CG-Proxy-Hosts (Book 12)
    lcn_limit: int = 1000                   # Book 6.3
    lcn_notify_threshold: int = 900         # Book 6.3
    check_server_features: bool = True      # Postcode-Fehlerfall (IT-5), If-Modified-Since (IT-18)
    server_test_postcode: str = "10115"     # gültige PLZ für den Test der serverseitigen Regionalisierung
    invalid_postcode: str = "00000"         # ungültige PLZ (erwartet ERROR_INVALID_POSTCODE)
    # Erkennungsmuster (regulärer Ausdruck auf UniqueIdentifier, Name, Anbieter) für LCN-Regeln des Book 6.6
    pattern_ard: str = r"^tag:ard\.de|^ARD"
    pattern_rtl: str = r"^tag:rtl\.de|RTL Deutschland"
    pattern_sat1: str = r"^tag:seven\.one,sat1|ProSiebenSat\.1"

    # Content Guide
    cg_max_services: int | None = None      # None = alle Services prüfen
    cg_days_past: int = 7                   # DTVP IT-6: 7 Tage Vergangenheit "should"
    cg_days_future: int = 7                 # DTVP IT-6: heute + 7 Tage "shall"
    cg_timezone: str = "Europe/Berlin"
    cg_min_coverage: float = 0.9            # Mindestabdeckung eines Tages durch Events
    cg_max_gap_minutes: int = 5
    cg_slow_seconds: float = 2.0
    cg_image_sample: int = 2                # EPG-Bilder pro Service prüfen

    # Images
    logo_width: int = 320                   # DTVP IT-6 "shall"
    logo_max_bytes: int = 64 * 1024         # DTVP IT-6 "should"
    epg_image_width: int = 640              # DTVP IT-6 "shall"
    epg_image_max_bytes: int = 100 * 1024   # DTVP IT-6 "should"
    image_variant_param: str = "image_variant"
    image_width_param: str = "w"
    logo_variants_mandatory: list[str] = field(default_factory=lambda: [
        "16x9_colour", "16x9_colour_light", "16x9_colour_dark"])
    logo_variants_optional: list[str] = field(default_factory=lambda: [
        "square_colour", "4x3_colour", "16x9_white", "square_white",
        "square_colour_light", "square_colour_dark"])
    check_optional_variants: bool = False

    # XML-AIT
    ait_xsd: str | None = None              # optional: Pfad zu mis_xmlait.xsd (ETSI TS 102 809)
    ait_check_app_url: bool = True          # Einstiegs-URL der Anwendung abrufen

    # Stream-Manifeste
    mpd_min_width: int = 960                # Book 7.3: Mindestqualität 960x540p
    mpd_min_height: int = 540
    mpd_min_bandwidth: int = 2_500_000      # Book 7.3: > 2,5 Mbit/s
    mpd_slow_seconds: float = 2.0
    check_hls: bool = True                  # HLS-Playlists (OtherDeliveryParameters) auf Erreichbarkeit prüfen

    @classmethod
    def load(cls, path: str | Path | None) -> "Config":
        cfg = cls()
        if path:
            data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
            flat: dict = {}
            for k, v in data.items():
                if isinstance(v, dict):
                    flat.update(v)       # Abschnitte [http], [cg] … sind nur Gliederung
                else:
                    flat[k] = v
            cfg.update(**flat)
        return cfg

    def update(self, **kw) -> None:
        names = {f.name for f in fields(self)}
        for k, v in kw.items():
            if v is None:
                continue
            if k not in names:
                raise ValueError(f"Unbekannte Konfigurationsoption: {k}")
            setattr(self, k, v)

"""Checks of the image endpoints (service logos, EPG images).

Basis: DTVP IT-6 "Image Variants and Size" and A177 5.2.8.2 (query parameters
for width and image variant).
"""
from __future__ import annotations

import io
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from PIL import Image, UnidentifiedImageError

from ..httpclient import HttpClient, add_query
from ..model import Findings, ModuleResult, Severity, Timer
from ..sl import LIST_LOGO_HOWRELATED
from . import Context

Image.MAX_IMAGE_PIXELS = 40_000_000
REF_SIZE = "DTVP IT-6 (Image Variants and Size)"
REF_VARIANT = "DTVP IT-6 / A177 5.2.8.2"


@dataclass
class Issue:
    code: str
    severity: Severity
    message: str
    ref: str = ""
    detail: str = ""


@dataclass
class ImageReport:
    url: str
    kind: str
    issues: list[Issue] = field(default_factory=list)
    status: int | None = None
    content_type: str = ""
    width: int | None = None
    height: int | None = None
    bytes: int = 0
    elapsed: float = 0.0
    variants: dict[str, str] = field(default_factory=dict)
    width_param: str | None = None

    def add(self, code, sev, msg, ref="", detail=""):
        self.issues.append(Issue(code, sev, msg, ref, detail))

    def to_dict(self):
        return {"url": self.url, "kind": self.kind, "status": self.status, "content_type": self.content_type,
                "width": self.width, "height": self.height, "bytes": self.bytes,
                "elapsed_s": round(self.elapsed, 2), "variants": self.variants, "width_param": self.width_param,
                "issues": len(self.issues)}


def _decode(data: bytes):
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        has_alpha = im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info
        return im.width, im.height, im.format, has_alpha


def check_image(client: HttpClient, url: str, *, kind: str, declared_ct: str, cfg) -> ImageReport:
    rep = ImageReport(url, kind)
    is_logo = kind == "logo"
    exp_width = cfg.logo_width if is_logo else cfg.epg_image_width
    max_bytes = cfg.logo_max_bytes if is_logo else cfg.epg_image_max_bytes
    if url.startswith("http://"):
        rep.add("IMG-HTTPS", Severity.WARNING, "Image URL without HTTPS")
    r = client.get(url)
    rep.status, rep.elapsed, rep.bytes, rep.content_type = r.status, r.elapsed, len(r.content), r.content_type
    if not r.ok:
        rep.add("IMG-HTTP", Severity.ERROR, "Image not retrievable", detail=r.describe())
        return rep
    if not rep.content_type.startswith("image/"):
        rep.add("IMG-CT", Severity.ERROR, "Response is not an image (Content-Type)", detail=rep.content_type or "(empty)")
    elif declared_ct and declared_ct.lower() != rep.content_type:
        rep.add("IMG-CT-MISMATCH", Severity.WARNING, "Declared contentType differs from the delivered one",
                detail=f"declared {declared_ct}, delivered {rep.content_type}")
    if rep.content_type == "image/svg+xml":
        rep.add("IMG-SVG", Severity.INFO, "SVG – pixel width cannot be measured")
        default_bytes = r.content
    else:
        try:
            rep.width, rep.height, fmt, _ = _decode(r.content)
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as e:
            rep.add("IMG-DECODE", Severity.ERROR, "Image data cannot be decoded", detail=str(e))
            return rep
        if rep.width != exp_width:
            rep.add("IMG-WIDTH", Severity.ERROR,
                    f"Default width {rep.width} px instead of {exp_width} px", ref=REF_SIZE + " 'shall'",
                    detail=f"{rep.width}×{rep.height}")
        if rep.height and abs(rep.width / rep.height - 16 / 9) > 0.05:
            rep.add("IMG-RATIO", Severity.WARNING, "Default variant is not 16:9",
                    ref=REF_VARIANT, detail=f"{rep.width}×{rep.height}")
        default_bytes = r.content
    if rep.bytes > max_bytes:
        rep.add("IMG-SIZE", Severity.WARNING, f"File larger than {max_bytes // 1024} kB",
                ref=REF_SIZE + " 'should'", detail=f"{rep.bytes // 1024} kB")

    if not is_logo:
        return rep

    # width parameter (the server may ignore it)
    probe_w = max(exp_width // 2, 16)
    rw = client.get(add_query(url, **{cfg.image_width_param: probe_w}))
    if rw.ok and rep.content_type != "image/svg+xml":
        try:
            w, _, _, _ = _decode(rw.content)
            rep.width_param = "supported" if w == probe_w else f"ignored (returns {w} px)"
        except Exception:  # noqa: BLE001
            rep.width_param = "response not decodable"
    else:
        rep.width_param = rw.describe()

    # image variants
    variants = list(cfg.logo_variants_mandatory)
    if cfg.check_optional_variants:
        variants += list(cfg.logo_variants_optional)
    for v in variants:
        mandatory = v in cfg.logo_variants_mandatory
        rv = client.get(add_query(url, **{cfg.image_variant_param: v}))
        if not rv.ok or not rv.content_type.startswith("image/"):
            rep.variants[v] = "missing"
            rep.add("IMG-VARIANT", Severity.ERROR if mandatory else Severity.INFO,
                    f"Image variant {v} not retrievable", ref=REF_VARIANT + (" (mandatory)" if mandatory else ""),
                    detail=rv.describe() + (f", Content-Type {rv.content_type}" if rv.ok else ""))
            continue
        if v.endswith(("_light", "_dark", "_white")) and rv.content == default_bytes:
            rep.variants[v] = "same as default"
            rep.add("IMG-VARIANT-SAME", Severity.WARNING if mandatory else Severity.INFO,
                    f"Variant {v} is byte-identical to the default variant – parameter is probably ignored",
                    ref=REF_VARIANT)
            continue
        if rv.content_type != "image/svg+xml":
            try:
                w, h, _, alpha = _decode(rv.content)
            except Exception as e:  # noqa: BLE001
                rep.variants[v] = "not decodable"
                rep.add("IMG-VARIANT", Severity.ERROR if mandatory else Severity.INFO,
                        f"Variant {v} cannot be decoded", ref=REF_VARIANT, detail=str(e))
                continue
            if "_on_transparent" in v or v.endswith(("_light", "_dark", "_white")):
                if not alpha:
                    rep.add("IMG-VARIANT-ALPHA", Severity.WARNING,
                            f"Variant {v} without transparency ('… on transparent')", ref=REF_VARIANT)
            rep.variants[v] = f"ok ({w}×{h})"
        else:
            rep.variants[v] = "ok (SVG)"
    return rep


def run(ctx: Context) -> ModuleResult:
    res = ModuleResult("images")
    f = Findings("images")
    with Timer() as t:
        if ctx.offline:
            res.skipped = "Offline mode"
        else:
            doc = ctx.doc
            users: dict[tuple[str, str], list] = defaultdict(list)
            for s in doc.services:
                for uri, ct in doc.logo_uris(s):
                    users[(uri, ct)].append(s)
            for rm in doc.xr("d:RelatedMaterial"):
                hr = rm.find("t:HowRelated", doc.nsmap)
                if hr is None or not LIST_LOGO_HOWRELATED.search(hr.get("href", "")):
                    continue                  # only the service list logo, not agreement apps
                for mu in rm.iterfind(".//t:MediaUri", doc.nsmap):
                    if mu.text:
                        users[(mu.text.strip(), mu.get("contentType", ""))]  # service list logo
            with ThreadPoolExecutor(max_workers=ctx.cfg.workers) as ex:
                futures = {key: ex.submit(check_image, ctx.client, key[0], kind="logo",
                                          declared_ct=key[1], cfg=ctx.cfg) for key in users}
            reports = []
            for key, fut in futures.items():
                rep = fut.result()
                reports.append(rep)
                svcs = users[key] or [None]
                for i in rep.issues:
                    for s in svcs:
                        f.add(i.code, i.severity, i.message, ref=i.ref,
                              service=s.uid if s else None, subject=s.name if s else "Service list logo",
                              detail=(i.detail + " · " if i.detail else "") + rep.url)
            ok = [r for r in reports if r.status and 200 <= r.status < 300]
            res.stats = {
                "logos_checked": len(reports),
                "logos_reachable": len(ok),
                "width_ok": sum(1 for r in reports if r.width == ctx.cfg.logo_width),
                "mandatory_variants_complete": sum(
                    1 for r in reports if all(r.variants.get(v, "").startswith("ok") for v in ctx.cfg.logo_variants_mandatory)),
                "avg_response_s": round(sum(r.elapsed for r in ok) / len(ok), 2) if ok else None,
                "images": [r.to_dict() for r in reports],
            }
    res.findings = f
    res.duration = t.elapsed
    return res

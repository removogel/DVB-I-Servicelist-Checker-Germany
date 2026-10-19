"""Zugriff auf eine DVB-I Service List unabhängig von der Schema-Revision."""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cached_property

from lxml import etree

SD_PREFIX = "urn:dvb:metadata:servicediscovery:"
TVA_PREFIX = "urn:tva:metadata:"
TYPES_PREFIX = "urn:dvb:metadata:servicediscovery-types:"
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"

# Namespace-Jahr -> A177-Revision
REVISIONS = {
    "2019": "A177 (r0)", "2020": "A177r1", "2021": "A177r2", "2022": "A177r3",
    "2022b": "A177r4", "2023": "A177r5", "2024": "A177r6", "2025": "A177r7", "2026": "A177r8",
}

LOGO_HOWRELATED = re.compile(r"HowRelatedCS:20\d\d:1001\.2$")
LIST_LOGO_HOWRELATED = re.compile(r"HowRelatedCS:20\d\d:1001\.1$")
OOS_BANNER_HOWRELATED = re.compile(r"HowRelatedCS:20\d\d:1000\.1$")   # Out-of-Service-Banner
OOS_APP = re.compile(r"LinkedApplicationCS:2019:2$")                      # Out-of-Service-App
LINKED_APP = re.compile(r"LinkedApplicationCS:2019:(1\.[12])$")


def safe_parser() -> etree.XMLParser:
    return etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True, remove_blank_text=False)


def local(el) -> str:
    return etree.QName(el).localname if isinstance(el.tag, str) else ""


@dataclass
class Service:
    uid: str
    name: str
    provider: str
    el: etree._Element
    service_type: str | None

    @property
    def label(self) -> str:
        return f"{self.name} [{self.uid}]"


class ServiceListDoc:
    def __init__(self, data: bytes, source: str = ""):
        self.raw = data
        self.source = source
        self.root = etree.fromstring(data, safe_parser())
        self.tree = self.root.getroottree()
        q = etree.QName(self.root)
        if q.localname != "ServiceList" or not (q.namespace or "").startswith(SD_PREFIX):
            raise ValueError(f"Kein DVB-I ServiceList-Dokument (Root: {self.root.tag})")
        self.ns = q.namespace
        self.ns_year = self.ns.rsplit(":", 1)[-1]
        self.tva_ns = self._find_ns(TVA_PREFIX) or "urn:tva:metadata:2024"
        self.types_ns = self._find_ns(TYPES_PREFIX) or "urn:dvb:metadata:servicediscovery-types:2023"
        self.nsmap = {"d": self.ns, "t": self.tva_ns, "ty": self.types_ns}

    def _find_ns(self, prefix: str) -> str | None:
        for el in self.root.iter():
            if isinstance(el.tag, str):
                ns = etree.QName(el).namespace or ""
                if ns.startswith(prefix):
                    return ns
        return None

    @property
    def revision(self) -> str:
        return REVISIONS.get(self.ns_year, f"unbekannt ({self.ns_year})")

    def x(self, el, path: str):
        return el.xpath(path, namespaces=self.nsmap)

    def xr(self, path: str):
        return self.root.xpath(path, namespaces=self.nsmap)

    def text(self, el, path: str) -> str | None:
        r = el.find(path, self.nsmap)
        return r.text.strip() if r is not None and r.text else None

    # ------------------------------------------------------------------ services
    @cached_property
    def services(self) -> list[Service]:
        out = []
        for s in self.root.findall("d:Service", self.nsmap):
            st = s.find("d:ServiceType", self.nsmap)
            out.append(Service(
                uid=self.text(s, "d:UniqueIdentifier") or "",
                name=self.text(s, "d:ServiceName") or "(ohne Name)",
                provider=self.text(s, "d:ProviderName") or "",
                el=s,
                service_type=st.get("href", "").rsplit(":", 1)[-1] if st is not None else None,
            ))
        return out

    @cached_property
    def by_uid(self) -> dict[str, Service]:
        return {s.uid: s for s in self.services}

    def service_of(self, el) -> Service | None:
        """Ermittelt den Service, zu dem ein beliebiges Element gehört."""
        p = el
        while p is not None:
            if isinstance(p.tag, str) and local(p) == "Service" and p.getparent() is self.root:
                uid = self.text(p, "d:UniqueIdentifier")
                return self.by_uid.get(uid)
            p = p.getparent()
        return None

    # ------------------------------------------------------------- list header
    @property
    def name(self) -> str:
        return self.text(self.root, "d:Name") or ""

    @property
    def provider(self) -> str:
        return self.text(self.root, "d:ProviderName") or ""

    # --------------------------------------------------------------- regions
    @cached_property
    def regions(self) -> dict[str, etree._Element]:
        return {r.get("regionID"): r for r in self.xr("d:RegionList//d:Region")}

    def region_depth(self, r) -> int:
        d = 0
        while r is not None and isinstance(r.tag, str) and local(r) == "Region":
            d += 1
            r = r.getparent()
        return d

    def region_name(self, rid: str) -> str:
        r = self.regions.get(rid)
        return (self.text(r, "d:RegionName") or rid) if r is not None else rid

    # ------------------------------------------------------------ LCN tables
    @cached_property
    def lcn_tables(self) -> list[etree._Element]:
        return self.xr("d:LCNTableList/d:LCNTable")

    def table_label(self, idx: int, t) -> str:
        regs = [x.text for x in t.findall("d:TargetRegion", self.nsmap)]
        names = ", ".join(self.region_name(r) for r in regs) or "ohne TargetRegion"
        return f"LCNTable #{idx} ({names})"

    # --------------------------------------------------- content guide sources
    @cached_property
    def cg_sources(self) -> dict[str, etree._Element]:
        return {c.get("CGSID"): c for c in self.xr("d:ContentGuideSourceList/d:ContentGuideSource")}

    def cg_source_for(self, svc: Service):
        """ContentGuideSource eines Service: inline > Referenz > Listen-Default (A177 5.5.x)."""
        inline = svc.el.find("d:ContentGuideSource", self.nsmap)
        if inline is not None:
            return inline
        ref = self.text(svc.el, "d:ContentGuideSourceRef")
        if ref and ref in self.cg_sources:
            return self.cg_sources[ref]
        return self.root.find("d:ContentGuideSource", self.nsmap)

    def cg_service_id(self, svc: Service) -> str:
        return self.text(svc.el, "d:ContentGuideServiceRef") or svc.uid

    def endpoint(self, cgs, name: str) -> str | None:
        if cgs is None:
            return None
        u = cgs.find(f"d:{name}/ty:URI", self.nsmap)
        return u.text.strip() if u is not None and u.text else None

    # ------------------------------------------------------------------ logos
    def logo_uris(self, svc: Service) -> list[tuple[str, str]]:
        out = []
        for rm in svc.el.findall("d:RelatedMaterial", self.nsmap):
            hr = rm.find("t:HowRelated", self.nsmap)
            if hr is not None and LOGO_HOWRELATED.search(hr.get("href", "")):
                for mu in rm.iterfind(".//t:MediaUri", self.nsmap):
                    if mu.text:
                        out.append((mu.text.strip(), mu.get("contentType", "")))
        return out

    def linked_apps(self, inst) -> list[str]:
        out = []
        for hr in inst.findall("d:RelatedMaterial/t:HowRelated", self.nsmap):
            m = LINKED_APP.search(hr.get("href", ""))
            if m:
                out.append(m.group(1))
        return out

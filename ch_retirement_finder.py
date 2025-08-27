# ch_retirement_finder.py — RADIUS + TURNOVER (web-ready; reads key from Streamlit secrets or env)

import base64
import datetime as dt
import math
import time
from typing import Any

import requests

# ---- Get API key from Streamlit Cloud secrets or environment variable ----
API_KEY = None
try:
    # When running on Streamlit Cloud, we can read from st.secrets
    import streamlit as st  # noqa
    API_KEY = (st.secrets.get("CH_API_KEY") if hasattr(st, "secrets") else None)
except Exception:
    pass

import os
API_KEY = API_KEY or os.getenv("CH_API_KEY", "")
if not API_KEY:
    raise RuntimeError("CH_API_KEY not set. Add it in Streamlit 'Secrets' or as an environment variable.")

API_BASE = "https://api.company-information.service.gov.uk"
DOC_BASE = "https://document-api.company-information.service.gov.uk"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "CH-Boomer-Radar/0.3"})

def _auth_header() -> dict[str, str]:
    token = base64.b64encode(f"{API_KEY}:".encode()).decode()
    return {"Authorization": f"Basic {token}"}

def _request(method: str, url: str, **kwargs) -> requests.Response:
    headers = kwargs.pop("headers", {})
    headers.update(_auth_header())
    for attempt in range(6):
        resp = SESSION.request(method, url, headers=headers, timeout=30, **kwargs)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "5"))
            time.sleep(retry_after)
            continue
        if resp.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        resp.raise_for_status()
        return resp
    resp.raise_for_status()
    return resp

def ch_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"{API_BASE}{path}"
    resp = _request("GET", url, params=params)
    if resp.status_code == 204:
        return {}
    return resp.json()

# ---------- Companies House calls ----------

def advanced_search_by_sic(
    sic_codes: list[str],
    size: int = 300,
    start_index: int = 0,
    company_status: str = "active",
) -> dict[str, Any]:
    params = {
        "sic_codes": ",".join(sic_codes),
        "size": size,
        "start_index": start_index,
        "company_status": company_status,
    }
    return ch_get("/advanced-search/companies", params)

def get_company_profile(company_number: str) -> dict[str, Any]:
    return ch_get(f"/company/{company_number}")

def get_directors(company_number: str) -> list[dict[str, Any]]:
    params = {"items_per_page": 100, "order_by": "appointed_on"}
    data = ch_get(f"/company/{company_number}/officers", params)
    items = data.get("items") or []
    directors: list[dict[str, Any]] = []
    for it in items:
        if it.get("officer_role") != "director":
            continue
        if it.get("resigned_on"):
            continue
        directors.append({"name": it.get("name"), "dob": it.get("date_of_birth") or {}})
    return directors

# ---------- Age helper ----------

def approx_age(dob: dict[str, int] | None) -> int | None:
    if not dob or "year" not in dob:
        return None
    year = dob["year"]
    month = dob.get("month", 6)
    today = dt.date.today()
    return today.year - year - (today.month < month)

# ---------- iXBRL turnover/profit (best-effort) ----------

from lxml import etree, html

TURNOVER_TAGS = [
    "Turnover", "TurnoverRevenue", "Revenue", "Sales",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
]
PROFIT_TAGS = [
    "ProfitLoss", "ProfitLossForPeriod",
    "ProfitLossOnOrdinaryActivitiesBeforeTax",
    "ProfitLossOnOrdinaryActivitiesAfterTax",
]

def _parse_number(text: str | None) -> float | None:
    if not text:
        return None
    s = text.strip().replace(",", "")
    try:
        return float(s)
    except Exception:
        return None

def _doc_fetch_content(document_id: str) -> tuple[bytes | None, str | None]:
    meta = _request("GET", f"{DOC_BASE}/document/{document_id}").json()
    resources = meta.get("resources") or {}
    for mime in ("application/xhtml+xml", "text/html", "application/pdf"):
        res = resources.get(mime)
        if not res:
            continue
        url = res.get("links", {}).get("self")
        if not url:
            continue
        resp = _request("GET", f"{DOC_BASE}{url}")
        if resp.is_redirect:
            loc = resp.headers.get("Location")
            if loc:
                resp = SESSION.get(loc, timeout=60)
        if resp.status_code < 400:
            return resp.content, mime
    return None, None

def extract_financials(company_number: str) -> dict[str, Any]:
    out = {"turnover": None, "profit": None}
    fh = ch_get(f"/company/{company_number}/filing-history",
                params={"category": "accounts", "items_per_page": 50})
    items = fh.get("items") or []
    doc_id = None
    for it in items:
        links = it.get("links") or {}
        meta_url = links.get("document_metadata")
        if meta_url and "/document/" in meta_url:
            doc_id = meta_url.rsplit("/document/", 1)[-1]
            break
    if not doc_id:
        return out
    content, mime = _doc_fetch_content(doc_id)
    if not content or mime == "application/pdf":
        return out
    try:
        root = html.fromstring(content)
    except Exception:
        try:
            root = etree.fromstring(content)
        except Exception:
            return out
    def find_first(tag_names: list[str]) -> float | None:
        xp = "|".join([f".//*[local-name()='{t}']" for t in tag_names])
        if not xp:
            return None
        for node in root.xpath(xp):
            val = _parse_number((node.text or "").strip())
            if val is not None:
                return val
        return None
    out["turnover"] = find_first(TURNOVER_TAGS)
    out["profit"] = find_first(PROFIT_TAGS)
    return out

# ---------- Geo & radius ----------

def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    import math
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dlmb/2)**2
    return 2 * R * math.asin(math.sqrt(a))

def _bulk_lookup_postcodes(postcodes: list[str]) -> dict[str, tuple[float | None, float | None]]:
    out: dict[str, tuple[float | None, float | None]] = {}
    uniq = [p for p in sorted({(p or "").strip().upper() for p in postcodes}) if p]
    for i in range(0, len(uniq), 100):
        chunk = uniq[i:i+100]
        try:
            r = SESSION.post("https://api.postcodes.io/postcodes", json={"postcodes": chunk}, timeout=30)
            if r.status_code == 200:
                for item in r.json().get("result", []):
                    q = (item.get("query") or "").strip().upper()
                    res = item.get("result") or {}
                    out[q] = (res.get("latitude"), res.get("longitude")) if res else (None, None)
        except Exception:
            for q in chunk:
                out[q] = (None, None)
    return out

def _lookup_one(pc: str) -> tuple[float | None, float | None]:
    pc = (pc or "").strip().upper()
    if not pc:
        return (None, None)
    try:
        r = SESSION.get(f"https://api.postcodes.io/postcodes/{pc}", timeout=15)
        if r.status_code == 200:
            res = r.json().get("result") or {}
            return (res.get("latitude"), res.get("longitude"))
    except Exception:
        pass
    return (None, None)

# ---------- Main search + radius filter ----------

def find_targets(
    sic_codes: list[str],
    min_age: int = 55,
    max_directors: int = 2,
    size: int = 300,
    pages: int = 1,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    start_index = 0
    for _ in range(pages):
        data = advanced_search_by_sic(sic_codes, size=size, start_index=start_index)
        items = data.get("items") or []
        if not items:
            break
        for c in items:
            cnum = c.get("company_number")
            directors = get_directors(cnum)
            if not directors or len(directors) > max_directors:
                continue
            ages = [approx_age(d.get("dob")) for d in directors]
            if any(a is None or a < min_age for a in ages):
                continue
            ro = c.get("registered_office_address") or {}
            postcode = ro.get("postal_code") or ro.get("postcode")
            if not postcode:
                prof = get_company_profile(cnum)
                ro = prof.get("registered_office_address") or {}
                postcode = ro.get("postal_code") or ro.get("postcode")
            fin = extract_financials(cnum)
            results.append({
                "company_number": cnum,
                "company_name": c.get("company_name"),
                "incorporated": c.get("date_of_creation"),
                "sic_codes": ",".join(c.get("sic_codes", [])),
                "active_directors": len(directors),
                "director_ages": ",".join(str(a) for a in ages if a is not None),
                "postcode": postcode,
                "turnover": fin.get("turnover"),
                "profit": fin.get("profit"),
            })
            time.sleep(0.03)
        start_index += size
    return results

def filter_by_radius(rows: list[dict[str, Any]], centre_postcode: str, radius_km: float) -> list[dict[str, Any]]:
    if not rows or not centre_postcode or radius_km <= 0:
        return rows
    lat0, lon0 = _lookup_one(centre_postcode)
    if lat0 is None or lon0 is None:
        return []
    pcs = [r.get("postcode") or "" for r in rows]
    latlons = _bulk_lookup_postcodes(pcs)
    out: list[dict[str, Any]] = []
    for r in rows:
        pc = (r.get("postcode") or "").strip().upper()
        lat, lon = latlons.get(pc, (None, None))
        if lat is None or lon is None:
            continue
        dist = _haversine_km(lat0, lon0, lat, lon)
        if dist <= radius_km:
            rr = dict(r)
            rr["distance_km"] = round(dist, 1)
            out.append(rr)
    out.sort(key=lambda x: x.get("distance_km", 1e9))
    return out

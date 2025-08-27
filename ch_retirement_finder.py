# ch_retirement_finder.py — RATE-LIMIT SAFE + RADIUS + TURNOVER + PSC + EMPLOYEES + TRADING AGE

import base64
import datetime as dt
import math
import os
import time
from collections import deque
from typing import Any
from urllib.parse import quote_plus

import requests
from lxml import etree, html

# ---- API key from Streamlit Cloud secrets or environment ----
API_KEY = os.getenv("CH_API_KEY", "")
try:
    import streamlit as st  # available on Streamlit Cloud
    API_KEY = API_KEY or (st.secrets.get("CH_API_KEY") if hasattr(st, "secrets") else "")
except Exception:
    pass
if not API_KEY:
    raise RuntimeError("CH_API_KEY not set. Add it in Streamlit Secrets or env var.")

API_BASE = "https://api.company-information.service.gov.uk"
DOC_BASE = "https://document-api.company-information.service.gov.uk"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "CH-Boomer-Radar/0.5"})

# ---- Throttle: stay under 600 requests / 5 min ----
_WINDOW = 300.0  # seconds
_LIMIT = 580
_REQ_TIMES: deque[float] = deque()
def _throttle():
    now = time.time()
    while _REQ_TIMES and now - _REQ_TIMES[0] > _WINDOW:
        _REQ_TIMES.popleft()
    if len(_REQ_TIMES) >= _LIMIT:
        sleep_for = _WINDOW - (now - _REQ_TIMES[0]) + 1
        time.sleep(max(1.0, sleep_for))
    _REQ_TIMES.append(time.time())

def _auth_header() -> dict[str, str]:
    token = base64.b64encode(f"{API_KEY}:".encode()).decode()
    return {"Authorization": f"Basic {token}"}

def _request(method: str, url: str, **kwargs) -> requests.Response:
    headers = kwargs.pop("headers", {})
    headers.update(_auth_header())
    _throttle()
    resp = SESSION.request(method, url, headers=headers, timeout=30, **kwargs)
    if resp.status_code in (429, 500, 502, 503, 504):
        retry_after = int(resp.headers.get("Retry-After", "5"))
        time.sleep(retry_after)
        _throttle()
        resp = SESSION.request(method, url, headers=headers, timeout=30, **kwargs)
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
    size: int = 100,
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
    out: list[dict[str, Any]] = []
    for it in items:
        if it.get("officer_role") != "director":
            continue
        if it.get("resigned_on"):
            continue
        out.append({"name": it.get("name"), "dob": it.get("date_of_birth") or {}})
    return out

def get_psc(company_number: str) -> list[dict[str, Any]]:
    """Active individual PSCs with DOB."""
    params = {"items_per_page": 100}
    data = ch_get(f"/company/{company_number}/persons-with-significant-control", params)
    items = data.get("items") or []
    out: list[dict[str, Any]] = []
    for it in items:
        if it.get("ceased_on"):
            continue
        if it.get("kind") != "individual-person-with-significant-control":
            continue
        out.append({"name": it.get("name"), "dob": it.get("date_of_birth") or {}})
    return out

# ---------- helpers ----------

def approx_age(dob: dict[str, int] | None) -> int | None:
    if not dob or "year" not in dob:
        return None
    year = dob["year"]
    month = dob.get("month", 6)
    today = dt.date.today()
    return today.year - year - (today.month < month)

# iXBRL tags
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
EMPLOYEE_TAGS = [
    "AverageNumberEmployeesDuringPeriod",
    "AverageNumberOfEmployeesDuringThePeriod",
    "AverageNumberOfEmployees",
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
    """Best-effort: turnover/profit/employees from latest HTML/iXBRL accounts."""
    out = {"turnover": None, "profit": None, "employees": None}
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

    def find_first(tags: list[str]) -> float | None:
        xp = "|".join([f".//*[local-name()='{t}']" for t in tags])
        if not xp:
            return None
        for node in root.xpath(xp):
            val = _parse_number((node.text or "").strip())
            if val is not None:
                return val
        return None

    out["turnover"] = find_first(TURNOVER_TAGS)
    out["profit"] = find_first(PROFIT_TAGS)
    out["employees"] = find_first(EMPLOYEE_TAGS)
    return out

# ---------- Geo & radius ----------

def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
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

# ---------- Main search with filters ----------

def find_targets(
    sic_codes: list[str],
    min_age: int = 55,
    max_directors: int = 2,
    size: int = 100,
    pages: int = 1,
    *,
    limit_companies: int = 120,          # cap total processed per run
    fetch_financials: bool = False,      # pull turnover/profit/employees for first N
    financials_top_n: int = 40,
    min_employees: int = 0,              # filter by employees (when available)
    min_years_trading: int = 0,          # e.g. 10 or 15
    fetch_psc: bool = False,             # pull PSCs (owners)
    psc_min_age: int = 0,                # e.g. 55
    psc_max_count: int = 2,              # e.g. ≤2 owners
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    start_index = 0
    processed = 0
    today = dt.date.today()

    for _ in range(pages):
        data = advanced_search_by_sic(sic_codes, size=size, start_index=start_index)
        items = data.get("items") or []
        if not items:
            break

        for c in items:
            if processed >= limit_companies:
                return results

            cnum = c.get("company_number")
            created = c.get("date_of_creation")
            years_trading = None
            if created:
                try:
                    y, m, d = map(int, created.split("-"))
                    years_trading = today.year - y - ((today.month, today.day) < (m, d))
                except Exception:
                    pass
            if min_years_trading and (years_trading is None or years_trading < min_years_trading):
                continue

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

            fin = {"turnover": None, "profit": None, "employees": None}
            if fetch_financials and processed < financials_top_n:
                fin = extract_financials(cnum)
                if min_employees and fin.get("employees") is not None and fin["employees"] < min_employees:
                    processed += 1
                    continue

            psc_ages: list[int] = []
            psc_count = None
            if fetch_psc:
                pscs = get_psc(cnum)
                psc_count = len(pscs)
                psc_ages = [a for a in (approx_age(p.get("dob")) for p in pscs) if a is not None]
                if psc_max_count and psc_count > psc_max_count:
                    processed += 1
                    continue
                if psc_min_age and (not psc_ages or any(a < psc_min_age for a in psc_ages)):
                    processed += 1
                    continue

            ch_link = f"https://find-and-update.company-information.service.gov.uk/company/{cnum}"
            google_link = f"https://www.google.com/search?q={quote_plus((c.get('company_name') or '') + ' ' + (postcode or ''))}"

            results.append({
                "company_number": cnum,
                "company_name": c.get("company_name"),
                "incorporated": created,
                "years_trading": years_trading,
                "sic_codes": ",".join(c.get("sic_codes", [])),
                "active_directors": len(directors),
                "director_ages": ",".join(str(a) for a in ages if a is not None),
                "postcode": postcode,
                "turnover": fin.get("turnover"),
                "profit": fin.get("profit"),
                "employees": fin.get("employees"),
                "psc_count": psc_count,
                "psc_ages": ",".join(map(str, psc_ages)) if psc_ages else None,
                "ch_link": ch_link,
                "google": google_link,
            })
            processed += 1
            time.sleep(0.02)

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

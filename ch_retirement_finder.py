# ch_retirement_finder.py
# Rate-limit safe + caching + radius + turnover/psc/employees + accounts + confirmation statement
# + risk flags + outstanding charges + geocoding helpers

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

# -------------------------------------------------
# Secrets / API key
# -------------------------------------------------
API_KEY = os.getenv("CH_API_KEY", "")
try:
    import streamlit as st

    API_KEY = API_KEY or (st.secrets.get("CH_API_KEY") if hasattr(st, "secrets") else "")
except Exception:
    st = None  # type: ignore

if not API_KEY:
    raise RuntimeError("CH_API_KEY not set. Add it in Streamlit Secrets or env var.")

# simple alias so the file works without Streamlit
def _cache_data(**kwargs):
    if st is None:
        def _wrap(fn):
            return fn
        return _wrap
    return st.cache_data(**kwargs)  # type: ignore

API_BASE = "https://api.company-information.service.gov.uk"
DOC_BASE = "https://document-api.company-information.service.gov.uk"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "CH-Boomer-Radar/0.9"})

# -------------------------------------------------
# Throttle (~600 requests / 5 min)
# -------------------------------------------------
_WINDOW = 300.0
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


# -------------------------------------------------
# Cached GET to Companies House
# -------------------------------------------------
@_cache_data(ttl=3600, show_spinner=False)
def _ch_get_cached(path: str, params_key: tuple[tuple[str, Any], ...]) -> dict[str, Any]:
    url = f"{API_BASE}{path}"
    params = dict(params_key)
    resp = _request("GET", url, params=params)
    if resp.status_code == 204:
        return {}
    return resp.json()


def ch_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    params_key = tuple(sorted((params or {}).items()))
    return _ch_get_cached(path, params_key)


# -------------------------------------------------
# Companies House endpoints
# -------------------------------------------------
def advanced_search_by_sic(
    sic_codes: list[str],
    size: int = 100,
    start_index: int = 0,
    company_status: str = "active",
) -> dict[str, Any]:
    return ch_get(
        "/advanced-search/companies",
        {"sic_codes": ",".join(sic_codes), "size": size, "start_index": start_index, "company_status": company_status},
    )


def get_company_profile(company_number: str) -> dict[str, Any]:
    return ch_get(f"/company/{company_number}")


def get_directors(company_number: str) -> list[dict[str, Any]]:
    data = ch_get(f"/company/{company_number}/officers", {"items_per_page": 100, "order_by": "appointed_on"})
    out: list[dict[str, Any]] = []
    for it in data.get("items") or []:
        if it.get("officer_role") != "director":
            continue
        if it.get("resigned_on"):
            continue
        out.append({"name": it.get("name"), "dob": it.get("date_of_birth") or {}})
    return out


def get_psc(company_number: str) -> list[dict[str, Any]]:
    data = ch_get(f"/company/{company_number}/persons-with-significant-control", {"items_per_page": 100})
    out: list[dict[str, Any]] = []
    for it in data.get("items") or []:
        if it.get("ceased_on"):
            continue
        if it.get("kind") != "individual-person-with-significant-control":
            continue
        out.append({"name": it.get("name"), "dob": it.get("date_of_birth") or {}})
    return out


@_cache_data(ttl=3600, show_spinner=False)
def get_outstanding_charges_count(company_number: str) -> int | None:
    """Count outstanding charges; returns None if endpoint fails."""
    try:
        data = ch_get(f"/company/{company_number}/charges")
        items = data.get("items") or []
        return sum(1 for c in items if (c.get("status") or "").lower() == "outstanding")
    except Exception:
        return None


# -------------------------------------------------
# Helpers
# -------------------------------------------------
def approx_age(dob: dict[str, int] | None) -> int | None:
    if not dob or "year" not in dob:
        return None
    y, m = dob["year"], dob.get("month", 6)
    t = dt.date.today()
    return t.year - y - (t.month < m)


def months_between(d1: dt.date, d2: dt.date) -> int:
    if d1 > d2:
        d1, d2 = d2, d1
    return (d2.year - d1.year) * 12 + (d2.month - d1.month) - (1 if d2.day < d1.day else 0)


TURNOVER_TAGS = [
    "Turnover",
    "TurnoverRevenue",
    "Revenue",
    "Sales",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
]
PROFIT_TAGS = [
    "ProfitLoss",
    "ProfitLossForPeriod",
    "ProfitLossOnOrdinaryActivitiesBeforeTax",
    "ProfitLossOnOrdinaryActivitiesAfterTax",
]
EMPLOYEE_TAGS = [
    "AverageNumberEmployeesDuringPeriod",
    "AverageNumberOfEmployeesDuringThePeriod",
    "AverageNumberOfEmployees",
]


def _num(s):
    if s is None:
        return None
    try:
        return float(str(s).replace(",", "").strip())
    except Exception:
        return None


# --------- cached doc fetch ----------
@_cache_data(ttl=43200, show_spinner=False)
def _doc_fetch_content_cached(document_id: str) -> tuple[bytes | None, str | None]:
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
        if resp.is_redirect and resp.headers.get("Location"):
            resp = SESSION.get(resp.headers["Location"], timeout=60)
        if resp.status_code < 400:
            return resp.content, mime
    return None, None


def extract_financials(company_number: str) -> dict[str, Any]:
    out = {"turnover": None, "profit": None, "employees": None}
    fh = ch_get(f"/company/{company_number}/filing-history", {"category": "accounts", "items_per_page": 50})
    doc_id = None
    for it in fh.get("items") or []:
        meta_url = (it.get("links") or {}).get("document_metadata")
        if meta_url and "/document/" in meta_url:
            doc_id = meta_url.rsplit("/document/", 1)[-1]
            break
    if not doc_id:
        return out
    content, mime = _doc_fetch_content_cached(doc_id)
    if not content or mime == "application/pdf":
        return out
    try:
        root = html.fromstring(content)
    except Exception:
        try:
            root = etree.fromstring(content)
        except Exception:
            return out

    def pick(tags: list[str]):
        xp = "|".join([f".//*[local-name()='{t}']" for t in tags])
        if not xp:
            return None
        for n in root.xpath(xp):
            v = _num((n.text or "").strip())
            if v is not None:
                return v
        return None

    out["turnover"] = pick(TURNOVER_TAGS)
    out["profit"] = pick(PROFIT_TAGS)
    out["employees"] = pick(EMPLOYEE_TAGS)
    return out


# -------------------------------------------------
# Geo / Radius
# -------------------------------------------------
@_cache_data(ttl=86400, show_spinner=False)
def _postcodes_bulk_cached(chunk: tuple[str, ...]) -> dict[str, tuple[float | None, float | None]]:
    out: dict[str, tuple[float | None, float | None]] = {}
    try:
        r = SESSION.post("https://api.postcodes.io/postcodes", json={"postcodes": list(chunk)}, timeout=30)
        if r.status_code == 200:
            for item in r.json().get("result", []):
                q = (item.get("query") or "").strip().upper()
                res = item.get("result") or {}
                out[q] = (res.get("latitude"), res.get("longitude")) if res else (None, None)
    except Exception:
        for q in chunk:
            out[q] = (None, None)
    return out


def _bulk_lookup_postcodes(postcodes: list[str]) -> dict[str, tuple[float | None, float | None]]:
    out: dict[str, tuple[float | None, float | None]] = {}
    uniq = [p for p in sorted({(p or "").strip().upper() for p in postcodes}) if p]
    for i in range(0, len(uniq), 100):
        chunk = tuple(uniq[i : i + 100])
        out.update(_postcodes_bulk_cached(chunk))
    return out


def geocode_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pcs = [(r.get("postcode") or "").strip().upper() for r in rows]
    latlons = _bulk_lookup_postcodes(pcs)
    out: list[dict[str, Any]] = []
    for r in rows:
        pc = (r.get("postcode") or "").strip().upper()
        lat, lon = latlons.get(pc, (None, None))
        r2 = dict(r)
        r2["lat"] = lat
        r2["lon"] = lon
        out.append(r2)
    return out


def _hav(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def filter_by_radius(rows: list[dict[str, Any]], centre_postcode: str, radius_km: float) -> list[dict[str, Any]]:
    if not rows or not centre_postcode or radius_km <= 0:
        return rows
    try:
        r = SESSION.get(f"https://api.postcodes.io/postcodes/{centre_postcode}", timeout=15)
        res = (r.json().get("result") or {}) if r.status_code == 200 else {}
        lat0, lon0 = res.get("latitude"), res.get("longitude")
    except Exception:
        lat0 = lon0 = None
    if lat0 is None or lon0 is None:
        return []
    pcs = [(r.get("postcode") or "").strip().upper() for r in rows]
    latlons = _bulk_lookup_postcodes(pcs)
    out: list[dict[str, Any]] = []
    for r in rows:
        pc = (r.get("postcode") or "").strip().upper()
        lat, lon = latlons.get(pc, (None, None))
        if lat is None or lon is None:
            continue
        dist = _hav(lat0, lon0, lat, lon)
        if dist <= radius_km:
            r2 = dict(r)
            r2["distance_km"] = round(dist, 1)
            r2["lat"] = lat
            r2["lon"] = lon
            out.append(r2)
    out.sort(key=lambda x: x.get("distance_km", 1e9))
    return out


# -------------------------------------------------
# Main search
# -------------------------------------------------
def find_targets(
    sic_codes: list[str],
    min_age: int = 55,
    max_directors: int = 2,
    size: int = 100,
    pages: int = 1,
    *,
    limit_companies: int = 120,
    fetch_financials: bool = False,
    financials_top_n: int = 40,
    min_employees: int = 0,
    min_years_trading: int = 0,
    fetch_psc: bool = False,
    psc_min_age: int = 0,
    psc_max_count: int = 2,
    # Accounts freshness
    require_accounts_within_months: int | None = None,
    exclude_overdue_accounts: bool = False,
    # Confirmation statement freshness
    require_confirmation_within_months: int | None = None,
    exclude_overdue_confirmation: bool = False,
    # Risk / Address flags
    exclude_insolvency_history: bool = False,
    exclude_undeliverable_address: bool = False,
    exclude_office_in_dispute: bool = False,
    # Charges
    fetch_charges_count: bool = False,
    charges_top_n: int = 50,
    max_outstanding_charges: int | None = None,
) -> list[dict[str, Any]]:
    """
    Returns list of dicts with lots of fields (see below). Filters are applied inline to save API calls.
    """
    results: list[dict[str, Any]] = []
    start_index = 0
    processed = 0
    today = dt.date.today()

    for _ in range(pages):
        items = advanced_search_by_sic(sic_codes, size=size, start_index=start_index).get("items") or []
        if not items:
            break
        for c in items:
            if processed >= limit_companies:
                return results

            cnum = c.get("company_number")
            created = c.get("date_of_creation")
            years = None
            if created:
                try:
                    y, m, d = map(int, created.split("-"))
                    t = today
                    years = t.year - y - ((t.month, t.day) < (m, d))
                except Exception:
                    years = None
            if min_years_trading and (years is None or years < min_years_trading):
                continue

            # Active directors & ages
            directors = get_directors(cnum)
            if not directors or len(directors) > max_directors:
                continue
            dir_ages = [approx_age(d.get("dob")) for d in directors]
            if any(a is None or a < min_age for a in dir_ages):
                continue
            avg_dir_age = round(sum(a for a in dir_ages if a is not None) / len(dir_ages), 1) if dir_ages else None

            # Profile for address + accounts + confirmation + flags
            prof = get_company_profile(cnum)
            ro = prof.get("registered_office_address") or {}
            pc = ro.get("postal_code") or ro.get("postcode")

            # Risk flags
            has_insolvency = bool(prof.get("has_insolvency_history"))
            has_charges_flag = bool(prof.get("has_charges"))
            undeliverable_ro = bool(prof.get("undeliverable_registered_office_address"))
            office_in_dispute = bool(prof.get("registered_office_is_in_dispute"))

            if exclude_insolvency_history and has_insolvency:
                processed += 1
                continue
            if exclude_undeliverable_address and undeliverable_ro:
                processed += 1
                continue
            if exclude_office_in_dispute and office_in_dispute:
                processed += 1
                continue

            # Accounts freshness
            accounts = prof.get("accounts") or {}
            la = accounts.get("last_accounts") or {}
            last_made = la.get("made_up_to") or la.get("period_end_on")
            last_made_date = None
            if last_made:
                try:
                    y, m, d = map(int, str(last_made).split("-"))
                    last_made_date = dt.date(y, m, d)
                except Exception:
                    pass
            months_since_accounts = None
            if last_made_date:
                months_since_accounts = months_between(last_made_date, today)
            accounts_overdue = bool(accounts.get("overdue") or (accounts.get("next_accounts") or {}).get("overdue"))
            next_accounts_due = (accounts.get("next_accounts") or {}).get("due_on") or (accounts.get("next_accounts") or {}).get("next_due")

            if exclude_overdue_accounts and accounts_overdue:
                processed += 1
                continue
            if require_accounts_within_months is not None:
                if months_since_accounts is None or months_since_accounts > int(require_accounts_within_months):
                    processed += 1
                    continue

            # Confirmation statement freshness
            conf = prof.get("confirmation_statement") or {}
            conf_last = conf.get("last_made_up_to")
            conf_last_date = None
            if conf_last:
                try:
                    y, m, d = map(int, str(conf_last).split("-"))
                    conf_last_date = dt.date(y, m, d)
                except Exception:
                    pass
            months_since_conf = None
            if conf_last_date:
                months_since_conf = months_between(conf_last_date, today)
            conf_overdue = bool((conf.get("overdue")))
            conf_next_due = conf.get("next_due")

            if exclude_overdue_confirmation and conf_overdue:
                processed += 1
                continue
            if require_confirmation_within_months is not None:
                if months_since_conf is None or months_since_conf > int(require_confirmation_within_months):
                    processed += 1
                    continue

            # Financials (optional; best-effort iXBRL)
            fin = {"turnover": None, "profit": None, "employees": None}
            if fetch_financials and processed < financials_top_n:
                fin = extract_financials(cnum)
                if min_employees and fin.get("employees") is not None and fin["employees"] < min_employees:
                    processed += 1
                    continue

            # PSC ages
            avg_psc_age = None
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
                if psc_ages:
                    avg_psc_age = round(sum(psc_ages) / len(psc_ages), 1)

            # Charges (optional)
            outstanding_charges = None
            if fetch_charges_count and processed < charges_top_n:
                outstanding_charges = get_outstanding_charges_count(cnum)
                if max_outstanding_charges is not None and outstanding_charges is not None:
                    if outstanding_charges > int(max_outstanding_charges):
                        processed += 1
                        continue

            ch_link = f"https://find-and-update.company-information.service.gov.uk/company/{cnum}"
            google = f"https://www.google.com/search?q={quote_plus((c.get('company_name') or '') + ' ' + (pc or ''))}"

            results.append(
                {
                    "company_number": cnum,
                    "company_name": c.get("company_name"),
                    "incorporated": created,
                    "years_trading": years,
                    "sic_codes": ",".join(c.get("sic_codes", [])),
                    "active_directors": len(directors),
                    "director_ages": ",".join(str(a) for a in dir_ages if a is not None),
                    "avg_director_age": avg_dir_age,
                    "avg_psc_age": avg_psc_age,
                    "postcode": pc,
                    # financials
                    "turnover": fin.get("turnover"),
                    "profit": fin.get("profit"),
                    "employees": fin.get("employees"),
                    # PSC
                    "psc_count": psc_count,
                    "psc_ages": ",".join(map(str, psc_ages)) if psc_ages else None,
                    # accounts
                    "last_accounts_made_up_to": str(last_made_date) if last_made_date else None,
                    "months_since_accounts": months_since_accounts,
                    "accounts_overdue": accounts_overdue,
                    "next_accounts_due": next_accounts_due,
                    # confirmation statement
                    "confirmation_last_made_up_to": str(conf_last_date) if conf_last_date else None,
                    "months_since_confirmation": months_since_conf,
                    "confirmation_overdue": conf_overdue,
                    "next_confirmation_due": conf_next_due,
                    # risk flags
                    "has_insolvency_history": has_insolvency,
                    "has_charges": has_charges_flag,
                    "undeliverable_registered_office_address": undeliverable_ro,
                    "registered_office_is_in_dispute": office_in_dispute,
                    # charges count
                    "outstanding_charges": outstanding_charges,
                    # links
                    "ch_link": ch_link,
                    "google": google,
                }
            )
            processed += 1
            time.sleep(0.02)

        start_index += size

    return results

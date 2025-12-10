# ch_retirement_finder.py — simplified for Boomer Radar v1
# Rate-limit safe Companies House helpers + radius filtering.

import base64
import datetime as dt
import math
import os
import time
from collections import deque
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus

import requests

# ---------------------------------------------------------------------------
# API key
# ---------------------------------------------------------------------------

API_KEY = os.getenv("CH_API_KEY", "")
try:
    import streamlit as st  # type: ignore
except Exception:
    st = None  # type: ignore

if st is not None:
    API_KEY = API_KEY or (st.secrets.get("CH_API_KEY") if hasattr(st, "secrets") else "")

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

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "CH-Boomer-Radar/1.0.0"})

# ---------------------------------------------------------------------------
# Throttling
# ---------------------------------------------------------------------------

_WINDOW = 300.0  # seconds
_LIMIT = 580
_REQ_TIMES: deque = deque()


def _throttle():
    now = time.time()
    while _REQ_TIMES and now - _REQ_TIMES[0] > _WINDOW:
        _REQ_TIMES.popleft()
    if len(_REQ_TIMES) >= _LIMIT:
        sleep_for = _WINDOW - (now - _REQ_TIMES[0]) + 1
        time.sleep(max(1.0, sleep_for))
    _REQ_TIMES.append(time.time())


def _auth_header() -> Dict[str, str]:
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


@_cache_data(ttl=3600, show_spinner=False)
def _ch_get_cached(path: str, params_key: Tuple[Tuple[str, Any], ...]) -> Dict[str, Any]:
    url = f"{API_BASE}{path}"
    params = dict(params_key)
    resp = _request("GET", url, params=params)
    if resp.status_code == 204:
        return {}
    return resp.json()


def ch_get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    params_key: Tuple[Tuple[str, Any], ...] = tuple(sorted((params or {}).items()))
    return _ch_get_cached(path, params_key)


# ---------------------------------------------------------------------------
# Companies House endpoints
# ---------------------------------------------------------------------------


def advanced_search_by_sic(
    sic_codes: List[str],
    size: int = 100,
    start_index: int = 0,
    company_status: str = "active",
) -> Dict[str, Any]:
    return ch_get(
        "/advanced-search/companies",
        {
            "sic_codes": ",".join(sic_codes),
            "size": size,
            "start_index": start_index,
            "company_status": company_status,
        },
    )


def get_company_profile(company_number: str) -> Dict[str, Any]:
    return ch_get(f"/company/{company_number}")


def get_directors(company_number: str) -> List[Dict[str, Any]]:
    data = ch_get(
        f"/company/{company_number}/officers",
        {"items_per_page": 100, "order_by": "appointed_on"},
    )
    out: List[Dict[str, Any]] = []
    for it in data.get("items") or []:
        if it.get("officer_role") != "director":
            continue
        if it.get("resigned_on"):
            continue
        out.append({"name": it.get("name"), "dob": it.get("date_of_birth") or {}})
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def approx_age(dob: Optional[Dict[str, int]]) -> Optional[int]:
    if not dob or "year" not in dob:
        return None
    y, m = dob["year"], dob.get("month", 6)
    t = dt.date.today()
    return t.year - y - (t.month < m)


def months_between(d1: dt.date, d2: dt.date) -> int:
    if d1 > d2:
        d1, d2 = d2, d1
    return (d2.year - d1.year) * 12 + (d2.month - d1.month) - (1 if d2.day < d1.day else 0)


# ---------------------------------------------------------------------------
# Geo / Radius
# ---------------------------------------------------------------------------


@_cache_data(ttl=86400, show_spinner=False)
def _postcodes_bulk_cached(chunk: Tuple[str, ...]) -> Dict[str, Tuple[Optional[float], Optional[float]]]:
    out: Dict[str, Tuple[Optional[float], Optional[float]]] = {}
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


def _bulk_lookup_postcodes(postcodes: List[str]) -> Dict[str, Tuple[Optional[float], Optional[float]]]:
    out: Dict[str, Tuple[Optional[float], Optional[float]]] = {}
    uniq = [p for p in sorted({(p or "").strip().upper() for p in postcodes}) if p]
    for i in range(0, len(uniq), 100):
        chunk = tuple(uniq[i : i + 100])
        out.update(_postcodes_bulk_cached(chunk))
    return out


def geocode_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    pcs = [(r.get("postcode") or "").strip().upper() for r in rows]
    latlons = _bulk_lookup_postcodes(pcs)
    out: List[Dict[str, Any]] = []
    for r in rows:
        pc = (r.get("postcode") or "").strip().upper()
        lat, lon = latlons.get(pc, (None, None))
        r2 = dict(r)
        r2["lat"] = lat
        r2["lon"] = lon
        out.append(r2)
    return out


def _hav(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def filter_by_radius(rows: List[Dict[str, Any]], centre_postcode: str, radius_km: float) -> List[Dict[str, Any]]:
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
    out: List[Dict[str, Any]] = []
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


# ---------------------------------------------------------------------------
# Main search
# ---------------------------------------------------------------------------


def find_targets(
    sic_codes: List[str],
    *,
    min_age: int = 63,
    max_directors: int = 2,
    min_years_trading: int = 10,
    size: int = 100,
    start_page: int = 0,
    max_companies: int = 200,
) -> List[Dict[str, Any]]:
    """
    Return a list of companies that roughly match our retirement-ready profile:
    - Active
    - SIC in given list
    - Director ages >= min_age
    - At most max_directors active directors
    - At least min_years_trading
    - Clean risk flags
    - Accounts & confirmation not badly overdue
    """
    results: List[Dict[str, Any]] = []
    today = dt.date.today()

    start_index = start_page * size
    data = advanced_search_by_sic(sic_codes, size=size, start_index=start_index)
    items = data.get("items") or []
    if not items:
        return results

    for c in items:
        if len(results) >= max_companies:
            break

        cnum = c.get("company_number")
        if not cnum:
            continue

        created = c.get("date_of_creation")
        years_trading: Optional[int] = None
        if created:
            try:
                y, m, d = map(int, created.split("-"))
                created_date = dt.date(y, m, d)
                years_trading = today.year - y - ((today.month, today.day) < (m, d))
            except Exception:
                years_trading = None
        if (years_trading is None) or (years_trading < min_years_trading):
            continue

        # Directors & ages
        directors = get_directors(cnum)
        if not directors or len(directors) > max_directors:
            continue
        dir_ages = [approx_age(d.get("dob")) for d in directors]
        if any(a is None or a < min_age for a in dir_ages):
            continue
        valid_ages = [a for a in dir_ages if a is not None]
        avg_dir_age = round(sum(valid_ages) / len(valid_ages), 1) if valid_ages else None

        # Company profile
        prof = get_company_profile(cnum)
        ro = prof.get("registered_office_address") or {}
        pc = ro.get("postal_code") or ro.get("postcode")

        # Risk flags
        has_insolvency = bool(prof.get("has_insolvency_history"))
        undeliverable_ro = bool(prof.get("undeliverable_registered_office_address"))
        office_in_dispute = bool(prof.get("registered_office_is_in_dispute"))

        if has_insolvency or undeliverable_ro or office_in_dispute:
            continue

        # Accounts
        accounts = prof.get("accounts") or {}
        la = accounts.get("last_accounts") or {}
        last_made = la.get("made_up_to") or la.get("period_end_on")
        last_accounts_date: Optional[dt.date] = None
        if last_made:
            try:
                y, m, d = map(int, str(last_made).split("-"))
                last_accounts_date = dt.date(y, m, d)
            except Exception:
                pass

        months_since_accounts: Optional[int] = None
        if last_accounts_date:
            months_since_accounts = months_between(last_accounts_date, today)

        accounts_overdue = bool(accounts.get("overdue") or (accounts.get("next_accounts") or {}).get("overdue"))

        # Confirmation statement
        conf = prof.get("confirmation_statement") or {}
        conf_last = conf.get("last_made_up_to")
        conf_last_date: Optional[dt.date] = None
        if conf_last:
            try:
                y, m, d = map(int, str(conf_last).split("-"))
                conf_last_date = dt.date(y, m, d)
            except Exception:
                pass

        months_since_conf: Optional[int] = None
        if conf_last_date:
            months_since_conf = months_between(conf_last_date, today)

        conf_overdue = bool(conf.get("overdue"))

        # Simple freshness rules: ignore badly overdue or missing
        if accounts_overdue or (months_since_accounts is None) or months_since_accounts > 36:
            continue
        if conf_overdue or (months_since_conf is None) or months_since_conf > 36:
            continue

        ch_link = f"https://find-and-update.company-information.service.gov.uk/company/{cnum}"
        google = f"https://www.google.com/search?q={quote_plus((c.get('company_name') or '') + ' ' + (pc or ''))}"

        results.append(
            {
                "company_number": cnum,
                "company_name": c.get("company_name"),
                "incorporated": created,
                "years_trading": years_trading,
                "sic_codes": ",".join(c.get("sic_codes", [])),
                "active_directors": len(directors),
                "director_ages": ",".join(str(a) for a in dir_ages if a is not None),
                "avg_director_age": avg_dir_age,
                "postcode": pc,
                "last_accounts_made_up_to": str(last_accounts_date) if last_accounts_date else None,
                "months_since_accounts": months_since_accounts,
                "accounts_overdue": accounts_overdue,
                "confirmation_last_made_up_to": str(conf_last_date) if conf_last_date else None,
                "months_since_confirmation": months_since_conf,
                "confirmation_overdue": conf_overdue,
                "has_insolvency_history": has_insolvency,
                "undeliverable_registered_office_address": undeliverable_ro,
                "registered_office_is_in_dispute": office_in_dispute,
                "ch_link": ch_link,
                "google": google,
            }
        )

        time.sleep(0.02)

    return results

# ch_retirement_finder.py — scoring helpers + geocoding + everything else

import base64, datetime as dt, math, os, time
from collections import deque
from typing import Any
from urllib.parse import quote_plus

import requests
from lxml import etree, html

API_KEY = os.getenv("CH_API_KEY", "")
try:
    import streamlit as st
    API_KEY = API_KEY or (st.secrets.get("CH_API_KEY") if hasattr(st, "secrets") else "")
except Exception:
    pass
if not API_KEY:
    raise RuntimeError("CH_API_KEY not set. Add it in Streamlit Secrets or env var.")

API_BASE = "https://api.company-information.service.gov.uk"
DOC_BASE = "https://document-api.company-information.service.gov.uk"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "CH-Boomer-Radar/0.7"})

# ---- throttle (stay under ~600 / 5 min) ----
_WINDOW=300.0; _LIMIT=580; _REQ_TIMES:deque[float]=deque()
def _throttle():
    now=time.time()
    while _REQ_TIMES and now-_REQ_TIMES[0]>_WINDOW: _REQ_TIMES.popleft()
    if len(_REQ_TIMES)>=_LIMIT:
        sleep_for=_WINDOW-(now-_REQ_TIMES[0])+1
        time.sleep(max(1.0, sleep_for))
    _REQ_TIMES.append(time.time())

def _auth_header()->dict[str,str]:
    import base64
    return {"Authorization": f"Basic {base64.b64encode(f'{API_KEY}:'.encode()).decode()}"}

def _request(method:str,url:str,**kw)->requests.Response:
    headers=kw.pop("headers",{}); headers.update(_auth_header())
    _throttle(); resp=SESSION.request(method,url,headers=headers,timeout=30,**kw)
    if resp.status_code in (429,500,502,503,504):
        time.sleep(int(resp.headers.get("Retry-After","5"))); _throttle()
        resp=SESSION.request(method,url,headers=headers,timeout=30,**kw)
    resp.raise_for_status(); return resp

def ch_get(path:str, params:dict[str,Any]|None=None)->dict[str,Any]:
    r=_request("GET", f"{API_BASE}{path}", params=params)
    return {} if r.status_code==204 else r.json()

# ---------- CH calls ----------

def advanced_search_by_sic(sic_codes:list[str], size:int=100, start_index:int=0, company_status:str="active")->dict[str,Any]:
    return ch_get("/advanced-search/companies", {
        "sic_codes": ",".join(sic_codes), "size": size, "start_index": start_index, "company_status": company_status
    })

def get_company_profile(company_number:str)->dict[str,Any]:
    return ch_get(f"/company/{company_number}")

def get_directors(company_number:str)->list[dict[str,Any]]:
    data=ch_get(f"/company/{company_number}/officers", {"items_per_page":100,"order_by":"appointed_on"})
    out=[]
    for it in data.get("items") or []:
        if it.get("officer_role")!="director": continue
        if it.get("resigned_on"): continue
        out.append({"name":it.get("name"),"dob":it.get("date_of_birth") or {}})
    return out

def get_psc(company_number:str)->list[dict[str,Any]]:
    data=ch_get(f"/company/{company_number}/persons-with-significant-control", {"items_per_page":100})
    out=[]
    for it in data.get("items") or []:
        if it.get("ceased_on"): continue
        if it.get("kind")!="individual-person-with-significant-control": continue
        out.append({"name":it.get("name"),"dob":it.get("date_of_birth") or {}})
    return out

# ---------- helpers ----------

def approx_age(dob:dict[str,int]|None)->int|None:
    if not dob or "year" not in dob: return None
    y, m = dob["year"], dob.get("month",6)
    t=dt.date.today()
    return t.year - y - (t.month < m)

TURNOVER_TAGS=["Turnover","TurnoverRevenue","Revenue","Sales","RevenueFromContractWithCustomerExcludingAssessedTax","RevenueFromContractWithCustomerIncludingAssessedTax"]
PROFIT_TAGS=["ProfitLoss","ProfitLossForPeriod","ProfitLossOnOrdinaryActivitiesBeforeTax","ProfitLossOnOrdinaryActivitiesAfterTax"]
EMPLOYEE_TAGS=["AverageNumberEmployeesDuringPeriod","AverageNumberOfEmployeesDuringThePeriod","AverageNumberOfEmployees"]

def _num(s): 
    if s is None: return None
    try: return float(str(s).replace(",","").strip())
    except: return None

def _doc_fetch_content(doc_id:str)->tuple[bytes|None,str|None]:
    meta=_request("GET", f"{DOC_BASE}/document/{doc_id}").json()
    for mime in ("application/xhtml+xml","text/html","application/pdf"):
        res=(meta.get("resources") or {}).get(mime); 
        if not res: continue
        url=res.get("links",{}).get("self"); 
        if not url: continue
        r=_request("GET", f"{DOC_BASE}{url}")
        if r.is_redirect and r.headers.get("Location"):
            r=SESSION.get(r.headers["Location"],timeout=60)
        if r.status_code<400: return r.content, mime
    return None, None

def extract_financials(company_number:str)->dict[str,Any]:
    out={"turnover":None,"profit":None,"employees":None}
    fh=ch_get(f"/company/{company_number}/filing-history", {"category":"accounts","items_per_page":50})
    doc_id=None
    for it in fh.get("items") or []:
        meta=(it.get("links") or {}).get("document_metadata")
        if meta and "/document/" in meta: doc_id=meta.rsplit("/document/",1)[-1]; break
    if not doc_id: return out
    content,mime=_doc_fetch_content(doc_id)
    if not content or mime=="application/pdf": return out
    from lxml import etree, html
    try: root=html.fromstring(content)
    except: 
        try: root=etree.fromstring(content)
        except: return out
    def pick(tags:list[str]):
        xp="|".join([f".//*[local-name()='{t}']" for t in tags]); 
        if not xp: return None
        for n in root.xpath(xp):
            v=_num((n.text or "").strip())
            if v is not None: return v
        return None
    out["turnover"]=pick(TURNOVER_TAGS); out["profit"]=pick(PROFIT_TAGS); out["employees"]=pick(EMPLOYEE_TAGS)
    return out

# ---------- geo/radius ----------

def _hav(lat1,lon1,lat2,lon2):
    R=6371.0
    p1,p2=math.radians(lat1),math.radians(lat2)
    dphi=math.radians(lat2-lat1); dlmb=math.radians(lon2-lon1)
    a=math.sin(dphi/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dlmb/2)**2
    return 2*R*math.asin(math.sqrt(a))

def _bulk_lookup_postcodes(postcodes:list[str])->dict[str,tuple[float|None,float|None]]:
    out={}; uniq=[p for p in sorted({(p or "").strip().upper() for p in postcodes}) if p]
    for i in range(0,len(uniq),100):
        ch=uniq[i:i+100]
        try:
            r=SESSION.post("https://api.postcodes.io/postcodes", json={"postcodes":ch}, timeout=30)
            if r.status_code==200:
                for item in r.json().get("result",[]):
                    q=(item.get("query") or "").strip().upper()
                    res=item.get("result") or {}
                    out[q]=(res.get("latitude"),res.get("longitude")) if res else (None,None)
        except: 
            for q in ch: out[q]=(None,None)
    return out

def geocode_rows(rows:list[dict[str,Any]])->list[dict[str,Any]]:
    """Add lat/lon for all rows with a postcode (no distance calc)."""
    pcs=[(r.get("postcode") or "").strip().upper() for r in rows]
    latlons=_bulk_lookup_postcodes(pcs)
    out=[]
    for r in rows:
        pc=(r.get("postcode") or "").strip().upper()
        lat,lon=latlons.get(pc,(None,None))
        r2=dict(r); r2["lat"]=lat; r2["lon"]=lon
        out.append(r2)
    return out

def filter_by_radius(rows:list[dict[str,Any]], centre_postcode:str, radius_km:float)->list[dict[str,Any]]:
    if not rows or not centre_postcode or radius_km<=0: return rows
    # centre
    try:
        r=SESSION.get(f"https://api.postcodes.io/postcodes/{centre_postcode}", timeout=15)
        res=(r.json().get("result") or {}) if r.status_code==200 else {}
        lat0,lon0=res.get("latitude"),res.get("longitude")
    except: lat0=lon0=None
    if lat0 is None or lon0 is None: return []
    # bulk geocode company postcodes
    pcs=[(r.get("postcode") or "").strip().upper() for r in rows]
    latlons=_bulk_lookup_postcodes(pcs)
    out=[]
    for r in rows:
        pc=(r.get("postcode") or "").strip().upper()
        lat,lon=latlons.get(pc,(None,None))
        if lat is None or lon is None: continue
        dist=_hav(lat0,lon0,lat,lon)
        if dist<=radius_km:
            r2=dict(r); r2["distance_km"]=round(dist,1); r2["lat"]=lat; r2["lon"]=lon
            out.append(r2)
    out.sort(key=lambda x: x.get("distance_km",1e9))
    return out

# ---------- main search (unchanged behaviour) ----------

def find_targets(
    sic_codes:list[str], min_age:int=55, max_directors:int=2, size:int=100, pages:int=1, *,
    limit_companies:int=120, fetch_financials:bool=False, financials_top_n:int=40,
    min_employees:int=0, min_years_trading:int=0, fetch_psc:bool=False, psc_min_age:int=0, psc_max_count:int=2,
)->list[dict[str,Any]]:
    results=[]; start_index=0; processed=0; today=dt.date.today()

    for _ in range(pages):
        items=advanced_search_by_sic(sic_codes,size=size,start_index=start_index).get("items") or []
        if not items: break
        for c in items:
            if processed>=limit_companies: return results
            cnum=c.get("company_number")
            created=c.get("date_of_creation")
            years=None
            if created:
                try:
                    y,m,d=map(int,created.split("-")); t=today
                    years=t.year-y-((t.month,t.day)<(m,d))
                except: years=None
            if min_years_trading and (years is None or years<min_years_trading): 
                continue

            directors=get_directors(cnum)
            if not directors or len(directors)>max_directors: continue
            dir_ages=[approx_age(d.get("dob")) for d in directors]
            if any(a is None or a<min_age for a in dir_ages): continue
            avg_dir_age=round(sum(a for a in dir_ages if a is not None)/len(dir_ages),1) if dir_ages else None

            # postcode
            ro=c.get("registered_office_address") or {}
            pc=ro.get("postal_code") or ro.get("postcode")
            if not pc:
                prof=get_company_profile(cnum); ro=prof.get("registered_office_address") or {}
                pc=ro.get("postal_code") or ro.get("postcode")

            fin={"turnover":None,"profit":None,"employees":None}
            if fetch_financials and processed<financials_top_n:
                fin=extract_financials(cnum)
                if min_employees and fin.get("employees") is not None and fin["employees"]<min_employees:
                    processed+=1; continue

            avg_psc_age=None; psc_ages=[]; psc_count=None
            if fetch_psc:
                pscs=get_psc(cnum); psc_count=len(pscs)
                psc_ages=[a for a in (approx_age(p.get("dob")) for p in pscs) if a is not None]
                if psc_max_count and psc_count>psc_max_count: processed+=1; continue
                if psc_min_age and (not psc_ages or any(a<psc_min_age for a in psc_ages)): processed+=1; continue
                if psc_ages: avg_psc_age=round(sum(psc_ages)/len(psc_ages),1)

            ch_link=f"https://find-and-update.company-information.service.gov.uk/company/{cnum}"
            google=f"https://www.google.com/search?q={quote_plus((c.get('company_name') or '')+' '+(pc or ''))}"

            results.append({
                "company_number":cnum,
                "company_name":c.get("company_name"),
                "incorporated":created,
                "years_trading":years,
                "sic_codes":",".join(c.get("sic_codes",[])),
                "active_directors":len(directors),
                "director_ages":",".join(str(a) for a in dir_ages if a is not None),
                "avg_director_age":avg_dir_age,
                "avg_psc_age":avg_psc_age,
                "postcode":pc,
                "turnover":fin.get("turnover"),
                "profit":fin.get("profit"),
                "employees":fin.get("employees"),
                "psc_count":psc_count,
                "psc_ages":",".join(map(str,psc_ages)) if psc_ages else None,
                "ch_link":ch_link,
                "google":google,
            })
            processed+=1; time.sleep(0.02)
        start_index+=size
    return results

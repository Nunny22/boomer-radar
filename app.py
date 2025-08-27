# Streamlit UI — Boomer Score + nicer layout

import math
import pandas as pd
import streamlit as st
from ch_retirement_finder import find_targets, filter_by_radius

st.set_page_config(page_title="Boomer Radar", page_icon="🎯", layout="wide")

# ---- little style polish ----
st.markdown("""
<style>
/* tidy the page a bit */
section[data-testid="stSidebar"] {border-right: 1px solid #eee;}
.block-container {padding-top: 1.2rem;}
.kpi {padding:12px 16px;border-radius:14px;background:#f7f7fb;border:1px solid #eee;}
.kpi h3 {margin:0;font-size:1.9rem;}
.kpi small {color:#666;}
</style>
""", unsafe_allow_html=True)

st.title("Companies House – Baby Boomer Radar (Prototype)")

with st.sidebar:
    st.header("Search Filters")
    sic_str = st.text_input("SIC codes (space or comma separated)", value="10110 10710 22220 25110 25620 25990 33120 33200")
    min_age = st.number_input("Minimum director age", min_value=50, max_value=90, value=55)
    max_directors = st.number_input("Max active directors", min_value=1, max_value=5, value=2)
    min_years_trading = st.slider("Min years trading", 0, 40, 10)

    st.divider()
    st.subheader("Rate-limit safe settings")
    limit_companies = st.slider("Max companies to scan", 20, 200, 120, step=10)
    size = st.slider("Advanced search page size", 50, 500, 100, step=50)
    pages = st.slider("Pages to fetch", 1, 10, 1)

    st.divider()
    st.subheader("Financials (best-effort iXBRL)")
    fetch_financials = st.checkbox("Fetch turnover/profit & employees (slower)", value=False)
    financials_top_n = st.slider("Only fetch for first N companies", 10, 100, 40, step=10)
    min_employees = st.slider("Min employees (if known)", 0, 500, 0, step=5)

    st.divider()
    st.subheader("Owners (PSC) filter")
    fetch_psc = st.checkbox("Check PSC owners", value=False)
    psc_min_age = st.slider("PSC min age", 0, 90, 55)
    psc_max_count = st.slider("PSC max count", 1, 5, 2)

    st.divider()
    st.subheader("Radius (optional)")
    centre_pc = st.text_input("Centre postcode (e.g. WA13 0AG)", value="")
    radius_km = st.number_input("Radius in km", min_value=1, max_value=200, value=25)

    st.divider()
    st.subheader("Boomer Score weights")
    w_dir = st.slider("Director age weight", 0.0, 5.0, 4.0, 0.5)
    w_psc = st.slider("PSC age weight", 0.0, 5.0, 3.0, 0.5)
    w_years = st.slider("Years trading weight", 0.0, 5.0, 3.0, 0.5)
    w_emp = st.slider("Employees weight", 0.0, 5.0, 2.0, 0.5)
    w_turn = st.slider("Turnover weight", 0.0, 5.0, 1.0, 0.5)
    w_dist = st.slider("Nearness weight (closer=better)", 0.0, 5.0, 2.0, 0.5)

    run = st.button("Run search", use_container_width=True)

def _norm(x, lo, hi):
    if x is None or (isinstance(x, float) and math.isnan(x)): return None
    if hi == lo: return 0.0
    v = max(lo, min(hi, float(x)))
    return (v - lo) / (hi - lo)

def add_boomer_score(df: pd.DataFrame, radius_used: float | None):
    # pieces scaled 0..1 then weighted, overall 0..100
    dir_age = df["avg_director_age"].apply(lambda a: _norm(a, 55, 85))
    # if PSC age missing, fall back to director age
    psc_age = df["avg_psc_age"].where(df["avg_psc_age"].notna(), df["avg_director_age"]).apply(lambda a: _norm(a, 55, 85))
    years   = df["years_trading"].apply(lambda y: _norm(y, 5, 30))
    emps    = df["employees"].apply(lambda e: _norm(e, 1, 100))
    # scale turnover (very rough — avoids giant numbers dominating)
    turn    = df["turnover"].apply(lambda t: _norm(t, 100_000, 5_000_000))
    # distance: closer = better
    if radius_used and "distance_km" in df.columns:
        near = df["distance_km"].apply(lambda d: None if pd.isna(d) else max(0.0, 1.0 - min(float(d)/float(radius_used), 1.0)))
    else:
        near = pd.Series([None]*len(df))

    weights = [w_dir, w_psc, w_years, w_emp, w_turn, w_dist]
    parts = [dir_age, psc_age, years, emps, turn, near]
    # fill missing components with 0
    parts = [p.fillna(0.0) for p in parts]
    total_w = sum(weights) if sum(weights) > 0 else 1.0
    score = 100.0 * sum(w*p for w,p in zip(weights, parts)) / total_w
    df["boomer_score"] = score.round(1)
    return df

if run:
    sic_codes = [s.strip() for part in sic_str.split(",") for s in part.split() if s.strip()]
    with st.spinner("Querying Companies House… (throttled)"):
        rows = find_targets(
            sic_codes,
            min_age=int(min_age),
            max_directors=int(max_directors),
            size=int(size),
            pages=int(pages),
            limit_companies=int(limit_companies),
            fetch_financials=bool(fetch_financials),
            financials_top_n=int(financials_top_n),
            min_employees=int(min_employees),
            min_years_trading=int(min_years_trading),
            fetch_psc=bool(fetch_psc),
            psc_min_age=int(psc_min_age),
            psc_max_count=int(psc_max_count),
        )

    if centre_pc.strip():
        with st.spinner("Filtering by radius…"):
            rows = filter_by_radius(rows, centre_pc.strip(), float(radius_km))

    if not rows:
        st.warning("No matching companies (or none within radius). Try more pages or relax filters.")
    else:
        df = pd.DataFrame(rows)
        df = add_boomer_score(df, radius_km if centre_pc.strip() else None)

        # KPIs
        c1,c2,c3,c4 = st.columns(4)
        with c1: st.markdown(f'<div class="kpi"><small>Results</small><h3>{len(df):,}</h3></div>', unsafe_allow_html=True)
        with c2: st.markdown(f'<div class="kpi"><small>Avg Boomer Score</small><h3>{df["boomer_score"].mean():.1f}</h3></div>', unsafe_allow_html=True)
        with c3: st.markdown(f'<div class="kpi"><small>Avg Years Trading</small><h3>{df["years_trading"].mean():.1f}</h3></div>', unsafe_allow_html=True)
        with c4: st.markdown(f'<div class="kpi"><small>Avg Dir Age</small><h3>{df["avg_director_age"].mean():.1f}</h3></div>', unsafe_allow_html=True)

        # Nice defaults for display
        view_cols = [
            "boomer_score","company_name","company_number","years_trading","avg_director_age","psc_count",
            "employees","turnover","profit","postcode","distance_km","ch_link","google"
        ]
        for col in view_cols:
            if col not in df.columns: df[col] = None
        df_view = df[view_cols].sort_values("boomer_score", ascending=False)

        st.dataframe(
            df_view,
            width="stretch",
            column_config={
                "boomer_score": st.column_config.NumberColumn("Boomer score", format="%.1f"),
                "years_trading": st.column_config.NumberColumn("Years", format="%.0f"),
                "avg_director_age": st.column_config.NumberColumn("Dir age (avg)", format="%.0f"),
                "employees": st.column_config.NumberColumn("Employees", format="%.0f"),
                "turnover": st.column_config.NumberColumn("Turnover", format="£%0.0f"),
                "profit":   st.column_config.NumberColumn("Profit",   format="£%0.0f"),
                "distance_km": st.column_config.NumberColumn("Km away", format="%.1f"),
                "ch_link": st.column_config.LinkColumn("Companies House", display_text="Open"),
                "google":  st.column_config.LinkColumn("Google", display_text="Search"),
            }
        )

        st.download_button(
            "Download outreach CSV",
            data=df.sort_values("boomer_score", ascending=False).to_csv(index=False),
            file_name="boomer_radar_targets.csv",
            mime="text/csv",
            use_container_width=True,
        )

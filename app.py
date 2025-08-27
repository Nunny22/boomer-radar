# Streamlit UI — Boomer Score + Map + Outreach + polished layout

import math, urllib.parse as ul
import pandas as pd
import streamlit as st
import pydeck as pdk

from ch_retirement_finder import find_targets, filter_by_radius, geocode_rows

st.set_page_config(page_title="Boomer Radar", page_icon="🎯", layout="wide")

# --- styles ---
st.markdown("""
<style>
section[data-testid="stSidebar"] {border-right: 1px solid #eee;}
.block-container {padding-top:1rem;}
.kpi {padding:12px 16px;border-radius:14px;background:#f7f7fb;border:1px solid #eee;}
.kpi h3 {margin:0;font-size:1.9rem;}
.kpi small{color:#667085;}
</style>
""", unsafe_allow_html=True)

st.markdown("### 🎯 Boomer Radar — Companies House deal finder")

with st.sidebar:
    st.header("Search Filters")
    sic_str = st.text_input("SIC codes (space or comma separated)", value="10110 10710 22220 25110 25620 25990 33120 33200")
    min_age = st.number_input("Minimum director age", min_value=50, max_value=90, value=55)
    max_directors = st.number_input("Max active directors", min_value=1, max_value=5, value=2)
    min_years_trading = st.slider("Min years trading", 0, 40, 10)

    st.divider()
    st.subheader("Rate-limit safe")
    limit_companies = st.slider("Max companies to scan", 20, 200, 120, step=10)
    size = st.slider("Advanced search page size", 50, 500, 100, step=50)
    pages = st.slider("Pages to fetch", 1, 10, 1)

    st.divider()
    st.subheader("Financials (iXBRL best-effort)")
    fetch_financials = st.checkbox("Fetch turnover/profit & employees (slower)", value=False)
    financials_top_n = st.slider("Only fetch for first N companies", 10, 100, 40, step=10)
    min_employees = st.slider("Min employees (if known)", 0, 500, 0, step=5)

    st.divider()
    st.subheader("Owners (PSC)")
    fetch_psc = st.checkbox("Check PSC owners", value=False)
    psc_min_age = st.slider("PSC min age", 0, 90, 55)
    psc_max_count = st.slider("PSC max count", 1, 5, 2)

    st.divider()
    st.subheader("Radius (optional)")
    centre_pc = st.text_input("Centre postcode (e.g. WA13 0AG)", value="")
    radius_km = st.number_input("Radius in km", min_value=1, max_value=200, value=25)

    st.divider()
    st.subheader("Boomer Score weights")
    w_dir = st.slider("Director age", 0.0, 5.0, 4.0, 0.5)
    w_psc = st.slider("PSC age", 0.0, 5.0, 3.0, 0.5)
    w_years = st.slider("Years trading", 0.0, 5.0, 3.0, 0.5)
    w_emp = st.slider("Employees", 0.0, 5.0, 2.0, 0.5)
    w_turn = st.slider("Turnover", 0.0, 5.0, 1.0, 0.5)
    w_dist = st.slider("Nearness (closer=better)", 0.0, 5.0, 2.0, 0.5)

    st.divider()
    st.subheader("Outreach template")
    your_name = st.text_input("Your name", value="Mike")
    your_company = st.text_input("Your company", value="Acquirer Ltd")
    your_phone = st.text_input("Phone", value="")
    tone = st.selectbox("Tone", ["Friendly", "Professional", "Direct"])

    run = st.button("Run search", use_container_width=True)

def _norm(x, lo, hi):
    if x is None or (isinstance(x, float) and math.isnan(x)): return None
    if hi == lo: return 0.0
    v = max(lo, min(hi, float(x)))
    return (v - lo) / (hi - lo)

def add_boomer_score(df: pd.DataFrame, radius_used: float | None):
    dir_age = df["avg_director_age"].apply(lambda a: _norm(a, 55, 85))
    psc_age = df["avg_psc_age"].where(df["avg_psc_age"].notna(), df["avg_director_age"]).apply(lambda a: _norm(a, 55, 85))
    years   = df["years_trading"].apply(lambda y: _norm(y, 5, 30))
    emps    = df["employees"].apply(lambda e: _norm(e, 1, 100))
    turn    = df["turnover"].apply(lambda t: _norm(t, 100_000, 5_000_000))
    if radius_used and "distance_km" in df.columns:
        near = df["distance_km"].apply(lambda d: None if pd.isna(d) else max(0.0, 1.0 - min(float(d)/float(radius_used), 1.0)))
    else:
        near = pd.Series([None]*len(df))
    weights = [w_dir, w_psc, w_years, w_emp, w_turn, w_dist]
    parts = [dir_age, psc_age, years, emps, turn, near]
    parts = [p.fillna(0.0) for p in parts]
    total_w = sum(weights) if sum(weights)>0 else 1.0
    score = 100.0 * sum(w*p for w,p in zip(weights, parts)) / total_w
    df["boomer_score"] = score.round(1)
    return df

def build_email(company_name, ch_link, your_name, your_company, your_phone, tone):
    subj = f"Succession / exit option for {company_name}"
    if tone == "Friendly":
        body = (f"Hi,\n\nI run {your_company}. We're looking to take over well-run businesses from owners "
                f"thinking about retirement. If you'd ever consider an exit or management handover, could we chat?\n\n"
                f"Companies House link: {ch_link}\n"
                f"{('Phone: ' + your_phone + '\\n') if your_phone else ''}"
                f"Best,\n{your_name}")
    elif tone == "Direct":
        body = (f"Hello,\n\nI represent {your_company}. We acquire profitable businesses with experienced owners "
                f"planning succession. Would you be open to a confidential conversation?\n\n{ch_link}\n\n"
                f"Regards,\n{your_name}{(' | '+your_phone) if your_phone else ''}")
    else:
        body = (f"Hello,\n\nI'm {your_name} from {your_company}. We specialise in succession purchases for established "
                f"firms. If an ownership transition is on your mind, I'd welcome a short call.\n\n"
                f"Company profile: {ch_link}\n\nKind regards,\n{your_name}{(' | '+your_phone) if your_phone else ''}")
    return subj, body

if run:
    sic_codes = [s.strip() for part in sic_str.split(",") for s in part.split() if s.strip()]
    with st.spinner("Querying Companies House… (throttled)"):
        rows = find_targets(
            sic_codes,
            min_age=int(min_age), max_directors=int(max_directors),
            size=int(size), pages=int(pages),
            limit_companies=int(limit_companies),
            fetch_financials=bool(fetch_financials), financials_top_n=int(financials_top_n),
            min_employees=int(min_employees), min_years_trading=int(min_years_trading),
            fetch_psc=bool(fetch_psc), psc_min_age=55 if fetch_psc else 0, psc_max_count=int(psc_max_count),
        )

    # radius or plain geocode (so we can map)
    if centre_pc.strip():
        with st.spinner("Filtering by radius…"):
            rows = filter_by_radius(rows, centre_pc.strip(), float(radius_km))
    else:
        rows = geocode_rows(rows)

    if not rows:
        st.warning("No matching companies (or none within radius). Try more pages or relax filters.")
    else:
        df = pd.DataFrame(rows)
        df = add_boomer_score(df, radius_km if centre_pc.strip() else None)

        # Outreach columns (subject/body + mailto with no To address)
        subs, bodies, links = [], [], []
        for _, r in df.iterrows():
            subj, body = build_email(r["company_name"], r["ch_link"], your_name, your_company, your_phone, tone)
            subs.append(subj); bodies.append(body)
            links.append("mailto:?subject=" + ul.quote(subj) + "&body=" + ul.quote(body))
        df["email_subject"] = subs
        df["email_body"] = bodies
        df["email_link"] = links

        # KPIs
        c1,c2,c3,c4 = st.columns(4)
        with c1: st.markdown(f'<div class="kpi"><small>Results</small><h3>{len(df):,}</h3></div>', unsafe_allow_html=True)
        with c2: st.markdown(f'<div class="kpi"><small>Avg score</small><h3>{df["boomer_score"].mean():.1f}</h3></div>', unsafe_allow_html=True)
        with c3: st.markdown(f'<div class="kpi"><small>Avg years</small><h3>{df["years_trading"].mean():.1f}</h3></div>', unsafe_allow_html=True)
        with c4: st.markdown(f'<div class="kpi"><small>Avg dir age</small><h3>{df["avg_director_age"].mean():.1f}</h3></div>', unsafe_allow_html=True)

        # Tabs: Results / Map / About
        t1, t2, t3 = st.tabs(["📋 Results", "🗺️ Map", "ℹ️ About"])

        with t1:
            view_cols = [
                "boomer_score","company_name","company_number","years_trading","avg_director_age",
                "psc_count","employees","turnover","profit","postcode","distance_km",
                "ch_link","google","email_link"
            ]
            for c in view_cols:
                if c not in df.columns: df[c] = None
            st.dataframe(
                df[view_cols].sort_values("boomer_score", ascending=False),
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
                    "email_link": st.column_config.LinkColumn("Email", display_text="Compose"),
                }
            )
            st.download_button(
                "Download outreach CSV",
                data=df.sort_values("boomer_score", ascending=False).to_csv(index=False),
                file_name="boomer_radar_targets.csv",
                mime="text/csv",
                use_container_width=True,
            )

        with t2:
            # Map only if we have lat/lon
            if {"lat","lon"}.issubset(df.columns) and df["lat"].notna().any():
                # nicer colour scale by score
                dmap = df.dropna(subset=["lat","lon"]).copy()
                dmap["size"] = 400 + (dmap["boomer_score"].fillna(50) * 6)  # radius
                layer = pdk.Layer(
                    "ScatterplotLayer",
                    data=dmap,
                    get_position='[lon, lat]',
                    get_radius="size",
                    pickable=True,
                )
                tooltip={"html": "<b>{company_name}</b><br/>Score: {boomer_score}<br/>{postcode}"}
                st.pydeck_chart(pdk.Deck(map_style="mapbox://styles/mapbox/light-v9",
                                         initial_view_state=pdk.ViewState(latitude=float(dmap["lat"].mean()),
                                                                          longitude=float(dmap["lon"].mean()),
                                                                          zoom=6),
                                         layers=[layer], tooltip=tooltip))
            else:
                st.info("No coordinates yet. Use the Radius filter or run without radius to geocode all.")

        with t3:
            st.markdown("""
**How scoring works**

- Director age, PSC age, years trading, employees, turnover and nearness are scaled to 0..1  
- Weighted by the sliders in the sidebar and combined into a **0–100 Boomer Score**  
- Use the CSV for outreach; each row includes a ready-made email subject/body and a `mailto:` link.

*Tip:* keep “Fetch turnover/profit” off for broad scans; switch on when you’re narrowing targets.
            """)

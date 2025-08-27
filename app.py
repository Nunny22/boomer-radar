# Streamlit UI — adds trading age, PSC, employees, outreach links + rate-limit controls

import pandas as pd
import streamlit as st
from ch_retirement_finder import find_targets, filter_by_radius

st.set_page_config(page_title="CH Retirement Finder", layout="wide")
st.title("Companies House – Baby Boomer Radar (Prototype)")

with st.sidebar:
    st.header("Search Filters")
    sic_str = st.text_input(
        "SIC codes (space or comma separated)",
        value="10110 10710 22220 25110 25620 25990 33120 33200",
    )
    min_age = st.number_input("Minimum director age", min_value=50, max_value=90, value=55)
    max_directors = st.number_input("Max active directors", min_value=1, max_value=5, value=2)
    min_years_trading = st.slider("Min years trading (incorporated)", 0, 40, 10)

    st.divider()
    st.subheader("Rate-limit safe settings")
    limit_companies = st.slider("Max companies to scan this run", 20, 200, 120, step=10)
    size = st.slider("Advanced search page size", 50, 500, 100, step=50)
    pages = st.slider("Pages to fetch", 1, 10, 1)

    st.divider()
    st.subheader("Financials (best-effort iXBRL)")
    fetch_financials = st.checkbox("Fetch turnover/profit & employees (slower)", value=False)
    financials_top_n = st.slider("Only fetch for first N companies", 10, 100, 40, step=10)
    min_employees = st.slider("Min employees (if known)", 0, 500, 0, step=5)

    st.divider()
    st.subheader("Owners (PSC) filter (optional)")
    fetch_psc = st.checkbox("Check PSC owners", value=False)
    psc_min_age = st.slider("PSC min age", 0, 90, 55)
    psc_max_count = st.slider("PSC max count", 1, 5, 2)

    st.divider()
    st.subheader("Radius filter (optional)")
    centre_pc = st.text_input("Centre postcode (e.g. WA13 0AG)", value="")
    radius_km = st.number_input("Radius in km", min_value=1, max_value=200, value=25)

    run = st.button("Run search")

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
        st.dataframe(df, width="stretch")
        st.download_button(
            "Download outreach CSV",
            data=df.to_csv(index=False),
            file_name="boomer_radar_targets.csv",
            mime="text/csv",
        )
        st.caption("CSV includes CH profile and Google search links for each company.")

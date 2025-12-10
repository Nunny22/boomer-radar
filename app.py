# app.py — Boomer Radar (simplified v1 + directors column + safe CH error handling)
# Focused on: older owners, long trading history, local radius, curated SICs
# Simple UI, clean results, score column, CSV export.

import math
import urllib.parse as ul
from typing import Dict, List

import pandas as pd
import streamlit as st

from ch_retirement_finder import (
    find_targets,
    filter_by_radius,
    geocode_rows,
)

# ---------------------------------------------------------------------------
# Page & global styles
# ---------------------------------------------------------------------------

st.set_page_config(page_title="Boomer Radar", page_icon="🎯", layout="wide")

st.markdown(
    """
<style>
/* Layout */
.block-container {
    padding-top: 2rem !important;
    padding-bottom: 2rem !important;
    max-width: 1200px;
}

/* Sidebar */
section[data-testid="stSidebar"] {
    border-right: 1px solid #e5e7eb;
    background: #f8fafc;
}
section[data-testid="stSidebar"] .block-container {
    padding-top: 1.5rem !important;
}

/* Page title area */
.boomer-header {
    padding: 18px 22px;
    border-radius: 18px;
    background: linear-gradient(135deg, #0f172a, #1d4ed8);
    color: #f9fafb;
    margin-bottom: 18px;
}
.boomer-header h1 {
    font-size: 1.8rem;
    margin: 0 0 4px 0;
}
.boomer-header p {
    margin: 0;
    opacity: 0.8;
    font-size: 0.95rem;
}

/* KPI cards */
.kpi-row {
    margin-bottom: 12px;
}
.kpi {
    padding: 12px 16px;
    border-radius: 16px;
    background: #ffffff;
    border: 1px solid #e5e7eb;
    box-shadow: 0 8px 18px rgba(15, 23, 42, 0.03);
}
.kpi h3 {
    margin: 2px 0 0 0;
    font-size: 1.4rem;
    line-height: 1.1;
    color: #0f172a;
}
.kpi small {
    color: #6b7280;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    font-size: 0.72rem;
}

/* Data table tweaks */
[data-testid="stDataFrame"] {
    border-radius: 14px;
    overflow: hidden;
    border: 1px solid #e5e7eb;
    box-shadow: 0 10px 24px rgba(15, 23, 42, 0.04);
}

/* Buttons */
.stButton button {
    border-radius: 999px;
    border: 1px solid #1d4ed8;
    background: #1d4ed8;
    color: #f9fafb;
    padding: 0.4rem 1.2rem;
    font-weight: 500;
}
.stButton button:hover {
    background: #1e40af;
    border-color: #1e40af;
}
</style>
""",
    unsafe_allow_html=True,
)

st.markdown(
    """
<div class="boomer-header">
  <h1>🎯 Boomer Radar</h1>
  <p>Find retirement-ready owners of boring, stable manufacturing and industrial businesses near WA14.</p>
</div>
""",
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------------
# Curated SIC groups (boring, stable, non-AI / physical work)
# ---------------------------------------------------------------------------


def sic_groups() -> Dict[str, List[str]]:
    return {
        "Fabrication & metalwork": [
            "25110", "25120", "25290", "25610", "25620", "25990", "24540",
        ],
        "Machinery & engineering": [
            "28220", "28290", "28410", "28490", "28990", "33120", "33140", "33200",
        ],
        "Plastics & packaging": [
            "22210", "22220", "22230", "22290", "17230", "17290",
        ],
        "Electrical & components": [
            "27120", "27900", "26511",
        ],
        "Industrial / trade supply": [
            "46620", "46690", "46720", "46740", "46900", "46130", "46730",
        ],
        "Automotive parts & filters": [
            "29320", "45310", "45320",
        ],
        "Joinery / wood products": [
            "16230", "16240",
        ],
        "Other boring manufacturing": [
            "20412", "20590", "32990", "38320",
        ],
    }


# ---------------------------------------------------------------------------
# Sidebar controls
# ---------------------------------------------------------------------------

with st.sidebar:
    st.header("Search filters")

    # Geography
    st.subheader("Location")
    centre_pc = st.text_input("Centre postcode", value="WA14 4YU")
    radius_km = st.slider("Radius (km)", min_value=5, max_value=50, value=25, step=5)

    st.subheader("Owner & business profile")
    min_age = st.number_input("Minimum director age", min_value=55, max_value=80, value=63)
    max_directors = st.number_input("Max active directors", min_value=1, max_value=4, value=2)
    min_years_trading = st.slider("Minimum years trading", 0, 40, 10)

    st.subheader("Industries (SIC groups)")
    selected_sics: List[str] = []
    for group_name, codes in sic_groups().items():
        checked = st.checkbox(f"{group_name} ({', '.join(codes)})", value=True)
        if checked:
            selected_sics.extend(codes)

    st.subheader("Companies House results page")
    page_number = st.number_input(
        "Advanced search page (0 = first page)",
        min_value=0,
        max_value=999,
        value=0,
        step=1,
        help="Use this to move through the CH advanced search results.",
    )

    run_search = st.button("Run search", use_container_width=True)

# ---------------------------------------------------------------------------
# Helper: scoring for prioritisation (0–100)
# ---------------------------------------------------------------------------


def compute_score_row(row, radius_used: float) -> float:
    score = 0.0

    # Director age: reward 63–75
    age = row.get("avg_director_age")
    if isinstance(age, (int, float)):
        if age >= 75:
            score += 40
        elif age >= 63:
            score += 25 + (age - 63) * 1.2  # gentle ramp

    # Years trading: reward 10–40
    yrs = row.get("years_trading")
    if isinstance(yrs, (int, float)):
        if yrs >= 30:
            score += 30
        elif yrs >= 10:
            score += 15 + (yrs - 10) * 0.75

    # Distance: closer is better
    dist = row.get("distance_km")
    if isinstance(dist, (int, float)) and radius_used > 0:
        if dist <= radius_used / 2:
            score += 20
        elif dist <= radius_used:
            score += 10

    # Filings freshness (accounts & confirmation)
    if not row.get("accounts_overdue") and not row.get("confirmation_overdue"):
        score += 10

    return round(min(score, 100.0), 1)


# ---------------------------------------------------------------------------
# Main search + results
# ---------------------------------------------------------------------------

if run_search:
    if not selected_sics:
        st.warning("Please keep at least one SIC group selected.")
    else:
        # -----------------------------
        # Call Companies House safely
        # -----------------------------
        with st.spinner("Querying Companies House…"):
            try:
                rows = find_targets(
                    selected_sics,
                    min_age=int(min_age),
                    max_directors=int(max_directors),
                    min_years_trading=int(min_years_trading),
                    size=100,
                    start_page=int(page_number),
                    max_companies=200,
                )
            except Exception:
                st.warning(
                    "Companies House didn't return results for this SIC + page + filter combination. "
                    "Try a different page number, or slightly widen your filters (radius / years / age)."
                )
                rows = []

        if not rows:
            st.info(
                "No companies matched your filters on this page. "
                "Try lowering director age, reducing years trading, widening radius, or trying a different page."
            )
        else:
            # Radius filtering / geocoding
            if centre_pc.strip():
                with st.spinner("Filtering by radius…"):
                    rows = filter_by_radius(rows, centre_pc.strip(), float(radius_km))
            else:
                rows = geocode_rows(rows)

            if not rows:
                st.info(
                    "Companies were found on Companies House, but none within this radius.\n\n"
                    "Try increasing the radius (e.g. 40–50km) or temporarily clearing the postcode."
                )
            else:
                df = pd.DataFrame(rows)

                # Compute score
                df["score"] = df.apply(lambda r: compute_score_row(r, float(radius_km)), axis=1)

                # Basic KPIs
                c1, c2, c3, c4 = st.columns(4)
                with c1:
                    st.markdown(
                        f'<div class="kpi"><small>RESULTS</small><h3>{len(df):,}</h3></div>',
                        unsafe_allow_html=True,
                    )
                with c2:
                    avg_score = df["score"].mean() if not df.empty else 0
                    st.markdown(
                        f'<div class="kpi"><small>AVG SCORE</small><h3>{avg_score:.1f}</h3></div>',
                        unsafe_allow_html=True,
                    )
                with c3:
                    avg_years = df["years_trading"].mean() if "years_trading" in df.columns else 0
                    st.markdown(
                        f'<div class="kpi"><small>AVG YEARS</small><h3>{avg_years:.1f}</h3></div>',
                        unsafe_allow_html=True,
                    )
                with c4:
                    avg_age = df["avg_director_age"].mean() if "avg_director_age" in df.columns else 0
                    st.markdown(
                        f'<div class="kpi"><small>AVG DIR AGE</small><h3>{avg_age:.1f}</h3></div>',
                        unsafe_allow_html=True,
                    )

                # Outreach links
                email_subjects, email_bodies, email_links = [], [], []
                for _, r in df.iterrows():
                    ch_link = r.get("ch_link", "")
                    company_name = r.get("company_name", "")
                    subj = f"Succession / exit option for {company_name}"
                    body = (
                        f"Hi,\n\nI run an acquisition company focused on long-established, well-run firms "
                        f"where the owner is considering retirement. Would you be open to a confidential chat?\n\n"
                        f"Companies House profile: {ch_link}\n\n"
                        f"Best regards,\n"
                    )
                    email_subjects.append(subj)
                    email_bodies.append(body)
                    email_links.append(
                        "mailto:?subject=" + ul.quote(subj) + "&body=" + ul.quote(body)
                    )

                df["email_subject"] = email_subjects
                df["email_body"] = email_bodies
                df["email_link"] = email_links

                # Columns to show (includes active_directors)
                view_cols = [
                    "score",
                    "company_name",
                    "company_number",
                    "years_trading",
                    "avg_director_age",
                    "active_directors",
                    "director_ages",
                    "postcode",
                    "distance_km",
                    "last_accounts_made_up_to",
                    "months_since_accounts",
                    "accounts_overdue",
                    "confirmation_last_made_up_to",
                    "months_since_confirmation",
                    "confirmation_overdue",
                    "sic_codes",
                    "ch_link",
                    "google",
                    "email_link",
                ]
                for c in view_cols:
                    if c not in df.columns:
                        df[c] = None

                st.subheader("Results")
                st.dataframe(
                    df[view_cols].sort_values("score", ascending=False),
                    use_container_width=True,
                )

                # CSV export
                st.subheader("Export")
                st.download_button(
                    "Download results CSV",
                    data=df[view_cols + ["email_subject", "email_body"]].to_csv(index=False),
                    file_name="boomer_radar_results.csv",
                    mime="text/csv",
                    use_container_width=True,
                )

else:
    st.info("Set your filters in the sidebar and click **Run search** to begin.")

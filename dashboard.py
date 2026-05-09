import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from scraper import load_all, load_robinhood_monthly, load_robinhood_from_uploads, RBHD_DIR

st.set_page_config(
    page_title="OCC Weekly Options Volume",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title("OCC Weekly Options Contract Volume")


# ── load data ─────────────────────────────────────────────────────────────────

@st.cache_data(show_spinner="Loading data…")
def get_data():
    return load_all()

df = get_data()

# ── sidebar filters ───────────────────────────────────────────────────────────

st.sidebar.header("Filters")

classes = sorted(df["report_class"].unique())
selected_classes = st.sidebar.multiselect("Asset Class", classes, default=classes)

sections = sorted(df["section"].unique())
selected_sections = st.sidebar.multiselect("Contract Type", sections, default=sections,
                                            help="standard = listed options; flex = FLEX options")

min_date = df["report_date"].min().date()
max_date = df["report_date"].max().date()
date_range = st.sidebar.date_input("Date Range", value=(min_date, max_date),
                                   min_value=min_date, max_value=max_date)

st.sidebar.divider()
st.sidebar.header("Robinhood Market Share")
uploaded_rbhd = st.sidebar.file_uploader(
    "Upload Robinhood Excel files",
    type="xlsx",
    accept_multiple_files=True,
    help="Monthly Metrics or Quarterly Supplement .xlsx files from Robinhood investor relations",
)

if st.sidebar.button("Refresh data from OCC"):
    from scraper import PARSED_CACHE
    if PARSED_CACHE.exists():
        PARSED_CACHE.unlink()
    st.cache_data.clear()
    st.rerun()

# ── apply filters ─────────────────────────────────────────────────────────────

start, end = (date_range[0], date_range[1]) if len(date_range) == 2 else (min_date, max_date)

mask = (
    df["report_class"].isin(selected_classes)
    & df["section"].isin(selected_sections)
    & (df["report_date"].dt.date >= start)
    & (df["report_date"].dt.date <= end)
)
filtered = df[mask].copy()

if filtered.empty:
    st.warning("No data for current filter selection.")
    st.stop()

# ── aggregates ────────────────────────────────────────────────────────────────

# Per-week totals across all selected classes/sections
weekly = (
    filtered.groupby("report_date", as_index=False)
    .agg(
        total_contracts=("combined_total_contracts", "sum"),
        calls_contracts=("calls_total_contracts", "sum"),
        puts_contracts=("puts_total_contracts", "sum"),
        total_premiums=("prem_combined", "sum"),
        cust_contracts=("combined_cust_all_total_contracts", "sum"),
        firm_contracts=("combined_firm_all_total_contracts", "sum"),
        mm_contracts=("combined_m-m_all_total_contracts", "sum"),
    )
)
weekly["put_call_ratio"] = weekly["puts_contracts"] / weekly["calls_contracts"]

latest = weekly.iloc[-1]
prev = weekly.iloc[-2] if len(weekly) > 1 else latest

def delta_pct(new, old):
    if old and old != 0:
        return f"{(new - old) / old * 100:+.1f}% WoW"
    return ""

# ── QTD helpers ───────────────────────────────────────────────────────────────

_today = pd.Timestamp.today().normalize()
_q_start_month = (_today.month - 1) // 3 * 3 + 1
qtd_start = pd.Timestamp(_today.year, _q_start_month, 1)
qtd_label = f"Q{(_q_start_month - 1) // 3 + 1} {_today.year} QTD"
weekly_qtd = weekly[weekly["report_date"] >= qtd_start]

# ── KPI cards ─────────────────────────────────────────────────────────────────

k1, k2, k3, k4 = st.columns(4)
k1.metric("Total Contracts (latest week)",
          f"{latest['total_contracts']:,.0f}",
          delta_pct(latest["total_contracts"], prev["total_contracts"]))
k2.metric("Total Premiums",
          f"${latest['total_premiums'] / 1e9:.2f}B",
          delta_pct(latest["total_premiums"], prev["total_premiums"]))
k3.metric("Put / Call Ratio",
          f"{latest['put_call_ratio']:.3f}",
          delta_pct(latest["put_call_ratio"], prev["put_call_ratio"]))
k4.metric("Avg Weekly Contracts (period)",
          f"{weekly['total_contracts'].mean():,.0f}")

st.divider()

# ── QTD volume ────────────────────────────────────────────────────────────────

st.subheader(f"{qtd_label} — Weekly Contract Volume")
if weekly_qtd.empty:
    st.info("No data yet for the current quarter.")
else:
    st.caption(f"Total contracts so far this quarter: {weekly_qtd['total_contracts'].sum():,.0f}")
    fig_qtd = px.bar(weekly_qtd, x="report_date", y="total_contracts",
                     labels={"report_date": "Week Ending", "total_contracts": "Contracts"},
                     color_discrete_sequence=["#4C78A8"])
    fig_qtd.update_layout(margin=dict(t=10, b=0), hovermode="x unified")
    st.plotly_chart(fig_qtd, use_container_width=True)

st.divider()

# ── row 1: total volume + calls/puts ─────────────────────────────────────────

col1, col2 = st.columns(2)

with col1:
    st.subheader("Total Weekly Contract Volume")
    fig = px.bar(weekly, x="report_date", y="total_contracts",
                 labels={"report_date": "Week Ending", "total_contracts": "Contracts"},
                 color_discrete_sequence=["#4C78A8"])
    fig.update_layout(margin=dict(t=10, b=0), hovermode="x unified")
    st.plotly_chart(fig, use_container_width=True)

with col2:
    st.subheader("Calls vs Puts Volume")
    cp = weekly[["report_date", "calls_contracts", "puts_contracts"]].melt(
        "report_date", var_name="type", value_name="contracts"
    )
    cp["type"] = cp["type"].map({"calls_contracts": "Calls", "puts_contracts": "Puts"})
    fig2 = px.area(cp, x="report_date", y="contracts", color="type",
                   labels={"report_date": "Week Ending", "contracts": "Contracts", "type": ""},
                   color_discrete_map={"Calls": "#54A24B", "Puts": "#E45756"})
    fig2.update_layout(margin=dict(t=10, b=0), hovermode="x unified")
    st.plotly_chart(fig2, use_container_width=True)

# ── row 2: by class + put/call ratio ─────────────────────────────────────────

col3, col4 = st.columns(2)

with col3:
    st.subheader("Volume by Asset Class")
    by_class = (
        filtered.groupby(["report_date", "report_class"], as_index=False)
        ["combined_total_contracts"].sum()
    )
    by_class.columns = ["Week Ending", "Class", "Contracts"]
    fig3 = px.area(by_class, x="Week Ending", y="Contracts", color="Class",
                   color_discrete_map={"equity": "#4C78A8", "etf": "#F58518", "index": "#72B7B2"})
    fig3.update_layout(margin=dict(t=10, b=0), hovermode="x unified")
    st.plotly_chart(fig3, use_container_width=True)

with col4:
    st.subheader("Put / Call Ratio")
    fig4 = go.Figure()
    fig4.add_trace(go.Scatter(x=weekly["report_date"], y=weekly["put_call_ratio"],
                              mode="lines", line=dict(color="#B279A2", width=2),
                              name="P/C Ratio"))
    fig4.add_hline(y=1.0, line_dash="dash", line_color="gray", opacity=0.5,
                   annotation_text="1.0", annotation_position="left")
    fig4.update_layout(margin=dict(t=10, b=0), hovermode="x unified",
                       yaxis_title="Ratio", xaxis_title="Week Ending", showlegend=False)
    st.plotly_chart(fig4, use_container_width=True)

# ── row 3: asset class % mix ─────────────────────────────────────────────────

st.subheader("Volume Mix by Asset Class (%)")
by_class_pct = (
    filtered.groupby(["report_date", "report_class"], as_index=False)
    ["combined_total_contracts"].sum()
)
by_class_pct["pct"] = by_class_pct.groupby("report_date")["combined_total_contracts"].transform(
    lambda x: x / x.sum() * 100
)
by_class_pct.columns = ["Week Ending", "Class", "Contracts", "Pct"]
fig_pct = px.area(by_class_pct, x="Week Ending", y="Pct", color="Class",
                  groupnorm="percent",
                  labels={"Pct": "Share (%)", "Week Ending": "Week Ending"},
                  color_discrete_map={"equity": "#4C78A8", "etf": "#F58518", "index": "#72B7B2"})
fig_pct.update_layout(margin=dict(t=10, b=0), hovermode="x unified",
                      yaxis=dict(ticksuffix="%", range=[0, 100]))
st.plotly_chart(fig_pct, use_container_width=True)

# ── row 4: standard vs flex % mix ────────────────────────────────────────────

st.subheader("Volume Mix by Contract Type (%)")
by_section_pct = (
    filtered.groupby(["report_date", "section"], as_index=False)
    ["combined_total_contracts"].sum()
)
fig_sec_pct = px.area(by_section_pct, x="report_date", y="combined_total_contracts", color="section",
                      groupnorm="percent",
                      labels={"report_date": "Week Ending", "combined_total_contracts": "Share (%)"},
                      color_discrete_map={"standard": "#4C78A8", "flex": "#EECA3B"})
fig_sec_pct.update_layout(margin=dict(t=10, b=0), hovermode="x unified",
                          yaxis=dict(ticksuffix="%", range=[0, 100]))
st.plotly_chart(fig_sec_pct, use_container_width=True)

# ── row 6: account type breakdown ─────────────────────────────────────────────

st.subheader("Volume by Account Type (Customer / Firm / Market Maker)")
acct = weekly[["report_date", "cust_contracts", "firm_contracts", "mm_contracts"]].melt(
    "report_date", var_name="acct", value_name="contracts"
)
acct["acct"] = acct["acct"].map({
    "cust_contracts": "Customer",
    "firm_contracts": "Firm",
    "mm_contracts": "Market Maker",
})
fig5 = px.area(acct, x="report_date", y="contracts", color="acct",
               labels={"report_date": "Week Ending", "contracts": "Contracts", "acct": ""},
               color_discrete_map={"Customer": "#4C78A8", "Firm": "#F58518", "Market Maker": "#E45756"})
fig5.update_layout(margin=dict(t=10, b=0), hovermode="x unified")
st.plotly_chart(fig5, use_container_width=True)

# ── row 4: standard vs flex ───────────────────────────────────────────────────

if len(selected_sections) > 1:
    st.subheader("Standard vs FLEX Options Volume")
    by_section = (
        filtered.groupby(["report_date", "section"], as_index=False)
        ["combined_total_contracts"].sum()
    )
    by_section.columns = ["Week Ending", "Section", "Contracts"]
    fig6 = px.bar(by_section, x="Week Ending", y="Contracts", color="Section",
                  barmode="stack",
                  color_discrete_map={"standard": "#4C78A8", "flex": "#EECA3B"})
    fig6.update_layout(margin=dict(t=10, b=0), hovermode="x unified")
    st.plotly_chart(fig6, use_container_width=True)

# ── Robinhood market share ────────────────────────────────────────────────────

st.divider()
st.header("Robinhood Options Market Share")
st.caption("Denominator: OCC standard equity + ETF + index contracts × 2 (both sides of each trade)")

try:
    if uploaded_rbhd:
        rbhd_monthly = load_robinhood_from_uploads(uploaded_rbhd)
    else:
        rbhd_monthly = load_robinhood_monthly()

    # OCC monthly denominator — always standard across all three classes,
    # independent of sidebar class/section/date filters
    occ_std = df[df["section"] == "standard"].copy()
    occ_std["year_month"] = occ_std["report_date"].dt.to_period("M")
    occ_monthly = (
        occ_std.groupby("year_month", as_index=False)["combined_total_contracts"]
        .sum()
        .rename(columns={"combined_total_contracts": "occ_contracts"})
    )
    occ_monthly["ym_str"] = occ_monthly["year_month"].astype(str)
    rbhd_monthly["ym_str"] = rbhd_monthly["year_month"].astype(str)

    ms = rbhd_monthly.merge(occ_monthly[["ym_str", "occ_contracts"]], on="ym_str", how="inner")
    ms["market_share_pct"] = ms["rbhd_contracts"] / (ms["occ_contracts"] * 2) * 100
    ms["date"] = ms["year_month"].dt.to_timestamp()
    ms = ms.sort_values("date").reset_index(drop=True)

    if ms.empty:
        st.warning("No overlapping months between Robinhood data and OCC data in the cache.")
    else:
        latest = ms.iloc[-1]
        prev = ms.iloc[-2] if len(ms) > 1 else latest

        def _mom(new, old):
            if old and old != 0:
                return f"{(new - old) / old * 100:+.1f}% MoM"
            return ""

        ms1, ms2, ms3 = st.columns(3)
        ms1.metric(
            f"Market Share ({latest['year_month']})",
            f"{latest['market_share_pct']:.2f}%",
            _mom(latest["market_share_pct"], prev["market_share_pct"]),
        )
        ms2.metric(
            "Robinhood Contracts",
            f"{latest['rbhd_contracts'] / 1e6:.1f}M",
            _mom(latest["rbhd_contracts"], prev["rbhd_contracts"]),
        )
        ms3.metric(
            "OCC Denominator (×2)",
            f"{latest['occ_contracts'] * 2 / 1e9:.2f}B",
        )

        fig_ms = px.line(
            ms, x="date", y="market_share_pct",
            labels={"date": "Month", "market_share_pct": "Market Share (%)"},
            color_discrete_sequence=["#FF5700"],
        )
        fig_ms.update_traces(mode="lines+markers")
        fig_ms.update_layout(
            margin=dict(t=10, b=0),
            hovermode="x unified",
            yaxis=dict(ticksuffix="%", rangemode="tozero"),
        )

        # QTD market share — align monthly Robinhood data with weekly OCC data
        # Only use OCC weeks whose calendar month has Robinhood coverage
        qtd_period_start = pd.Period(qtd_start, freq="M")
        qtd_rbhd_qs = rbhd_monthly[rbhd_monthly["year_month"] >= qtd_period_start]
        if not qtd_rbhd_qs.empty:
            qtd_occ_qs = occ_std[occ_std["year_month"].isin(qtd_rbhd_qs["year_month"])]
            if not qtd_occ_qs.empty:
                qtd_rbhd_sum = qtd_rbhd_qs["rbhd_contracts"].sum()
                qtd_occ_sum = qtd_occ_qs["combined_total_contracts"].sum()
                qtd_ms_pct = qtd_rbhd_sum / (qtd_occ_sum * 2) * 100
                qtd_x = qtd_rbhd_qs["year_month"].max().to_timestamp()
                fig_ms.add_trace(go.Scatter(
                    x=[qtd_x], y=[qtd_ms_pct],
                    mode="markers+text",
                    marker=dict(color="#FF5700", size=14, symbol="star"),
                    text=[f"{qtd_label}: {qtd_ms_pct:.2f}%"],
                    textposition="top center",
                    name=qtd_label,
                ))

        st.plotly_chart(fig_ms, use_container_width=True)

        with st.expander("Monthly detail"):
            detail = ms[["year_month", "rbhd_contracts", "occ_contracts", "market_share_pct"]].copy()
            detail.columns = ["Month", "RH Contracts", "OCC Standard Contracts", "Market Share %"]
            detail["RH Contracts"] = detail["RH Contracts"].map("{:,.0f}".format)
            detail["OCC Standard Contracts"] = detail["OCC Standard Contracts"].map("{:,.0f}".format)
            detail["Market Share %"] = detail["Market Share %"].map("{:.2f}%".format)
            st.dataframe(detail, use_container_width=True, hide_index=True)

except FileNotFoundError:
    st.info("Upload Robinhood monthly or quarterly Excel files using the sidebar uploader to enable this section.")
except Exception as e:
    st.error(f"Could not load Robinhood data: {e}")

# ── raw data table ────────────────────────────────────────────────────────────

with st.expander("Raw data"):
    st.dataframe(filtered, use_container_width=True)
    csv = filtered.to_csv(index=False).encode()
    st.download_button("Download CSV", csv, "occ_weekly_volume.csv", "text/csv")

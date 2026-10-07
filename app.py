"""CarbonLedger dashboard: streamlit run app.py"""
from __future__ import annotations

import duckdb
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from carbonledger import appraisal, embodied, optimise, pipeline, warehouse
from carbonledger.assistant import Assistant
from carbonledger.governance import AuditLog, allowed_tables, guard_sql, scoped_connection, UnsafeQuery
from carbonledger.reference import GAS_EF, GRANT_CAP_GBP_PER_T, PRICES, UPFRONT_BENCHMARKS, grid_ef

st.set_page_config(page_title="CarbonLedger", layout="wide")

SAVES, COSTS, NEUTRAL = "#1baf7a", "#eb6834", "#2a78d6"
MODULE_COLOURS = {"A1-A3": "#2a78d6", "A4": "#eb6834", "A5": "#1baf7a", "C1-C4": "#eda100"}


@st.cache_resource
def ensure_built() -> bool:
    if not warehouse.DB_PATH.exists():
        pipeline.run()
    return True


ensure_built()
with duckdb.connect(str(warehouse.DB_PATH), read_only=True) as _c:
    CLIENTS = _c.execute("SELECT DISTINCT client_id, client_name FROM buildings ORDER BY 1").fetchall()

# ---- role ------------------------------------------------------------------
st.sidebar.title("CarbonLedger")
st.sidebar.caption("Carbon + cost decision platform for built-environment decarbonisation. All data synthetic.")
labels = {"Consultant (analyst)": ("analyst", None)}
labels.update({f"Client: {name}": ("client", cid) for cid, name in CLIENTS})
labels["Public benchmarks"] = ("public", None)
choice = st.sidebar.radio("Signed in as", list(labels))
role, client_id = labels[choice]
actor = f"{role}:{client_id}" if client_id else role
con = scoped_connection(warehouse.DB_PATH, role, client_id)
tables = allowed_tables(role)
audit = AuditLog()
st.sidebar.markdown(f"**Tables visible:** {', '.join(sorted(tables))}")
st.sidebar.caption("Clients see only their own buildings and never the consultancy's unit rates. "
                   "The scope is enforced by what the session's database contains.")


def money(x: float) -> str:
    return f"£{x / 1e6:,.2f}m" if abs(x) >= 1e6 else f"£{x:,.0f}"


tab_names = ["Portfolio", "Abatement curve", "Investment plan", "Design options", "Data & governance", "Ask the data"]
tabs = st.tabs(tab_names)

# ---- public role -------------------------------------------------------------
if role == "public":
    with tabs[0]:
        st.subheader("Energy intensity benchmarks by building type")
        st.dataframe(con.execute("SELECT * FROM benchmarks ORDER BY 1").df(), hide_index=True)
        st.info("Public users see anonymised benchmarks only. Sign in as a client or consultant for appraisals.")
    for t in tabs[1:4]:
        with t:
            st.info("Not available to the public role.")
else:
    b = con.execute("SELECT * FROM buildings").df()
    a = con.execute("SELECT * FROM appraisal").df()
    an = con.execute("SELECT * FROM anomalies WHERE flag <> 'ok'").df()

    # ---- Portfolio -------------------------------------------------------
    with tabs[0]:
        base_t = (b.gas_kwh * GAS_EF + b.elec_kwh * grid_ef(2026)).sum() / 1000
        base_cost = (b.gas_kwh * PRICES["gas"] + b.elec_kwh * PRICES["electricity"]).sum()
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Buildings", f"{len(b)}  ·  {b.gia_m2.sum() / 1000:,.0f}k m²")
        c2.metric("Operational carbon, 2026", f"{base_t:,.0f} tCO2e/yr")
        c3.metric("Energy spend", f"{money(base_cost)}/yr")
        c4.metric("Energy anomalies to review", f"{len(an)}")
        per = a.groupby("building_id", as_index=False).agg(all_measures_capex=("capex_gbp", "sum"),
                                                           whole_life_tco2e=("net_tco2e", "sum"))
        view = b.merge(per, on="building_id")
        view["gas_kwh_m2"] = (view.gas_kwh / view.gia_m2).round()
        view["elec_kwh_m2"] = (view.elec_kwh / view.gia_m2).round()
        st.subheader("Estate")
        st.dataframe(view[["building_id", "name", "building_type", "gia_m2", "year_built", "gas_kwh_m2", "elec_kwh_m2",
                           "all_measures_capex", "whole_life_tco2e"]].round(0), hide_index=True,
                     column_config={"all_measures_capex": st.column_config.NumberColumn("Capex, all measures (£)", format="%,.0f"),
                                    "whole_life_tco2e": st.column_config.NumberColumn("Net whole-life tCO2e", format="%,.0f")})
        if len(an):
            st.subheader("Flagged for review")
            st.caption("Robust z-score of log energy intensity against peers of the same type (|z| > 3). "
                       "Could be a metering fault or a real waste opportunity: a person decides.")
            st.dataframe(an[["building_id", "metric", "value_kwh_m2", "peer_median", "robust_z"]], hide_index=True)

    # ---- MACC ------------------------------------------------------------
    with tabs[1]:
        st.subheader("Marginal abatement cost curve")
        st.caption("Bar width = net whole-life tCO2e saved (operational minus embodied). Height = lifetime cost per "
                   "tonne after energy savings, discounted at 3.5%. Below zero, the measure pays for itself.")
        m = appraisal.macc(a)
        fig = go.Figure(go.Bar(
            x=(m.x_start + m.x_end) / 2, y=m.abatement_gbp_per_t, width=m.net_tco2e * 0.985,
            marker_color=[SAVES if v < 0 else COSTS for v in m.abatement_gbp_per_t],
            text=m.measure, textposition="outside",
            customdata=np.stack([m.measure_name, m.net_tco2e, m.capex_gbp, m.buildings], axis=1),
            hovertemplate="<b>%{customdata[0]}</b><br>%{y:,.0f} £/tCO2e<br>%{customdata[1]:,.0f} tCO2e net<br>"
                          "capex £%{customdata[2]:,.0f}<br>%{customdata[3]} buildings<extra></extra>"))
        fig.update_layout(height=430, margin=dict(t=20, b=40), xaxis_title="Cumulative net tCO2e (whole life)",
                          yaxis_title="£ per tCO2e", showlegend=False, plot_bgcolor="rgba(0,0,0,0)")
        fig.update_yaxes(gridcolor="rgba(128,128,128,0.15)", zerolinecolor="rgba(128,128,128,0.6)")
        st.plotly_chart(fig, width="stretch")
        st.dataframe(m[["measure_name", "buildings", "capex_gbp", "net_tco2e", "abatement_gbp_per_t"]].round(0),
                     hide_index=True)
        st.caption("Why PV looks so cheap per tonne but small: it saves a lot of money, but the grid it displaces is "
                   "decarbonising, so its lifetime carbon is modest. Heat pumps are the opposite: they raise running "
                   "cost at current prices, yet they are the largest source of carbon reduction.")

    # ---- Optimiser -------------------------------------------------------
    with tabs[2]:
        st.subheader("Where should the next pound go?")
        pkgs = appraisal.packages(a)
        full = float(pkgs.groupby("building_id").capex_gbp.max().sum())
        c1, c2, c3 = st.columns([2, 1, 1])
        budget = 1e6 * c1.slider("Capital budget (£m)", 0.0, float(np.ceil(full / 1e5) / 10),
                                 float(min(full, 3e6) // 1e5 / 10), step=0.1, format="£%.1fm")
        grant_only = c2.checkbox(f"Grant-eligible only (≤ £{GRANT_CAP_GBP_PER_T:.0f}/t)")
        npv_floor = c3.checkbox("Must not lose money (NPV ≥ 0)")
        plan = optimise.optimise(pkgs, budget, grant_only=grant_only, min_npv=0.0 if npv_floor else None)
        audit.record(actor, "plan_investment", {"budget_gbp": budget, "grant_only": grant_only, "npv_floor": npv_floor})
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Capex committed", money(plan.capex_gbp))
        k2.metric("Net whole-life saving", f"{plan.net_tco2e:,.0f} tCO2e")
        k3.metric("First-year saving", f"{plan.year1_tco2e:,.0f} tCO2e")
        k4.metric("Portfolio NPV", money(plan.npv_gbp))
        st.caption("Exact MILP (multiple-choice knapsack): one fabric-first package per building, maximising "
                   "net whole-life tCO2e within the budget.")
        budgets = np.concatenate([[0.0], np.geomspace(5e4, full, 16)])
        front = [optimise.optimise(pkgs, x, grant_only=grant_only, min_npv=0.0 if npv_floor else None) for x in budgets]
        ff = pd.DataFrame({"budget": budgets, "t": [p.net_tco2e for p in front], "npv": [p.npv_gbp for p in front]})
        fig = go.Figure(go.Scatter(x=ff.budget, y=ff.t, mode="lines+markers", line=dict(color=NEUTRAL, width=2),
                                   marker=dict(size=8), customdata=ff.npv,
                                   hovertemplate="Budget £%{x:,.0f}<br>%{y:,.0f} tCO2e<br>NPV £%{customdata:,.0f}<extra></extra>"))
        fig.add_vline(x=budget, line_dash="dot", line_color="rgba(128,128,128,0.8)")
        fig.update_layout(height=320, margin=dict(t=20, b=40), xaxis_title="Budget (£)",
                          yaxis_title="Net whole-life tCO2e saved", plot_bgcolor="rgba(0,0,0,0)")
        fig.update_yaxes(gridcolor="rgba(128,128,128,0.15)")
        st.plotly_chart(fig, width="stretch")
        st.caption("Diminishing returns: the first pound buys the cheapest tonnes.")
        st.dataframe(plan.chosen[["building_id", "measures", "capex_gbp", "net_tco2e", "npv_gbp", "grant_gbp_per_t"]].round(0),
                     hide_index=True)

    # ---- Embodied --------------------------------------------------------
    with tabs[3]:
        st.subheader("Design options: upfront carbon against cost")
        bench = st.selectbox("Benchmark", list(UPFRONT_BENCHMARKS), index=1)
        d = embodied.compare(benchmark=bench)
        st.caption(f"1,200 m² three-storey extension; frame, upper floors, substructure and roof only. "
                   f"Biogenic carbon excluded. {bench} whole-building upfront budget: "
                   f"{UPFRONT_BENCHMARKS[bench]:.0f} kgCO2e/m², so the frame should use well under it.")
        fig = go.Figure()
        for mod, colour in MODULE_COLOURS.items():
            fig.add_trace(go.Bar(name=mod, y=d.option, x=d[mod] / embodied.GIA_M2, orientation="h", marker_color=colour,
                                 marker_line=dict(color="white", width=2),
                                 hovertemplate=f"{mod}: %{{x:,.0f}} kgCO2e/m²<extra></extra>"))
        fig.update_layout(barmode="stack", height=280, margin=dict(t=20, b=40), xaxis_title="kgCO2e per m² GIA",
                          legend=dict(orientation="h", y=1.15, traceorder="normal"), plot_bgcolor="rgba(0,0,0,0)")
        fig.update_yaxes(autorange="reversed")
        st.plotly_chart(fig, width="stretch")
        show = d[["option", "upfront_kg_m2", "cost_gbp_m2", "carbon_vs_A_pct", "cost_vs_A_pct", "gbp_per_t_avoided"]].copy()
        show["share_of_benchmark_pct"] = 100 * show.upfront_kg_m2 / UPFRONT_BENCHMARKS[bench]
        st.dataframe(show.round(1), hide_index=True)
        st.caption("gbp_per_t_avoided puts a price on choosing a lower-carbon option, so it can be compared with the "
                   "abatement curve: is a tonne cheaper to save in the frame or in the client's existing estate?")

# ---- Governance --------------------------------------------------------------
with tabs[4]:
    st.subheader("What this session can see")
    for t in sorted(tables):
        cols = [r[0] for r in con.execute(f"DESCRIBE {t}").fetchall()]
        st.markdown(f"**{t}**: {', '.join(cols)}")
    st.subheader("Try a query")
    q = st.text_input("SQL (read-only, guarded)", "SELECT * FROM measures")
    try:
        st.dataframe(con.execute(guard_sql(q, tables)).df(), hide_index=True)
    except (UnsafeQuery, duckdb.Error) as e:
        audit.record(actor, "sql_blocked", {"sql": q, "reason": str(e)})
        st.error(f"Blocked: {e}")
    if role == "analyst":
        with duckdb.connect(str(warehouse.DB_PATH), read_only=True) as w:
            st.subheader("Data provenance and classification")
            st.dataframe(w.execute("SELECT * FROM provenance").df(), hide_index=True)
            st.subheader("Quarantined at ingestion")
            st.dataframe(w.execute("SELECT building_id, client_id, gia_m2, gas_kwh, reason FROM quarantine").df(),
                         hide_index=True)
            st.subheader("Lineage")
            st.dataframe(w.execute("SELECT * FROM lineage").df(), hide_index=True)
        st.subheader("Audit log (latest)")
        st.dataframe(pd.DataFrame(audit.tail(15)[::-1]), hide_index=True)

# ---- Assistant ---------------------------------------------------------------
with tabs[5]:
    assistant = Assistant(con, role, actor, audit)
    st.subheader("Ask the data")
    st.caption("Claude with two tools: guarded SQL over this session's data and the investment optimiser. "
               + ("Live mode." if assistant.online else "No API key set: offline keyword mode."))
    examples = ["Which measures are the cheapest way to cut carbon?", "Plan a £2m budget",
                "Are there any meter anomalies?", "Which heat pumps have the best case?"]
    pick = st.selectbox("Example", [""] + examples)
    question = st.text_input("Question", pick)
    if question:
        ans = assistant.ask(question)
        st.markdown(ans.text)
        with st.expander(f"Tool calls ({len(ans.steps)})"):
            for s in ans.steps:
                st.code(f"{s.tool}({s.input})\n-> {s.output[:800]}")

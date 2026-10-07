"""Carbon-cost appraisal engine.

Measures are applied to each building in a fixed fabric-first order, and each
one is appraised against the state the previous measures left behind. That
captures the interactions that matter in practice: insulation shrinks the heat
pump, LEDs shrink what PV can self-consume, a heat pump adds electricity load.

For each step we report, over the measure's life:
  capex, annual £ saving, NPV (Green Book discounting),
  operational tCO2e saved (year-by-year grid factor),
  embodied tCO2e (A1-A5), net whole-life tCO2e,
  lifetime abatement cost £/tCO2e  = -NPV / net tCO2e  (negative = pays for itself),
  grant metric £capex per lifetime tCO2e (PSDS/Salix-style screen).

Cumulative "packages" (level k = first k measures) feed the optimiser.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .reference import (BASE_YEAR, DISCOUNT_RATE, GAS_EF, GRANT_CAP_GBP_PER_T, LIGHTING_SHARE, MEASURES,
                        PRICES, SELF_CONSUMPTION, grid_ef)

SCOP = 2.8
BOILER_EFF = 0.85
HEAT_FULL_LOAD_HOURS = 1_800
PV_YIELD = 850                  # kWh/kWp/yr, North East England
PV_KWP_PER_ROOF_M2 = 0.10       # ~50% usable roof at ~0.2 kWp/m2 panel


@dataclass
class State:
    gas: float
    elec_import: float
    lighting: float
    pv_kwp: float = 0.0
    export: float = 0.0


def _apply(code: str, b: pd.Series, s: State) -> tuple[State, float]:
    """Return the new state and the quantity (in the measure's unit) installed."""
    gia = b.gia_m2
    if code == "BMS":
        light = s.lighting * 0.94
        return State(s.gas * 0.90, s.elec_import - (s.lighting - light) - (s.elec_import - s.lighting) * 0.06,
                     light, s.pv_kwp, s.export), gia
    if code == "LED":
        saved = s.lighting * 0.55
        return State(s.gas, s.elec_import - saved, s.lighting - saved, s.pv_kwp, s.export), gia
    if code == "INS":
        return State(s.gas * 0.82, s.elec_import, s.lighting, s.pv_kwp, s.export), gia
    if code == "GLZ":
        return State(s.gas * 0.90, s.elec_import, s.lighting, s.pv_kwp, s.export), gia
    if code == "PV":
        kwp = min(b.roof_m2 * PV_KWP_PER_ROOF_M2, s.elec_import / PV_YIELD)
        gen = kwp * PV_YIELD
        sc = gen * SELF_CONSUMPTION[b.building_type]
        return State(s.gas, s.elec_import - sc, s.lighting, kwp, gen - sc), kwp
    if code == "ASHP":
        heat = s.gas * 0.90 * BOILER_EFF
        kw = heat / HEAT_FULL_LOAD_HOURS
        return State(s.gas * 0.10, s.elec_import + heat / SCOP, s.lighting, s.pv_kwp, s.export), kw
    raise KeyError(code)


def _finance(code: str, qty: float, before: State, after: State) -> dict:
    m = MEASURES[code]
    capex = m.rate_gbp * qty + m.fixed_gbp if qty > 0 else 0.0
    d_gas = before.gas - after.gas
    d_elec = before.elec_import - after.elec_import
    d_export = after.export - before.export
    saving = d_gas * PRICES["gas"] + d_elec * PRICES["electricity"] + d_export * PRICES["export"]
    maint = capex * m.maint_pct
    npv = -capex
    op_kg = 0.0
    for t in range(m.life_years):
        npv += (saving - maint) / (1 + DISCOUNT_RATE) ** (t + 1)
        op_kg += d_gas * GAS_EF + d_elec * grid_ef(BASE_YEAR + t)
    emb_kg = m.embodied_kg_per_unit * qty
    net_t = (op_kg - emb_kg) / 1000
    return {
        "capex_gbp": capex,
        "annual_saving_gbp": saving - maint,
        "npv_gbp": npv,
        "op_tco2e": op_kg / 1000,
        "embodied_tco2e": emb_kg / 1000,
        "net_tco2e": net_t,
        "year1_tco2e": (d_gas * GAS_EF + d_elec * grid_ef(BASE_YEAR)) / 1000,
        "abatement_gbp_per_t": (-npv / net_t) if net_t > 0 else float("nan"),
        "grant_gbp_per_t": (capex / net_t) if net_t > 0 else float("nan"),
        "payback_years": capex / (saving - maint) if saving - maint > 0 else float("nan"),
    }


def appraise_building(b: pd.Series) -> list[dict]:
    s = State(b.gas_kwh, b.elec_kwh, b.elec_kwh * LIGHTING_SHARE[b.building_type])
    rows = []
    for m in sorted(MEASURES.values(), key=lambda m: m.order):
        new, qty = _apply(m.code, b, s)
        rows.append({"building_id": b.building_id, "client_id": b.client_id, "measure": m.code,
                     "measure_name": m.name, "order": m.order, "quantity": qty, "unit": m.unit,
                     "life_years": m.life_years, **_finance(m.code, qty, s, new)})
        s = new
    return rows


def appraise(estate: pd.DataFrame) -> pd.DataFrame:
    rows = [r for _, b in estate.iterrows() for r in appraise_building(b)]
    df = pd.DataFrame(rows)
    df["grant_eligible"] = df.grant_gbp_per_t <= GRANT_CAP_GBP_PER_T
    return df


def packages(appraisal: pd.DataFrame) -> pd.DataFrame:
    """Cumulative packages per building: level k installs measures 1..k."""
    a = appraisal.sort_values(["building_id", "order"]).copy()
    g = a.groupby("building_id")
    out = a[["building_id", "client_id", "order"]].rename(columns={"order": "level"}).copy()
    for col in ("capex_gbp", "npv_gbp", "net_tco2e", "year1_tco2e", "annual_saving_gbp"):
        out[col] = g[col].cumsum()
    out["measures"] = g["measure"].transform(lambda s: ["+".join(s.iloc[: i + 1]) for i in range(len(s))])
    out["grant_gbp_per_t"] = out.capex_gbp / out.net_tco2e.where(out.net_tco2e > 0)
    return out.reset_index(drop=True)


def macc(appraisal: pd.DataFrame) -> pd.DataFrame:
    """Portfolio marginal abatement cost curve, one bar per measure type."""
    pos = appraisal[appraisal.net_tco2e > 0]
    g = pos.groupby(["measure", "measure_name"], as_index=False).agg(
        capex_gbp=("capex_gbp", "sum"), npv_gbp=("npv_gbp", "sum"), net_tco2e=("net_tco2e", "sum"),
        buildings=("building_id", "nunique"))
    g["abatement_gbp_per_t"] = -g.npv_gbp / g.net_tco2e
    g = g.sort_values("abatement_gbp_per_t").reset_index(drop=True)
    g["x_end"] = g.net_tco2e.cumsum()
    g["x_start"] = g.x_end - g.net_tco2e
    return g

"""Whole-life embodied carbon and cost for design options (RICS WLCA modules).

Takes an element-based bill of quantities per option and returns kgCO2e by
module, cost, and intensity per m2 GIA against a chosen upfront benchmark:

  A1-A3  product stage           quantity x material factor
  A4     transport to site       mass x distance x HGV factor
  A5     construction            wastage share of A1-A4 + RICS default site emissions per £100k
  C1-C4  end of life             transport to processing + processing/disposal

Biogenic carbon (timber sequestration) is excluded from the totals, which is
the conservative convention for upfront comparisons.
"""
from __future__ import annotations

import pandas as pd

from .reference import (A5_SITE_KG_PER_100K, EOL_KG_PER_KG, EOL_TRANSPORT_KM, MATERIALS, TRANSPORT_EF_PER_TKM,
                        UPFRONT_BENCHMARKS, WASTE_RATE)

GIA_M2 = 1_200  # scheme: three-storey teaching/office extension

# Frame, upper floors, substructure and roof only. Quantities are indicative.
OPTIONS: dict[str, list[tuple[str, float, float]]] = {
    # (material, quantity, transport km)
    "A. Steel frame + composite deck": [
        ("steel_sections", 52_000, 180), ("metal_deck", 800, 180), ("concrete_c32_cem1", 210, 25),
        ("rebar", 14_000, 60), ("mineral_wool", 90, 120),
    ],
    "B. RC frame, 50% GGBS": [
        ("concrete_c32_ggbs50", 560, 25), ("rebar", 58_000, 60), ("mineral_wool", 90, 120),
    ],
    "C. Glulam + CLT hybrid": [
        ("glulam", 95, 1_400), ("clt", 260, 1_400), ("concrete_c32_ggbs50", 150, 25),
        ("rebar", 9_000, 60), ("steel_sections", 6_000, 180), ("mineral_wool", 90, 120),
    ],
}


def assess_option(lines: list[tuple[str, float, float]], gia_m2: float = GIA_M2) -> dict:
    a13 = a4 = c = cost = 0.0
    for code, qty, km in lines:
        m = MATERIALS[code]
        mass_t = qty * m["density"] / 1000
        a13 += qty * m["ef"]
        a4 += mass_t * km * TRANSPORT_EF_PER_TKM
        c += mass_t * EOL_TRANSPORT_KM * TRANSPORT_EF_PER_TKM + mass_t * 1000 * EOL_KG_PER_KG
        cost += qty * m["rate_gbp"]
    a5 = WASTE_RATE * (a13 + a4) + A5_SITE_KG_PER_100K * cost / 100_000
    upfront = a13 + a4 + a5
    return {"A1-A3": a13, "A4": a4, "A5": a5, "C1-C4": c, "upfront_kg": upfront, "wlc_kg": upfront + c,
            "cost_gbp": cost, "upfront_kg_m2": upfront / gia_m2, "cost_gbp_m2": cost / gia_m2}


def compare(options: dict | None = None, benchmark: str = "LETI 2030 (office)") -> pd.DataFrame:
    options = options or OPTIONS
    df = pd.DataFrame([{"option": k, **assess_option(v)} for k, v in options.items()])
    df["benchmark_kg_m2"] = UPFRONT_BENCHMARKS[benchmark]
    base = df.iloc[0]
    df["carbon_vs_A_pct"] = 100 * (df.upfront_kg / base.upfront_kg - 1)
    df["cost_vs_A_pct"] = 100 * (df.cost_gbp / base.cost_gbp - 1)
    # Shadow price: extra £ per tonne of upfront carbon avoided relative to option A.
    d_t = (base.upfront_kg - df.upfront_kg) / 1000
    df["gbp_per_t_avoided"] = (df.cost_gbp - base.cost_gbp) / d_t.where(d_t > 0)
    return df

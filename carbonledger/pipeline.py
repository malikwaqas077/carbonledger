"""Build the warehouse from the synthetic estate and export headline results."""
from __future__ import annotations

import json

import duckdb

from . import appraisal, embodied, estate, optimise, warehouse


def run() -> dict:
    stats = warehouse.build(estate.generate())
    con = duckdb.connect(str(warehouse.DB_PATH), read_only=True)
    a = con.execute("SELECT * FROM appraisal").df()
    con.close()
    pkgs = appraisal.packages(a)
    plans = {}
    for budget in (1_000_000, 3_000_000, 6_000_000):
        p = optimise.optimise(pkgs, budget)
        plans[f"{budget // 1_000_000}m"] = {"capex_gbp": round(p.capex_gbp), "net_tco2e": round(p.net_tco2e),
                                            "npv_gbp": round(p.npv_gbp), "buildings": len(p.chosen)}
    m = appraisal.macc(a)
    results = {
        **stats,
        "all_measures": {"capex_gbp": round(a.capex_gbp.sum()), "net_tco2e": round(a.net_tco2e.sum())},
        "plans": plans,
        "macc": m[["measure", "net_tco2e", "abatement_gbp_per_t"]].round(0).to_dict("records"),
        "design_options": embodied.compare()[["option", "upfront_kg_m2", "cost_gbp_m2"]].round(1).to_dict("records"),
    }
    out = warehouse.ROOT / "docs" / "results.json"
    out.write_text(json.dumps(results, indent=2))
    return results


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))

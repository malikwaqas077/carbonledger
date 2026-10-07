"""Ingestion, validation and the central DuckDB warehouse.

Records that fail validation are quarantined with a reason rather than
silently fixed, so the client can correct the source. Each run stamps a
lineage record (input row count, rejects, reference-data version).
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd

from . import analytics, appraisal
from .reference import MEASURES, PROVENANCE

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "carbonledger.duckdb"
REFERENCE_VERSION = "2026.10-demo"

RULES = [
    ("gia_m2 missing or non-positive", lambda d: ~(d.gia_m2 > 0)),
    ("negative meter reading", lambda d: (d.gas_kwh < 0) | (d.elec_kwh < 0)),
    ("duplicate building_id", lambda d: d.building_id.duplicated(keep="first")),
    ("roof area exceeds floor area x2", lambda d: d.roof_m2 > 2 * d.gia_m2),
]


def validate(raw: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    reasons = pd.Series("", index=raw.index)
    for label, rule in RULES:
        hit = rule(raw).fillna(True)
        reasons[hit] = reasons[hit].where(reasons[hit] == "", reasons[hit] + "; ") + label
    bad = reasons != ""
    quarantine = raw[bad].assign(reason=reasons[bad])
    return raw[~bad].reset_index(drop=True), quarantine.reset_index(drop=True)


SCHEMA_DOC = """Tables (DuckDB):
buildings(building_id, client_id, client_name, building_type, name, gia_m2, roof_m2, year_built, gas_kwh, elec_kwh)
appraisal(building_id, client_id, measure, measure_name, order, quantity, unit, life_years, capex_gbp,
          annual_saving_gbp, npv_gbp, op_tco2e, embodied_tco2e, net_tco2e, year1_tco2e,
          abatement_gbp_per_t, grant_gbp_per_t, grant_eligible, payback_years)
  -- one row per building x measure, applied fabric-first in 'order'; net_tco2e = whole-life operational
  -- minus embodied; abatement_gbp_per_t < 0 means the measure saves money over its life.
measures(code, name, life_years, note [, rate_gbp, fixed_gbp, embodied_kg_per_unit for analysts])
anomalies(building_id, client_id, building_type, metric, value_kwh_m2, peer_median, robust_z, flag)
benchmarks(building_type, buildings, gas_kwh_m2, elec_kwh_m2)   -- public role only
Not every role can see every table or column."""


def build(raw: pd.DataFrame, db_path: Path | str = DB_PATH) -> dict:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    clean, quarantine = validate(raw)
    appr = appraisal.appraise(clean)
    anomalies = analytics.energy_anomalies(clean)
    measures = pd.DataFrame([{**asdict(m), "embodied_kg_per_unit": m.embodied_kg_per_unit} for m in MEASURES.values()])
    provenance = pd.DataFrame([{"field": k, "classification": v[0], "source": v[1]} for k, v in PROVENANCE.items()])
    lineage = pd.DataFrame([{"run_at": datetime.now().isoformat(timespec="seconds"), "rows_in": len(raw),
                             "rows_loaded": len(clean), "rows_quarantined": len(quarantine),
                             "reference_version": REFERENCE_VERSION}])
    if db_path.exists():
        db_path.unlink()
    con = duckdb.connect(str(db_path))
    for name, df in {"buildings": clean, "appraisal": appr, "measures": measures, "anomalies": anomalies,
                     "quarantine": quarantine, "provenance": provenance, "lineage": lineage}.items():
        con.register("_df", df)
        con.execute(f"CREATE TABLE {name} AS SELECT * FROM _df")
        con.unregister("_df")
    con.close()
    return {"loaded": len(clean), "quarantined": len(quarantine), "appraisal_rows": len(appr),
            "anomalies": int((anomalies.flag != "ok").sum())}

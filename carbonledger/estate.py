"""Synthetic client estates (four fictional clients, 40 buildings).

Energy intensities are drawn around typical UK non-domestic benchmarks with
log-normal spread. A few deliberate problems are planted so the data-quality
and anomaly layers have something real to catch:

  - two records fail validation (negative meter reading, missing floor area)
  - three buildings have implausibly high electricity use (metering fault or
    a genuine waste opportunity, which the analytics layer flags for review)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

CLIENTS = [
    ("C01", "Northgate Academy Trust", "school", 12, (900, 6_000)),
    ("C02", "Wearside Primary Care Partnership", "health_centre", 10, (500, 2_500)),
    ("C03", "Quayside Commercial Estates", "office", 9, (1_200, 9_000)),
    ("C04", "Coastline University", "university", 9, (2_000, 14_000)),
]

# Median energy use intensity, kWh/m2/yr (gas, electricity).
EUI = {"school": (110, 45), "health_centre": (150, 90), "office": (90, 110), "university": (160, 140)}

TOWNS = ["Sunderland", "Newcastle", "Durham", "Gateshead", "South Shields", "Washington", "Hartlepool", "Middlesbrough"]


def generate(seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for client_id, client, btype, n, (lo, hi) in CLIENTS:
        for i in range(n):
            gia = float(round(np.exp(rng.uniform(np.log(lo), np.log(hi))), -1))
            g, e = EUI[btype]
            rows.append({
                "building_id": f"{client_id}-B{i + 1:02d}",
                "client_id": client_id,
                "client_name": client,
                "building_type": btype,
                "name": f"{rng.choice(TOWNS)} {btype.replace('_', ' ').title()} {i + 1}",
                "gia_m2": gia,
                "roof_m2": round(gia / rng.uniform(1.4, 3.0), -1),
                "year_built": int(rng.integers(1955, 2012)),
                "gas_kwh": round(gia * g * float(np.exp(rng.normal(0, 0.22))), -2),
                "elec_kwh": round(gia * e * float(np.exp(rng.normal(0, 0.22))), -2),
            })
    df = pd.DataFrame(rows)
    # Planted anomalies: electricity far above peers.
    for bid in ("C01-B04", "C03-B02", "C04-B06"):
        df.loc[df.building_id == bid, "elec_kwh"] *= 2.6
    # Planted validation failures.
    df.loc[df.building_id == "C02-B07", "gas_kwh"] = -48_200.0
    df.loc[df.building_id == "C03-B08", "gia_m2"] = np.nan
    return df

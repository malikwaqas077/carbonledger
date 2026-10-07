"""Analytics on client energy data.

Peer benchmarking with a robust z-score: each building's energy intensity
(kWh/m2) is compared with the median of its building type on a log scale,
scaled by the median absolute deviation, so a few extreme sites cannot hide
themselves by inflating the spread. Flags go to a human to decide whether
it is a metering fault or a genuine waste opportunity.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

THRESHOLD = 3.0


def energy_anomalies(estate: pd.DataFrame, threshold: float = THRESHOLD) -> pd.DataFrame:
    out = []
    for metric, col in (("gas", "gas_kwh"), ("electricity", "elec_kwh")):
        eui = estate[col] / estate.gia_m2
        log = np.log(eui.clip(lower=1e-6))
        for btype, idx in estate.groupby("building_type").groups.items():
            x = log.loc[idx]
            med = x.median()
            mad = 1.4826 * (x - med).abs().median()
            z = (x - med) / mad if mad > 0 else x * 0
            for i in idx:
                out.append({"building_id": estate.at[i, "building_id"], "client_id": estate.at[i, "client_id"],
                            "building_type": btype, "metric": metric, "value_kwh_m2": round(float(eui[i]), 1),
                            "peer_median": round(float(np.exp(med)), 1), "robust_z": round(float(z[i]), 2),
                            "flag": "high" if z[i] > threshold else ("low" if z[i] < -threshold else "ok")})
    return pd.DataFrame(out)

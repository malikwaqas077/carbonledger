"""Portfolio investment planner.

Chooses one package level per building (0 = do nothing) to maximise net
whole-life tCO2e saved within a capital budget. It is a multiple-choice
knapsack solved exactly as a MILP with scipy (HiGHS).

Options:
  grant_only    keep only packages under the grant cost-effectiveness cap
  min_npv       optional floor on portfolio NPV (e.g. 0 = must not lose money overall)
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.optimize import Bounds, LinearConstraint, milp

from .reference import GRANT_CAP_GBP_PER_T


@dataclass
class Plan:
    chosen: pd.DataFrame
    capex_gbp: float
    net_tco2e: float
    npv_gbp: float
    year1_tco2e: float
    status: str


def optimise(pkgs: pd.DataFrame, budget_gbp: float, grant_only: bool = False,
             min_npv: float | None = None) -> Plan:
    opts = pkgs[pkgs.net_tco2e > 0].copy()
    if grant_only:
        opts = opts[opts.grant_gbp_per_t <= GRANT_CAP_GBP_PER_T]
    opts = opts.reset_index(drop=True)
    if opts.empty:
        return Plan(opts, 0.0, 0.0, 0.0, 0.0, "no eligible options")

    n = len(opts)
    buildings = opts.building_id.unique()
    pick_one = np.zeros((len(buildings), n))
    for i, bid in enumerate(buildings):
        pick_one[i, (opts.building_id == bid).to_numpy()] = 1
    rows = [pick_one, opts.capex_gbp.to_numpy()[None, :]]
    lo = [np.zeros(len(buildings)), [-np.inf]]
    hi = [np.ones(len(buildings)), [budget_gbp]]
    if min_npv is not None:
        rows.append(opts.npv_gbp.to_numpy()[None, :])
        lo.append([min_npv])
        hi.append([np.inf])
    cons = LinearConstraint(np.vstack(rows), np.concatenate(lo), np.concatenate(hi))
    res = milp(-opts.net_tco2e.to_numpy(), constraints=cons, integrality=np.ones(n),
               bounds=Bounds(0, 1), options={"time_limit": 20})
    if res.x is None:
        return Plan(opts.iloc[0:0], 0.0, 0.0, 0.0, 0.0, res.message)
    chosen = opts[res.x > 0.5].sort_values("building_id").reset_index(drop=True)
    return Plan(chosen, chosen.capex_gbp.sum(), chosen.net_tco2e.sum(), chosen.npv_gbp.sum(),
                chosen.year1_tco2e.sum(), "optimal" if res.status == 0 else res.message)

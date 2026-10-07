import json

import duckdb
import pandas as pd
import pytest

from carbonledger import analytics, appraisal, embodied, estate, optimise, warehouse
from carbonledger.assistant import Assistant
from carbonledger.governance import AuditLog, UnsafeQuery, guard_sql, redact, scoped_connection
from carbonledger.reference import BASE_YEAR, GRANT_CAP_GBP_PER_T, MEASURES, grid_ef


@pytest.fixture(scope="session")
def db(tmp_path_factory):
    path = tmp_path_factory.mktemp("wh") / "test.duckdb"
    warehouse.build(estate.generate(), path)
    return path


@pytest.fixture(scope="session")
def appr(db):
    with duckdb.connect(str(db), read_only=True) as c:
        return c.execute("SELECT * FROM appraisal").df()


@pytest.fixture
def audit(tmp_path):
    return AuditLog(tmp_path / "audit.jsonl")


# ---- reference / validation ---------------------------------------------------
def test_grid_trajectory_monotonic():
    vals = [grid_ef(y) for y in range(BASE_YEAR, 2050)]
    assert vals[0] == pytest.approx(0.177) and vals[-1] == pytest.approx(0.030)
    assert all(a >= b for a, b in zip(vals, vals[1:]))


def test_validation_quarantines_planted_errors():
    clean, q = warehouse.validate(estate.generate())
    assert set(q.building_id) == {"C02-B07", "C03-B08"}
    assert (clean.gia_m2 > 0).all() and (clean.gas_kwh >= 0).all()


def test_duplicate_ids_rejected():
    raw = estate.generate()
    raw = pd.concat([raw, raw.iloc[[0]]], ignore_index=True)
    _, q = warehouse.validate(raw)
    assert "duplicate building_id" in " ".join(q.reason)


# ---- appraisal -------------------------------------------------------------------
def test_every_building_gets_every_measure(appr):
    assert (appr.groupby("building_id").measure.count() == len(MEASURES)).all()


def test_net_is_operational_minus_embodied(appr):
    assert (appr.net_tco2e - (appr.op_tco2e - appr.embodied_tco2e)).abs().max() < 1e-9


def test_fabric_first_shrinks_heat_pump():
    b = estate.generate().iloc[0].copy()
    with_fabric = {r["measure"]: r for r in appraisal.appraise_building(b)}
    _, kw_direct = appraisal._apply("ASHP", b, appraisal.State(b.gas_kwh, b.elec_kwh, 0))
    assert with_fabric["ASHP"]["quantity"] < kw_direct


def test_heat_pump_adds_electricity_but_cuts_carbon(appr):
    hp = appr[appr.measure == "ASHP"]
    assert (hp.net_tco2e > 0).all()
    # at 2026 prices gas heat is cheaper than heat-pump heat: running cost rises
    assert (hp.annual_saving_gbp < 0).all()


def test_pv_carbon_shrinks_with_grid(appr):
    pv = appr[appr.measure == "PV"].iloc[0]
    assert pv.op_tco2e < pv.year1_tco2e * pv.life_years


def test_abatement_sign_matches_npv(appr):
    ok = appr[appr.net_tco2e > 0]
    assert ((ok.abatement_gbp_per_t < 0) == (ok.npv_gbp > 0)).all()


def test_packages_are_cumulative(appr):
    p = appraisal.packages(appr)
    last = p[p.level == len(MEASURES)]
    totals = appr.groupby("building_id").capex_gbp.sum()
    assert (last.set_index("building_id").capex_gbp - totals).abs().max() < 1e-6


def test_macc_sorted_and_contiguous(appr):
    m = appraisal.macc(appr)
    assert m.abatement_gbp_per_t.is_monotonic_increasing
    assert m.x_start.iloc[1:].to_numpy() == pytest.approx(m.x_end.iloc[:-1].to_numpy())


# ---- optimiser -------------------------------------------------------------------
def test_optimiser_respects_budget_and_one_package(appr):
    p = appraisal.packages(appr)
    plan = optimise.optimise(p, 2_000_000)
    assert plan.status == "optimal"
    assert plan.capex_gbp <= 2_000_000 + 1e-6
    assert plan.chosen.building_id.is_unique


def test_more_budget_never_less_carbon(appr):
    p = appraisal.packages(appr)
    t = [optimise.optimise(p, b).net_tco2e for b in (5e5, 1e6, 2e6, 4e6)]
    assert all(a <= b + 1e-6 for a, b in zip(t, t[1:]))


def test_optimiser_beats_greedy(appr):
    p = appraisal.packages(appr)
    budget = 1_500_000
    greedy, spent, seen = 0.0, 0.0, set()
    for _, r in p[p.net_tco2e > 0].assign(eff=p.net_tco2e / p.capex_gbp).sort_values("eff", ascending=False).iterrows():
        if r.building_id not in seen and spent + r.capex_gbp <= budget:
            greedy += r.net_tco2e
            spent += r.capex_gbp
            seen.add(r.building_id)
    assert optimise.optimise(p, budget).net_tco2e >= greedy - 1e-6


def test_grant_and_npv_constraints(appr):
    p = appraisal.packages(appr)
    g = optimise.optimise(p, 3e6, grant_only=True)
    assert (g.chosen.grant_gbp_per_t <= GRANT_CAP_GBP_PER_T).all()
    n = optimise.optimise(p, 3e6, min_npv=0.0)
    assert n.npv_gbp >= -1e-6


# ---- embodied --------------------------------------------------------------------
def test_embodied_modules_add_up():
    d = embodied.compare()
    assert ((d["A1-A3"] + d.A4 + d.A5) - d.upfront_kg).abs().max() < 1e-6
    assert (d.wlc_kg > d.upfront_kg).all()


def test_timber_has_lowest_upfront_but_highest_transport():
    d = embodied.compare().set_index("option")
    timber = d.index[d.index.str.contains("Glulam")][0]
    assert d.upfront_kg.idxmin() == timber
    assert d.A4.idxmax() == timber


# ---- analytics -------------------------------------------------------------------
def test_planted_anomalies_found(db):
    with duckdb.connect(str(db), read_only=True) as c:
        flagged = set(c.execute("SELECT building_id FROM anomalies WHERE flag = 'high'").df().building_id)
    assert {"C01-B04", "C03-B02", "C04-B06"} <= flagged


def test_anomaly_detector_quiet_on_clean_data():
    df = estate.generate(seed=11)
    df = df[df.gia_m2 > 0]
    df = df[df.gas_kwh > 0]
    clean = df[~df.building_id.isin(["C01-B04", "C03-B02", "C04-B06"])]
    assert (analytics.energy_anomalies(clean).flag == "ok").mean() > 0.95


# ---- governance ------------------------------------------------------------------
def test_client_sees_only_own_buildings(db):
    con = scoped_connection(db, "client", "C02")
    assert set(con.execute("SELECT DISTINCT client_id FROM buildings").df().client_id) == {"C02"}
    assert set(con.execute("SELECT DISTINCT client_id FROM appraisal").df().client_id) == {"C02"}


def test_client_cannot_see_unit_rates(db):
    con = scoped_connection(db, "client", "C01")
    cols = {r[0] for r in con.execute("DESCRIBE measures").fetchall()}
    assert "rate_gbp" not in cols and "fixed_gbp" not in cols


def test_client_session_cannot_reattach_warehouse(db):
    con = scoped_connection(db, "client", "C01")
    with pytest.raises(duckdb.Error):
        con.execute(f"ATTACH '{db.as_posix()}' AS x (READ_ONLY)")


def test_public_sees_only_benchmarks(db):
    con = scoped_connection(db, "public")
    assert {r[0] for r in con.execute("SHOW TABLES").fetchall()} == {"benchmarks", "measures"}


def test_client_role_requires_client_id(db):
    with pytest.raises(PermissionError):
        scoped_connection(db, "client")


@pytest.mark.parametrize("sql", [
    "DROP TABLE buildings",
    "SELECT 1; DELETE FROM buildings",
    "SELECT * FROM read_csv('secrets.csv')",
    "SELECT * FROM quarantine",
    "COPY buildings TO 'out.csv'",
])
def test_guard_blocks_unsafe_sql(sql):
    with pytest.raises(UnsafeQuery):
        guard_sql(sql, {"buildings", "appraisal"})


def test_guard_allows_cte_and_caps_rows():
    out = guard_sql("WITH x AS (SELECT * FROM buildings) SELECT * FROM x", {"buildings"})
    assert out.endswith("LIMIT 500")


def test_redaction():
    assert redact("email jo@client.org or call 07700 900123") == "email [EMAIL] or call [PHONE]"


# ---- assistant (offline) ---------------------------------------------------------
def test_offline_plan_within_budget(db, audit):
    a = Assistant(scoped_connection(db, "client", "C04"), "client", "client:C04", audit, api_key="")
    ans = a.ask("Plan a £1m budget")
    out = json.loads(ans.steps[0].output)
    assert out["capex_gbp"] <= 1_000_000
    assert all(b["building_id"].startswith("C04") for b in out["buildings"])
    assert any(e["event"] == "plan_investment" for e in audit.tail())


def test_assistant_blocks_other_tables(db, audit):
    a = Assistant(scoped_connection(db, "client", "C01"), "client", "client:C01", audit, api_key="")
    out = json.loads(a.run_sql("SELECT * FROM quarantine"))
    assert "Blocked" in out["error"]
    assert audit.tail()[-1]["event"] == "sql_blocked"


def test_public_assistant_cannot_plan(db, audit):
    a = Assistant(scoped_connection(db, "public"), "public", "public", audit, api_key="")
    assert "error" in json.loads(a.plan_investment(1e6))

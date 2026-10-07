"""Data governance and security controls.

Three roles, enforced by what data a session can physically reach, not by
filters an LLM could talk its way around:

  analyst   Consultancy staff. Everything, including proprietary unit rates.
  client    One client organisation. Its own buildings and appraisals only;
            costs as totals, unit rates removed.
  public    Anonymised benchmarks only (by building type, no client names).

scoped_connection() builds a fresh in-memory DuckDB holding only the tables
that role may see, then detaches the warehouse. LLM-written SQL also goes
through guard_sql (single read-only SELECT, allowlisted tables, no file
functions, row cap). Every access is written to an append-only audit log.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import duckdb
import sqlglot
from sqlglot import exp

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_AUDIT = ROOT / "data" / "audit_log.jsonl"

ROLES = ("analyst", "client", "public")

# role -> {table: SELECT over the warehouse (attached as wh)}
_SCOPES = {
    "analyst": {
        "buildings": "SELECT * FROM wh.buildings",
        "appraisal": "SELECT * FROM wh.appraisal",
        "measures": "SELECT * FROM wh.measures",
        "anomalies": "SELECT * FROM wh.anomalies",
    },
    "client": {
        "buildings": "SELECT * FROM wh.buildings WHERE client_id = $client",
        "appraisal": ("SELECT building_id, client_id, measure, measure_name, \"order\", quantity, unit, life_years, capex_gbp, "
                      "annual_saving_gbp, npv_gbp, op_tco2e, embodied_tco2e, net_tco2e, year1_tco2e, "
                      "abatement_gbp_per_t, grant_gbp_per_t, grant_eligible, payback_years "
                      "FROM wh.appraisal WHERE client_id = $client"),
        "measures": "SELECT code, name, life_years, note FROM wh.measures",
        "anomalies": "SELECT * FROM wh.anomalies WHERE client_id = $client",
    },
    "public": {
        "benchmarks": ("SELECT building_type, COUNT(*) AS buildings, ROUND(MEDIAN(gas_kwh / gia_m2)) AS gas_kwh_m2, "
                       "ROUND(MEDIAN(elec_kwh / gia_m2)) AS elec_kwh_m2 FROM wh.buildings GROUP BY 1"),
        "measures": "SELECT code, name, life_years, note FROM wh.measures",
    },
}

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
UK_PHONE = re.compile(r"(?:\+44\s?|0)(?:\d\s?){9,10}")


def redact(text: str) -> str:
    return UK_PHONE.sub("[PHONE]", EMAIL.sub("[EMAIL]", text))


def allowed_tables(role: str) -> set[str]:
    return set(_SCOPES[role])


def scoped_connection(db_path: Path | str, role: str, client_id: str | None = None) -> duckdb.DuckDBPyConnection:
    if role not in ROLES:
        raise PermissionError(f"Unknown role {role!r}")
    if role == "client" and not client_id:
        raise PermissionError("Client role needs a client_id")
    con = duckdb.connect()
    con.execute(f"ATTACH '{Path(db_path).as_posix()}' AS wh (READ_ONLY)")
    for table, sql in _SCOPES[role].items():
        params = {"client": client_id} if "$client" in sql else {}
        con.execute(f"CREATE TABLE {table} AS {sql}", params)
    con.execute("DETACH wh")
    con.execute("SET enable_external_access = false")
    return con


class UnsafeQuery(ValueError):
    pass


FORBIDDEN = (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter, exp.Command,
             exp.Copy, exp.Attach, exp.Detach, exp.Pragma, exp.Set, exp.Merge)


def guard_sql(sql: str, tables: set[str], max_rows: int = 500) -> str:
    try:
        statements = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None]
    except sqlglot.errors.ParseError as e:
        raise UnsafeQuery(f"Could not parse SQL: {e}") from e
    if len(statements) != 1:
        raise UnsafeQuery("Exactly one SQL statement is allowed.")
    tree = statements[0]
    if not tree.find(exp.Select):
        raise UnsafeQuery("Only SELECT queries are allowed.")
    for node in tree.walk():
        if isinstance(node, FORBIDDEN):
            raise UnsafeQuery(f"{type(node).__name__} statements are not allowed.")
        if isinstance(node, exp.Anonymous) and node.name.lower().startswith(("read_", "write_", "glob")):
            raise UnsafeQuery("File access functions are not allowed.")
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    for t in tree.find_all(exp.Table):
        if t.name.lower() not in tables and t.name.lower() not in ctes:
            raise UnsafeQuery(f"Table '{t.name}' is not available to this role.")
    return f"SELECT * FROM ({tree.sql(dialect='duckdb')}) AS q LIMIT {max_rows}"


class AuditLog:
    def __init__(self, path: Path | str = DEFAULT_AUDIT):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, actor: str, event: str, detail: dict) -> None:
        entry = {"ts": datetime.now().isoformat(timespec="seconds"), "actor": actor, "event": event, **detail}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    def tail(self, n: int = 50) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()[-n:]]

"""AI analyst: plain-English questions over the role-scoped carbon-cost data.

Claude gets two tools:
  run_sql          read-only SQL over the tables this role can see (guarded)
  plan_investment  runs the portfolio optimiser for a budget, within scope

The assistant only ever holds a scoped connection, so a client user's
assistant cannot reach another client's buildings or the consultancy's unit
rates whatever the prompt says. Text is redacted before it leaves the
platform and every tool call is audited.

Without ANTHROPIC_API_KEY it runs an offline mode where keyword templates
stand in for the model, so the platform can be evaluated end to end.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import duckdb

from . import appraisal, optimise
from .governance import AuditLog, UnsafeQuery, allowed_tables, guard_sql, redact
from .warehouse import SCHEMA_DOC

MODEL = "claude-opus-5-5"

SYSTEM_PROMPT = f"""You are the carbon-cost analyst inside a decarbonisation consultancy's client platform.
Users are estates managers, finance leads and consultants, not SQL specialists.

{SCHEMA_DOC}

How to work:
- Base every figure on a tool result. Never estimate numbers.
- Be explicit about carbon accounting: say whether a figure is first-year or whole-life, and that net tCO2e
  already subtracts embodied carbon.
- Answer first, then the key figures (GBP, tCO2e, rounded sensibly), then one recommended next step.
- Flag anything that needs a human check (anomalous meter data, measures that increase running cost).
- If a table is not available to this user, say the data is outside their access rather than guessing."""

TOOLS = [
    {
        "name": "run_sql",
        "description": "Run one read-only DuckDB SELECT over the tables available to this user. Returns up to 50 rows as JSON.",
        "input_schema": {"type": "object", "properties": {
            "sql": {"type": "string"}, "purpose": {"type": "string"}},
            "required": ["sql", "purpose"], "additionalProperties": False},
        "strict": True,
    },
    {
        "name": "plan_investment",
        "description": "Choose the retrofit package for each building that maximises whole-life net tCO2e saved "
                       "within a capital budget (exact MILP). Optionally restrict to grant-eligible packages.",
        "input_schema": {"type": "object", "properties": {
            "budget_gbp": {"type": "number"}, "grant_only": {"type": "boolean"}},
            "required": ["budget_gbp", "grant_only"], "additionalProperties": False},
        "strict": True,
    },
]


@dataclass
class Step:
    tool: str
    input: dict
    output: str


@dataclass
class Answer:
    text: str
    steps: list[Step] = field(default_factory=list)
    mode: str = "claude"


class Assistant:
    def __init__(self, con: duckdb.DuckDBPyConnection, role: str, actor: str,
                 audit: AuditLog | None = None, api_key: str | None = None):
        self.con, self.role, self.actor = con, role, actor
        self.tables = allowed_tables(role)
        self.audit = audit or AuditLog()
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")

    @property
    def online(self) -> bool:
        return bool(self.api_key)

    def run_sql(self, sql: str, purpose: str = "") -> str:
        try:
            safe = guard_sql(sql, self.tables)
        except UnsafeQuery as e:
            self.audit.record(self.actor, "sql_blocked", {"sql": sql, "reason": str(e)})
            return json.dumps({"error": f"Blocked by governance guard: {e}"})
        try:
            df = self.con.execute(safe).df()
        except duckdb.Error as e:
            return json.dumps({"error": f"SQL error: {e}"})
        self.audit.record(self.actor, "sql_run", {"purpose": purpose, "sql": sql, "rows": len(df)})
        return df.head(50).to_json(orient="records", double_precision=2)

    def plan_investment(self, budget_gbp: float, grant_only: bool = False) -> str:
        if "appraisal" not in self.tables:
            return json.dumps({"error": "Investment planning is not available to this role."})
        pkgs = appraisal.packages(self.con.execute("SELECT * FROM appraisal").df())
        plan = optimise.optimise(pkgs, budget_gbp, grant_only=grant_only)
        self.audit.record(self.actor, "plan_investment", {"budget_gbp": budget_gbp, "grant_only": grant_only,
                                                          "status": plan.status})
        return json.dumps({
            "status": plan.status, "capex_gbp": round(plan.capex_gbp), "net_tco2e_whole_life": round(plan.net_tco2e),
            "first_year_tco2e": round(plan.year1_tco2e, 1), "npv_gbp": round(plan.npv_gbp),
            "buildings": plan.chosen[["building_id", "measures", "capex_gbp", "net_tco2e"]].round(0).to_dict("records"),
        })

    def _call(self, name: str, args: dict) -> str:
        if name == "run_sql":
            return self.run_sql(args.get("sql", ""), args.get("purpose", ""))
        if name == "plan_investment":
            return self.plan_investment(float(args.get("budget_gbp", 0)), bool(args.get("grant_only", False)))
        return json.dumps({"error": f"Unknown tool {name}"})

    def ask(self, question: str, max_turns: int = 8) -> Answer:
        question = redact(question)
        self.audit.record(self.actor, "question", {"question": question, "online": self.online})
        if not self.online:
            return self._offline(question)

        import anthropic

        client = anthropic.Anthropic(api_key=self.api_key)
        system = SYSTEM_PROMPT + f"\n\nCurrent user role: {self.role}. Tables available: {sorted(self.tables)}."
        messages: list[dict] = [{"role": "user", "content": question}]
        steps: list[Step] = []
        for _ in range(max_turns):
            response = client.beta.messages.create(
                model=MODEL, max_tokens=16000, system=system, tools=TOOLS,
                output_config={"effort": "medium"},
                betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                messages=messages,
            )
            if response.stop_reason == "refusal":
                return Answer("The model declined to answer this request.", steps)
            messages.append({"role": "assistant", "content": response.content})
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not tool_uses:
                text = "\n".join(b.text for b in response.content if b.type == "text").strip()
                return Answer(text or "(no answer returned)", steps)
            results = []
            for tu in tool_uses:
                out = self._call(tu.name, tu.input)
                steps.append(Step(tu.name, dict(tu.input), out))
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": out,
                                "is_error": out.startswith('{"error"')})
            messages.append({"role": "user", "content": results})
        return Answer("Stopped after the maximum number of reasoning steps.", steps)

    # ---- offline demo mode -------------------------------------------------
    OFFLINE = [
        (("cheapest", "best value", "cost-effective", "abatement", "macc"),
         "Measures ranked by lifetime abatement cost (negative = saves money):",
         "SELECT measure_name, COUNT(*) AS buildings, ROUND(SUM(net_tco2e)) AS net_tco2e, "
         "ROUND(-SUM(npv_gbp)/SUM(net_tco2e)) AS gbp_per_t FROM appraisal WHERE net_tco2e > 0 "
         "GROUP BY 1 ORDER BY gbp_per_t"),
        (("anomal", "outlier", "unusual", "meter"),
         "Buildings whose energy use is far above their peers:",
         "SELECT building_id, metric, value_kwh_m2, peer_median, robust_z FROM anomalies WHERE flag <> 'ok' "
         "ORDER BY robust_z DESC"),
        (("grant", "salix", "psds", "eligible"),
         "Grant-eligible measures by building:",
         "SELECT building_id, measure_name, ROUND(capex_gbp) AS capex_gbp, ROUND(grant_gbp_per_t) AS gbp_per_t "
         "FROM appraisal WHERE grant_eligible ORDER BY grant_gbp_per_t LIMIT 15"),
        (("heat pump", "ashp", "gas"),
         "Heat pump business case by building:",
         "SELECT building_id, ROUND(capex_gbp) AS capex_gbp, ROUND(annual_saving_gbp) AS annual_saving_gbp, "
         "ROUND(net_tco2e) AS net_tco2e, ROUND(abatement_gbp_per_t) AS gbp_per_t FROM appraisal "
         "WHERE measure = 'ASHP' ORDER BY abatement_gbp_per_t LIMIT 15"),
        (("benchmark", "typical", "intensity"),
         "Energy intensity benchmarks:",
         "SELECT * FROM benchmarks"),
    ]

    def _offline(self, question: str) -> Answer:
        q = question.lower()
        parts = ["_Offline demo mode: no API key set, so a keyword template stands in for Claude._"]
        steps: list[Step] = []
        if any(w in q for w in ("budget", "invest", "plan", "£")):
            import re
            m = re.search(r"£?\s*([\d.]+)\s*(m|k|million)?", q.replace(",", ""))
            budget = 2_000_000.0
            if m:
                budget = float(m.group(1)) * {"m": 1e6, "million": 1e6, "k": 1e3}.get(m.group(2) or "", 1)
            out = self.plan_investment(budget, grant_only="grant" in q)
            steps.append(Step("plan_investment", {"budget_gbp": budget}, out))
            r = json.loads(out)
            if "error" in r:
                parts.append(r["error"])
            else:
                parts.append(f"**Best plan for £{budget:,.0f}:** £{r['capex_gbp']:,} capex saves "
                             f"**{r['net_tco2e_whole_life']:,} tCO2e whole-life** ({r['first_year_tco2e']:,} t in year one), "
                             f"NPV £{r['npv_gbp']:,}, across {len(r['buildings'])} buildings.")
            return Answer("\n\n".join(parts), steps, mode="offline")
        intent = next((i for i in self.OFFLINE if any(k in q for k in i[0])), None)
        if not intent:
            parts.append("Try asking about abatement cost, anomalies, grant eligibility, heat pumps, or "
                         "'plan a £2m budget'.")
            return Answer("\n\n".join(parts), steps, mode="offline")
        _, heading, sql = intent
        out = self.run_sql(sql, "offline template")
        steps.append(Step("run_sql", {"sql": sql}, out))
        rows = json.loads(out)
        if isinstance(rows, dict):
            parts.append(rows["error"])
        elif rows:
            cols = list(rows[0])
            lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
            lines += ["| " + " | ".join((f"{r[c]:,.0f}" if abs(r[c]) >= 100 else f"{r[c]:,.2f}") if isinstance(r[c], float) else str(r[c]) for c in cols) + " |"
                      for r in rows[:15]]
            parts += [f"**{heading}**", "\n".join(lines)]
        return Answer("\n\n".join(parts), steps, mode="offline")

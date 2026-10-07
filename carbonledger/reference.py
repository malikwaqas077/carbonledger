"""Reference data: carbon factors, energy prices, retrofit measures and materials.

Every value carries a provenance tag so the platform can show where a number
came from and who may see it:

  open          Published or public-domain style factor (illustrative values in the
                range of DESNZ conversion factors / ICE v3; verify before real use).
  proprietary   Consultancy cost data (unit rates). Synthetic here. In a live system
                these are Identity Consult's commercially sensitive rates, so clients
                see totals, never the rates themselves.
  assumption    Modelling assumption a user can change.

All monetary values are 2026 real GBP. Nothing here is client data.
"""
from __future__ import annotations

from dataclasses import dataclass

BASE_YEAR = 2026
DISCOUNT_RATE = 0.035          # HM Treasury Green Book real discount rate (assumption)

PRICES = {                      # assumption: flat real prices, £/kWh
    "gas": 0.065,
    "electricity": 0.245,
    "export": 0.055,
}

GAS_EF = 0.183                  # kgCO2e/kWh, natural gas (gross CV), open


def grid_ef(year: int) -> float:
    """Grid electricity carbon factor, kgCO2e/kWh.

    Illustrative decarbonisation trajectory: 0.177 in 2026 falling linearly to
    0.030 in 2040, flat after. Swap in DESNZ Green Book long-run factors for
    real appraisals. This trajectory matters: it is why a gas-to-heat-pump switch
    keeps getting better over its life while PV savings shrink.
    """
    if year <= BASE_YEAR:
        return 0.177
    if year >= 2040:
        return 0.030
    return 0.177 + (0.030 - 0.177) * (year - BASE_YEAR) / (2040 - BASE_YEAR)


# Grant screening: PSDS/Salix-style cost-effectiveness cap on capex per lifetime
# tonne. Illustrative only; check current scheme guidance.
GRANT_CAP_GBP_PER_T = 325.0


@dataclass(frozen=True)
class Measure:
    code: str
    name: str
    order: int                  # fabric first: applied in this sequence
    unit: str                   # what quantity the rates apply to
    rate_gbp: float             # proprietary unit rate
    fixed_gbp: float            # proprietary fixed cost per site
    embodied_kg_per_unit: float  # open-style A1-A5 factor, kgCO2e per unit
    life_years: int
    maint_pct: float            # annual maintenance as share of capex
    note: str


MEASURES: dict[str, Measure] = {m.code: m for m in [
    Measure("BMS", "BMS controls optimisation", 1, "m2 GIA", 6.0, 3_000, 0.5, 10, 0.02,
            "Saves 10% of gas and 6% of electricity through scheduling and setpoints."),
    Measure("LED", "LED lighting with controls", 2, "m2 GIA", 30.0, 3_000, 3.0, 15, 0.0,
            "Saves 55% of lighting electricity."),
    Measure("INS", "Loft and cavity insulation", 3, "m2 GIA", 22.0, 3_000, 6.0, 30, 0.0,
            "Saves 18% of remaining gas."),
    Measure("PV", "Rooftop solar PV", 4, "kWp", 950.0, 5_000, 750.0, 25, 0.01,
            "Sized to usable roof and capped at annual demand; only self-consumed output cuts grid carbon."),
    Measure("GLZ", "Double/secondary glazing upgrade", 5, "m2 GIA", 60.0, 3_000, 20.0, 30, 0.0,
            "Saves 10% of remaining gas."),
    Measure("ASHP", "Air-source heat pump replacing gas boiler", 6, "kW heat", 1_000.0, 15_000, 120.0, 20, 0.01,
            "Displaces 90% of remaining gas, SCOP 2.8, sized on post-fabric demand."),
]}

PROVENANCE = {
    "factors.gas_ef": ("open", "DESNZ-style conversion factor (illustrative)"),
    "factors.grid_ef": ("assumption", "Linear trajectory 0.177 -> 0.030 kgCO2e/kWh by 2040"),
    "measures.rate_gbp": ("proprietary", "Consultancy unit rates (synthetic in this demo)"),
    "measures.fixed_gbp": ("proprietary", "Consultancy prelims per site (synthetic)"),
    "measures.embodied": ("open", "A1-A5 factors in the range of published LCAs (illustrative)"),
    "materials.ef": ("open", "ICE v3-range factors (illustrative; use project EPDs)"),
    "materials.rate_gbp": ("proprietary", "Consultancy material rates (synthetic)"),
    "estate.energy": ("client-confidential", "Client meter data (synthetic)"),
}

# Building-type assumptions used by the appraisal engine.
LIGHTING_SHARE = {"school": 0.30, "health_centre": 0.25, "office": 0.35, "university": 0.20}
SELF_CONSUMPTION = {"school": 0.65, "health_centre": 0.85, "office": 0.80, "university": 0.90}

# Construction materials for design-option (embodied carbon) comparison.
# ef: kgCO2e per unit (A1-A3). density: kg per unit (for transport A4 / end of life C).
MATERIALS = {
    "concrete_c32_cem1": {"name": "Concrete C32/40, CEM I", "unit": "m3", "ef": 290.0, "density": 2400, "rate_gbp": 165.0},
    "concrete_c32_ggbs50": {"name": "Concrete C32/40, 50% GGBS", "unit": "m3", "ef": 170.0, "density": 2400, "rate_gbp": 172.0},
    "rebar": {"name": "Reinforcing steel", "unit": "kg", "ef": 1.99, "density": 1, "rate_gbp": 1.35},
    "steel_sections": {"name": "Structural steel sections", "unit": "kg", "ef": 1.55, "density": 1, "rate_gbp": 2.90},
    "glulam": {"name": "Glulam (fossil only, no sequestration)", "unit": "m3", "ef": 260.0, "density": 500, "rate_gbp": 950.0},
    "clt": {"name": "Cross-laminated timber (fossil only)", "unit": "m3", "ef": 210.0, "density": 480, "rate_gbp": 850.0},
    "metal_deck": {"name": "Composite metal decking", "unit": "m2", "ef": 22.0, "density": 11, "rate_gbp": 38.0},
    "mineral_wool": {"name": "Mineral wool insulation", "unit": "m3", "ef": 45.0, "density": 35, "rate_gbp": 60.0},
}

TRANSPORT_EF_PER_TKM = 0.107    # kgCO2e per tonne-km, average HGV (open, illustrative)
A5_SITE_KG_PER_100K = 1_400.0   # RICS WLCA default for site activities per £100k project value
EOL_KG_PER_KG = 0.013           # C3-C4 processing/disposal, illustrative
EOL_TRANSPORT_KM = 50
WASTE_RATE = 0.05               # A5 material wastage

# Upfront carbon (A1-A5) benchmarks, kgCO2e/m2 GIA. LETI-style office targets;
# configurable because each client picks its own benchmark.
UPFRONT_BENCHMARKS = {"LETI 2020 (office)": 600.0, "LETI 2030 (office)": 350.0}

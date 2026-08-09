"""
Billing rules engine.

The AI proposes: "here's what this email is about, here's the rule code
and the quantity". The engine disposes: it takes rule_code + quantity +
any special fields (estimate_amount, building_count) and computes the
actual billable hours DETERMINISTICALLY.

This split matters:
    * LLMs are excellent at classification and extraction, but bad at
      compound arithmetic. Asking them to do `min(pages * 1.5, 25)` gets
      wrong answers 5-10% of the time.
    * A billing audit needs "here's exactly why this line reads 15.33 hrs".
      If the AI produced that number opaquely, we can't explain it. If
      the engine did it via `(1_277_422 / 500_000) * 6 = 15.33`, we can.

Every rule is coded once here. When the client changes a rate, we change
ONE constant and re-run — no prompts to re-tune, no cached analyses to
invalidate (the AI output stays valid, only the derived hours change).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal


# ---------------------------------------------------------------------------
# Rule specifications
# ---------------------------------------------------------------------------
RuleShape = Literal[
    "flat",             # fixed hours, quantity ignored (initial report, bid review)
    "per_unit",         # hours = rate * quantity
    "prorated_500k",    # hours = ceil(estimate_usd / 500_000) * base_hours * buildings
]


@dataclass(frozen=True)
class RuleSpec:
    """Immutable definition of ONE billing rule. Keyed by `code`."""
    code: str
    category: str
    description: str
    shape: RuleShape
    rate_hours: float                # per unit (or flat total when shape='flat')
    unit_label: str                  # human-readable, appears on invoice
    max_hours: float | None = None   # optional cap (e.g. SoW at 25 hrs)
    notes: str = ""


# All 25 rules — MUST match `code` values in the DB seed migration.
# When adding/editing, remember to:
#   1. Update the prompt table in prompt_service.py
#   2. Update the seed migration in alembic/versions/
#   3. Bump PROMPT_VERSION so cached analyses re-run
RULES: dict[str, RuleSpec] = {
    "EMAIL_SHORT":         RuleSpec("EMAIL_SHORT", "Communication", "Short client email (1-2 lines)",  "per_unit", 0.1,  "email"),
    "EMAIL_DESCRIPTIVE":   RuleSpec("EMAIL_DESCRIPTIVE", "Communication", "Descriptive client email (per half page)", "per_unit", 0.6, "half page"),
    "DOC_REVIEW_STD":      RuleSpec("DOC_REVIEW_STD", "Document Review", "Standard document review", "per_unit", 0.2, "page"),
    "DOC_REVIEW_COMPLEX":  RuleSpec("DOC_REVIEW_COMPLEX", "Document Review", "Complex document review (HMI / structural)", "per_unit", 0.4, "page"),
    "DATA_ENTRY":          RuleSpec("DATA_ENTRY", "Data Entry", "Data entry (per line — 2 min)", "per_unit", 2/60, "line"),
    "AUDIT_SIMPLE":        RuleSpec("AUDIT_SIMPLE", "Audit", "Simple audit (per page)", "per_unit", 0.5, "page"),
    "AUDIT_COMPLEX":       RuleSpec("AUDIT_COMPLEX", "Audit", "Complex audit (per line — 5 min)", "per_unit", 5/60, "line"),
    "RCV_ACV":             RuleSpec("RCV_ACV", "Estimate", "RCV / ACV estimate (per building, prorated)", "prorated_500k", 6.0, "building"),
    "BUDGET_RESERVE":      RuleSpec("BUDGET_RESERVE", "Estimate", "Budget Reserve (per building, prorated)", "prorated_500k", 5.0, "building"),
    "INITIAL_REPORT":      RuleSpec("INITIAL_REPORT", "Report", "Initial Report", "flat", 0.9, "flat"),
    "GEN_COND_FULL_DEMO":  RuleSpec("GEN_COND_FULL_DEMO", "General Conditions", "Gen Condition + full demolition (total loss)", "flat", 2.75, "flat", notes="Range 2.5-3, midpoint used"),
    "GEN_COND_PARTIAL_DEMO":  RuleSpec("GEN_COND_PARTIAL_DEMO", "General Conditions", "Gen Condition + partial demolition", "flat", 5.0, "flat"),
    "PRICING_DEMO":        RuleSpec("PRICING_DEMO", "Pricing", "Pricing template — demolition (per scenario)", "per_unit", 0.2, "scenario"),
    "PRICING_RECON":       RuleSpec("PRICING_RECON", "Pricing", "Pricing template — reconstruction (25 categories)", "flat", 0.6, "flat"),
    "SCENARIO_RECON":      RuleSpec("SCENARIO_RECON", "Pricing", "Reconstruction scenario (per scenario)", "per_unit", 1.1, "scenario"),
    "SOW_EXCEL":           RuleSpec("SOW_EXCEL", "Scope of Work", "SoW — Excel template (per page)", "per_unit", 1.5, "page", max_hours=25.0),
    "SOW_XACTIMATE":       RuleSpec("SOW_XACTIMATE", "Scope of Work", "SoW — Xactimate template (per page)", "per_unit", 0.7, "page", max_hours=25.0),
    "SPEC_SHEET":          RuleSpec("SPEC_SHEET", "Specification", "Specification sheet (per line, 3 min)", "per_unit", 3/60, "line"),
    "BID_DEMO":            RuleSpec("BID_DEMO", "Bid Review", "Demolition bid review", "flat", 1.2, "flat"),
    "BID_RECON":           RuleSpec("BID_RECON", "Bid Review", "Reconstruction bid review", "flat", 1.8, "flat"),
    "XACT_SKETCH":         RuleSpec("XACT_SKETCH", "Xactimate", "Xactimate sketch (per floor, 30 min)", "per_unit", 0.5, "floor"),
    "REPORT_PAYMENT_REC":  RuleSpec("REPORT_PAYMENT_REC", "Report", "Payment recommendation report (per page)", "per_unit", 0.7, "page"),
    "RESEARCH_CONTRACTORS":      RuleSpec("RESEARCH_CONTRACTORS", "Research", "Research contractors (per contractor, 15 min)", "per_unit", 0.25, "contractor"),
    "SITE_VISIT":          RuleSpec("SITE_VISIT", "Field Work", "Site Visit", "flat", 2.0, "flat"),
    "CALLING_TASK":        RuleSpec("CALLING_TASK", "Communication", "Calling Task (internal assignment, 15 min)", "per_unit", 0.25, "call"),
}


# ---------------------------------------------------------------------------
# Result — what the engine returns
# ---------------------------------------------------------------------------
@dataclass
class ComputedLine:
    """One billable line, computed from the rule + AI's proposed quantity."""
    rule_code: str
    category: str
    description: str
    quantity: float
    quantity_unit: str
    hours: Decimal
    hours_reasoning: str
    hit_cap: bool = False
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Compute
# ---------------------------------------------------------------------------
def _round_hours(hrs: float) -> Decimal:
    """Round to 0.01 hr, banker's rounding for stability. Billing sheets
    stay tidy this way and the 0.01 precision matches the client's manual
    process."""
    return Decimal(str(hrs)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def compute_hours(
    rule_code: str,
    quantity: float,
    *,
    estimate_amount_usd: float | None = None,
    building_count: int | None = None,
) -> ComputedLine | None:
    """
    Given a rule code and the AI's proposed quantity, compute billable hours.

    Returns None if the rule_code is unknown (caller should mark the line
    "requires manual review" — never invent a line).

    Extra fields:
        * estimate_amount_usd — REQUIRED for prorated rules (RCV/ACV,
          BUDGET_RESERVE). Ignored for others.
        * building_count — REQUIRED for prorated rules. Defaults to 1.
    """
    spec = RULES.get(rule_code)
    if spec is None:
        return None

    warnings: list[str] = []

    # ---- Flat rate ------------------------------------------------------
    if spec.shape == "flat":
        hours = spec.rate_hours
        reasoning = f"{spec.description}: flat rate of {spec.rate_hours} hrs"
        return ComputedLine(
            rule_code=spec.code,
            category=spec.category,
            description=spec.description,
            quantity=1.0,
            quantity_unit=spec.unit_label,
            hours=_round_hours(hours),
            hours_reasoning=reasoning,
        )

    # ---- Per unit ------------------------------------------------------
    if spec.shape == "per_unit":
        if quantity <= 0:
            warnings.append(f"Quantity was {quantity}; treating as 0 hrs but review recommended.")
            hours_raw = 0.0
        else:
            hours_raw = spec.rate_hours * quantity
        hit_cap = False
        if spec.max_hours is not None and hours_raw > spec.max_hours:
            hit_cap = True
            hours_raw = spec.max_hours
            reasoning = (
                f"{quantity} {spec.unit_label}(s) × {spec.rate_hours} hrs = "
                f"{spec.rate_hours * quantity:.2f} hrs, capped at "
                f"{spec.max_hours} hrs per policy"
            )
        else:
            reasoning = (
                f"{quantity} {spec.unit_label}(s) × {spec.rate_hours} hrs = "
                f"{hours_raw:.2f} hrs"
            )
        return ComputedLine(
            rule_code=spec.code,
            category=spec.category,
            description=spec.description,
            quantity=float(quantity),
            quantity_unit=spec.unit_label,
            hours=_round_hours(hours_raw),
            hours_reasoning=reasoning,
            hit_cap=hit_cap,
            warnings=warnings,
        )

    # ---- Prorated per $500K --------------------------------------------
    if spec.shape == "prorated_500k":
        buildings = max(1, building_count or 1)
        if not estimate_amount_usd or estimate_amount_usd <= 0:
            warnings.append(
                "Prorated rule but estimate_amount_usd missing — falling back "
                "to base rate. Review recommended."
            )
            hours_raw = spec.rate_hours * buildings
            reasoning = (
                f"Base {spec.rate_hours} hrs × {buildings} building(s) "
                f"(no estimate to prorate against)"
            )
        else:
            # PRORATED per client's spec: 6 hrs per $500K, per building.
            proration = estimate_amount_usd / 500_000.0
            hours_raw = spec.rate_hours * proration * buildings
            reasoning = (
                f"${estimate_amount_usd:,.0f} / $500,000 = {proration:.3f} × "
                f"{spec.rate_hours} hrs × {buildings} building(s) = "
                f"{hours_raw:.2f} hrs"
            )
        return ComputedLine(
            rule_code=spec.code,
            category=spec.category,
            description=spec.description,
            quantity=float(buildings),
            quantity_unit="building" if buildings == 1 else "buildings",
            hours=_round_hours(hours_raw),
            hours_reasoning=reasoning,
            warnings=warnings,
        )

    # Unreachable — every shape handled above.
    return None


def get_rule(rule_code: str) -> RuleSpec | None:
    return RULES.get(rule_code)


def all_rules() -> list[RuleSpec]:
    """Ordered list of all rules (stable order = insertion order in RULES)."""
    return list(RULES.values())

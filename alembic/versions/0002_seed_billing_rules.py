"""Seed the 25 billing rules from GNC's Internal Hours guidelines.

Revision ID: 0002_seed_billing_rules
Revises: 0001_initial_schema
Create Date: 2026-08-01

Every rule matches exactly one code the AI prompt teaches and the runtime
billing engine (app/services/billing_service.py RULES) computes against.

The engine is the CANONICAL source of truth for math. This table exists so
the UI can list rules for reviewers and (later) admins can toggle client
overrides.

UOM here is coarse (limited to the CHECK-constraint enum in constants).
The engine internally works with finer units (buildings, floors, calls,
scenarios etc.); the DB label is just for display.

Idempotent: INSERT ... ON CONFLICT DO NOTHING on `code`. Re-running the
migration on an environment that already has rules leaves them alone.
"""
from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic
revision = "0002_seed_billing_rules"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


# (code, category, description, charge_type, base_hours, uom, conditions)
# NB: rule codes MUST exactly match app.services.billing_service.RULES.
RULES = [
    ("EMAIL_SHORT",           "Email",       "Client incoming email (1-2 lines)",                       "hourly", "0.1",    "per_line", {"unit_desc": "per_email"}),
    ("EMAIL_DESCRIPTIVE",     "Email",       "Client incoming email (descriptive)",                     "hourly", "0.6",    "per_page", {"unit_desc": "per_half_page", "chars_per_half_page": 1250}),
    ("DOC_REVIEW_STD",        "Document",    "Document review (standard, per page)",                    "hourly", "0.2",    "per_page", {}),
    ("DOC_REVIEW_COMPLEX",    "Document",    "Document review (complex — HMI/structural, per page)",   "hourly", "0.4",    "per_page", {"complex": True}),
    ("DATA_ENTRY",            "Data",        "Data entry (per line)",                                   "hourly", "0.0333", "per_line", {"alt_rate_per_page_hrs": 0.3}),
    ("AUDIT_SIMPLE",          "Audit",       "Simple audit (per page)",                                 "hourly", "0.5",    "per_page", {}),
    ("AUDIT_COMPLEX",         "Audit",       "Complex audit (per line)",                                "hourly", "0.0833", "per_line", {"complex": True}),
    ("RCV_ACV",               "Estimate",    "RCV / ACV (per building, prorated per $500K)",           "hourly", "6",      "flat",     {"unit_desc": "per_building_prorated", "prorate_per_dollars": 500000}),
    ("BUDGET_RESERVE",        "Estimate",    "Budget Reserve (per building, prorated per $500K)",       "hourly", "5",      "flat",     {"unit_desc": "per_building_prorated", "prorate_per_dollars": 500000}),
    ("INITIAL_REPORT",        "Report",      "Initial Report",                                          "hourly", "0.9",    "flat",     {}),
    ("GEN_COND_FULL_DEMO",    "Scope",       "General condition + full demolition (total loss)",        "hourly", "2.75",   "flat",     {"range": "2.5-3"}),
    ("GEN_COND_PARTIAL_DEMO", "Scope",       "General condition + partial demolition (partial loss)",   "hourly", "5",      "flat",     {}),
    ("PRICING_DEMO",          "Pricing",     "Pricing template — demolition (per scenario)",            "hourly", "0.2",    "per_line", {"unit_desc": "per_scenario"}),
    ("PRICING_RECON",         "Pricing",     "Pricing template — reconstruction (25 categories)",       "hourly", "0.6",    "flat",     {"categories": 25}),
    ("SCENARIO_RECON",        "Scenario",    "Reconstruction scenario (per scenario)",                  "hourly", "1.1",    "per_line", {"unit_desc": "per_scenario"}),
    ("SOW_EXCEL",             "SoW",         "SoW — Excel template (per page)",                         "hourly", "1.5",    "per_page", {"max_hours": 25}),
    ("SOW_XACTIMATE",         "SoW",         "SoW — Xactimate template (per page)",                     "hourly", "0.7",    "per_page", {"max_hours": 25}),
    ("SPEC_SHEET",            "Spec",        "Specification sheet (per line item)",                     "hourly", "0.05",   "per_line", {"unit_desc": "per_line_item"}),
    ("BID_DEMO",              "Bid",         "Demolition bid review (3 vendors, single scenario)",      "hourly", "1.2",    "flat",     {"vendors": 3}),
    ("BID_RECON",             "Bid",         "Reconstruction bid review (3 vendors, single scenario)",  "hourly", "1.8",    "flat",     {"vendors": 3, "alt_min_per_line": 3}),
    ("XACT_SKETCH",           "Sketch",      "Xactimate sketch (per floor, 2500 sqft standard)",        "hourly", "0.5",    "per_line", {"unit_desc": "per_floor", "standard_sqft": 2500}),
    ("REPORT_PAYMENT_REC",    "Report",      "Payment recommendation report (per page)",                "hourly", "0.7",    "per_page", {}),
    ("RESEARCH_CONTRACTORS",  "Research",    "Research contractors (per contractor, 15 min)",           "hourly", "0.25",   "per_line", {"unit_desc": "per_contractor"}),
    ("SITE_VISIT",            "Site",        "Site visit (standard 2500 sqft house)",                   "hourly", "2",      "flat",     {"standard_sqft": 2500}),
    ("CALLING_TASK",          "Calling",     "Calling Task (internal assignment, 15 min per call)",     "hourly", "0.25",   "per_call", {"unit_desc": "per_call", "is_internal_exception": True}),
]


INSERT_SQL = sa.text("""
    INSERT INTO billing_rules
      (id, code, category, description, charge_type, base_hours,
       flat_fee, uom, conditions, client_scope, client_ids,
       comments, is_active, version, created_at, updated_at)
    VALUES
      (gen_random_uuid(), :code, :category, :description, :charge_type,
       CAST(:base_hours AS numeric), NULL, :uom, CAST(:conditions AS jsonb),
       'global', ARRAY[]::uuid[], '', TRUE, 1, NOW(), NOW())
    ON CONFLICT (code) DO NOTHING
""")


def upgrade() -> None:
    """Insert rules — idempotent via ON CONFLICT."""
    conn = op.get_bind()
    for code, category, description, charge_type, base_hours, uom, conditions in RULES:
        conn.execute(INSERT_SQL, {
            "code": code,
            "category": category,
            "description": description,
            "charge_type": charge_type,
            "base_hours": base_hours,
            "uom": uom,
            "conditions": json.dumps(conditions),
        })


def downgrade() -> None:
    codes = [r[0] for r in RULES]
    op.execute(
        sa.text("DELETE FROM billing_rules WHERE code = ANY(:codes)")
        .bindparams(sa.bindparam("codes", value=codes, expanding=False))
    )

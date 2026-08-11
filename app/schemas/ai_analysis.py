"""
Pydantic schemas for AI outputs.

These schemas serve THREE purposes at once:
    1. They define the JSON shape we send to OpenAI's `response_format`
       structured-output feature. OpenAI guarantees the returned JSON
       will match this schema exactly.
    2. They validate the response — if OpenAI hallucinates a field
       that shouldn't exist, or omits one that should, Pydantic raises
       and we retry / mark as failed.
    3. They serve as documentation for reviewers: THIS is what the AI
       is expected to produce, no more, no less.

Design principles:
    * Every field has a `description` that OpenAI reads in the schema —
      this is effectively part of the prompt. Write these descriptions
      carefully — they steer the model.
    * No optional fields except where truly ambiguous (e.g. quantity is
      null for flat-rate rules). Optionality invites hallucination.
    * All enums are Literal[...] tuples — OpenAI honours enum constraints
      in strict mode, so we get free type safety.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, ConfigDict


# ---------------------------------------------------------------------------
# Enums (Literal types for OpenAI strict mode)
# ---------------------------------------------------------------------------

# Email/attachment classification. Every AI output must pick exactly one.
# See prompt for full definitions.
EMAIL_CLASSIFICATION = Literal[
    "BILLABLE",            # a normal chargeable communication with an external party
    "NON_BILLABLE",        # internal, informational, out-of-scope — do not bill
    "CALLING_TASK",        # internal email assigning a phone call TASK — bill as Calling Task
    "SPAM_AUTOMATED",      # auto-reply, calendar invite, newsletter, delivery receipt
    "UNCLEAR",             # AI cannot decide — human must review
]

# Unit of measure the AI reports the QUANTITY in. The rules engine converts
# quantity + unit + rule_code into billable hours.
QUANTITY_UNIT = Literal[
    "FLAT",           # single flat rate — quantity is 1 (e.g. Initial Report)
    "PAGES",          # documents review, SoW pages, reports
    "HALF_PAGES",     # descriptive emails (0.6 hrs per half page)
    "LINE_ITEMS",     # spec sheet lines, bid review line items
    "SCENARIOS",      # pricing / reconstruction scenarios
    "CALLS",          # Calling Task — count of assigned calls
    "CONTRACTORS",    # Research Contractors
    "BUILDINGS",      # RCV / Budget Reserve (with estimate_amount_usd)
    "FLOORS",         # Xactimate Sketch (per floor)
    "EMAILS",         # short 1-2 line client emails (rule 1)
]


# ---------------------------------------------------------------------------
# Quantity block — reused across email + attachment outputs
# ---------------------------------------------------------------------------
class Quantity(BaseModel):
    """The measurable quantity we bill against. Null when classification
    is NON_BILLABLE / SPAM_AUTOMATED / UNCLEAR."""
    model_config = ConfigDict(extra="forbid")

    value: float = Field(
        description=(
            "The numerical quantity. For PAGES this is the page count, "
            "for LINE_ITEMS the number of lines, for FLAT always 1.0, "
            "for BUILDINGS the number of buildings in the RCV/ACV report."
        ),
        ge=0,
    )
    unit: QUANTITY_UNIT = Field(
        description=(
            "The unit for `value`. Must exactly match one of the enum "
            "values. Choose FLAT if the billing rule is a fixed hours "
            "figure regardless of scale."
        )
    )
    reasoning: str = Field(
        description=(
            "One sentence explaining how you arrived at this quantity. "
            "E.g. 'The RCV Report PDF contains 13 pages of technical text'."
        ),
        max_length=500,
    )


# ---------------------------------------------------------------------------
# Email analysis
# ---------------------------------------------------------------------------
class EmailAnalysisOutput(BaseModel):
    """Structured AI output for ONE email.

    The AI receives: subject, from, to/cc, body text, plus a summary of
    the billing rules and attachment filenames. It returns this object.

    Business rule reminders (also in the system prompt):
        * @gncgroup.ca ↔ @gncgroup.ca emails are NON_BILLABLE by default,
          UNLESS the email assigns a phone call TASK to another GNC teammate,
          in which case it is CALLING_TASK.
        * Automated messages (out-of-office, calendar, delivery receipts)
          are always SPAM_AUTOMATED regardless of sender.
        * If the AI is not sure which category applies, it must pick UNCLEAR
          — never guess.
    """
    model_config = ConfigDict(extra="forbid")

    # ---- Classification --------------------------------------------------
    classification: EMAIL_CLASSIFICATION = Field(
        description=(
            "The billing category for this email. "
            "BILLABLE = normal external client communication. "
            "NON_BILLABLE = internal @gncgroup.ca comms with no billable content. "
            "CALLING_TASK = an internal email that ASSIGNS A PHONE CALL TASK to "
            "another GNC teammate (per client's business rule). "
            "SPAM_AUTOMATED = automated system messages (calendar invites, "
            "auto-replies, delivery receipts, newsletters). "
            "UNCLEAR = you cannot confidently pick one of the above — human must review."
        )
    )

    # ---- Billing rule --------------------------------------------------
    billing_rule_code: str | None = Field(
        default=None,
        description=(
            "The exact rule CODE from the billing rules table that applies "
            "(e.g. 'EMAIL_SHORT', 'DOC_REVIEW_STD', 'RCV_ACV', 'SITE_VISIT'). "
            "Use exactly the codes listed in the system prompt. "
            "Null if classification is NON_BILLABLE / SPAM_AUTOMATED / UNCLEAR."
        ),
        max_length=50,
    )

    # ---- Quantity for billing ------------------------------------------
    quantity: Quantity | None = Field(
        default=None,
        description=(
            "The measurable quantity for this billing rule. Null if "
            "classification is not BILLABLE / CALLING_TASK."
        ),
    )

    # ---- Special fields for RCV / Budget Reserve rules ------------------
    estimate_amount_usd: float | None = Field(
        default=None,
        description=(
            "For RCV_ACV or BUDGET_RESERVE rules ONLY: the total RCV/ACV "
            "dollar amount mentioned in the email (used for proration: "
            "6 hrs per $500K). Null otherwise. Extract as a plain number "
            "with no currency symbol, e.g. 1277422.16 for $1,277,422.16."
        ),
        ge=0,
    )
    building_count: int | None = Field(
        default=None,
        description=(
            "For RCV_ACV or BUDGET_RESERVE rules ONLY: how many DISTINCT "
            "buildings the estimate covers. Defaults to 1 if not explicitly "
            "stated. Null for other rules."
        ),
        ge=1,
    )

    # ---- Text outputs ---------------------------------------------------
    summary: str = Field(
        description=(
            "A concise 1-3 sentence summary of what this email is about "
            "and why it matters for billing. Written for a busy reviewer."
        ),
        max_length=1000,
    )
    invoice_description: str = Field(
        description=(
            "A one-line professional description suitable for the invoice "
            "'Description' column. Keep under 15 words. Example: "
            "'Review of RCV Estimate v2 (revised O&P allowance)'."
        ),
        max_length=200,
    )

    # ---- Confidence + reasoning ----------------------------------------
    confidence: int = Field(
        description=(
            "How confident you are (0-100) that the classification, rule, "
            "and quantity are correct. Below 70 will be flagged for manual "
            "review. If you had to guess, use 40 or lower and explain in "
            "reasoning."
        ),
        ge=0, le=100,
    )
    reasoning: str = Field(
        description=(
            "2-4 sentences explaining WHY you chose this classification and "
            "billing rule. Cite specific phrases from the email that led "
            "you to this decision. This is the audit trail."
        ),
        max_length=1500,
    )
    warnings: list[str] = Field(
        default_factory=list,
        description=(
            "Any edge cases or concerns a human reviewer should double-check. "
            "Examples: 'Email mentions two claim numbers — verify which one to bill'; "
            "'Sender's role unclear — could be adjuster or contractor'; "
            "'RCV amount seems unusually high — verify against report'."
        ),
        max_length=10,
    )
    requires_manual_review: bool = Field(
        description=(
            "True if this analysis should NOT auto-populate the invoice — the "
            "reviewer must confirm every field before including. Set this "
            "true when confidence < 70, when warnings has entries, or when "
            "classification is UNCLEAR."
        )
    )


# ---------------------------------------------------------------------------
# Key facts block — typed extraction for enrichment
# ---------------------------------------------------------------------------
# WHY typed instead of dict[str, Any]:
#     OpenAI strict-mode json_schema rejects free-form dicts —
#     `additionalProperties: true` isn't allowed and empty-properties
#     objects fail validation ("Extra required key 'key_facts' supplied").
#     A concrete sub-model gives the AI a defined shape AND lets our
#     enrichment code use typed access instead of dict.get() everywhere.
class KeyFacts(BaseModel):
    """Facts the AI can pull from a document, used to auto-populate the
    client + claim + insured records. Every field is optional — the AI
    fills only what it can confidently extract."""
    model_config = ConfigDict(extra="forbid")

    insured_name: str | None = Field(
        default=None,
        description="The homeowner / policyholder name (e.g. 'John Doe').",
        max_length=255,
    )
    client_name: str | None = Field(
        default=None,
        description=(
            "The GNC CLIENT that engaged us — usually the ADJUSTING FIRM "
            "(e.g. 'Kendal Adjusters Inc.'), NOT the insurance carrier "
            "(e.g. 'Peace Hills Insurance') and NOT the insured."
        ),
        max_length=255,
    )
    claim_no: str | None = Field(
        default=None,
        description="Insurance carrier's claim number, e.g. '000-00-055683'.",
        max_length=100,
    )
    gnc_file_no: str | None = Field(
        default=None,
        description="GNC's internal file number, e.g. '2413BA'.",
        max_length=100,
    )
    adjuster_file_no: str | None = Field(
        default=None,
        description="Adjusting firm's file number, e.g. '26-032939'.",
        max_length=100,
    )
    date_of_loss: str | None = Field(
        default=None,
        description="ISO date (YYYY-MM-DD) of the loss event, if stated.",
        max_length=10,
    )
    total_rcv: float | None = Field(
        default=None,
        description="Total Replacement Cost Value in USD, e.g. 1277422.16.",
        ge=0,
    )
    total_acv: float | None = Field(
        default=None,
        description="Total Actual Cash Value in USD, if reported.",
        ge=0,
    )
    op_percent: float | None = Field(
        default=None,
        description="Overhead & Profit percentage (as number 0-100).",
        ge=0, le=100,
    )


# ---------------------------------------------------------------------------
# Attachment analysis
# ---------------------------------------------------------------------------
class AttachmentAnalysisOutput(BaseModel):
    """Structured AI output for ONE attachment.

    The AI receives: filename, extension, page_count (already extracted
    by extraction_service), file_size, and up to 60KB of extracted text.
    It returns this object.
    """
    model_config = ConfigDict(extra="forbid")

    # ---- Document type (semantic, not filetype) ------------------------
    document_type: Literal[
        "RCV_ESTIMATE",         # RCV / ACV / dollar estimate
        "COST_REPORT",          # detailed report of costs
        "XACTIMATE_SKETCH",     # Xactimate sketch / floor plan
        "XACTIMATE_ESTIMATE",   # Xactimate line-by-line estimate
        "SOW_EXCEL",            # Scope of Work in Excel template
        "SOW_XACTIMATE",        # Scope of Work in Xactimate format
        "SPEC_SHEET",           # Specification sheet with line items
        "BID_DEMOLITION",       # Demolition bid from vendor
        "BID_RECONSTRUCTION",   # Reconstruction bid
        "PHOTO",                # Photograph (site visit documentation)
        "HMI_REPORT",           # Hazardous Material Inventory report
        "STRUCTURAL_REPORT",    # Structural inspection report
        "PAYMENT_RECOMMENDATION_REPORT",   # descriptive report
        "INITIAL_REPORT",       # first assessment report
        "ASSIGNMENT_EMAIL",     # forwarded email (.eml, letter of assignment)
        "OTHER",                # doesn't fit any of the above
    ] = Field(
        description=(
            "Semantic type of the document, based on filename and content. "
            "This determines which billing rule applies. Photos usually "
            "don't drive billing directly — they support Site Visit hours."
        )
    )

    # ---- Billing rule --------------------------------------------------
    billing_rule_code: str | None = Field(
        default=None,
        description=(
            "Rule CODE that applies (e.g. 'DOC_REVIEW_STD', 'RCV_ACV', "
            "'XACT_SKETCH', 'SOW_EXCEL'). Null if not billable (e.g. photos, "
            "assignment emails)."
        ),
        max_length=50,
    )

    quantity: Quantity | None = Field(
        default=None,
        description=(
            "Measurable quantity for the rule. For document-review rules "
            "the value is the page count. For SoW/report rules it is also "
            "the page count. For XACT_SKETCH it is the number of floors. "
            "Null when not billable."
        ),
    )

    # ---- RCV-specific ---------------------------------------------------
    estimate_amount_usd: float | None = Field(
        default=None,
        description=(
            "For RCV_ESTIMATE documents: the total RCV amount shown in the "
            "document (excluding O&P and taxes). Extract as plain number, "
            "no currency symbol. Null for other document types."
        ),
        ge=0,
    )
    building_count: int | None = Field(
        default=None,
        description=(
            "For RCV_ESTIMATE / COST_REPORT: number of distinct buildings "
            "covered. Defaults to 1 if not stated."
        ),
        ge=1,
    )

    # ---- Text outputs ---------------------------------------------------
    summary: str = Field(
        description=(
            "1-4 sentence summary of the document's contents. Focus on: "
            "type of document, key numbers/dates/parties, why it matters."
        ),
        max_length=1500,
    )
    invoice_description: str = Field(
        description=(
            "One-line description for the invoice. Under 15 words. "
            "Example: 'Review of Replacement Cost Value Report (13 pages)'."
        ),
        max_length=200,
    )

    key_facts: KeyFacts = Field(
        default_factory=KeyFacts,
        description=(
            "Extracted facts used to auto-populate the client + claim + "
            "insured records. Fill only the fields you can confidently "
            "extract; leave the rest null. See KeyFacts definition for "
            "field semantics — especially client_name (the adjusting firm, "
            "NOT the carrier or the insured)."
        ),
    )

    # ---- Confidence + reasoning ----------------------------------------
    confidence: int = Field(
        description="Confidence in classification + rule + quantity (0-100).",
        ge=0, le=100,
    )
    reasoning: str = Field(
        description=(
            "Why this document type and rule? Cite specific text from the "
            "document (e.g. 'Filename contains RCV and the text shows a "
            "total of $1,277,422.16')."
        ),
        max_length=1500,
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Edge cases needing manual review.",
        max_length=10,
    )
    requires_manual_review: bool = Field(
        description="True if human must confirm before billing this document."
    )


# ---------------------------------------------------------------------------
# Helper — schema for OpenAI's strict mode
# ---------------------------------------------------------------------------
def openai_response_format(model: type[BaseModel], name: str) -> dict[str, Any]:
    """Build the `response_format` block for OpenAI chat completions.

    `strict=True` guarantees the returned JSON matches the schema — no
    hallucinated fields, no missing required ones. It also forbids
    additional properties, which is why our models set `extra="forbid"`.
    """
    schema = model.model_json_schema()
    # OpenAI's strict mode requires `additionalProperties: false` at
    # every object level and `required` covering every property.
    _make_strict(schema)
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": schema,
        },
    }


def _make_strict(schema: dict[str, Any]) -> None:
    """Recursively enforce OpenAI's strict-mode invariants on a JSON schema."""
    if schema.get("type") == "object" or "properties" in schema:
        schema.setdefault("additionalProperties", False)
        props = schema.get("properties") or {}
        # All defined properties must be in `required` for strict mode.
        schema["required"] = list(props.keys())
        for sub in props.values():
            _make_strict(sub)
    for key in ("items", "additionalProperties"):
        val = schema.get(key)
        if isinstance(val, dict):
            _make_strict(val)
    for key in ("anyOf", "oneOf", "allOf"):
        for sub in schema.get(key) or []:
            _make_strict(sub)
    # Resolve $defs so strict rules also apply inside them.
    for sub in (schema.get("$defs") or {}).values():
        _make_strict(sub)

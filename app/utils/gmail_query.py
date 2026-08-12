"""
Build Gmail search queries from user input.

Gmail's search syntax is well-documented (google.com/search/help/gmail).
We use a small subset:
    * quoted phrases  — for exact matching of claim numbers and file names
    * subject:...     — restrict a term to subject
    * from:...        — restrict by sender
    * has:attachment  — attachments only
    * newer_than:Nd   — last N days
    * OR              — MUST be capitalized in Gmail

Design principle: whitelist what we send. Never pass raw user input into
the query — it could inject `is:starred` and change semantics silently.
We accept only alphanumerics, dash, underscore, slash, and dot in identifiers.
"""
from __future__ import annotations

import re

_SAFE_IDENTIFIER = re.compile(r"[^A-Za-z0-9._/\- ]")


def _sanitize(term: str) -> str:
    """Strip any characters that could change the Gmail query's meaning."""
    return _SAFE_IDENTIFIER.sub("", (term or "").strip())


def build_search_query(
    claim_no: str | None = None,
    file_name: str | None = None,
    gnc_file_no: str | None = None,
    newer_than_days: int | None = None,
    include_body: bool = True,
) -> str:
    """
    Compose a Gmail `q=` parameter.

    Matches when ANY of the supplied identifiers appears in the message
    (subject or body). At least one identifier must be provided.

    Example:
        build_search_query(claim_no="123-45", file_name="Smith Fire")
        →  '("123-45" OR "Smith Fire")'
    """
    tokens: list[str] = []
    for term in (claim_no, file_name, gnc_file_no):
        clean = _sanitize(term or "")
        if clean:
            tokens.append(f'"{clean}"')

    if not tokens:
        raise ValueError("At least one identifier (claim_no / file_name / gnc_file_no) required.")

    query = f"({' OR '.join(tokens)})" if len(tokens) > 1 else tokens[0]

    if not include_body:
        # Wrap terms in subject: to restrict — Gmail applies to each token.
        query = " OR ".join(f"subject:{t}" for t in tokens)
        query = f"({query})"

    if newer_than_days and newer_than_days > 0:
        query += f" newer_than:{int(newer_than_days)}d"

    return query


def is_internal_email(
    from_email: str,
    to_emails: list[str],
    cc_emails: list[str],
    internal_domain: str,
) -> bool:
    """
    Return True if EVERY participant is on the internal domain.

    Per business rule (2026-08-01 client note): emails where the entire
    conversation is between @gncgroup.ca addresses are still stored, but
    won't count toward billable hours. If even one external party is
    involved, it's a normal (billable) email.
    """
    domain = "@" + internal_domain.lstrip("@").lower()
    all_parties = [from_email or "", *to_emails, *cc_emails]
    if not all_parties:
        return False
    return all(p.lower().endswith(domain) for p in all_parties if p)

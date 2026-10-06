"""AgentCore Platform v1.0"""

# LOG-C2-011 — State schema and shared domain contracts.
#
# State is a flat TypedDict — never a Pydantic BaseModel. LangGraph checkpoints
# are serialised with msgpack, and Pydantic objects corrupt silently there.
# Extend AgentState with agent-specific fields only; never add credentials,
# secrets or Pydantic models.
#
# Serialisation contract: every dict/list-valued field is stored as a
# JSON-serialised Optional[str]. Use to_json() / from_json() at every producer
# and consumer node — one contract end to end. Never type a dict/list field as
# a bare dict/list.
#
# This module also holds the two domain-wide numeric contracts, so producer and
# gate share one definition:
#   finite_in_range()   — the parser every caller-supplied number goes through
#   to_external_grid()  — the rounding grid the external declaration renders on

import json
import math
import re
from typing import Any, NotRequired, Optional

from framework.schemas.agent_state import AgentState

# The external declaration draft states monetary figures as aggregates rounded
# to the nearest 1,000 of the declaration currency. The renderer rounds onto
# this grid and the output gate independently enforces it, so both sides read
# the same constant.
EXTERNAL_ROUND_UNIT = 1000


def to_json(value: Any) -> Optional[str]:
    """Serialize a value to a JSON string for State storage."""
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def from_json(value: Optional[str], default: Any = None) -> Any:
    """Deserialize a JSON string from State storage."""
    if value is None:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


# Caller-supplied strings that are rendered into the declaration draft are
# locked to this inert identifier grammar: 1-64 characters from
# [A-Za-z0-9._-], with at least one non-digit. The non-digit requirement is not
# cosmetic — a bare digit run is indistinguishable from a monetary figure at the
# output boundary, where the precision gate would rewrite it, so a reference
# that could be mangled is rejected at the door instead.
_INERT_IDENTIFIER_RE = re.compile(r"^(?=.*[A-Za-z_.-])[A-Za-z0-9._-]{1,64}$")


def is_inert_identifier(value: Any) -> bool:
    """True when *value* is a caller string safe to render verbatim."""
    return isinstance(value, str) and bool(_INERT_IDENTIFIER_RE.match(value))


def finite_in_range(value: Any, lo: float, hi: float) -> Optional[float]:
    """Parse a caller-controlled number: a FINITE float within [lo, hi], else None.

    Rejects bools, non-numeric types, and — critically — non-finite values.
    ``float()`` happily parses "NaN"/"Infinity", Python's ``json`` accepts bare
    ``NaN`` in a request body, and every IEEE comparison against NaN is False:
    a NaN threshold would silently pass every bounds check and suppress the very
    determination this template exists to make. Every caller-supplied number
    goes through here, and a rejection fails CLOSED.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or not lo <= parsed <= hi:
        return None
    return parsed


def to_external_grid(value: float) -> int:
    """Round a monetary figure onto the external declaration grid.

    The declaration renders monetary aggregates in units of EXTERNAL_ROUND_UNIT;
    rendering through this helper is what keeps the rendered text and the output
    gate's enforcement in agreement.
    """
    return int(round(value / EXTERNAL_ROUND_UNIT) * EXTERNAL_ROUND_UNIT)


class State(AgentState):
    """Flat TypedDict for LOG-C2-011.

    All shared fields (user_input, status, session_id, node_history,
    error_log, hitl_*, etc.) are inherited from AgentState.

    dict/list fields use JSON-serialized Optional[str].
    formatted_output is NOT re-declared here — it is inherited from AgentState.
    """

    # ------------------------------------------------------------------
    # Outer layer — set by PreProcessNode (pre_process backbone)
    # ------------------------------------------------------------------

    # Validated and normalised JSON string of the shipment payload.
    # Produced by PreProcessNode; consumed by inner InputValidateNode.
    validated_input: NotRequired[Optional[str]]

    # JSON-serialised channel/request metadata dict (stored as str).
    # Shape: {"source": str, "channel": str, "shipment_id": str}
    enriched_context: NotRequired[Optional[str]]

    # ------------------------------------------------------------------
    # Inner layer — domain nodes (DomainWorkflowGraph)
    # ------------------------------------------------------------------

    # JSON-serialised declaration options accepted from the caller's
    # input_context, after validation (stored as str).
    # Shape: {broker_review_threshold: float, value_tier_high: float,
    #   value_tier_standard: float, declaration_reference: str}
    declaration_options: NotRequired[Optional[str]]

    # JSON-serialised parsed shipment payload (stored as str).
    # Shape: {shipment_id, exporter (dict), importer (dict),
    #   goods (list of {description, hs_code, quantity, unit_value,
    #   net_weight_kg, country_of_origin, line_value}), transport (dict),
    #   incoterms (str), currency (str), total_declared_value (float),
    #   package_count (int), total_quantity (int), total_net_weight_kg (float),
    #   hs_chapters (list[str]), value_tier (str)}
    shipment_data: NotRequired[Optional[str]]

    # JSON-serialised customs-declaration section dict (stored as str).
    # Keys match the 5 customs declaration sections:
    #   hs_code_classification, country_of_origin, declared_value,
    #   package_details, compliance_declarations
    # Each value is the rendered text for that section.
    customs_sections: NotRequired[Optional[str]]

    # JSON-serialised Japan Customs Act compliance check result (stored as str).
    # Shape: {broker_review_required: bool, reason: str, flags: list[str],
    #   invalid_hs_codes: list[str], restricted_goods: list[str],
    #   total_declared_value: float, currency: str, value_threshold: float}
    compliance_flags: NotRequired[Optional[str]]

    # True if a licensed customs broker must review before submission.
    # Criteria: restricted/prohibited goods, invalid HS codes, missing
    # country of origin, or declared value at/above the review threshold.
    broker_review_required: NotRequired[Optional[bool]]

    # Final formatted customs declaration document (plain text, draft).
    # Assembled by inner OutputFormatNode from customs_sections + compliance_flags.
    customs_document: NotRequired[Optional[str]]

    # ------------------------------------------------------------------
    # Outer layer — set by PostProcessNode (post_process backbone)
    # ------------------------------------------------------------------

    # Primary result surfaced to the caller.
    # Set to the same content as customs_document after the output gate passes.
    # formatted_output (from AgentState) is also set by PostProcessNode.
    result: NotRequired[Optional[str]]

    # ------------------------------------------------------------------
    # Tracing / audit — framework-managed; do NOT write from node code
    # ------------------------------------------------------------------

    trace_id: NotRequired[Optional[str]]
    correlation_id: NotRequired[Optional[str]]
    error_code: Optional[str]
    # node_history inherited from AgentState

"""AgentCore Platform v1.0"""

# LOG-C2-011 — ComplianceCheckNode
# Inner domain node 4: Japan Customs Act (関税法) compliance pre-check.
#
# Determines whether the draft customs declaration must be reviewed by a
# licensed customs broker before official submission. Rule-based.
#
# Broker-review criteria (any one triggers review):
#   - an HS code fails the NNNN.NN structural format check
#   - a goods line is missing its country of origin
#   - a goods description matches a restricted/prohibited-goods keyword
#   - the total declared value is at/above the review threshold
#
# The review threshold is the declaration's own default unless the caller
# supplied one on input_context, in which case the validated caller value is
# used. It is applied on the external rounding grid, so the threshold this node
# states in the document is exactly the threshold it applied.
#
# Inner node — ANONYMOUS trust.
# Returns ONLY the state keys this node writes (partial-dict contract).

import logging
import re
from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.nodes.input_validate_node import DEFAULT_BROKER_REVIEW_THRESHOLD
from src.schemas.state import finite_in_range, from_json, to_external_grid, to_json

logger = logging.getLogger(__name__)

# Structural HS-code format: 4 digits, optional dot-separated subheadings
# (e.g. 6109, 6109.10, 6109.10.00). Not a tariff-database lookup — format only.
_HS_CODE_RE = re.compile(r"^\d{4}(?:\.\d{2}){0,2}$")

# Restricted / controlled-goods keywords (indicative, not exhaustive) — a
# match escalates to mandatory broker review under the Japan Customs Act.
_RESTRICTED_KEYWORDS = (
    "firearm",
    "weapon",
    "ammunition",
    "explosive",
    "narcotic",
    "drug",
    "ivory",
    "endangered",
    "cultural property",
    "currency",
    "tobacco",
    "alcohol",
    "pharmaceutical",
    "hazardous",
)


def _find_restricted(description: str) -> Optional[str]:
    """Return the first restricted keyword found in the description, or None."""
    low = description.lower()
    for kw in _RESTRICTED_KEYWORDS:
        if kw in low:
            return kw
    return None


def _format_amount(value: float, currency: str) -> str:
    """Render a monetary figure for the declaration: an aggregate on the grid."""
    return f"{to_external_grid(value):,d} {currency}"


def _effective_threshold(options: Dict[str, Any]) -> float:
    """Return the review threshold to apply, snapped onto the external grid.

    Defence in depth: the caller's value was validated at the input boundary, so
    a non-finite value here means state was tampered with between nodes — the
    declared default is used instead of trusting it. Snapping to the grid keeps
    the threshold the document states identical to the threshold applied.
    """
    parsed = finite_in_range(options.get("broker_review_threshold", DEFAULT_BROKER_REVIEW_THRESHOLD), 0, 1e12)
    if parsed is None:
        parsed = DEFAULT_BROKER_REVIEW_THRESHOLD
    return float(to_external_grid(parsed))


def _evaluate_compliance(shipment_data: Dict[str, Any], threshold: float) -> Dict[str, Any]:
    """Evaluate broker-review criteria; return a compliance result dict."""
    goods: List[Dict[str, Any]] = shipment_data.get("goods", [])
    currency = shipment_data.get("currency", "USD")
    total_value = float(shipment_data.get("total_declared_value", 0.0))

    invalid_hs_codes: List[str] = []
    restricted_goods: List[str] = []
    missing_origin: List[str] = []

    for g in goods:
        hs_code = str(g.get("hs_code", ""))
        if not _HS_CODE_RE.match(hs_code):
            invalid_hs_codes.append(hs_code or "(empty)")
        if not g.get("country_of_origin"):
            missing_origin.append(str(g.get("description", "N/A")))
        restricted = _find_restricted(str(g.get("description", "")))
        if restricted:
            restricted_goods.append(f"{g.get('description', 'N/A')} ({restricted})")

    value_threshold_met = total_value >= threshold

    flags: List[str] = []
    if invalid_hs_codes:
        flags.append(f"invalid HS code format: {', '.join(invalid_hs_codes)}")
    if missing_origin:
        flags.append(f"missing country of origin: {', '.join(missing_origin)}")
    if restricted_goods:
        flags.append(f"restricted/controlled goods: {', '.join(restricted_goods)}")
    if value_threshold_met:
        flags.append(
            f"declared value ({_format_amount(total_value, currency)}) "
            f">= review threshold ({_format_amount(threshold, currency)})"
        )

    broker_review_required = bool(flags)
    reason = (
        "; ".join(flags)
        if flags
        else (
            "No broker-review triggers detected (routine consignment); a licensed "
            "customs broker should still confirm the final declaration."
        )
    )

    return {
        "broker_review_required": broker_review_required,
        "reason": reason,
        "flags": flags,
        "invalid_hs_codes": invalid_hs_codes,
        "restricted_goods": restricted_goods,
        "missing_origin": missing_origin,
        # Monetary figures in this result are stated on the same external
        # rounding grid as the declaration itself.
        "total_declared_value": to_external_grid(total_value),
        "currency": currency,
        "value_threshold": to_external_grid(threshold),
        "regulatory_basis": "関税法 (Japan Customs Act) import/export procedures",
    }


class ComplianceCheckNode(FunctionNode):
    """Japan Customs Act broker-review pre-check for LOG-C2-011.

    Evaluates whether the draft declaration needs licensed-broker review and
    produces a compliance_flags dict with the traceability data the document's
    compliance section renders.

    Inner node — ANONYMOUS trust (see module docstring).

    Input state keys:
        shipment_data:       str  — JSON-serialised enriched shipment payload
        declaration_options: str  — JSON-serialised validated caller options

    Output state keys (partial dict):
        compliance_flags:       str   — JSON-serialised compliance result dict
        broker_review_required: bool  — True if licensed-broker review is required
        status:                 str
        error_log:              list[str]  (only on ERROR)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> dict[str, Any]:
        # A reason settled earlier in the run is the real one: pass it through
        # untouched instead of doing work on input that was already declined.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        shipment_data: Dict[str, Any] = from_json(state.get("shipment_data"), {})

        if not shipment_data:
            logger.error("ComplianceCheckNode: shipment_data missing in state")
            emit_trace_event(
                "compliance_check_failed",
                {"reason": "missing_shipment_data"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ComplianceCheckNode: shipment_data missing in state"],
            }

        options: Dict[str, Any] = from_json(state.get("declaration_options"), {}) or {}
        threshold = _effective_threshold(options)

        shipment_id = shipment_data.get("shipment_id", "unknown")
        compliance_result = _evaluate_compliance(shipment_data, threshold)
        broker_review_required = bool(compliance_result["broker_review_required"])

        logger.info(
            "ComplianceCheckNode: shipment_id=%s broker_review_required=%s flags=%d",
            shipment_id,
            broker_review_required,
            len(compliance_result["flags"]),
        )
        emit_trace_event(
            "compliance_check_complete",
            {
                "shipment_id": shipment_id,
                "broker_review_required": broker_review_required,
                "flag_count": len(compliance_result["flags"]),
            },
            state,
        )

        return {
            "compliance_flags": to_json(compliance_result),
            "broker_review_required": broker_review_required,
            "status": AgentStatus.SUCCESS.value,
        }

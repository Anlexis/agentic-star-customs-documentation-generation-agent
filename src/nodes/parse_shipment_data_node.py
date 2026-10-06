"""AgentCore Platform v1.0"""

# LOG-C2-011 — ParseShipmentDataNode
# Inner domain node 2: enrich and aggregate the validated shipment data.
#
# Responsibilities:
#   - aggregate total declared value, quantity, weight, package count
#   - derive the set of HS chapters (first 2 digits of each HS code)
#   - classify a value tier against the declaration's tier thresholds
#   - annotate shipment_data with the derived fields downstream nodes consume
#
# Every number reaching this node has already passed the bounded, finite parser
# in InputValidateNode, and the tier thresholds come from the validated
# declaration options — so the aggregation here is arithmetic on trusted values.
#
# Inner node — ANONYMOUS trust.
# Returns ONLY the state keys this node writes (partial-dict contract).

import logging
from typing import Any, ClassVar, Dict, List

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.nodes.input_validate_node import (
    DEFAULT_VALUE_TIER_HIGH,
    DEFAULT_VALUE_TIER_STANDARD,
)
from src.schemas.state import finite_in_range, from_json, to_json

logger = logging.getLogger(__name__)


def _classify_value_tier(total_declared_value: float, high: float, standard: float) -> str:
    """Classify the consignment by total declared value against the tier thresholds."""
    if total_declared_value >= high:
        return "high"
    if total_declared_value >= standard:
        return "standard"
    return "low"


def _hs_chapter(hs_code: str) -> str:
    """Return the 2-digit HS chapter for an HS code (e.g. '6109.10' -> '61')."""
    digits = "".join(ch for ch in str(hs_code) if ch.isdigit())
    return digits[:2] if len(digits) >= 2 else ""


class ParseShipmentDataNode(FunctionNode):
    """Enrich shipment data with aggregates and derived classification fields.

    Inner node — ANONYMOUS trust (see module docstring).

    Input state keys:
        shipment_data:       str  — JSON-serialised shipment payload
        declaration_options: str  — JSON-serialised validated caller options

    Output state keys (partial dict):
        shipment_data: str  — enriched JSON string, same key updated
        status:        str
        error_log:     list[str]  (only on ERROR)
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
            logger.error("ParseShipmentDataNode: shipment_data is empty or missing")
            emit_trace_event(
                "parse_shipment_data_failed",
                {"reason": "missing_shipment_data"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ParseShipmentDataNode: shipment_data missing in state"],
            }

        options: Dict[str, Any] = from_json(state.get("declaration_options"), {}) or {}
        # Defence in depth: the options were validated at the input boundary, so
        # a value that is not finite here means state was tampered with between
        # nodes — fall back to the declared default rather than trusting it.
        tier_high = finite_in_range(options.get("value_tier_high", DEFAULT_VALUE_TIER_HIGH), 0, 1e12)
        tier_standard = finite_in_range(options.get("value_tier_standard", DEFAULT_VALUE_TIER_STANDARD), 0, 1e12)
        high = DEFAULT_VALUE_TIER_HIGH if tier_high is None else tier_high
        standard = DEFAULT_VALUE_TIER_STANDARD if tier_standard is None else tier_standard

        shipment_id = shipment_data.get("shipment_id", "unknown")
        goods: List[Dict[str, Any]] = shipment_data.get("goods", [])

        # -- Aggregate across goods lines --------------------------------------
        total_declared_value = round(sum(float(g.get("line_value", 0.0)) for g in goods), 2)
        total_quantity = sum(int(g.get("quantity", 0)) for g in goods)
        total_net_weight = round(sum(float(g.get("net_weight_kg", 0.0)) for g in goods), 3)
        package_count = len(goods)

        # -- Derive HS chapters (deduplicated, order-preserving) ----------------
        hs_chapters: List[str] = []
        seen: set[str] = set()
        for g in goods:
            chapter = _hs_chapter(g.get("hs_code", ""))
            if chapter and chapter not in seen:
                seen.add(chapter)
                hs_chapters.append(chapter)

        value_tier = _classify_value_tier(total_declared_value, high, standard)

        # -- Enrich shipment_data (local copy — never mutate a shared default) --
        enriched: Dict[str, Any] = dict(shipment_data)
        enriched["total_declared_value"] = total_declared_value
        enriched["total_quantity"] = total_quantity
        enriched["total_net_weight_kg"] = total_net_weight
        enriched["package_count"] = package_count
        enriched["hs_chapters"] = hs_chapters
        enriched["value_tier"] = value_tier

        logger.info(
            "ParseShipmentDataNode: shipment_id=%s packages=%d tier=%s",
            shipment_id,
            package_count,
            value_tier,
        )
        emit_trace_event(
            "parse_shipment_data_complete",
            {
                "shipment_id": shipment_id,
                "package_count": package_count,
                "value_tier": value_tier,
                "hs_chapters": hs_chapters,
            },
            state,
        )

        return {
            "shipment_data": to_json(enriched),
            "status": AgentStatus.SUCCESS.value,
        }

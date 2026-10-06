"""AgentCore Platform v1.0"""

# LOG-C2-011 — GenerateCustomsSectionsNode
# Inner domain node 3: generate all 5 customs-declaration sections.
#
# The generator is DETERMINISTIC and rule-based — no model is invoked. Section
# text is synthesised directly from the enriched shipment_data fields using
# fixed templates.
#
# Reserved model contract (NOT consumed here): config/config.yaml declares an
# `llm` block (system_prompt_template -> prompts/customs_declaration.j2,
# temperature, max_tokens). Those settings are reserved for a future build that
# wires a real model into this node; the current node neither loads the Jinja2
# template nor calls any model. The keys are forwarded to the inner graph by
# CustomsDocumentationGraphNode._parent_config() so they are reachable, and are
# documented rather than faked. A future build must take them via the node
# constructor or via State — never as an extra execute() parameter: the node
# contract is exactly `execute(self, state) -> dict`.
#
# External rendering rules (the schema the output gate enforces):
#   - monetary figures appear ONLY as aggregates, rounded to the nearest 1,000
#     of the declaration currency; per-line values are never rendered;
#   - non-monetary measurements render as unit-suffixed tokens ("1500pcs",
#     "300kg") so they are structurally distinct from monetary figures and are
#     never mistaken for one at the output boundary.
#
# Sections produced:
#   1. hs_code_classification
#   2. country_of_origin
#   3. declared_value
#   4. package_details
#   5. compliance_declarations
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

from src.schemas.state import EXTERNAL_ROUND_UNIT, from_json, to_external_grid, to_json

logger = logging.getLogger(__name__)

# Printed inside the Declared Value section so a reader of the declaration
# knows the precision the figures are stated at.
_PRECISION_NOTE = (
    f"  NOTE: monetary figures on this declaration are stated as aggregates "
    f"rounded to the nearest {EXTERNAL_ROUND_UNIT:,d} of the declaration "
    f"currency; per-line values are not rendered on this draft."
)


def _section_hs_code_classification(d: Dict[str, Any]) -> str:
    """Generate the HS Code Classification section."""
    goods: List[Dict[str, Any]] = d.get("goods", [])
    if not goods:
        return "No goods lines to classify."
    lines = []
    for i, g in enumerate(goods, start=1):
        lines.append(
            f"  {i}. HS {g.get('hs_code', 'N/A')}  —  {g.get('description', 'N/A')} "
            f"(qty {int(g.get('quantity', 0))}pcs)"
        )
    chapters = ", ".join(d.get("hs_chapters", [])) or "N/A"
    lines.append(f"  HS Chapters covered: {chapters}")
    return "\n".join(lines)


def _section_country_of_origin(d: Dict[str, Any]) -> str:
    """Generate the Country of Origin section."""
    goods: List[Dict[str, Any]] = d.get("goods", [])
    lines = []
    for i, g in enumerate(goods, start=1):
        origin = g.get("country_of_origin") or "UNDECLARED"
        lines.append(f"  {i}. {g.get('description', 'N/A')} — Origin: {origin}")
    exporter_country = (d.get("exporter") or {}).get("country", "N/A")
    importer_country = (d.get("importer") or {}).get("country", "N/A")
    lines.append(f"  Exporting country: {exporter_country}")
    lines.append(f"  Importing country: {importer_country}")
    return "\n".join(lines)


def _section_declared_value(d: Dict[str, Any]) -> str:
    """Generate the Declared Value section — aggregates only, on the external grid."""
    currency = d.get("currency", "USD")
    total = to_external_grid(float(d.get("total_declared_value", 0.0)))
    lines = [
        f"  Total declared value: {total:,d} {currency}",
        f"  Declaration currency: {currency}.",
        f"  Incoterms: {d.get('incoterms') or 'N/A'}.",
        _PRECISION_NOTE,
    ]
    return "\n".join(lines)


def _section_package_details(d: Dict[str, Any]) -> str:
    """Generate the Package Details section.

    Counts and measurements render as unit-suffixed tokens so they stay
    structurally distinct from the monetary aggregates above.
    """
    transport: Dict[str, Any] = d.get("transport") or {}
    lines = [
        f"  Number of packages/line items: {d.get('package_count', 0)}",
        f"  Total quantity: {int(d.get('total_quantity', 0))}pcs",
        f"  Total net weight: {int(round(float(d.get('total_net_weight_kg', 0.0))))}kg",
        f"  Transport mode: {transport.get('mode') or 'N/A'}",
        f"  Port of loading: {transport.get('port_of_loading') or 'N/A'}",
        f"  Port of discharge: {transport.get('port_of_discharge') or 'N/A'}",
    ]
    return "\n".join(lines)


def _section_compliance_declarations(d: Dict[str, Any]) -> str:
    """Generate the Compliance Declarations section."""
    exporter = (d.get("exporter") or {}).get("name") or "the exporter"
    lines = [
        f"  I/We, {exporter}, declare that the particulars given in this",
        "  customs declaration are true and complete to the best of our knowledge.",
        "  The goods described conform to the stated HS classification and",
        "  country of origin. This declaration is prepared for submission under",
        "  the Japan Customs Act (関税法) import/export procedures.",
        "  NOTE: This is a machine-generated DRAFT and MUST be reviewed and",
        "  certified by a licensed customs broker before official submission.",
    ]
    return "\n".join(lines)


class GenerateCustomsSectionsNode(FunctionNode):
    """Generate all 5 customs-declaration sections.

    Deterministic template-based synthesis — no model call is made. The
    reserved `llm` block in config/config.yaml is not consumed here (see the
    module docstring).

    Inner node — ANONYMOUS trust (see module docstring).

    Input state keys:
        shipment_data: str  — JSON-serialised enriched shipment payload

    Output state keys (partial dict):
        customs_sections: str  — JSON-serialised section dict
        status:           str
        error_log:        list[str]  (only on ERROR)
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
            logger.error("GenerateCustomsSectionsNode: shipment_data missing in state")
            emit_trace_event(
                "generate_customs_sections_failed",
                {"reason": "missing_shipment_data"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["GenerateCustomsSectionsNode: shipment_data missing in state"],
            }

        shipment_id = shipment_data.get("shipment_id", "unknown")

        # -- Generate all sections ---------------------------------------------
        sections: Dict[str, str] = {
            "hs_code_classification": _section_hs_code_classification(shipment_data),
            "country_of_origin": _section_country_of_origin(shipment_data),
            "declared_value": _section_declared_value(shipment_data),
            "package_details": _section_package_details(shipment_data),
            "compliance_declarations": _section_compliance_declarations(shipment_data),
        }

        logger.info(
            "GenerateCustomsSectionsNode: shipment_id=%s sections=%d",
            shipment_id,
            len(sections),
        )
        emit_trace_event(
            "generate_customs_sections_complete",
            {
                "shipment_id": shipment_id,
                "section_count": len(sections),
                "section_keys": sorted(sections.keys()),
            },
            state,
        )

        return {
            "customs_sections": to_json(sections),
            "status": AgentStatus.SUCCESS.value,
        }

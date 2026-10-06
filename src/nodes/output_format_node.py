"""AgentCore Platform v1.0"""

# LOG-C2-011 — OutputFormatNode (inner domain node 5, last in DomainWorkflowGraph)
# Assembles the final customs declaration document from customs_sections and
# compliance_flags. This is the last inner node — it produces the
# customs_document string the outer PostProcessNode passes through the external
# output gate.
#
# Inner node — ANONYMOUS trust.
# Returns ONLY the state keys this node writes (partial-dict contract).

import logging
from typing import Any, ClassVar, Dict

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json

logger = logging.getLogger(__name__)

# Section order for the final document (customs-declaration recommended order).
_SECTION_ORDER = [
    "hs_code_classification",
    "country_of_origin",
    "declared_value",
    "package_details",
    "compliance_declarations",
]

# Human-readable section headers.
_SECTION_HEADERS: Dict[str, str] = {
    "hs_code_classification": "1. HS Code Classification",
    "country_of_origin": "2. Country of Origin",
    "declared_value": "3. Declared Value",
    "package_details": "4. Package Details",
    "compliance_declarations": "5. Compliance Declarations",
}

_SEPARATOR = "=" * 72
_SUBSEP = "-" * 72


def _assemble_document(
    shipment_id: str,
    declaration_reference: str,
    sections: Dict[str, str],
    compliance: Dict[str, Any],
) -> str:
    """Assemble the full customs declaration document from sections + compliance."""
    lines = [
        _SEPARATOR,
        "CUSTOMS DECLARATION (DRAFT)",
        f"Shipment ID: {shipment_id}",
    ]
    if declaration_reference:
        lines.append(f"Declaration Reference: {declaration_reference}")
    lines += [_SEPARATOR, ""]

    for key in _SECTION_ORDER:
        header = _SECTION_HEADERS.get(key, key.replace("_", " ").title())
        content = sections.get(key, "(Section not generated)")
        lines.append(header)
        lines.append(_SUBSEP)
        lines.append(content)
        lines.append("")

    # Append the compliance trailer.
    broker_review_required = compliance.get("broker_review_required", False)
    reason = compliance.get("reason", "N/A")
    regulatory = compliance.get(
        "regulatory_basis",
        "関税法 (Japan Customs Act) import/export procedures",
    )
    lines += [
        _SEPARATOR,
        "REGULATORY COMPLIANCE NOTE",
        _SUBSEP,
        f"  Regulatory Basis:          {regulatory}",
        "  Licensed-Broker Review:    "
        + ("REQUIRED before submission" if broker_review_required else "Recommended (routine)"),
        f"  Determination Basis:       {reason}",
        "  DISCLAIMER: This document is a machine-generated DRAFT. It must be",
        "  verified and certified by a licensed customs broker before official",
        "  submission to customs authorities.",
        _SEPARATOR,
    ]

    return "\n".join(lines)


class OutputFormatNode(FunctionNode):
    """Assemble the final customs declaration document (inner domain node).

    Reads customs_sections and compliance_flags from State, renders the full
    draft customs declaration text, and writes it to customs_document (and
    result) for the outer PostProcessNode.

    Inner node — ANONYMOUS trust (see module docstring).

    Input state keys:
        customs_sections: str  — JSON-serialised section dict
        compliance_flags: str  — JSON-serialised compliance result
        shipment_data:    str  — JSON-serialised shipment payload

    Output state keys (partial dict):
        customs_document: str
        result:           str  (same as customs_document — backbone convention)
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
        sections: Dict[str, str] = from_json(state.get("customs_sections"), {})
        compliance: Dict[str, Any] = from_json(state.get("compliance_flags"), {})
        shipment_data: Dict[str, Any] = from_json(state.get("shipment_data"), {})

        shipment_id = shipment_data.get("shipment_id", "unknown")
        declaration_reference = str(shipment_data.get("declaration_reference") or "")

        if not sections:
            logger.error(
                "OutputFormatNode: customs_sections missing in state for shipment_id=%s",
                shipment_id,
            )
            emit_trace_event(
                "output_format_failed",
                {"reason": "missing_customs_sections", "shipment_id": shipment_id},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"OutputFormatNode: customs_sections missing for shipment_id={shipment_id}"],
            }

        # -- Assemble the document ---------------------------------------------
        document = _assemble_document(shipment_id, declaration_reference, sections, compliance)

        logger.info(
            "OutputFormatNode: shipment_id=%s document_chars=%d broker_review_required=%s",
            shipment_id,
            len(document),
            compliance.get("broker_review_required", False),
        )
        emit_trace_event(
            "output_format_complete",
            {
                "shipment_id": shipment_id,
                "document_length": len(document),
                "section_count": len(sections),
                "broker_review_required": compliance.get("broker_review_required", False),
            },
            state,
        )

        return {
            "customs_document": document,
            "result": document,
            "status": AgentStatus.SUCCESS.value,
        }

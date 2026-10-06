# LOG-C2-011 — the outer graph's get_output() surfaces the domain result.
#
# Without the get_output() override on CustomsDocumentationGeneratorAgent,
# CustomsDocumentationGraphNode.merge_output() wrote customs_document,
# customs_sections, compliance_flags and broker_review_required into the OUTER
# state, but AgentBaseGraph.get_output() returns only the minimal
# {output, status, trace_id, correlation_id, node_history} envelope — so a
# successful agent.invoke() dropped every structured domain key to None. This
# compiled-outer-graph test locks in that they are surfaced on the gated success
# path (fail-closed at the output gate).

import json

import pytest

from framework.schemas.agent_status import AgentStatus


def _valid_payload() -> str:
    """A SUCCESS-yielding customs-shipment payload (total value < broker-review
    threshold, valid HS codes, no restricted goods)."""
    return json.dumps(
        {
            "shipment_id": "LOG-SHIP-20260712-001",
            "exporter": {"name": "Kanto Textiles K.K.", "address": "1-2-3 Minato, Tokyo, Japan", "country": "Japan"},
            "importer": {
                "name": "West Coast Imports LLC",
                "address": "500 Market Street, San Francisco, CA",
                "country": "USA",
            },
            "goods": [
                {
                    "description": "Cotton knit T-shirts",
                    "hs_code": "6109.10",
                    "quantity": 1200,
                    "unit_value": 4.5,
                    "net_weight_kg": 180.0,
                    "country_of_origin": "Japan",
                },
                {
                    "description": "Wool blend sweaters",
                    "hs_code": "6110.11",
                    "quantity": 300,
                    "unit_value": 22.0,
                    "net_weight_kg": 120.0,
                    "country_of_origin": "Japan",
                },
            ],
            "transport": {"mode": "sea", "port_of_loading": "Tokyo", "port_of_discharge": "Los Angeles"},
            "incoterms": "FOB",
            "currency": "USD",
        }
    )


class TestOuterGraphGetOutput:
    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        for mod in (
            "pre_process_node",
            "input_validate_node",
            "parse_shipment_data_node",
            "generate_customs_sections_node",
            "compliance_check_node",
            "output_format_node",
            "post_process_node",
        ):
            monkeypatch.setattr(f"src.nodes.{mod}.emit_trace_event", lambda *a, **k: None)

    def _invoke(self):
        from framework.schemas.invocation_context import InvocationContext, TrustLevel
        from src.graph.graph import CustomsDocumentationGeneratorAgent

        agent = CustomsDocumentationGeneratorAgent()
        agent.compile()
        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        return agent.invoke(_valid_payload(), ctx=ctx)

    def test_success_invoke_surfaces_domain_result(self):
        result = self._invoke()
        assert result["status"] == AgentStatus.SUCCESS.value

        # Post-gate, caller-facing document (the output gate already applied).
        assert result["formatted_output"] is not None
        assert result["result"] is not None
        assert result["customs_document"] is not None
        assert "CUSTOMS DECLARATION" in result["customs_document"]
        assert "LOG-SHIP-20260712-001" in result["customs_document"]

        # Structured domain fields — surfaced only on the gated success path,
        # and therefore NOT None here.
        assert result["customs_sections"] is not None
        assert result["compliance_flags"] is not None
        # Routine consignment (< 100,000 threshold, valid HS, no restricted goods).
        assert result["broker_review_required"] is False

    def test_base_envelope_keys_preserved(self):
        """get_output() must extend, not replace, the base envelope."""
        result = self._invoke()
        for key in (
            "output",
            "status",
            "customs_document",
            "customs_sections",
            "compliance_flags",
            "broker_review_required",
            "formatted_output",
            "result",
        ):
            assert key in result, f"get_output() dropped expected key: {key}"

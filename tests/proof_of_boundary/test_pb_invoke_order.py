# PB-6: Invoke Execution Order Verification
# Verifies BaseNode.__call__() enforces: trust gate -> node_start audit ->
# _security_gate_input() -> execute() -> _security_gate_output() ->
# node_complete audit, for every concrete node under src/nodes/.
#
# Also verifies the full backbone invoke order for the outer
# CustomsDocumentationGeneratorAgent (Cat 2 two-layer nested graph):
#   InitializeNode -> PreProcessNode (pre_process) -> CustomsDocumentationGraphNode (main)
#   -> PostProcessNode (post_process) -> FinalizeNode
#
# PB-6 invoke uses VERIFIED_EXTERNAL caller trust (the real external path) — NEVER
# for_internal(). A VERIFIED_EXTERNAL InvocationContext exercises the same code path a
# real deployed caller uses: it clears the outer PreProcessNode trust gate
# (required_trust_level = VERIFIED_EXTERNAL) AND passes through the inner ANONYMOUS
# domain nodes. for_internal() (INTERNAL) would not represent a real external caller,
# so it is deliberately not used.

import importlib
import inspect
import json
import pkgutil
from pathlib import Path

import pytest

# ── Template-specific constants ───────────────────────────────────────────────

# Class name of the node in the `main` backbone slot.
_MAIN_SLOT_NODE = "CustomsDocumentationGraphNode"

# A SUCCESS-yielding customs-shipment payload for the backbone invoke test.
# All PreProcessNode required fields present (shipment_id, goods, exporter,
# importer) and every goods line carries description + valid NNNN.NN HS code +
# numeric quantity/unit_value/net_weight_kg + country_of_origin. Total declared
# value (12,000 USD) is below the 100,000 broker-review threshold, HS codes are
# structurally valid, and no restricted-goods keyword matches — so the pipeline
# runs clean to SUCCESS with broker_review_required=False.
#
# CONTRACT: deploy/invoke_payload.json["input"] MUST equal this exact string —
# the deployment smoke-test invoke and the PB-6 test must exercise the
# identical payload. test_invoke_payload_matches_pb6 below asserts that
# equality so the two can never drift.
_VALID_PAYLOAD = json.dumps(
    {
        "shipment_id": "LOG-SHIP-20260712-001",
        "exporter": {
            "name": "Kanto Textiles K.K.",
            "address": "1-2-3 Minato, Tokyo, Japan",
            "country": "Japan",
        },
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
        "transport": {
            "mode": "sea",
            "port_of_loading": "Tokyo",
            "port_of_discharge": "Los Angeles",
        },
        "incoterms": "FOB",
        "currency": "USD",
    }
)

# ─────────────────────────────────────────────────────────────────────────────


def _discover_node_classes() -> list[type]:
    """Import every module under src/nodes/ and collect concrete BaseNode subclasses."""
    from framework.nodes.base_node import BaseNode

    try:
        pkg = importlib.import_module("src.nodes")
    except ImportError:
        return []

    discovered = []
    for _, modname, _ in pkgutil.walk_packages(pkg.__path__, prefix="src.nodes."):
        module = importlib.import_module(modname)
        for attr in vars(module).values():
            if (
                isinstance(attr, type)
                and issubclass(attr, BaseNode)
                and attr is not BaseNode
                and attr.__module__ == modname
                and not inspect.isabstract(attr)
            ):
                discovered.append(attr)
    return discovered


def _patch_domain_emit(monkeypatch):
    """Patch emit_trace_event in every domain node module (avoids audit-backend calls)."""
    for mod_suffix in (
        "pre_process_node",
        "input_validate_node",
        "parse_shipment_data_node",
        "generate_customs_sections_node",
        "compliance_check_node",
        "output_format_node",
        "post_process_node",
    ):
        try:
            monkeypatch.setattr(
                f"src.nodes.{mod_suffix}.emit_trace_event",
                lambda *a, **k: None,
            )
        except AttributeError:
            pass  # module not yet imported / no emit symbol; fine


class TestInvokeOrder:
    """PB-6: __call__ must run trust gate -> node_start -> input gate ->
    execute() -> output gate -> node_complete."""

    def test_call_order_for_every_node(self, monkeypatch):
        _patch_domain_emit(monkeypatch)

        node_classes = _discover_node_classes()
        if not node_classes:
            pytest.skip("no concrete BaseNode subclasses found under src/nodes/")

        import framework.nodes.base_node as base_node_module

        failures: list[str] = []
        for node_cls in node_classes:
            order: list[str] = []
            monkeypatch.setattr(
                base_node_module,
                "emit_trace_event",
                lambda event_type, _payload, _state, _o=order: _o.append(f"event:{event_type}"),
            )

            for method_name, label in (
                ("_security_gate_input", "security_gate_input"),
                ("execute", "execute"),
                ("_security_gate_output", "security_gate_output"),
            ):
                original = getattr(node_cls, method_name)

                def spy(self, arg, _o=order, _label=label, _orig=original):
                    _o.append(_label)
                    return _orig(self, arg)

                monkeypatch.setattr(node_cls, method_name, spy)

            instance = node_cls()
            # caller trust == the node's required level so the trust gate always passes
            # here; the denial branch is asserted separately in TestTrustGate.
            state = {
                "caller_trust_level": node_cls.required_trust_level.value,
                "correlation_id": "pb6-invoke-order-test",
            }
            instance(state)

            expected = [
                "event:node_start",
                "security_gate_input",
                "execute",
                "security_gate_output",
                "event:node_complete",
            ]
            if order != expected:
                failures.append(
                    f"{node_cls.__name__}: invoke order violation.\n" f"expected: {expected}\nactual:   {order}"
                )

        assert not failures, "\n\n".join(failures)


class TestTrustGate:
    """PB-6: the trust gate in BaseNode.__call__ runs BEFORE execute() and denies
    a caller whose trust is below the node's required_trust_level."""

    def test_pre_process_denies_anonymous_caller(self, monkeypatch):
        """PreProcessNode (required VERIFIED_EXTERNAL) must refuse an ANONYMOUS caller."""
        _patch_domain_emit(monkeypatch)
        from framework.schemas.agent_status import AgentStatus
        from framework.schemas.invocation_context import TrustLevel
        from src.nodes.pre_process_node import PreProcessNode

        node = PreProcessNode()
        result = node(
            {
                "caller_trust_level": TrustLevel.ANONYMOUS.value,
                "user_input": _VALID_PAYLOAD,
                "correlation_id": "pb6-s1-denial",
            }
        )
        assert result["status"] == AgentStatus.ERROR.value
        assert any(
            "trust gate" in e.lower() for e in result.get("error_log", [])
        ), f"expected a trust-gate denial, got error_log={result.get('error_log')}"

    def test_pre_process_admits_verified_external_caller(self, monkeypatch):
        """The same node admits a VERIFIED_EXTERNAL caller and runs execute() to SUCCESS."""
        _patch_domain_emit(monkeypatch)
        from framework.schemas.agent_status import AgentStatus
        from framework.schemas.invocation_context import TrustLevel
        from src.nodes.pre_process_node import PreProcessNode

        node = PreProcessNode()
        result = node(
            {
                "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
                "user_input": _VALID_PAYLOAD,
                "input_context": {},
                "correlation_id": "pb6-s1-admit",
            }
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["validated_input"] is not None


class TestBackboneInvokeOrder:
    """PB-6 backbone: a full Graph().invoke() runs the 5-node backbone in order.

    Backbone order: InitializeNode -> PreProcessNode (pre_process) ->
                    CustomsDocumentationGraphNode (main) ->
                    PostProcessNode (post_process) -> FinalizeNode

    Uses VERIFIED_EXTERNAL caller trust — the real external path.
    InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL) is mandatory;
    NEVER use for_internal(), which would not represent a real external caller.
    """

    def _invoke(self, monkeypatch):
        _patch_domain_emit(monkeypatch)
        from framework.schemas.invocation_context import InvocationContext, TrustLevel
        from src.graph.graph import Graph

        agent = Graph()
        agent.compile()
        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        return agent.invoke(_VALID_PAYLOAD, ctx=ctx)

    def test_backbone_invoke_succeeds_and_returns_output(self, monkeypatch):
        from framework.schemas.agent_status import AgentStatus

        result = self._invoke(monkeypatch)
        assert result.get("status") == AgentStatus.SUCCESS.value, (
            f"Expected status={AgentStatus.SUCCESS.value!r}, got: {result.get('status')!r}\n"
            f"error_log: {result.get('error_log')}"
        )
        assert result.get("output") is not None, "output must be set after a successful invoke"
        # The assembled customs declaration must be present in the surfaced output.
        assert "CUSTOMS DECLARATION" in result["output"]
        assert "LOG-SHIP-20260712-001" in result["output"]

    def test_backbone_node_history_matches_expected_order(self, monkeypatch):
        result = self._invoke(monkeypatch)
        history = result.get("node_history", [])
        assert history == [
            "InitializeNode",
            "PreProcessNode",
            "CustomsDocumentationGraphNode",
            "PostProcessNode",
            "FinalizeNode",
        ], f"unexpected backbone node_history: {history}"

    def test_main_slot_is_customs_documentation_graph_node(self):
        """The `main` backbone slot must be CustomsDocumentationGraphNode (a GraphNode — Cat 2)."""
        from framework.nodes.graph_node import GraphNode
        from src.graph.graph import (
            CustomsDocumentationGeneratorAgent,
            CustomsDocumentationGraphNode,
        )

        agent = CustomsDocumentationGeneratorAgent()
        agent.compile()
        main_node = agent._nodes.get("main")
        assert main_node is not None, "main slot must be registered"
        assert isinstance(
            main_node, CustomsDocumentationGraphNode
        ), f"main slot must be CustomsDocumentationGraphNode, got {type(main_node).__name__}"
        assert isinstance(main_node, GraphNode), "main slot node must subclass GraphNode (Cat 2 contract)"
        assert main_node.__class__.__name__ == _MAIN_SLOT_NODE

    def test_invoke_payload_matches_pb6(self):
        """deploy/invoke_payload.json["input"] MUST equal _VALID_PAYLOAD.

        The deployment smoke-test invoke POSTs invoke_payload.json as the request
        body, so it must exercise the same payload PB-6 asserts yields SUCCESS.
        """
        repo_root = Path(__file__).resolve().parents[2]
        payload_file = repo_root / "deploy" / "invoke_payload.json"
        assert payload_file.exists(), "deploy/invoke_payload.json is required to deploy"
        body = json.loads(payload_file.read_text())
        assert (
            body.get("input") == _VALID_PAYLOAD
        ), "deploy/invoke_payload.json['input'] must equal the PB-6 _VALID_PAYLOAD"
        # And the payload the server forwards to agent.invoke() must itself be a
        # valid, PreProcessNode-parseable shipment JSON object.
        shipment = json.loads(body["input"])
        for required in ("shipment_id", "goods", "exporter", "importer"):
            assert required in shipment, f"invoke_payload input missing required field: {required}"

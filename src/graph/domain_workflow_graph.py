"""AgentCore Platform v1.0"""

# LOG-C2-011 — DomainWorkflowGraph (inner BaseGraph)
#
# This is the INNER graph for the Category 2 two-layer nested architecture.
# It encapsulates the customs-documentation generation pipeline:
#
#   START
#     -> input_validate            (InputValidateNode)
#     -> parse_shipment_data       (ParseShipmentDataNode)
#     -> generate_customs_sections (GenerateCustomsSectionsNode)
#     -> compliance_check          (ComplianceCheckNode)
#     -> output_format             (OutputFormatNode)
#     -> END
#
# Called by CustomsDocumentationGraphNode.get_subgraph() (graph.py).
# get_output() shapes the sub_result dict consumed by merge_output() there.
#
# Rules enforced:
#   - inherits BaseGraph (fully custom topology — no forced backbone)
#   - implements all 7 BaseGraph abstract members
#   - register_nodes() does NOT call super() (abstract in BaseGraph)
#   - does NOT register initialize / finalize (outer backbone concerns)
#   - all inner nodes declare required_trust_level = TrustLevel.ANONYMOUS
#   - get_output() designed together with CustomsDocumentationGraphNode.merge_output()
#   - all inner node constructors are empty-parens (no constructor arguments)
#   - _extra_initial_state() seeds the caller's input_context (context bridge)
#   - no platform-SDK imports

from typing import Any

from langgraph.graph import END, START

from framework.errors import ConfigError
from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.graph.context_bridge import get_caller_input_context
from src.nodes.compliance_check_node import ComplianceCheckNode
from src.nodes.generate_customs_sections_node import GenerateCustomsSectionsNode
from src.nodes.input_validate_node import InputValidateNode
from src.nodes.output_format_node import OutputFormatNode
from src.nodes.parse_shipment_data_node import ParseShipmentDataNode
from src.schemas.state import State, finite_in_range

# Bounds for the runtime parameters the outer graph forwards from
# config/config.yaml. A declared value outside these bounds is a deployment
# misconfiguration, so construction fails rather than silently degrading.
_MAX_RETRY_MAX = 10
_TIMEOUT_S_MAX = 3600


class DomainWorkflowGraph(BaseGraph):
    """Inner domain workflow graph for LOG-C2-011.

    Inherits BaseGraph directly for a fully custom node topology.
    Called by CustomsDocumentationGraphNode.get_subgraph() in graph.py.

    Pipeline (linear):
        START
          -> input_validate            (InputValidateNode)
          -> parse_shipment_data       (ParseShipmentDataNode)
          -> generate_customs_sections (GenerateCustomsSectionsNode)
          -> compliance_check          (ComplianceCheckNode)
          -> output_format             (OutputFormatNode)
          -> END

    All nodes are FunctionNode subclasses with ANONYMOUS trust_level.
    initialize / finalize are outer backbone concerns — not registered here.
    """

    # -- Identity -------------------------------------------------------------

    @property
    def name(self) -> str:
        return "log_c2_011_customs_documentation_workflow"

    @property
    def state_schema(self) -> type:
        return State

    # -- Config validation ----------------------------------------------------

    def _validate_config(self) -> None:
        """Validate the runtime parameters forwarded by the outer graph.

        The declaration pipeline itself is rule-based and needs no mandatory
        configuration, but the values the outer graph forwards from
        config/config.yaml (max_retry, timeout_s) are validated here so a
        malformed declared value fails at compile time instead of reaching the
        pipeline. Absent keys are legitimate — the nodes carry their defaults.
        """
        max_retry = self.config.get("max_retry")
        if max_retry is not None:
            parsed_retry = finite_in_range(max_retry, 0, _MAX_RETRY_MAX)
            if parsed_retry is None or float(parsed_retry) != int(parsed_retry):
                raise ConfigError(
                    f"[{self.__class__.__name__}] 'max_retry' must be a whole number " f"between 0 and {_MAX_RETRY_MAX}"
                )
        timeout_s = self.config.get("timeout_s")
        if timeout_s is not None and finite_in_range(timeout_s, 1, _TIMEOUT_S_MAX) is None:
            raise ConfigError(
                f"[{self.__class__.__name__}] 'timeout_s' must be a finite number " f"between 1 and {_TIMEOUT_S_MAX}"
            )

    # -- Initial state --------------------------------------------------------

    def _extra_initial_state(self) -> dict[str, Any]:
        """Seed the inner state with the caller's input_context.

        The framework's GraphNode.execute() does not forward the outer state's
        input_context into subgraph.invoke(), so the outer graph stashes it in a
        ContextVar (CustomsDocumentationGraphNode.extract_input) and this hook
        reads it back — see src/graph/context_bridge.py. Without this, inner
        reads of state["input_context"] (the per-invocation declaration options)
        would always see {}.
        """
        return {"input_context": get_caller_input_context()}

    # -- Node registration ----------------------------------------------------

    def register_nodes(self) -> None:
        """Register all 5 domain nodes.

        No super() call — BaseGraph.register_nodes() is abstract.
        Do NOT register initialize or finalize; those are outer backbone
        concerns handled by AgentBaseGraph in graph.py.
        Every key registered here is referenced in add_edges().
        All nodes are instantiated with empty parens: FunctionNode subclasses
        take no constructor arguments.
        """
        self._nodes["input_validate"] = InputValidateNode()
        self._nodes["parse_shipment_data"] = ParseShipmentDataNode()
        self._nodes["generate_customs_sections"] = GenerateCustomsSectionsNode()
        self._nodes["compliance_check"] = ComplianceCheckNode()
        self._nodes["output_format"] = OutputFormatNode()

    # -- Edge wiring ----------------------------------------------------------

    def add_edges(self) -> None:
        """Wire the linear customs-documentation generation topology.

        Linear flow:
            input_validate -> parse_shipment_data -> generate_customs_sections
            -> compliance_check -> output_format -> END.

        No conditional branching — all paths through the document pipeline are
        linear, so route() satisfies the abstract contract but is not used at
        runtime.
        """
        self._sg.add_edge(START, "input_validate")
        self._sg.add_edge("input_validate", "parse_shipment_data")
        self._sg.add_edge("parse_shipment_data", "generate_customs_sections")
        self._sg.add_edge("generate_customs_sections", "compliance_check")
        self._sg.add_edge("compliance_check", "output_format")
        self._sg.add_edge("output_format", END)

    # -- Routing --------------------------------------------------------------

    def route(self, state: AgentState) -> str:
        """Conditional routing — required by the BaseGraph abstract contract.

        Linear topology; add_conditional_edges() is not used, so this method is
        never called at runtime. Returns END on error so an unexpected
        invocation does not re-enter a processing node.
        """
        if state.get("status") == AgentStatus.ERROR.value:
            return END
        return "output_format"

    # -- Output shape ---------------------------------------------------------

    def get_output(self, state: AgentState) -> dict[str, Any]:
        """Shape the output dict returned to the outer graph as sub_result.

        This dict is received by CustomsDocumentationGraphNode.merge_output()
        in graph.py as the `sub_result` argument. Both methods are designed
        together to guarantee field-name consistency:

            Inner get_output() emits:   "customs_document", "customs_sections",
                                        "compliance_flags",
                                        "broker_review_required", "status"
            Outer merge_output() reads: sub_result.get(...) for each key above.
        """
        return {
            # the reason must leave the subgraph or the outer graph cannot report it
            "error_code": state.get("error_code"),
            "customs_document": state.get("customs_document"),
            "customs_sections": state.get("customs_sections"),
            "compliance_flags": state.get("compliance_flags"),
            "broker_review_required": state.get("broker_review_required", False),
            "status": state.get("status"),
            "node_history": state.get("node_history", []),
            "correlation_id": state.get("correlation_id"),
        }

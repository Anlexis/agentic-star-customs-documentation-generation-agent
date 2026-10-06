"""AgentCore Platform v1.0"""

# LOG-C2-011 — Outer graph (AgentBaseGraph; Category 2 two-layer nested architecture)
#
# Architecture (Category 2):
#
#   Outer backbone (fixed — identical to Category 1; do NOT override add_edges()):
#     START -> initialize -> pre_process -> main -> {route} -> post_process
#              -> finalize -> END
#                            | (RETRY, up to max_retry)
#                            -> pre_process
#
#   The `main` slot is a GraphNode subclass (CustomsDocumentationGraphNode) that
#   delegates the full domain workflow to DomainWorkflowGraph (inner BaseGraph).
#
#   Domain complexity is fully encapsulated inside the inner graph. The outer
#   backbone is never modified.
#
# Directory layout:
#   src/graph/graph.py                 <- outer graph (this file)
#   src/graph/domain_workflow_graph.py <- inner graph (multi-step topology)
#   src/graph/context_bridge.py        <- input_context hand-off (outer -> inner)
#
# Rules enforced:
#   - CustomsDocumentationGeneratorAgent inherits AgentBaseGraph
#   - super().register_nodes() called first (fills initialize + finalize)
#   - CustomsDocumentationGraphNode assigned to self._nodes["main"]
#   - PreProcessNode (VERIFIED_EXTERNAL) in the pre_process slot (trust gate)
#   - PostProcessNode in the post_process slot (external output gate)
#   - the output gate is exposed on the agent class
#   - merge_output() returns only changed keys
#   - get_output() surfaces the domain result
#   - the package re-exports the class for registry discovery (src/graph/__init__.py)
#   - the class name matches the manifest `class:` entry exactly
#   - add_edges() is NOT overridden on the outer graph
#   - no platform-SDK imports

import os
from typing import TYPE_CHECKING, Any, ClassVar, Optional, cast

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.graph.context_bridge import set_caller_input_context
from src.nodes.post_process_node import PostProcessNode, _security_gate_output
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, typing only
    from src.graph.domain_workflow_graph import DomainWorkflowGraph

# Runtime parameters live in config/config.yaml at the repo root (three levels
# up from this file: src/graph/graph.py -> src/graph -> src -> <repo root>).
# config/agent.yaml is the static manifest and carries no runtime block.
_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "config",
    "config.yaml",
)


def _runtime_config() -> dict[str, Any]:
    """Return the runtime parameters declared in config/config.yaml.

    Best-effort: a missing or unparseable file yields ``{}`` so graph
    construction never breaks (the graph and its nodes then fall back to their
    declared defaults). PyYAML is loaded lazily — it is a framework runtime
    dependency, so importing it on demand avoids a hard module-load coupling.
    """
    try:
        import yaml

        with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
        return cast(dict[str, Any], loaded) if isinstance(loaded, dict) else {}
    except Exception:
        return {}


class CustomsDocumentationGraphNode(GraphNode):
    """GraphNode subclass assigned to the `main` slot of CustomsDocumentationGeneratorAgent.

    Wraps DomainWorkflowGraph (the inner Category 2 BaseGraph).
    Called by the AgentBaseGraph backbone after pre_process and before post_process.

    Contracts:
      get_subgraph()  — instantiate and return DomainWorkflowGraph
      extract_input() — pull validated_input from outer state; bridge input_context
      merge_output()  — map sub_result fields into the outer state delta (changed keys only)
      error_strategy  — "propagate": re-raise inner errors as SubgraphError (fail-fast)
    """

    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = False

    def get_subgraph(self) -> "DomainWorkflowGraph":
        """Instantiate and return the inner domain workflow graph.

        DomainWorkflowGraph is imported lazily (inside the method) to avoid
        circular-import risk at module load time.
        """
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        return DomainWorkflowGraph(config=self._parent_config())

    def execute(self, state: AgentState) -> dict[str, Any]:
        """Skip the inner graph when the request was already found unacceptable.

        A request declined by pre_process has no validated input to act on, so
        running the inner graph would only produce a second, vaguer reason for
        the same rejection - and overwrite the specific one already settled.
        """
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        result: dict[str, Any] = super().execute(state)
        return result

    def extract_input(self, state: AgentState) -> str:
        """Return the string input passed into inner_graph.invoke().

        PreProcessNode validates and normalises the raw user_input and writes
        the result to validated_input. Prefer that; fall back to user_input if
        validated_input is absent (e.g. in unit tests).

        Also bridges the caller's input_context to the inner graph: the
        framework's GraphNode.execute() does not forward input_context on
        subgraph.invoke(), and extract_input is the last hook this repository
        controls before that call — see src/graph/context_bridge.py.
        """
        set_caller_input_context(cast("dict[str, Any] | None", state.get("input_context")))
        return str(state.get("validated_input") or state.get("user_input") or "")

    def merge_output(self, state: AgentState, sub_result: dict[str, Any]) -> dict[str, Any]:
        """Map the inner graph sub_result back into the outer state delta.

        sub_result is the dict returned by DomainWorkflowGraph.get_output().
        Returns ONLY changed keys — never the full state.

        Key coupling (designed together with DomainWorkflowGraph.get_output()):
          Inner get_output() emits  -> "customs_document", "customs_sections",
                                       "compliance_flags",
                                       "broker_review_required", "status"
          This merge_output() reads -> sub_result.get(...) for each of these keys.

        PostProcessNode (outer post_process) reads customs_document +
        broker_review_required from state to apply the output gate and set
        formatted_output.
        """
        return {
            # Outer reason wins: a reason settled before the inner run is the real
            # one, and a plain sub_result.get() would erase it.
            "error_code": state.get("error_code") or sub_result.get("error_code", ""),
            "customs_document": sub_result.get("customs_document"),
            "customs_sections": sub_result.get("customs_sections"),
            "compliance_flags": sub_result.get("compliance_flags"),
            "broker_review_required": sub_result.get("broker_review_required", False),
            "status": sub_result.get("status"),
        }

    def _parent_config(self) -> dict[str, Any]:
        """Forward the declared runtime parameters to the inner graph.

        The runtime block lives in config/config.yaml; the declared, non-None
        settings are forwarded both at the top level (where the inner graph
        validates them) and under the ``configurable`` key, so the inner graph
        can reach them instead of being constructed with no config at all.

        The generator is deterministic and rule-based: the forwarded ``llm.*``
        keys (system_prompt_template / temperature / max_tokens) are reserved
        for a future build that wires a real model and are not consumed by the
        current nodes. Only keys the configuration actually declares (non-None)
        are forwarded; absent keys fall back to node defaults.
        """
        cfg = _runtime_config()
        llm = cfg.get("llm") or {}
        declared = {
            "max_retry": cfg.get("max_retry"),
            "timeout_s": cfg.get("timeout_s"),
            "system_prompt_template": llm.get("system_prompt_template"),
            "temperature": llm.get("temperature"),
            "max_tokens": llm.get("max_tokens"),
        }
        forwarded = {k: v for k, v in declared.items() if v is not None}
        return {**forwarded, "configurable": dict(forwarded)}


class CustomsDocumentationGeneratorAgent(AgentBaseGraph):
    """Outer graph for LOG-C2-011 (Category 2 document-generation pipeline).

    Inherits AgentBaseGraph directly. Domain logic is fully encapsulated in
    CustomsDocumentationGraphNode (main slot), which delegates to
    DomainWorkflowGraph (inner BaseGraph).

    Backbone (fixed — identical to Category 1):
        START -> initialize -> pre_process -> main -> post_process -> finalize -> END

    register_nodes() and get_output() are the only overrides:
      - super().register_nodes() fills: initialize, finalize (framework defaults)
      - pre_process:  PreProcessNode  (VERIFIED_EXTERNAL — caller trust gate)
      - main:         CustomsDocumentationGraphNode (delegates to DomainWorkflowGraph)
      - post_process: PostProcessNode (external output gate)
      - get_output(): surfaces the domain result

    add_edges() is NOT overridden — backbone wiring belongs to the framework.

    The class name MUST match the manifest `class:` entry exactly, and is
    re-exported from src/graph/__init__.py for registry discovery.
    server.py imports this as `Graph` via the alias below.
    """

    # Domain output gate exposed on the agent class. The functional
    # credential/PII scan lives module-level in post_process_node.py: a
    # node-instance `_extra_security_gate_*` method would be wrapped into the
    # framework's gate chain and return None, whereas a plain agent-class
    # staticmethod is not wrapped. PostProcessNode.execute() invokes the SAME
    # module-level function, so the gate is genuinely applied at runtime.
    _security_gate_output = staticmethod(_security_gate_output)

    def __init__(self, config: Optional[dict[str, Any]] = None) -> None:
        """Construct the agent with the declared runtime parameters.

        A caller (or the registry) may pass an explicit ``config``; otherwise
        the declared runtime block in config/config.yaml is loaded, so declared
        values such as ``max_retry`` actually govern the backbone instead of
        silently falling back to framework defaults.
        """
        super().__init__(config if config is not None else _runtime_config())

    @property
    def name(self) -> str:
        """Agent identifier registered with the agent registry."""
        return "CustomsDocumentationGeneratorAgent"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        """Fill all 5 backbone slots.

        super().register_nodes() MUST be called first — it injects the
        framework's default InitializeNode (sets schema_version, session_id,
        trust_level) and FinalizeNode (builds response_metadata, total_time_ms).
        """
        super().register_nodes()  # fills: initialize, finalize

        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = CustomsDocumentationGraphNode()
        self._nodes["post_process"] = PostProcessNode()

    def get_output(self, state: AgentState) -> dict[str, Any]:
        """Surface the domain customs-documentation result on the outer invoke() return.

        AgentBaseGraph.get_output() returns only the minimal ``{output, status,
        trace_id, correlation_id, node_history}`` envelope, which on the success
        path drops the structured domain result — the fields the inner
        DomainWorkflowGraph produces (merged into outer state by
        CustomsDocumentationGraphNode.merge_output(): customs_document,
        customs_sections, compliance_flags, broker_review_required) and the
        gated PostProcessNode outputs (formatted_output, result). This override
        extends the base envelope so a successful invocation actually returns
        the domain result.

        The output-gate invariant is preserved (fail-closed):
          * ``formatted_output`` is what the gated PostProcessNode produced, so
            it is the only caller-facing value on either path (on a block it is
            the gate's own content-free withholding notice).
          * ``result`` is the same document, and is surfaced ONLY when
            status == SUCCESS. The base envelope resolves its ``output`` key as
            ``formatted_output or result`` without consulting status, so on a
            non-success outcome that fallback is re-resolved here as well: an
            absent gate output stays absent and never becomes the document.
            Without both, any status flip AFTER the gate passed would hand the
            caller the full declaration inside an error envelope — the gate's
            own clearing was the only thing preventing it, which made this
            accessor unfalsifiable rather than safe.
          * The document is surfaced ONLY via the gated value, NEVER the
            pre-gate raw ``state["customs_document"]``, so the output gate
            cannot be bypassed.
          * The structured fields (customs_sections / compliance_flags /
            broker_review_required) are surfaced ONLY when the gate passed
            (status == SUCCESS). On any non-success outcome — including a
            blocked document — they are withheld (None).
        """
        output: dict[str, Any] = cast(
            "dict[str, Any]", super().get_output(state)
        )  # {output, status, trace_id, correlation_id, node_history}
        # A run that completed WITHOUT carrying out the request holds the
        # sentence saying what to correct, not a product: none of the
        # structured fields below were produced, so none is released.
        if state.get("error_code"):
            return output
        succeeded = state.get("status") == AgentStatus.SUCCESS.value
        formatted_output = state.get("formatted_output")

        # The gate's own output — the caller's whole surface on either path.
        output["formatted_output"] = formatted_output
        if succeeded:
            # Surface the customs document only via the POST-gate value, never
            # the pre-gate raw state["customs_document"] — fail-closed.
            output["result"] = state.get("result")
            output["customs_document"] = formatted_output or state.get("result")
        else:
            output["result"] = None
            output["customs_document"] = None
            # Re-resolve the base envelope's value without the `or result`
            # fallback: on a non-success outcome the gate's output is all the
            # caller may see, and an absent one stays absent.
            output["output"] = formatted_output or None

        # Structured domain result — surfaced only on the gated success path.
        output["customs_sections"] = state.get("customs_sections") if succeeded else None
        output["compliance_flags"] = state.get("compliance_flags") if succeeded else None
        output["broker_review_required"] = state.get("broker_review_required") if succeeded else None
        return output

    # add_edges() is NOT overridden — backbone wiring belongs to the framework.


# Alias for backward compatibility (server.py imports Graph).
# The class name CustomsDocumentationGeneratorAgent matches the manifest `class:` entry.
Graph = CustomsDocumentationGeneratorAgent

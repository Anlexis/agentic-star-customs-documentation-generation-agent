# LOG-C2-011 — Unit tests: the outer invoke() envelope
# (CustomsDocumentationGeneratorAgent.get_output()).
#
# The envelope is the last thing between graph state and the caller, and it is
# the SECOND HALF of the output-gate contract. AgentBaseGraph.get_output()
# resolves its `output` key as `formatted_output or result` WITHOUT consulting
# status, so an override that forwards state verbatim hands the caller the very
# declaration the output gate refused, inside an error envelope.
#
# Measured on this repo before the fix: with a non-success status and the
# document still in state, `result` and `output` both surfaced the full
# declaration for EVERY non-success status (error / timeout / cancelled /
# retry / pending / awaiting_human). It was masked in practice only because
# PostProcessNode._blocked() happened to overwrite both fields with its stub —
# i.e. the accessor was unfalsifiable rather than safe. These call get_output()
# directly on a state dict so the resolution rule itself is pinned, independent
# of which node produced that state.

import json

import pytest

from framework.schemas.agent_status import AgentStatus

from src.graph.graph import CustomsDocumentationGeneratorAgent

_DOCUMENT = (
    "========================================\n"
    "CUSTOMS DECLARATION (DRAFT)\n"
    "Shipment ID: LOG-SHIP-20260712-001\n"
    "Exporter: Kanto Textiles K.K.\n"
    "Importer: West Coast Imports LLC\n"
    "REGULATORY COMPLIANCE NOTE\n"
)
_SECTIONS = json.dumps({"declared_value": "  Total declared value: USD 47,000"})
_FLAGS = json.dumps({"broker_review_required": True, "reason": "restricted goods"})

# Every non-success status, not just ERROR: a terminal status routes straight to
# finalize without post_process running at all.
_NON_SUCCESS = [
    AgentStatus.ERROR,
    AgentStatus.TIMEOUT,
    AgentStatus.CANCELLED,
    AgentStatus.RETRY,
    AgentStatus.PENDING,
    AgentStatus.AWAITING_HUMAN,
]


def _state(**overrides) -> dict:
    state = {
        "status": AgentStatus.SUCCESS.value,
        "formatted_output": _DOCUMENT,
        "result": _DOCUMENT,
        "customs_document": _DOCUMENT,
        "customs_sections": _SECTIONS,
        "compliance_flags": _FLAGS,
        "broker_review_required": True,
        "trace_id": "envelope-test",
        "correlation_id": "envelope-test",
        "node_history": [],
    }
    state.update(overrides)
    return state


def _envelope(**overrides) -> dict:
    return CustomsDocumentationGeneratorAgent().get_output(_state(**overrides))


class TestSuccessEnvelope:
    """The control. Without it every containment assertion below would pass on
    an envelope that returns nothing at all."""

    def test_success_surfaces_the_gated_document(self):
        envelope = _envelope()
        assert envelope["status"] == AgentStatus.SUCCESS.value
        assert envelope["output"] == _DOCUMENT
        assert envelope["formatted_output"] == _DOCUMENT
        assert envelope["result"] == _DOCUMENT
        assert envelope["customs_document"] == _DOCUMENT
        assert envelope["customs_sections"] == _SECTIONS
        assert envelope["compliance_flags"] == _FLAGS
        assert envelope["broker_review_required"] is True
        for key in ("trace_id", "correlation_id", "node_history"):
            assert key in envelope, "get_output() must extend, not replace, the base envelope"


class TestNonSuccessContainment:
    @pytest.mark.parametrize("status", _NON_SUCCESS, ids=[s.value for s in _NON_SUCCESS])
    def test_pre_gate_document_is_never_surfaced_as_result(self, status):
        envelope = _envelope(status=status.value)
        assert envelope["result"] is None
        assert envelope["customs_document"] is None

    @pytest.mark.parametrize("status", _NON_SUCCESS, ids=[s.value for s in _NON_SUCCESS])
    def test_the_or_result_fallback_is_dead(self, status):
        """The hole in the base envelope: with no formatted_output, `output`
        falls through to `result`. On a non-success outcome an absent gate
        output must STAY absent, never become the declaration."""
        envelope = _envelope(status=status.value, formatted_output=None)
        assert not envelope["output"]
        blob = json.dumps(envelope, ensure_ascii=False)
        for released in ("CUSTOMS DECLARATION", "Kanto Textiles", "West Coast Imports", "USD 47,000"):
            assert released not in blob, f"{released!r} released on a {status.value} envelope"

    @pytest.mark.parametrize("status", _NON_SUCCESS, ids=[s.value for s in _NON_SUCCESS])
    def test_structured_fields_are_withheld(self, status):
        envelope = _envelope(status=status.value)
        for key in ("customs_sections", "compliance_flags", "broker_review_required"):
            assert envelope[key] is None

    def test_error_surfaces_the_gate_notice_and_nothing_else(self):
        """What the gate itself produced is the caller's whole error surface —
        so a blocked invocation still explains itself without releasing the
        declaration."""
        notice = "[CUSTOMS DOCUMENT WITHHELD: output contained a disallowed pattern (national_id_pattern).]"
        envelope = _envelope(status=AgentStatus.ERROR.value, formatted_output=notice, result=notice)
        assert envelope["output"] == notice
        assert envelope["formatted_output"] == notice
        assert envelope["result"] is None
        assert envelope["customs_document"] is None
        assert "Kanto Textiles" not in json.dumps(envelope, ensure_ascii=False)

    def test_envelope_key_inventory_is_pinned(self):
        """Inventory guard: a future key added to the envelope must be given a
        status guard deliberately, not inherit one by omission."""
        assert set(_envelope(status=AgentStatus.ERROR.value)) == {
            "output",
            "status",
            "trace_id",
            "correlation_id",
            "node_history",
            "formatted_output",
            "result",
            "customs_document",
            "customs_sections",
            "compliance_flags",
            "broker_review_required",
        }

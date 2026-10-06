"""AgentCore Platform v1.0"""

# LOG-C2-011 — PreProcessNode
# Outer backbone pre_process slot: caller trust gate + structural validation of
# the shipment request.
#
# Responsibilities:
#   - enforce VERIFIED_EXTERNAL caller trust (required_trust_level)
#   - bound the request: reject an oversized, empty or non-JSON body early
#   - confirm the required shipment fields are present
#   - refuse payload text carrying an instruction-override directive (the
#     template's OWN injection screen — see below)
#   - validate the caller's channel tag against the inert identifier grammar
#   - write validated_input (normalised JSON string) + enriched_context to State
#   - emit an audit event for every validation decision
#
# The instruction-override refusal is this template's own guarantee. The
# platform's input gate (1.0.1+) screens user_input ahead of execute() as
# well, but a template whose only defence is the platform's fails OPEN
# wherever that gate is absent (1.0.0), configured off, or below its
# confidence threshold: the hostile goods description then reaches the
# section-generation prompt and the invoke returns success. The screen below
# runs inside execute(), so calling execute() directly still refuses, and a
# refusal carries NOTHING forward — no validated_input, no enriched_context.
#
# Rejections fail CLOSED and name the field only — a rejected value is never
# echoed into an error message or an audit event. Domain-level rules (bounded
# numerics, goods lines, party and transport fields) belong to the inner
# InputValidateNode; this node owns the trust and structural boundary.
#
# Returns ONLY the state keys this node writes (partial-dict contract).

import json
import logging
import re
from typing import Any, ClassVar, Dict, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import INPUT_REJECTED
from src.services.progress import emit_progress

from src.schemas.state import is_inert_identifier, to_json

logger = logging.getLogger(__name__)

# Required top-level keys for a valid shipment payload.
# shipment_id is the mandatory identifier; goods drives every declaration
# section; exporter/importer are needed for country-of-origin + parties.
_REQUIRED_SHIPMENT_KEYS = frozenset(
    {
        "shipment_id",
        "goods",
        "exporter",
        "importer",
    }
)

# Upper bound on the raw request body, matched to the transport adapter's
# request-size cap: a larger body is refused before it is parsed.
_MAX_PAYLOAD_CHARS = 256 * 1024

# Channel tag used when the caller supplies none.
_DEFAULT_CHANNEL = "unspecified"

# Instruction-override screen — deliberately NARROW. Genuine customs text
# borrows every verb an injection uses: "act as an importer of record",
# "a binding ruling may override the classification", "ignore the previous
# declaration when filing an amendment" are all real goods/entry language,
# and substring screens have refused "specific" (cif) and "adapt" (dap).
# So every alternative here is anchored on a whole directive PHRASE — the
# override verb plus the prompt-specific noun — or on a chat-template
# control token, which has no legitimate reading in a shipment payload.
_INSTRUCTION_OVERRIDE_RE = re.compile(
    # Chat-template control tokens: <|im_start|>, <|system|>, [INST], <<SYS>>.
    r"<\|[a-z_]{2,32}\|>"
    r"|\[/?INST\]"
    r"|<</?SYS>>"
    # "ignore/disregard/forget the previous instructions/rules/prompt"
    r"|\b(?:ignore|disregard|forget)\s+(?:all\s+|any\s+|the\s+)?"
    r"(?:previous|prior|above|earlier|preceding)\s+"
    r"(?:instruction|instructions|prompt|prompts|rule|rules)\b"
    # "ignore all rules/instructions" (no noun of its own — the bare form)
    r"|\b(?:ignore|disregard)\s+all\s+(?:rules|instructions)\b"
    # "reveal/print the system prompt / hidden instructions"
    r"|\b(?:reveal|show|print|repeat|output|disclose|dump)\s+(?:me\s+)?(?:your|the)\s+"
    r"(?:system\s+prompt|system\s+message|initial\s+prompt|hidden\s+instructions)\b"
    # "you are now a ..." — but "you are now an importer/exporter/agent of
    # record" is ordinary customs-authority phrasing
    r"|\byou\s+are\s+now\s+(?:a|an)\s+(?!importer\b|exporter\b|agent\s+of\s+record)"
    # "act as developer/admin/root/jailbroken/unrestricted mode"
    r"|\bact\s+as\s+(?:if\s+you\s+(?:are|were)\s+)?(?:a\s+|an\s+)?"
    r"(?:developer|admin|administrator|root|jailbroken|unrestricted)\s+mode\b"
    # "override your/the instructions/rules/safety" — never "override the
    # classification/declaration", which is real tariff language
    r"|\boverride\s+(?:your|the)\s+"
    r"(?:instruction|instructions|rule|rules|safety|guardrail|guardrails|"
    r"restriction|restrictions)\b"
    # "new system prompt:" header form
    r"|\b(?:new|updated)\s+system\s+(?:prompt|instructions)\s*[:=]",
    re.IGNORECASE,
)

# Schema field names that may be echoed into a rejection message. Any payload
# key outside this set is caller-controlled text, so the path shows a
# placeholder instead — a hostile field NAME is never echoed either.
_KNOWN_FIELD_NAMES = frozenset(
    {
        "shipment_id",
        "goods",
        "exporter",
        "importer",
        "transport",
        "incoterms",
        "currency",
        "description",
        "hs_code",
        "quantity",
        "unit_value",
        "net_weight_kg",
        "country_of_origin",
        "name",
        "address",
        "country",
        "mode",
        "port_of_loading",
        "port_of_discharge",
    }
)


def _scan_for_instruction_override(value: Any, path: str) -> Optional[str]:
    """Depth-first scan of every string in the payload — keys included.

    Returns the JSON path of the first string carrying an instruction-override
    directive, or None. Path components outside the declared schema are
    masked, so the returned path is always safe to name in an error message.
    """
    if isinstance(value, str):
        return (path or "payload") if _INSTRUCTION_OVERRIDE_RE.search(value) else None
    if isinstance(value, dict):
        for key, item in value.items():
            safe_key = key if isinstance(key, str) and key in _KNOWN_FIELD_NAMES else "<unrecognised-field>"
            key_path = f"{path}.{safe_key}" if path else safe_key
            if isinstance(key, str) and _INSTRUCTION_OVERRIDE_RE.search(key):
                return key_path
            found = _scan_for_instruction_override(item, key_path)
            if found is not None:
                return found
    if isinstance(value, list):
        for index, item in enumerate(value):
            found = _scan_for_instruction_override(item, f"{path}[{index}]")
            if found is not None:
                return found
    return None


class PreProcessNode(FunctionNode):
    """Trust gate and structural input validation for LOG-C2-011.

    Validates the caller-supplied shipment request before the domain workflow
    runs. This is the outer backbone's pre_process slot — the only node
    requiring VERIFIED_EXTERNAL trust, so unauthenticated or anonymous callers
    are rejected here (fail-fast; inner domain nodes carry ANONYMOUS trust and
    never see untrusted input directly).

    Input state keys:
        user_input:    str   — caller-supplied JSON shipment payload
        input_context: dict  — per-invocation options; `channel` is read here

    Output state keys (partial dict):
        validated_input:  str        — normalised JSON string (re-serialised)
        enriched_context: str        — JSON-serialised channel metadata
        status:           str        — AgentStatus.SUCCESS.value or AgentStatus.ERROR.value
        error_log:        list[str]  — set only on ERROR
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _reject(self, state: AgentState, reason: str, message: str, code: str = "INVALID_REQUEST") -> dict[str, Any]:
        """Fail closed, naming the field — never the rejected value."""
        logger.warning("PreProcessNode: %s", message)
        emit_trace_event("pre_process_validation_failed", {"reason": reason}, state)
        if code:
            # A value the caller can correct: the run COMPLETES carrying the
            # reason so the request can be sent again on the same conversation.
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": code,
                "error_log": [f"PreProcessNode: {message}"],
            }
        return {
            "status": AgentStatus.ERROR.value,
            "error_log": [f"PreProcessNode: {message}"],
        }

    def execute(self, state: AgentState) -> dict[str, Any]:
        user_input = state.get("user_input", "")
        input_context = state.get("input_context") or {}

        # -- Emptiness check ---------------------------------------------------
        if not user_input or not isinstance(user_input, str) or not user_input.strip():
            return self._reject(state, "empty_input", "user_input is empty or missing", "EMPTY_INPUT")

        # -- Size bound --------------------------------------------------------
        if len(user_input) > _MAX_PAYLOAD_CHARS:
            return self._reject(
                state,
                "payload_too_large",
                f"user_input exceeds the {_MAX_PAYLOAD_CHARS}-character request limit",
                "QUESTION_TOO_LONG",
            )

        # -- JSON parse --------------------------------------------------------
        try:
            payload: Dict[str, Any] = json.loads(user_input.strip())
        except (json.JSONDecodeError, ValueError):
            return self._reject(state, "json_parse_error", "user_input is invalid JSON")

        if not isinstance(payload, dict):
            return self._reject(state, "payload_not_object", "user_input JSON root must be an object")

        # -- Required field check ----------------------------------------------
        missing = _REQUIRED_SHIPMENT_KEYS - payload.keys()
        if missing:
            emit_trace_event(
                "pre_process_validation_failed",
                {"reason": "missing_required_fields", "missing": sorted(missing)},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PreProcessNode: missing required fields: {sorted(missing)}"],
            }

        # -- Instruction-override screen ---------------------------------------
        # Scanned on the PARSED strings, not the raw body, so a JSON-escaped
        # directive (r"\u0069gnore" decodes to "ignore") cannot slip past a
        # screen that only ever saw the escaped bytes. A hit fails
        # CLOSED: ERROR status, and neither validated_input nor
        # enriched_context is written, so nothing reaches the inner pipeline
        # or the section-generation prompt.
        flagged_path = _scan_for_instruction_override(payload, "")
        if flagged_path is not None:
            # Not a value the caller can correct: the content itself is
            # refused, so the run terminates rather than inviting a resend.
            return self._reject(
                state,
                "instruction_override",
                f"rejected shipment field: {flagged_path} carries an instruction-override directive",
                "",
            )

        # -- Channel tag -------------------------------------------------------
        # The channel is caller-controlled metadata carried into the audit
        # trail, so it is locked to the inert identifier grammar rather than
        # accepted as free text.
        if not isinstance(input_context, dict):
            return self._reject(state, "input_context_not_object", "input_context must be a JSON object")
        channel = input_context.get("channel", _DEFAULT_CHANNEL)
        if not is_inert_identifier(channel):
            return self._reject(
                state,
                "invalid_channel",
                "input_context.channel must be 1-64 characters of [A-Za-z0-9._-]",
            )

        # -- Success -----------------------------------------------------------
        # Metadata only; the inner InputValidateNode owns the identifier grammar.
        shipment_id = str(payload.get("shipment_id", "")).strip()[:64]
        normalised_json = json.dumps(payload, ensure_ascii=False)

        logger.info(
            "PreProcessNode: accepted a shipment request with %d top-level fields",
            len(payload),
        )
        emit_trace_event(
            "pre_process_validated",
            {"payload_keys": sorted(payload.keys()), "channel": channel},
            state,
        )

        return {
            "validated_input": normalised_json,
            "enriched_context": to_json(
                {
                    "source": "CustomsDocumentationGeneratorAgent",
                    "channel": channel,
                    "shipment_id": shipment_id,
                }
            ),
            "status": AgentStatus.SUCCESS.value,
        }

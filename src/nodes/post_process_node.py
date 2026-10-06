"""AgentCore Platform v1.0"""

# LOG-C2-011 — PostProcessNode
# Outer backbone post_process slot: the external-output boundary for the
# customs declaration draft. Four layers, in this order:
#
#   (1) verbatim payload redaction — the declaration is a rendering of the
#       shipment payload, never the payload itself: a verbatim embedding of the
#       caller's raw JSON payload in the document is a leak, not a feature, and
#       is replaced with [REDACTED] plus an audit event;
#   (2) credential / restricted-identifier scan — API keys, JWTs, bearer
#       tokens, password assignments, and national-ID / payment-card number
#       forms anywhere in the document withhold the whole document (sanitised
#       stub, status ERROR). This runs BEFORE the numeric layer: the precision
#       grammar deliberately treats a standalone 3-letter uppercase word as a
#       currency marker (a false snap fails safe), so snapping first could
#       rewrite digits inside a labelled sequence such as "SSN 123-45-6789" and
#       hide it from this scan;
#   (3) monetary precision grid — the declaration states monetary figures as
#       aggregates in units of 1,000; every monetary-form token is snapped onto
#       that grid (an off-grid value is a full-precision figure reaching the
#       external surface), with an audit event;
#   (4) the layer-2 scan repeated, so the final bytes that leave the agent are
#       never an unscanned surface.
#
# The domain output gate is the module-level function `_security_gate_output`
# called from inside execute() — NOT an instance method on the node class (the
# framework auto-wraps node instance methods on the real invoke path, which
# would raise AttributeError). The agent class re-exports the same function as
# its output gate, so there is one source of truth.
#
# Returns ONLY the state keys this node writes (partial-dict contract).

import logging
import re
from typing import Any, ClassVar, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from framework.security.credential_detector import detect_credentials
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED, INVALID_VALUE, TOO_LONG

from src.schemas.state import EXTERNAL_ROUND_UNIT

logger = logging.getLogger(__name__)

# DOMAIN-ONLY patterns, scanned IN ADDITION TO the framework's own
# detect_credentials() — never instead of it. A local pattern set narrower than
# the framework's is itself a bypass: the framework scans every node result with
# its own recognizer and RAISES on a hit, and a raise makes BaseNode.__call__
# discard the whole return — including the containment clearing in _blocked()
# below. A shape the framework refuses and this gate missed would therefore skip
# the gate entirely and leave the un-cleared state in place, which is exactly
# what happened here before the fix: `sk_live_…`, `AKIA…`, a dotless `eyJ…` JWT
# header and `postgresql://…` all passed this table and were refused by the
# framework one node earlier, so PostProcessNode never even ran.
#
# The entries below are the ones the framework does NOT carry, plus two that are
# deliberately BROADER than its equivalents (`(?:sk|pk|ak)-` at 16+ chars vs its
# `sk-` at 20+; `Bearer` at 8+ vs its 16+). Party names, addresses and goods
# descriptions are legitimate declaration content, so this set targets secrets
# and the identifier forms a customs declaration never needs: national-ID and
# payment-card numbers.
_CREDENTIAL_PATTERNS: List[Tuple[str, str]] = [
    (r"(?:sk|pk|ak)-[A-Za-z0-9]{16,}", "api_key_pattern"),
    (r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "jwt_pattern"),
    (r"Bearer\s+[A-Za-z0-9_\-\.]{8,}", "bearer_token"),
    (
        r"(?:password|passwd|secret|api_key|token|access_key|private_key)" r"\s*[:=]\s*\S{8,}",
        "credential_assignment",
    ),
    (r"\b\d{3}-\d{2}-\d{4}\b", "national_id_pattern"),
    (r"\b(?:\d{4}[ -]?){3}\d{4}\b", "payment_card_pattern"),
]

# State fields that must NEVER be embedded verbatim in the external document.
# The declaration is rendered section by section from validated fields; the raw
# caller payload re-appearing verbatim means caller-controlled content reached
# the external surface unrendered.
_BLOCKED_FIELDS = frozenset({"user_input", "validated_input"})

# EXPLICIT output schema — monetary values are identified by FORM and by
# CURRENCY CONTEXT, never by magnitude:
#   form:    comma-grouped numbers (9,999 / 1,234,567) and unformatted runs
#            of 5+ digits (a rendering-regression leak);
#   context: any bare 1-4 digit number associated with a currency marker is
#            monetary even though short — SYMMETRICALLY: a 3-letter uppercase
#            code or a currency symbol (incl. fullwidth ￥ and 円/₩), before or
#            after the value, attached or separated by ANY whitespace run
#            (spaces, tabs, newlines — the delimiter grammar is `\s*`),
#            signed or unsigned. Word-boundary guards keep embedded acronyms
#            structural ("STAR 2026" does not match — the 3 letters must be a
#            standalone word). Any standalone 3-letter uppercase word counts
#            as a code on purpose: a false snap fails SAFE while a missed
#            leak does not.
# Domain guard: this declaration's core tokens are alphanumeric identifiers
# with embedded digit runs, hyphens and dots (LOG-SHIP-20260712-001, HS code
# 6109.10, container and reference numbers). A monetary token never starts or
# ends INSIDE such an identifier, so the whole grammar is wrapped in a pair of
# single-character, fixed-width identifier guards: a match may not be
# immediately preceded by [A-Za-z0-9.-] nor followed by [A-Za-z0-9-]. The
# leading guard also carries the decimal point (see _VAL_FRACTION below);
# the trailing one deliberately does not. Shipment references
# therefore stay byte-identical while every standalone monetary form still
# snaps. renders_unchanged() below exposes the same grammar to the input
# boundary so a caller reference this gate WOULD rewrite is rejected at the
# door rather than silently mangled in the document.
# Structural tokens stay untouched: unit-suffixed measurements ("300kg",
# "1500pcs"), HS codes, bare counts, years without currency adjacency.
# ALL matched tokens, at ANY magnitude, must sit on the rounding grid;
# off-grid = a full-precision figure reaching the external surface,
# snapped + audited.
# Grammar (group-based; no variable-width lookbehinds — the marker/value
# delimiter is a `\s*` GROUP, so it can be ARBITRARY whitespace: spaces, tabs,
# newlines, any run length; the identifier guards above are exact
# single-character assertions). Every value accepts an optional explicit
# +/- sign and an optional decimal part.
# Branch order matters: currency-context branches first, then form-based.
_CURRENCY_MARKER = r"(?:\b[A-Z]{3}|[¥￥$€£円₩])"
# Delimiter between a currency marker and its value: horizontal whitespace and at
# most ONE newline — never a paragraph break. A plain `\s*` spans blank lines, so a
# 3-letter uppercase word ending a line would bind to the number that opens the next
# block and rewrite it ("Currency: JPY\n\n3. Cash Position" -> "0. Cash Position").
# Every enumerated leak form (spaces, tabs, single newline, signed, symmetric,
# comma-grouped) still matches.
_GATE_DELIM = r"[ \t]*(?:\n[ \t]*)?"

# A monetary amount may carry a decimal part, and every value alternative
# absorbs it into the SAME token. Without that the fraction stands alone: the
# fraction of "9999.99999" is a five-digit run and `.` is not an identifier
# character, so the guards admitted it and the form-based branch rewrote a
# percentage into "9999.100,000". The same blindness cut the other way — the
# gate saw only the integer part of "USD 1000.5", found it on the grid, and
# published an off-grid amount untouched. The decimal point therefore also
# joins the LEADING guard, so no match can begin inside a fraction — but NOT
# the trailing guard, where an amount ending a sentence ("totals JPY 9999.")
# would escape the grid entirely.
# Nor may the absorption be a plain optional. `(?:\.\d+)?` is a backtracking
# point: the moment the character behind the fraction fails the trailing
# guard, the engine abandons the fraction and re-matches the bare integer
# part, and a suffixed "JPY 1234.56m" is corrupted to "JPY 1,000.56m" exactly
# as before. Two arms, no choice: the fraction is taken whole, or the grammar
# asserts none begins here.
_VAL_FRACTION = r"(?:\.\d+|(?!\.\d))"

_NUM_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9.-])"
    # marker THEN value: "JPY 9999", "JPY  -9999", "JPY\t9999", "¥9999", "USD\n+9999".
    # The value alternatives accept a comma-grouped form FIRST: the regex is
    # leftmost-first, so without it "JPY 1,234" would match as marker + "1"
    # (mangling the number on the snap) instead of as the whole grouped value
    # — an on-grid "JPY 1,000" must stay byte-identical, and an off-grid
    # "JPY 1,234" must snap as 1234, not as 1.
    rf"(?:(?P<pre>{_CURRENCY_MARKER}{_GATE_DELIM})"
    rf"(?P<val_after>[+-]?\d{{1,3}}(?:,\d{{3}})+{_VAL_FRACTION}|[+-]?\d{{1,4}}{_VAL_FRACTION})"
    # value THEN marker: "9999 JPY", "-9999\tJPY", "9999円", "+9999  $"
    rf"|(?P<val_before>[+-]?\d{{1,4}}{_VAL_FRACTION})"
    rf"(?P<post>{_GATE_DELIM}(?:[A-Z]{{3}}\b|[¥￥$€£円₩]))"
    # form-based, standalone at any magnitude: comma-grouped or 5+-digit runs
    rf"|(?P<val_form>[+-]?\d{{1,3}}(?:,\d{{3}})+{_VAL_FRACTION}|[+-]?\d{{5,}}{_VAL_FRACTION}))"
    r"(?![A-Za-z0-9-])"
)


def _enforce_precision(result: str) -> Tuple[str, int]:
    """Snap every monetary-form token onto the approved external grid.

    Returns (sanitised_result, redaction_count). A redaction means a
    full-precision monetary figure reached the external surface — the gate
    rounds it onto the approved grid. The currency marker, the original
    delimiter whitespace, and the explicit sign of the original token are all
    preserved on the snapped replacement.
    """
    redactions = 0

    def _snap(match: "re.Match[str]") -> str:
        nonlocal redactions
        pre = match.group("pre") or ""
        post = match.group("post") or ""
        token = match.group("val_after") or match.group("val_before") or match.group("val_form")
        # float(), not int(): the token may carry a decimal part, and the WHOLE
        # amount decides whether it is on the grid — "USD 1000.5" is off-grid
        # even though its integer part is not.
        value = float(token.replace(",", ""))  # float() understands leading +/-
        if value % EXTERNAL_ROUND_UNIT == 0:
            return match.group(0)
        redactions += 1
        snapped = round(value / EXTERNAL_ROUND_UNIT) * EXTERNAL_ROUND_UNIT
        plus = "+" if token.startswith("+") and snapped >= 0 else ""
        return f"{pre}{plus}{snapped:,d}{post}"

    return _NUM_TOKEN_RE.sub(_snap, result), redactions


def renders_unchanged(text: str) -> bool:
    """True when *text* passes the precision grid byte-identically.

    The input boundary calls this on every caller-supplied string that is
    rendered into the declaration: a value this gate would rewrite is rejected
    with a field-naming error instead of being silently mangled in the
    document. Identifiers whose digit runs sit inside a longer alphanumeric
    token ("LOG-SHIP-20260712-001") pass; a bare monetary-looking form
    ("LOG-1234") does not.
    """
    return _enforce_precision(text)[1] == 0


def _security_gate_output(content: str) -> Optional[str]:
    """Scan output for disallowed credential / restricted-identifier patterns.

    Returns the first violation name, or None if the output is clean. The name
    is a PATTERN LABEL, never the matched value — echoing the value would put
    the refused string back into this node's own result, where the framework's
    output-side credential scan raises and DISCARDS the whole return, undoing
    the containment in _blocked().

    Recognition is the UNION of the framework's own detect_credentials() and
    the domain-only table above. The framework half is not optional: it refuses
    shapes this table cannot express, and it raises on them one node earlier,
    so a gate that did not also refuse them would never run at all (see the
    _CREDENTIAL_PATTERNS note). The two therefore cannot drift apart.

    Module-level function (not a node instance method) — the framework
    auto-wraps node instance methods on the real invoke path, so the gate must
    live at module level. The agent class re-exports this function, so there is
    one source of truth for the pattern set.
    """
    findings = detect_credentials(content)
    if findings:
        return str(findings[0]["type"])
    for pattern, name in _CREDENTIAL_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            return name
    return None


def is_publishable(text: str) -> bool:
    """True when *text* would pass the external output gate untouched.

    Combines both boundary rules — no disallowed pattern, and byte-identical
    under the precision grid — so the input boundary can reject a caller value
    the output gate would otherwise withhold or rewrite, and say which field
    caused it.
    """
    return _security_gate_output(text) is None and renders_unchanged(text)


def _redact_blocked_fields(result: str, state: AgentState) -> Tuple[str, List[str]]:
    """Replace verbatim embeddings of the caller's raw payload with [REDACTED].

    Returns (sanitised_result, redacted_field_names). Only substantial values
    (len > 10) are matched so short incidental overlaps are not redacted.
    """
    redacted: List[str] = []
    sanitised = result
    for field in sorted(_BLOCKED_FIELDS):
        value = state.get(field)
        if isinstance(value, str) and len(value) > 10 and value in sanitised:
            sanitised = sanitised.replace(value, "[REDACTED]")
            redacted.append(field)
    return sanitised, redacted


# Reason code -> the sentence the caller reads. A code with no entry falls
# back to the generic one rather than leaking the code itself.
_DEGRADED_MESSAGES = {
    "EMPTY_INPUT": EMPTY_INPUT,
    "QUESTION_TOO_LONG": TOO_LONG,
    "INVALID_REQUEST": INVALID_VALUE,
}


class PostProcessNode(FunctionNode):
    """Apply the external output gate and expose the customs declaration document.

    Outer backbone post_process slot. Declared ANONYMOUS — caller trust was
    already enforced at PreProcessNode (VERIFIED_EXTERNAL).

    Input state keys:
        customs_document:       str   — formatted document from inner OutputFormatNode
        broker_review_required: bool  — licensed-broker review flag

    Output state keys (partial dict):
        formatted_output: str
        result:           str
        status:           str
        error_log:        list[str]  (only on ERROR)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    @staticmethod
    def _blocked(violation: str, state: AgentState) -> dict[str, Any]:
        """Withhold the document: the output gate found a disallowed pattern.

        EVERY outward representation is replaced with the same sanitised stub —
        the blocked content must not survive under another field name.

        CONTAINMENT: blocking is not a status flip. The framework envelope
        resolves the caller-facing value as ``formatted_output or result``
        WITHOUT consulting status, so returning ERROR while leaving the
        document-bearing fields populated still ships the refused declaration
        inside the error envelope. Every field that carries declaration content
        is therefore overwritten here — not only the two the envelope reads,
        but ``customs_document`` / ``customs_sections`` / ``compliance_flags``
        too, so the block does not depend on the accessor remembering to
        withhold them.

        The stub is deliberately NON-EMPTY. A falsy replacement ("" or {}) is
        exactly what re-opens the hole: ``formatted_output or result`` would
        fall straight through to the pre-gate value the gate just refused.

        ``broker_review_required`` is left alone on purpose: it is an inert
        boolean determination, carries no declaration text, and is withheld by
        the accessor on every non-success path. The surviving key set is pinned
        by an inventory guard in the tests so a future content-bearing field
        cannot quietly join it.

        The violation NAME is a pattern label only — never the matched value.
        Echoing it would put the refused string back into this node's own
        result, where the framework credential scan raises and DISCARDS this
        entire return, leaving the un-cleared state (and the document) in place.
        """
        logger.error("PostProcessNode: output gate violation — %s", violation)
        emit_trace_event(
            "post_process_output_gate_violation",
            {"violation": violation},
            state,
        )
        sanitised = (
            f"[CUSTOMS DOCUMENT WITHHELD: output contained a disallowed pattern "
            f"({violation}). Contact the trade-compliance team for the original document.]"
        )
        return {
            "formatted_output": sanitised,
            "result": sanitised,
            "customs_document": None,
            "customs_sections": None,
            "compliance_flags": None,
            "status": AgentStatus.ERROR.value,
            "error_log": [f"PostProcessNode: output gate — {violation} detected in document"],
        }

    def execute(self, state: AgentState) -> dict[str, Any]:
        # A run declined upstream has nothing to format. Render the reason as
        # the caller-facing body and carry the marker onward.
        marker = state.get("error_code")
        if marker:
            message = _DEGRADED_MESSAGES.get(marker, INPUT_REJECTED)
            emit_trace_event("post_process_degraded", {"reason": marker}, state)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": marker,
                "result": message,
                "formatted_output": message,
            }
        customs_document: str = state.get("customs_document") or ""
        broker_review_required: bool = bool(state.get("broker_review_required"))

        # -- Fallback for an empty document -----------------------------------
        if not customs_document.strip():
            logger.warning("PostProcessNode: customs_document is empty — using fallback message")
            customs_document = (
                "[Customs Documentation] No document content generated. " "Check error_log for upstream failures."
            )

        # -- Layer 1: verbatim caller-payload redaction ------------------------
        customs_document, redacted_fields = _redact_blocked_fields(customs_document, state)
        if redacted_fields:
            logger.error(
                "PostProcessNode: caller payload embedded verbatim in the document — %s",
                ", ".join(redacted_fields),
            )
            emit_trace_event(
                "post_process_verbatim_redaction",
                {"fields": redacted_fields},
                state,
            )

        # -- Layer 2: credential / restricted-identifier scan ------------------
        # Runs BEFORE the numeric layer so the snap cannot rewrite a labelled
        # identifier out of this scan's reach.
        violation = _security_gate_output(customs_document)
        if violation:
            return self._blocked(violation, state)

        # -- Layer 3: monetary precision grid ----------------------------------
        customs_document, precision_redactions = _enforce_precision(customs_document)
        if precision_redactions:
            logger.warning(
                "PostProcessNode: %d off-grid monetary token(s) snapped to the external grid",
                precision_redactions,
            )
            emit_trace_event(
                "post_process_precision_redaction",
                {"redaction_count": precision_redactions},
                state,
            )

        # -- Layer 4: re-scan the rewritten surface ----------------------------
        # Snapping cannot introduce a secret, but the final bytes that leave the
        # agent are never an unscanned surface.
        violation = _security_gate_output(customs_document)
        if violation:
            return self._blocked(violation, state)

        logger.info(
            "PostProcessNode: output gate passed — length=%d broker_review_required=%s",
            len(customs_document),
            broker_review_required,
        )
        emit_trace_event(
            "post_process_complete",
            {
                "output_length": len(customs_document),
                "broker_review_required": broker_review_required,
                "precision_redactions": precision_redactions,
            },
            state,
        )

        return {
            "formatted_output": customs_document,
            "result": customs_document,
            "status": AgentStatus.SUCCESS.value,
        }

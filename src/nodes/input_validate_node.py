"""AgentCore Platform v1.0"""

# LOG-C2-011 — InputValidateNode
# Inner domain node 1: domain-level validation of the shipment payload and of
# the caller's per-invocation declaration options.
#
# Distinct from PreProcessNode (caller trust + structural JSON check): this node
# applies the domain business rules — field types, bounded numerics, goods-line
# validation, country-code normalisation, HS-code canonicalisation and per-line
# value computation — and it is the node that consumes the caller's
# input_context, bridged in from the outer graph (src/graph/context_bridge.py).
#
# Two rules govern every caller-supplied value here:
#   numbers  — every one goes through finite_in_range(): bools, non-numerics,
#              NaN/+-Infinity and out-of-range magnitudes are rejected. NaN
#              would otherwise parse fine and compare False against every
#              bound, silently suppressing the broker-review determination.
#   strings  — every string that is rendered into the declaration must survive
#              the external output boundary untouched (is_publishable): a
#              reference the precision grid would rewrite, or text carrying a
#              disallowed pattern, is refused here with a field-naming error
#              rather than mangled or withheld downstream.
# Rejections fail CLOSED and name the field only — a rejected value is never
# echoed into an error message or an audit event.
#
# Inner node — ANONYMOUS trust: the outer PreProcessNode (VERIFIED_EXTERNAL)
# already enforced caller trust, and inner nodes must be ANONYMOUS so the outer
# InvocationContext passes through the GraphNode boundary without rejection.
#
# Returns ONLY the state keys this node writes (partial-dict contract).

import json
import logging
import re
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import INPUT_REJECTED
from src.services.progress import emit_progress

from src.nodes.post_process_node import is_publishable
from src.schemas.state import finite_in_range, is_inert_identifier, to_json

logger = logging.getLogger(__name__)

# Structural limits on the caller payload.
_MAX_GOODS_LINES = 200
_MAX_DESCRIPTION_CHARS = 120
_MAX_NAME_CHARS = 120
_MAX_ADDRESS_CHARS = 200
_MAX_TRANSPORT_CHARS = 80

# Per-field magnitude bounds for the caller's numbers.
_QUANTITY_MAX = 1_000_000
_UNIT_VALUE_MAX = 1_000_000_000.0
_NET_WEIGHT_MAX = 1_000_000.0
# Declaration-option bounds (monetary thresholds in the declaration currency).
_THRESHOLD_MAX = 1_000_000_000_000.0

# Declared defaults for the options a caller may override per invocation.
DEFAULT_BROKER_REVIEW_THRESHOLD = 100_000.0
DEFAULT_VALUE_TIER_HIGH = 100_000.0
DEFAULT_VALUE_TIER_STANDARD = 10_000.0

# ISO 4217 currency code and Incoterms rule — both are 3 uppercase letters.
_CODE_3_RE = re.compile(r"^[A-Z]{3}$")

# Control characters are stripped from free text: a newline in a caller field
# would let caller content forge a section break in the rendered declaration.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

# Digits-only HS codes are canonicalised to the dotted NNNN.NN[.NN[.NN]] form
# customs declarations use.
_HS_DIGITS_RE = re.compile(r"^\d{4}(?:\d{2}){0,3}$")

# ISO 3166-1 alpha-2 country-code aliases for normalisation (customs documents
# require canonical two-letter codes).
_COUNTRY_ALIASES: Dict[str, str] = {
    "japan": "JP",
    "jpn": "JP",
    "jp": "JP",
    "usa": "US",
    "united states": "US",
    "us": "US",
    "china": "CN",
    "prc": "CN",
    "cn": "CN",
    "korea": "KR",
    "south korea": "KR",
    "kr": "KR",
    "germany": "DE",
    "de": "DE",
    "united kingdom": "GB",
    "uk": "GB",
    "gb": "GB",
}


def _sanitise_text(raw: Any, cap: int) -> str:
    """Normalise caller free text: no control characters, collapsed whitespace, capped."""
    text = _CONTROL_RE.sub(" ", str(raw))
    return re.sub(r"\s+", " ", text).strip()[:cap]


def _normalise_country(raw: Any) -> str:
    """Normalise a country name/code to an ISO 3166-1 alpha-2 code."""
    if not raw:
        return ""
    cleaned = _sanitise_text(raw, _MAX_NAME_CHARS)
    if not cleaned:
        return ""
    return _COUNTRY_ALIASES.get(cleaned.lower(), cleaned.upper()[:2])


def _normalise_hs_code(raw: Any) -> str:
    """Canonicalise an HS code to the dotted form used on the declaration.

    "610910" -> "6109.10", "61091000" -> "6109.10.00". Codes that are already
    dotted, or that are not a plain digit sequence, are returned as supplied
    (structural validity is a broker-review criterion, not a rejection reason).
    """
    code = _sanitise_text(raw, 32).upper()
    if _HS_DIGITS_RE.match(code) and len(code) > 4:
        return ".".join([code[:4]] + [code[i : i + 2] for i in range(4, len(code), 2)])
    return code


def _validate_good(raw: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Validate and normalise a single goods line.

    Returns (line, None) on success or (None, field_name) naming the first field
    that failed, so the caller can report WHICH field was rejected without ever
    echoing the rejected value.
    """
    if not isinstance(raw, dict):
        return None, "goods[]"
    description = _sanitise_text(raw.get("description", ""), _MAX_DESCRIPTION_CHARS)
    if not description or not is_publishable(description):
        return None, "goods[].description"
    hs_code = _normalise_hs_code(raw.get("hs_code", ""))
    if not hs_code or not is_publishable(hs_code):
        return None, "goods[].hs_code"

    quantity = finite_in_range(raw.get("quantity", 0), 0, _QUANTITY_MAX)
    if quantity is None or float(quantity) != int(quantity):
        return None, "goods[].quantity"
    unit_value = finite_in_range(raw.get("unit_value", 0.0), 0, _UNIT_VALUE_MAX)
    if unit_value is None:
        return None, "goods[].unit_value"
    net_weight_kg = finite_in_range(raw.get("net_weight_kg", 0.0), 0, _NET_WEIGHT_MAX)
    if net_weight_kg is None:
        return None, "goods[].net_weight_kg"

    country_of_origin = _normalise_country(raw.get("country_of_origin", ""))
    if country_of_origin and not is_publishable(country_of_origin):
        return None, "goods[].country_of_origin"

    return {
        "description": description,
        "hs_code": hs_code,
        "quantity": int(quantity),
        "unit_value": float(unit_value),
        "net_weight_kg": float(net_weight_kg),
        "country_of_origin": country_of_origin,
        "line_value": round(int(quantity) * float(unit_value), 2),
    }, None


def _validate_declaration_options(
    input_context: Any,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Validate the caller's per-invocation declaration options.

    Every option is optional; an absent option falls back to the declared
    default. A present-but-invalid option fails CLOSED and names the field —
    the thresholds decide whether a licensed broker must review the
    declaration, so accepting an unparseable or non-finite one would silently
    disable that determination.

    Returns (options, None) or (None, field_name).
    """
    options: Dict[str, Any] = {
        "broker_review_threshold": DEFAULT_BROKER_REVIEW_THRESHOLD,
        "value_tier_high": DEFAULT_VALUE_TIER_HIGH,
        "value_tier_standard": DEFAULT_VALUE_TIER_STANDARD,
        "declaration_reference": "",
    }
    if not isinstance(input_context, dict) or not input_context:
        return options, None

    if "broker_review_threshold" in input_context:
        threshold = finite_in_range(input_context["broker_review_threshold"], 0, _THRESHOLD_MAX)
        if threshold is None:
            return None, "input_context.broker_review_threshold"
        options["broker_review_threshold"] = threshold

    if "value_tier_thresholds" in input_context:
        table = input_context["value_tier_thresholds"]
        if not isinstance(table, dict):
            return None, "input_context.value_tier_thresholds"
        for key, option_key in (("high", "value_tier_high"), ("standard", "value_tier_standard")):
            if key not in table:
                continue
            parsed = finite_in_range(table[key], 0, _THRESHOLD_MAX)
            if parsed is None:
                return None, f"input_context.value_tier_thresholds.{key}"
            options[option_key] = parsed
        if options["value_tier_high"] < options["value_tier_standard"]:
            return None, "input_context.value_tier_thresholds.high"

    if "declaration_reference" in input_context:
        reference = input_context["declaration_reference"]
        if not is_inert_identifier(reference) or not is_publishable(str(reference)):
            return None, "input_context.declaration_reference"
        options["declaration_reference"] = str(reference)

    return options, None


class InputValidateNode(FunctionNode):
    """Domain validation of the shipment payload and caller options for LOG-C2-011.

    Applies business-rule checks beyond the structural JSON check in
    PreProcessNode: bounded numerics, goods-line validation, country and
    HS-code normalisation, per-line value computation, and validation of the
    caller's declaration options carried on input_context.

    Inner node — ANONYMOUS trust (see module docstring).

    Input state keys:
        validated_input: str   — normalised JSON string from PreProcessNode
                                 Falls back to user_input for unit-test convenience.
        input_context:   dict  — per-invocation declaration options (bridged in)

    Output state keys (partial dict):
        shipment_data:       str  — JSON-serialised normalised shipment payload
        declaration_options: str  — JSON-serialised validated caller options
        status:              str
        error_log:           list[str]  (only on ERROR)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def _reject(self, state: AgentState, reason: str, message: str, code: str = "INVALID_REQUEST") -> dict[str, Any]:
        """Fail closed, naming the field — never the rejected value."""
        logger.warning("InputValidateNode: %s", message)
        emit_trace_event("input_validate_failed", {"reason": reason}, state)
        if code:
            # A value the caller can correct: the run COMPLETES carrying the
            # reason so the request can be sent again on the same conversation.
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": code,
                "error_log": [f"InputValidateNode: {message}"],
            }
        return {
            "status": AgentStatus.ERROR.value,
            "error_log": [f"InputValidateNode: {message}"],
        }

    def execute(self, state: AgentState) -> dict[str, Any]:
        raw = state.get("validated_input") or state.get("user_input", "")

        # -- Parse -------------------------------------------------------------
        try:
            payload: Dict[str, Any] = json.loads(raw) if isinstance(raw, str) else {}
        except (json.JSONDecodeError, ValueError):
            return self._reject(state, "json_parse_error", "user_input is not parseable JSON")

        if not isinstance(payload, dict):
            return self._reject(state, "payload_not_dict", "user_input is not a JSON object")

        # -- Caller declaration options (bridged input_context) -----------------
        options, bad_option = _validate_declaration_options(state.get("input_context"))
        if options is None:
            return self._reject(
                state,
                "invalid_declaration_option",
                f"rejected declaration option: {bad_option} is missing, non-finite or out of range",
            )

        # -- Shipment identity --------------------------------------------------
        # Sanitised without truncation to 64: the inert grammar's own length
        # bound must REJECT an over-long reference, never silently shorten it
        # into a different reference.
        shipment_id = _sanitise_text(payload.get("shipment_id", ""), 256)
        if not shipment_id:
            return self._reject(state, "empty_shipment_id", "shipment_id is missing or empty")
        if not is_inert_identifier(shipment_id) or not is_publishable(shipment_id):
            return self._reject(
                state,
                "invalid_shipment_id",
                "shipment_id must be 1-64 characters of [A-Za-z0-9._-] and must not read "
                "as a monetary figure at the output boundary",
            )

        # -- Declaration currency and delivery terms ---------------------------
        currency = _sanitise_text(payload.get("currency", "USD"), 8).upper() or "USD"
        if not _CODE_3_RE.match(currency):
            return self._reject(state, "invalid_currency", "currency must be a 3-letter ISO 4217 code")
        incoterms = _sanitise_text(payload.get("incoterms", ""), 8).upper()
        if incoterms and not _CODE_3_RE.match(incoterms):
            return self._reject(state, "invalid_incoterms", "incoterms must be a 3-letter delivery-terms code")

        # -- Validate goods lines ----------------------------------------------
        raw_goods = payload.get("goods", [])
        if not isinstance(raw_goods, list) or not raw_goods:
            return self._reject(state, "no_goods", "goods list is empty")
        if len(raw_goods) > _MAX_GOODS_LINES:
            return self._reject(
                state,
                "too_many_goods_lines",
                f"goods list exceeds the {_MAX_GOODS_LINES}-line limit",
            )

        goods: List[Dict[str, Any]] = []
        for raw_good in raw_goods:
            validated, bad_field = _validate_good(raw_good)
            if validated is None:
                return self._reject(
                    state,
                    "invalid_goods_line",
                    f"rejected goods line: {bad_field} is missing, out of range or "
                    "not renderable on the declaration",
                )
            goods.append(validated)

        # -- Normalise parties --------------------------------------------------
        exporter = payload.get("exporter") or {}
        importer = payload.get("importer") or {}
        if not isinstance(exporter, dict) or not isinstance(importer, dict):
            return self._reject(state, "party_not_object", "exporter and importer must be JSON objects")
        parties: Dict[str, Dict[str, str]] = {}
        for role, party in (("exporter", exporter), ("importer", importer)):
            name = _sanitise_text(party.get("name", ""), _MAX_NAME_CHARS)
            address = _sanitise_text(party.get("address", ""), _MAX_ADDRESS_CHARS)
            if name and not is_publishable(name):
                return self._reject(state, "invalid_party_name", f"rejected party field: {role}.name")
            if address and not is_publishable(address):
                return self._reject(state, "invalid_party_address", f"rejected party field: {role}.address")
            parties[role] = {
                "name": name,
                "address": address,
                "country": _normalise_country(party.get("country", "")),
            }

        # -- Normalise transport -------------------------------------------------
        raw_transport = payload.get("transport") or {}
        if not isinstance(raw_transport, dict):
            return self._reject(state, "transport_not_object", "transport must be a JSON object")
        transport: Dict[str, str] = {}
        for key in ("mode", "port_of_loading", "port_of_discharge"):
            value = _sanitise_text(raw_transport.get(key, ""), _MAX_TRANSPORT_CHARS)
            if value and not is_publishable(value):
                return self._reject(state, "invalid_transport_field", f"rejected transport field: {key}")
            transport[key] = value

        shipment_data: Dict[str, Any] = {
            "shipment_id": shipment_id,
            "declaration_reference": options["declaration_reference"],
            "exporter": parties["exporter"],
            "importer": parties["importer"],
            "goods": goods,
            "transport": transport,
            "incoterms": incoterms,
            "currency": currency,
        }

        logger.info(
            "InputValidateNode: shipment_id=%s goods=%d exporter=%s importer=%s",
            shipment_id,
            len(goods),
            parties["exporter"]["country"],
            parties["importer"]["country"],
        )
        emit_trace_event(
            "input_validate_complete",
            {
                "shipment_id": shipment_id,
                "goods_count": len(goods),
                "exporter_country": parties["exporter"]["country"],
                "importer_country": parties["importer"]["country"],
                "caller_options_applied": sorted(k for k, v in options.items() if v not in ("", None)),
            },
            state,
        )

        return {
            "shipment_data": to_json(shipment_data),
            "declaration_options": to_json(options),
            "status": AgentStatus.SUCCESS.value,
        }

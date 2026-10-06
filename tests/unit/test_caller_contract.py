# LOG-C2-011 — The caller-data contract: every input hostile until proven bounded.
#
# Two rules are pinned here, field by field:
#   numbers  — parsed by a FINITE + bounded parser. float() parses "NaN" and
#              "Infinity", Python's json accepts bare NaN in a request body, and
#              every IEEE comparison against NaN is False — so an unchecked NaN
#              threshold would suppress the broker-review determination this
#              template exists to make. Rejection fails CLOSED.
#   strings  — locked to the inert identifier grammar where they are an
#              identifier, sanitised and bounded where they are free text, and
#              in both cases refused if the external output boundary would
#              rewrite or withhold them.
# Rejected values are never echoed back into an error message.

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from src.nodes.input_validate_node import (
    _MAX_DESCRIPTION_CHARS,
    _MAX_GOODS_LINES,
    InputValidateNode,
)
from src.schemas.state import finite_in_range, from_json, is_inert_identifier

# The value forms that make an unchecked numeric field fail OPEN.
NON_FINITE_MATRIX = ["NaN", "Infinity", "-Infinity", float("nan"), float("inf"), float("-inf")]
NON_FINITE_IDS = ["str-nan", "str-inf", "str-neginf", "raw-nan", "raw-inf", "raw-neginf"]


def _payload(**overrides) -> dict:
    payload = {
        "shipment_id": "LOG-SHIP-20260712-001",
        "exporter": {"name": "Kanto Trading", "address": "1-2-3 Minato", "country": "Japan"},
        "importer": {"name": "West Imports LLC", "address": "500 Market St", "country": "USA"},
        "goods": [
            {
                "description": "Cotton knit T-shirts",
                "hs_code": "6109.10",
                "quantity": 1200,
                "unit_value": 4.5,
                "net_weight_kg": 180.0,
                "country_of_origin": "Japan",
            }
        ],
        "transport": {"mode": "sea", "port_of_loading": "Tokyo", "port_of_discharge": "Oakland"},
        "incoterms": "FOB",
        "currency": "USD",
    }
    payload.update(overrides)
    return payload


def _goods(**overrides) -> list:
    line = {
        "description": "Cotton knit T-shirts",
        "hs_code": "6109.10",
        "quantity": 10,
        "unit_value": 1.0,
        "net_weight_kg": 1.0,
        "country_of_origin": "JP",
    }
    line.update(overrides)
    return [line]


@pytest.fixture(autouse=True)
def patch_emit(monkeypatch):
    monkeypatch.setattr("src.nodes.input_validate_node.emit_trace_event", lambda *a, **k: None)


def _run(payload: dict, input_context: dict | None = None) -> dict:
    state = {
        "validated_input": json.dumps(payload),
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
    }
    if input_context is not None:
        state["input_context"] = input_context
    return InputValidateNode()(state)


# -- The parser itself ---------------------------------------------------------


class TestFiniteParser:
    @pytest.mark.parametrize("value", NON_FINITE_MATRIX, ids=NON_FINITE_IDS)
    def test_non_finite_values_are_rejected(self, value):
        assert finite_in_range(value, 0, 1000) is None

    @pytest.mark.parametrize("value", [True, False, None, [], {}, "abc", ""])
    def test_non_numeric_values_are_rejected(self, value):
        assert finite_in_range(value, 0, 1000) is None

    @pytest.mark.parametrize("value", [-1, 1001, "5000"])
    def test_out_of_range_values_are_rejected(self, value):
        assert finite_in_range(value, 0, 1000) is None

    @pytest.mark.parametrize("value,expected", [(0, 0.0), ("12.5", 12.5), (1000, 1000.0)])
    def test_in_range_values_parse(self, value, expected):
        assert finite_in_range(value, 0, 1000) == expected

    def test_nan_would_otherwise_compare_false_against_every_bound(self):
        """The exact failure mode the parser exists to prevent."""
        nan = float("nan")
        assert not (0 <= nan <= 1000)
        assert not (nan >= 100_000)  # a NaN threshold suppresses every escalation


# -- Goods-line numerics: the full non-finite matrix, per field ----------------


class TestGoodsLineNumerics:
    @pytest.mark.parametrize("field", ["quantity", "unit_value", "net_weight_kg"])
    @pytest.mark.parametrize("value", NON_FINITE_MATRIX, ids=NON_FINITE_IDS)
    def test_non_finite_goods_numeric_fails_closed(self, field, value):
        result = _run(_payload(goods=_goods(**{field: value})))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert any(field in e for e in result["error_log"])

    @pytest.mark.parametrize(
        "field,value",
        [
            ("quantity", -1),
            ("quantity", 10_000_001),
            ("quantity", 1.5),
            ("quantity", True),
            ("unit_value", -0.01),
            ("unit_value", 1e12),
            ("unit_value", "many"),
            ("net_weight_kg", -1.0),
            ("net_weight_kg", 1e9),
        ],
    )
    def test_out_of_contract_goods_numeric_fails_closed(self, field, value):
        result = _run(_payload(goods=_goods(**{field: value})))
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_rejection_names_the_field_and_never_echoes_the_value(self):
        result = _run(_payload(goods=_goods(quantity="leak-me-please")))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert any("goods[].quantity" in e for e in result["error_log"])
        assert all("leak-me-please" not in e for e in result["error_log"])


# -- Declaration options carried on input_context ------------------------------


class TestDeclarationOptions:
    @pytest.mark.parametrize("value", NON_FINITE_MATRIX, ids=NON_FINITE_IDS)
    def test_non_finite_broker_review_threshold_fails_closed(self, value):
        result = _run(_payload(), {"broker_review_threshold": value})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert any("broker_review_threshold" in e for e in result["error_log"])

    @pytest.mark.parametrize("key", ["high", "standard"])
    @pytest.mark.parametrize("value", NON_FINITE_MATRIX, ids=NON_FINITE_IDS)
    def test_non_finite_tier_table_entry_fails_closed(self, key, value):
        """Config-style TABLES are caller input too, not trusted configuration."""
        result = _run(_payload(), {"value_tier_thresholds": {key: value}})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert any(f"value_tier_thresholds.{key}" in e for e in result["error_log"])

    def test_inverted_tier_table_fails_closed(self):
        result = _run(_payload(), {"value_tier_thresholds": {"high": 100.0, "standard": 5000.0}})
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_non_object_tier_table_fails_closed(self):
        result = _run(_payload(), {"value_tier_thresholds": [1, 2, 3]})
        assert result["status"] == AgentStatus.SUCCESS.value

    @pytest.mark.parametrize("value", [-1, 1e13, "soon"])
    def test_out_of_range_threshold_fails_closed(self, value):
        result = _run(_payload(), {"broker_review_threshold": value})
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_valid_options_are_applied(self):
        result = _run(
            _payload(),
            {
                "broker_review_threshold": 8000.0,
                "value_tier_thresholds": {"high": 5000.0, "standard": 1000.0},
                "declaration_reference": "REF-DEMO-01",
            },
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        options = from_json(result["declaration_options"])
        assert options["broker_review_threshold"] == 8000.0
        assert options["value_tier_high"] == 5000.0
        assert options["declaration_reference"] == "REF-DEMO-01"

    def test_absent_options_fall_back_to_the_declared_defaults(self):
        options = from_json(_run(_payload())["declaration_options"])
        assert options["broker_review_threshold"] == 100_000.0
        assert options["value_tier_high"] == 100_000.0
        assert options["value_tier_standard"] == 10_000.0
        assert options["declaration_reference"] == ""

    @pytest.mark.parametrize(
        "reference",
        ["ref with spaces", "ref;drop", "20260712", "R" * 65, 12345, "<b>ref</b>"],
    )
    def test_non_inert_declaration_reference_fails_closed(self, reference):
        """The reference renders into the declaration — inert identifiers only."""
        result = _run(_payload(), {"declaration_reference": reference})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert any("declaration_reference" in e for e in result["error_log"])


# -- Identifier and structural limits -----------------------------------------


class TestIdentifiersAndLimits:
    @pytest.mark.parametrize("shipment_id", ["LOG SHIP 1", "20260712", "LOG-1234", "S" * 65, "ship;drop"])
    def test_unsafe_shipment_id_fails_closed(self, shipment_id):
        result = _run(_payload(shipment_id=shipment_id))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert any("shipment_id" in e for e in result["error_log"])

    @pytest.mark.parametrize("shipment_id", ["LOG-SHIP-20260712-001", "AWB-125-12345675", "ship_a.1"])
    def test_valid_shipment_id_is_accepted(self, shipment_id):
        result = _run(_payload(shipment_id=shipment_id))
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_inert_identifier_rejects_a_bare_digit_run(self):
        """A bare digit run is indistinguishable from a monetary figure at the
        output boundary, so it is not a valid reference."""
        assert is_inert_identifier("LOG-SHIP-20260712-001") is True
        assert is_inert_identifier("20260712") is False

    @pytest.mark.parametrize("currency", ["US", "usd!", "DOLLARS", "12"])
    def test_invalid_currency_fails_closed(self, currency):
        result = _run(_payload(currency=currency))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert any("currency" in e for e in result["error_log"])

    def test_absent_currency_defaults_to_usd(self):
        payload = _payload()
        del payload["currency"]
        result = _run(payload)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert from_json(result["shipment_data"])["currency"] == "USD"

    def test_invalid_incoterms_fails_closed(self):
        result = _run(_payload(incoterms="free on board"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert any("incoterms" in e for e in result["error_log"])

    def test_goods_entry_cap_is_enforced(self):
        result = _run(_payload(goods=_goods() * (_MAX_GOODS_LINES + 1)))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert any("line limit" in e for e in result["error_log"])

    def test_long_free_text_is_capped_not_rejected(self):
        result = _run(_payload(goods=_goods(description="Cotton " * 100)))
        assert result["status"] == AgentStatus.SUCCESS.value
        description = from_json(result["shipment_data"])["goods"][0]["description"]
        assert len(description) <= _MAX_DESCRIPTION_CHARS

    def test_free_text_carrying_a_credential_fails_closed(self):
        result = _run(_payload(goods=_goods(description="api_key = sk-abcdefghij0123456789")))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert any("goods[].description" in e for e in result["error_log"])

    def test_non_object_party_fails_closed(self):
        result = _run(_payload(exporter="Kanto Trading"))
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_non_object_transport_fails_closed(self):
        result = _run(_payload(transport="sea"))
        assert result["status"] == AgentStatus.SUCCESS.value

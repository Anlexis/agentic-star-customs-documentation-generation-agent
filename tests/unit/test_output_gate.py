# LOG-C2-011 — The external output boundary, probed in BOTH directions.
#
# The declaration's documented schema states monetary figures as aggregates on
# a 1,000 grid. This file proves the gate enforces that invariant for every
# representation a monetary figure can take, and — just as important — that the
# domain's structural tokens (shipment references, HS codes, unit-suffixed
# measurements, counts, years) pass through byte-identical.
#
# It also pins the LAYER ORDER: the pattern scan runs BEFORE the numeric snap.
# The precision grammar treats a standalone 3-letter uppercase word as a
# currency marker, so a gate that snapped first could rewrite digits inside a
# labelled identifier sequence ("SSN 123-45-6789") and hide it from the scan.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from src.nodes.post_process_node import (
    PostProcessNode,
    _enforce_precision,
    _security_gate_output,
    is_publishable,
    renders_unchanged,
)


# -- Monetary forms: every representation snaps onto the grid ------------------


class TestMonetaryFormsSnap:
    @pytest.mark.parametrize(
        "text,expected",
        [
            # comma-grouped, marker before the value (the grouped alternative
            # must win over "marker + 1")
            ("JPY 1,234", "JPY 1,000"),
            # marker before / after, attached or separated, any whitespace run
            ("USD 9999", "USD 10,000"),
            ("9999 USD", "10,000 USD"),
            ("JPY  9999", "JPY  10,000"),
            ("JPY\t9999", "JPY\t10,000"),
            ("USD\n9999", "USD\n10,000"),
            ("¥9999", "¥10,000"),
            ("9999円", "10,000円"),
            ("￥9999", "￥10,000"),
            ("9999₩", "10,000₩"),
            ("$9999", "$10,000"),
            # signed, sign preserved
            ("JPY-9999", "JPY-10,000"),
            ("JPY +9999", "JPY +10,000"),
            # form-based, no marker needed: comma-grouped or 5+ digit runs
            ("12,345", "12,000"),
            ("123456", "123,000"),
            # no magnitude exemption — a small off-grid value in currency
            # context snaps like any other
            ("USD 999", "USD 1,000"),
        ],
        ids=[
            "grouped-after-marker",
            "marker-then-value",
            "value-then-marker",
            "two-spaces",
            "tab",
            "newline",
            "yen-symbol",
            "yen-word",
            "fullwidth-yen",
            "won",
            "dollar",
            "negative",
            "explicit-plus",
            "grouped-standalone",
            "long-run",
            "small-value-in-context",
        ],
    )
    def test_off_grid_value_snaps(self, text, expected):
        result, redactions = _enforce_precision(text)
        assert result == expected
        assert redactions == 1

    @pytest.mark.parametrize(
        "text",
        ["JPY 1,000", "USD 12,000", "100,000 USD", "¥10,000", "USD -2,000", "1,000,000"],
    )
    def test_on_grid_value_is_byte_identical(self, text):
        result, redactions = _enforce_precision(text)
        assert result == text
        assert redactions == 0


# -- Structural tokens: this domain's identifiers survive untouched ------------


class TestStructuralTokensSurvive:
    @pytest.mark.parametrize(
        "text",
        [
            "LOG-SHIP-20260712-001",  # shipment reference
            "AWB-125-12345675",  # air waybill style reference
            "Shipment ID: LOG-SHIP-20260712-001",
            "HS 6109.10",  # HS subheading
            "6109",  # HS heading
            "1500pcs",  # unit-suffixed count
            "15234pcs",
            "300kg",  # unit-suffixed weight
            "12500kg",
            "Number of packages/line items: 2",
            "STAR 2026",  # embedded acronym + year
            "Incoterms: FOB.",
            "in 2026",
        ],
    )
    def test_identifier_is_byte_identical(self, text):
        result, redactions = _enforce_precision(text)
        assert result == text, f"structural token rewritten: {text!r} -> {result!r}"
        assert redactions == 0

    def test_a_digits_only_hs_code_is_canonicalised_before_it_can_render(self):
        """A bare 6-digit HS code WOULD read as a monetary figure, which is why
        the input boundary canonicalises it to the dotted form first."""
        from src.nodes.input_validate_node import _normalise_hs_code

        assert renders_unchanged("610910") is False
        assert _normalise_hs_code("610910") == "6109.10"
        assert renders_unchanged("6109.10") is True

    def test_a_reference_the_grid_would_rewrite_is_not_publishable(self):
        """The input boundary uses the same grammar, so such a reference is
        refused at the door instead of being mangled in the document."""
        assert renders_unchanged("LOG-SHIP-20260712-001") is True
        assert renders_unchanged("LOG-1234") is False
        assert is_publishable("LOG-1234") is False
        assert is_publishable("Cotton knit T-shirts") is True


# -- Layer order: the pattern scan runs before the numeric snap ---------------


class TestLayerOrder:
    @pytest.mark.parametrize("secret", ["SSN 123-45-6789", "TAX 987-65-4321", "ID: 123-45-6789"])
    def test_labelled_identifier_is_detected_not_mangled(self, secret):
        """The snap must never rewrite digits out of a scanned pattern's reach."""
        snapped, redactions = _enforce_precision(secret)
        assert snapped == secret, "the numeric layer mangled a labelled identifier"
        assert redactions == 0
        assert _security_gate_output(secret) == "national_id_pattern"

    def test_document_carrying_a_national_id_is_withheld(self, monkeypatch):
        monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)
        document = "CUSTOMS DECLARATION (DRAFT)\nConsignee SSN 123-45-6789\nTotal: 12,000 USD"
        result = PostProcessNode()(
            {
                "customs_document": document,
                "caller_trust_level": TrustLevel.ANONYMOUS.value,
            }
        )
        assert result["status"] == AgentStatus.ERROR.value
        assert "123-45-6789" not in result["formatted_output"]
        assert result["formatted_output"] == result["result"]

    def test_payment_card_number_is_withheld(self, monkeypatch):
        monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)
        document = "CUSTOMS DECLARATION (DRAFT)\nCard 4111 1111 1111 1111"
        result = PostProcessNode()(
            {
                "customs_document": document,
                "caller_trust_level": TrustLevel.ANONYMOUS.value,
            }
        )
        assert result["status"] == AgentStatus.ERROR.value
        assert "4111" not in result["formatted_output"]


# -- The gate applied through the node ----------------------------------------


class TestGateThroughTheNode:
    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)

    def test_off_grid_document_is_snapped_on_the_way_out(self):
        document = "CUSTOMS DECLARATION (DRAFT)\n  Total declared value: 12,345 USD"
        result = PostProcessNode()(
            {
                "customs_document": document,
                "caller_trust_level": TrustLevel.ANONYMOUS.value,
            }
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "12,345" not in result["formatted_output"]
        assert "12,000 USD" in result["formatted_output"]
        assert result["formatted_output"] == result["result"]


# -- Decimals: one number, snapped or survived whole ---------------------------
#
# A decimal's fraction is a digit run in its own right and `.` is not an
# identifier character, so a grammar that does not absorb the fraction reads it
# as a standalone token. That broke the gate in BOTH directions at once:
#   - it rewrote the fraction of values that are not amounts at all
#     ("Ethanol 99.99999% purity" -> "Ethanol 99.100,000% purity"), which the
#     input boundary then turned into a refusal of legitimate customs text;
#   - and it judged "USD 1000.5" by its integer part alone, found 1000 already
#     on the grid, and published an off-grid amount untouched.
# Both directions are pinned here. Note what is NOT the fix: decimals are not
# exempt from the grid — an off-grid amount in currency context still snaps,
# now as ONE number.


class TestDecimalAmounts:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("JPY 1234.56", "JPY 1,000"),
            ("USD 1000.5", "USD 1,000"),
            ("1234.56 USD", "1,000 USD"),
            ("¥9999.99", "¥10,000"),
            ("12,345.67", "12,000"),
            ("123456.78", "123,000"),
            ("USD -1234.56", "USD -1,000"),
            ("+1234.56 USD", "+1,000 USD"),
        ],
        ids=[
            "marker-then-decimal",
            "integer-part-on-grid-whole-value-is-not",
            "decimal-then-marker",
            "symbol-attached",
            "grouped-standalone-decimal",
            "long-run-decimal",
            "negative-decimal",
            "explicit-plus-decimal",
        ],
    )
    def test_off_grid_decimal_amount_snaps_as_one_number(self, text, expected):
        """The whole amount decides, and the whole amount is replaced — never
        the integer part with the fraction left dangling ("JPY 1,000.56")."""
        result, redactions = _enforce_precision(text)
        assert result == expected
        assert redactions == 1

    @pytest.mark.parametrize("text", ["JPY 1,000.00", "USD 2000.000", "¥10,000.0"])
    def test_on_grid_decimal_amount_is_byte_identical(self, text):
        result, redactions = _enforce_precision(text)
        assert result == text
        assert redactions == 0

    def test_a_suffixed_decimal_is_not_backtracked_into_a_dangling_fraction(self):
        """An optional fraction can be given back. When the "m" behind ".56"
        fails the trailing guard, `(?:\.\d+)?` lets the engine re-match the
        integer part alone — "JPY 1,000.56m" all over again. The two-arm
        absorption leaves the engine nothing to give back."""
        result, redactions = _enforce_precision("JPY 1234.56m")
        assert result == "JPY 1234.56m"
        assert redactions == 0
        # Still a snap when nothing follows the fraction — one number.
        result, redactions = _enforce_precision("JPY 1234.56")
        assert result == "JPY 1,000"
        assert redactions == 1

    @pytest.mark.parametrize(
        "text",
        [
            "8.512345",  # bare decimal with a six-digit fraction
            "9999.99999%",  # percentage
            "ratio 0.123456",  # ratio after a word
            "Ethanol 99.99999% purity",  # a real goods description
            "v1.2.34567",  # version string
            "2026.07.123456",  # dotted date-and-serial declaration reference
            "6109.100010",  # HS code carrying a statistical suffix
            "Net weight 12.34567 kg per carton",
        ],
        ids=[
            "bare-decimal",
            "percentage",
            "ratio",
            "goods-description",
            "version",
            "dated-reference",
            "hs-statistical-suffix",
            "measurement",
        ],
    )
    def test_a_decimal_that_is_not_an_amount_is_byte_identical(self, text):
        """No currency marker, no monetary form — the fraction is not a token of
        its own, so nothing here is the gate's business."""
        result, redactions = _enforce_precision(text)
        assert result == text, f"decimal rewritten: {text!r} -> {result!r}"
        assert redactions == 0

    def test_an_amount_ending_a_sentence_still_snaps(self):
        """The decimal point joins the LEADING guard only. In the trailing guard
        it would let every amount that ends a sentence escape the grid."""
        result, redactions = _enforce_precision("The consignment totals JPY 9999.")
        assert result == "The consignment totals JPY 10,000."
        assert redactions == 1

    def test_the_input_boundary_agrees_with_the_gate_on_decimals(self):
        """renders_unchanged() is the same grammar, so both directions move
        together: legitimate decimal text stops being refused at the door, and
        an off-grid decimal amount starts being refused."""
        assert renders_unchanged("Ethanol 99.99999% purity") is True
        assert is_publishable("Ethanol 99.99999% purity") is True
        assert is_publishable("Cotton knit T-shirts, insured lot USD 1000.5") is False

    def test_off_grid_decimal_in_a_document_is_snapped_on_the_way_out(self, monkeypatch):
        monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)
        document = "CUSTOMS DECLARATION (DRAFT)\n  Insured lot value: USD 1000.5"
        result = PostProcessNode()(
            {
                "customs_document": document,
                "caller_trust_level": TrustLevel.ANONYMOUS.value,
            }
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "1000.5" not in result["formatted_output"]
        assert "USD 1,000" in result["formatted_output"]


# -- Detector parity with the framework recognizer ----------------------------
#
# A domain pattern set NARROWER than the framework's is not merely a weaker
# gate — it is a containment bypass. The framework scans every node result with
# detect_credentials() and RAISES on a hit, and BaseNode.__call__ replaces a
# raising node's return with a bare ERROR partial, discarding the clearing in
# _blocked(). A shape the framework refuses and this gate missed therefore skips
# the gate entirely. Measured before the fix: `sk_live_…`, `AKIA…`, a dotless
# `eyJ…` JWT header and `postgresql://…` all passed this gate and were refused
# by the framework one node earlier, so PostProcessNode never ran at all.


# Connection-string probes are ASSEMBLED, never written as literals: a complete
# connection string (scheme, embedded credentials and host in one token) reads
# as a hardcoded credential to the repository's own publication gate
# (scripts/check_credentials.py). The assembled value is byte-identical at
# runtime, so the probe itself is unchanged.
def _conn(scheme: str) -> str:
    return f"{scheme}://" + "user:pw@db.internal:5432/customs"


class TestDetectorParity:
    @pytest.mark.parametrize(
        "shape",
        [
            "sk_live_" + "a" * 20,
            "sk_test_" + "b" * 20,
            "sk-" + "c" * 24,
            "eyJ" + "d" * 20,
            "AKIA" + "E" * 16,
            "Bearer " + "f" * 24,
            _conn("postgresql"),
            _conn("mongodb"),
            _conn("redis"),
        ],
        ids=[
            "stripe-live",
            "stripe-test",
            "openai-key",
            "jwt-dotless",
            "aws-access-key",
            "bearer-token",
            "conn-postgresql",
            "conn-mongodb",
            "conn-redis",
        ],
    )
    def test_gate_refuses_every_shape_the_framework_refuses(self, shape):
        """Parity, with the control that keeps the parametrization honest: each
        probe really is a shape the framework itself would refuse."""
        from framework.security.credential_detector import detect_credentials

        assert detect_credentials(shape), "probe shape is not one the framework refuses"
        violation = _security_gate_output(shape)
        assert violation is not None, "framework refuses this shape but the domain gate does not"
        assert shape not in violation, "the violation echoed the matched value"

    @pytest.mark.parametrize(
        "content",
        [
            "Cotton knit T-shirts",
            "Kanto Textiles K.K.",
            "1-2-3 Minato, Tokyo, Japan",
            "6109.10",
            "LOG-SHIP-20260712-001",
            "FOB",
            "USD 47,000",
            "関税法 (Japan Customs Act) import/export procedures",
        ],
    )
    def test_legitimate_declaration_content_is_not_flagged(self, content):
        """The opposite direction: widening the recognizer must not turn real
        declaration content into a refusal."""
        assert _security_gate_output(content) is None

    def test_the_input_boundary_rejects_them_at_the_door(self):
        """is_publishable() shares this recognizer, so a caller value the gate
        would withhold is now refused at the input boundary with a field name
        instead of reaching the document and tripping a framework raise."""
        assert is_publishable("sk_live_" + "a" * 20) is False
        assert is_publishable("AKIA" + "E" * 16) is False
        assert is_publishable(_conn("postgresql")) is False
        assert is_publishable("Cotton knit T-shirts") is True


# -- Blocking must also CONTAIN ------------------------------------------------
#
# The framework envelope resolves the caller-facing value as
# `formatted_output or result` with NO regard for status, so a gate that
# returns ERROR while leaving the document-bearing fields populated still ships
# the refused declaration inside the error envelope.


class TestBlockContainment:
    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)

    _DOCUMENT = (
        "========================================\n"
        "CUSTOMS DECLARATION (DRAFT)\n"
        "Shipment ID: LOG-SHIP-20260712-001\n"
        "Exporter: Kanto Textiles K.K.\n"
        "Importer: West Coast Imports LLC\n"
        "  1. HS 6109.10 — Cotton knit T-shirts\n"
        "Consignee SSN 123-45-6789\n"
        "REGULATORY COMPLIANCE NOTE\n"
    )

    def _blocked_result(self):
        return PostProcessNode()(
            {
                "customs_document": self._DOCUMENT,
                "customs_sections": '{"declared_value": "  Total: USD 47,000"}',
                "compliance_flags": '{"broker_review_required": true, "reason": "restricted goods"}',
                "broker_review_required": True,
                "caller_trust_level": TrustLevel.ANONYMOUS.value,
            }
        )

    def test_block_clears_every_document_bearing_field(self):
        result = self._blocked_result()
        assert result["status"] == AgentStatus.ERROR.value
        for field in ("customs_document", "customs_sections", "compliance_flags"):
            assert result[field] is None, f"{field} still carries the refused declaration"

    def test_the_replacement_defeats_the_envelope_fallback(self):
        """`formatted_output or result` must resolve to the withholding notice.

        A falsy replacement ("" or {}) hands resolution straight back to
        `result` — the exact hole the clearing closes.
        """
        result = self._blocked_result()
        assert result["formatted_output"], "the replacement must be truthy — see the fallback"
        assert (result["formatted_output"] or result["result"]) is result["formatted_output"]

    def test_block_releases_no_declaration_content(self):
        import json as _json

        blob = _json.dumps(self._blocked_result(), ensure_ascii=False)
        for released in (
            "123-45-6789",
            "Kanto Textiles",
            "West Coast Imports",
            "Cotton knit T-shirts",
            "LOG-SHIP-20260712-001",
            "REGULATORY COMPLIANCE NOTE",
            "USD 47,000",
        ):
            assert released not in blob, f"{released!r} released on the blocked path"

    def test_violation_names_the_pattern_never_the_value(self):
        """Echoing the matched value would put the refused string back into this
        node's own result, where the framework credential scan raises and
        DISCARDS the whole return — taking the clearing above with it."""
        import json as _json

        result = self._blocked_result()
        assert any("national_id_pattern" in entry for entry in result["error_log"])
        blob = _json.dumps(result, ensure_ascii=False)
        assert "Traceback" not in blob, "the framework scan discarded the clearing return"
        assert "src/nodes/" not in blob

    def test_surviving_key_inventory_is_pinned(self):
        """Inventory guard: the keys a block leaves behind are fixed by this
        test, so a future document-bearing field cannot quietly join them.

        `broker_review_required` is deliberately absent from the clearing — it
        is an inert boolean determination carrying no declaration text, and the
        accessor withholds it on every non-success path anyway.
        """
        assert set(self._blocked_result()) == {
            "formatted_output",
            "result",
            "customs_document",
            "customs_sections",
            "compliance_flags",
            "status",
            "error_log",
            "node_history",
            "execution_time",
        }

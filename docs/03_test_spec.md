# Test Specification — LOG-C2-011 Customs Documentation Generator

## 1. Test Strategy

- **Agent:** LOG-C2-011 — Customs Documentation Generator (Cat 2 document-generation
  pattern, two-layer nested graph: outer `AgentBaseGraph` backbone + inner
  `DomainWorkflowGraph` `BaseGraph`).
- **Coverage target:** ≥ 90% of `src/nodes/` + `src/graph/` branches.
- **Test types:** Unit (per node + graph wiring) · Proof-of-Boundary (framework
  security/serialization contracts) · end-to-end through the real HTTP entry point.
- **Framework provisioning:** `framework` (`agenticstar-agentcore`) is supplied by
  the environment; tests import the real modules, and there are no stub nodes.
- **Audit events:** `emit_trace_event` is patched at the node module level in unit
  tests to avoid audit-backend calls, never via a `sys.modules` stub (which would
  break the real `shared` package the framework loads at import time).

### Test file map

| File | Scope |
|------|-------|
| `tests/unit/test_nodes.py` | All domain/backbone nodes + outer & inner graph wiring |
| `tests/unit/test_caller_contract.py` | The caller-data contract: finite/bounded numerics, inert identifiers, entry caps, option table |
| `tests/unit/test_output_gate.py` | The external output boundary in both directions + gate layer order + detector parity with the framework recognizer + blocked-output containment |
| `tests/unit/test_output_envelope.py` | The outer `invoke()` envelope (`get_output()`) — the second half of the output-gate contract: what a NON-SUCCESS envelope may carry |
| `tests/unit/test_main_node.py` | Governance — `src/nodes/main_node.py` stays deleted (module + file absent) |
| `tests/unit/test_framework_compliance_tc06_tc07.py` | TC-06/TC-07 — the framework's input/output gates are non-bypassable |
| `tests/proof_of_boundary/test_pb_invoke_order.py` | PB-6 per-node + backbone invoke order (VERIFIED_EXTERNAL) + trust gate + payload alignment |
| `tests/proof_of_boundary/test_invoke_e2e.py` | End-to-end behaviour through `POST /invoke` (Bearer auth), including the auth and size boundaries + output-gate containment on the real surface |
| `tests/proof_of_boundary/test_import_isolation.py` | PB-4 import isolation (AST scan) |
| `tests/proof_of_boundary/test_state_safety.py` | PB-2/PB-5 State serialization/credential safety (AST scan) |
| `tests/proof_of_boundary/test_pb7_hitl_interrupt_propagation.py` | PB-7 HITL interrupt propagation (skip stub — no cross-boundary HITL) |

### Canonical valid payload (PB-6 `_VALID_PAYLOAD`)

The customs-shipment payload used by the backbone invoke test and by
`deploy/invoke_payload.json` (the two MUST stay identical — asserted by
`test_invoke_payload_matches_pb6`):

```json
{
  "shipment_id": "LOG-SHIP-20260712-001",
  "exporter": {"name": "Kanto Textiles K.K.", "address": "1-2-3 Minato, Tokyo, Japan", "country": "Japan"},
  "importer": {"name": "West Coast Imports LLC", "address": "500 Market Street, San Francisco, CA", "country": "USA"},
  "goods": [
    {"description": "Cotton knit T-shirts", "hs_code": "6109.10", "quantity": 1200, "unit_value": 4.5, "net_weight_kg": 180.0, "country_of_origin": "Japan"},
    {"description": "Wool blend sweaters", "hs_code": "6110.11", "quantity": 300, "unit_value": 22.0, "net_weight_kg": 120.0, "country_of_origin": "Japan"}
  ],
  "transport": {"mode": "sea", "port_of_loading": "Tokyo", "port_of_discharge": "Los Angeles"},
  "incoterms": "FOB",
  "currency": "USD"
}
```

Broker-review determination: the total declared value (12,000 USD) is below the
100,000 default review threshold, both HS codes are structurally valid (`NNNN.NN`),
country of origin is present, and no restricted-goods keyword matches ⇒
`broker_review_required = False` (routine consignment) and the pipeline runs clean to
`status = success`.

## 2. Framework Compliance Tests (Mandatory)

| TC-ID | Test | Expected Result | Where |
|-------|------|----------------|-------|
| TC-01 | State contract: flat `TypedDict`, domain fields `NotRequired`, no Pydantic/dataclass | AST scan: 0 violations | `test_state_safety.py` |
| TC-02 | Empty / oversized / non-JSON / non-object input, and a non-inert `channel`, rejected at PreProcessNode | `status=success` (caller-correctable — the run completes carrying the reason), error_log names the field | `TestPreProcessNode` |
| TC-02a | A required shipment field (`goods`) absent | `status=error` (terminates), error_log names the field | `test_missing_required_field_returns_error` |
| TC-02b | Instruction-override directive anywhere in the payload, including an unrecognised or nested field | `status=error` (terminates); no `validated_input` / `enriched_context` carried forward; the schema field path is named, the directive text and an unrecognised field's own name are not; genuine customs phrasing still accepted | `TestInstructionOverrideScreen` |
| TC-03 | No credential material in State | credential scan: 0 violations | `test_state_safety.py` |
| TC-04 | `execute(self, state)` contract — no `_invoke_impl` | signature `(self, state)`, `_invoke_impl` absent | `test_execute_signature_is_state_first` |
| TC-04a | The placeholder `MainNode` stays removed | `src.nodes.main_node` module and file both absent | `test_main_node.py` |
| TC-05 | `emit_trace_event()` called inside each node `execute()` | ≥1 domain event per node (positional form) | every node test patches it at module level |
| TC-06 / TC-07 | The framework input/output gates cannot be overridden | class definition raises `TypeError` | `test_framework_compliance_tc06_tc07.py` |
| TC-08 | `required_trust_level` enforced in `__call__` before `execute()` | ANONYMOUS caller → refused; VERIFIED_EXTERNAL → admitted | `TestTrustGate` |
| TC-08a | Outer `PreProcessNode` = VERIFIED_EXTERNAL; inner nodes + post_process = ANONYMOUS | trust levels asserted per node | `test_trust_level_*` |
| TC-11 | Output gate on post_process | credential/identifier pattern → withheld + `status=error`; clean → pass | `TestPostProcessNode`, `test_output_gate.py` |

## 3. Proof-of-Boundary Tests (Mandatory)

| PB-ID | Boundary | Test | Expected Result | Where |
|-------|----------|------|----------------|-------|
| PB-2 | State serialization | AST scan of `src/schemas/state.py` | primitives only; no Pydantic/dataclass | `test_state_safety.py` |
| PB-4 | Import isolation | AST scan of `src/` | 0 platform-internal imports | `test_import_isolation.py` |
| PB-5 | Checkpoint safety | no credential-named fields / prohibited types in State | inspection pass | `test_state_safety.py` |
| PB-6 | Invoke execution order (per node) | `__call__`: node_start → input gate → `execute()` → output gate → node_complete | order verified for every `src/nodes/` class | `TestInvokeOrder` |
| PB-6b | Backbone invoke order | full `Graph().invoke(_VALID_PAYLOAD, ctx=VERIFIED_EXTERNAL)` | `status=success`; node_history = `[Initialize, PreProcess, CustomsDocumentationGraphNode, PostProcess, Finalize]` | `TestBackboneInvokeOrder` |
| PB-6c | Real external caller | `InvocationContext(caller_trust_level=VERIFIED_EXTERNAL)` — **never** `for_internal()` | inner ANONYMOUS nodes accept the passthrough trust; SUCCESS end to end | `TestBackboneInvokeOrder` |
| PB-6d | Trust-gate denial | `PreProcessNode({caller_trust_level: ANONYMOUS})` | `status=error`, error_log carries a trust-gate denial | `TestTrustGate` |
| PB-6e | Payload alignment | `deploy/invoke_payload.json["input"] == _VALID_PAYLOAD` | the deployment smoke-test invoke exercises the PB-6 payload | `test_invoke_payload_matches_pb6` |
| PB-7 | HITL interrupt propagation | skip stub — `propagate_hitl=False`, no cross-boundary interrupt() checkpoint | skipped with reason (real assertion when HITL is wired) | `test_pb7_hitl_interrupt_propagation.py` |
| PB-8 | HTTP entry point | `POST /invoke` with/without a Bearer token, oversized `input_context` | 401 without a token, 413 over the adapter cap, 200 + a real declaration with one | `test_invoke_e2e.py` |

## 4. Business Logic Tests

| BL-ID | Test | Input | Expected Result | Where |
|-------|------|-------|----------------|-------|
| BL-01 | Happy-path document generation | `_VALID_PAYLOAD` | 5-section declaration; `CUSTOMS DECLARATION (DRAFT)` + shipment_id present | `test_backbone_invoke_succeeds_and_returns_output`, `TestInvokeEndToEnd` |
| BL-02 | Country-code normalisation | `"Japan"` / `"USA"` | canonical `JP` / `US` on parties + goods | `test_country_codes_are_normalised` |
| BL-03 | Goods-line value computation | qty 1200 × 4.5 | `line_value = 5400.0` | `test_valid_input_builds_shipment_data` |
| BL-04 | Shipment aggregation | 2 goods lines | total value 12,000; qty 1,500; weight 300 kg; 2 packages | `TestParseShipmentDataNode` |
| BL-05 | Value-tier classification | value matrix + caller thresholds | high / standard / low, caller-overridable | `test_value_tier_*`, `test_caller_tier_thresholds_are_applied` |
| BL-06 | HS-chapter derivation | `6109.10`, `6110.11` | `["61"]` (2-digit, deduped, order-preserving) | `test_hs_chapters_deduped_and_order_preserving` |
| BL-06a | HS-code canonicalisation | `610910` | `6109.10` | `test_digits_only_hs_code_is_canonicalised` |
| BL-07 | Broker review — value threshold | total 120,000 / caller threshold 8,000 | `broker_review_required=True` (value flag) | `test_high_value_requires_broker_review`, `test_caller_threshold_lowers_the_review_bar` |
| BL-08 | Broker review — invalid HS code | `hs_code="ABC"` | `broker_review_required=True`; invalid_hs_codes populated | `test_invalid_hs_code_requires_broker_review` |
| BL-09 | Broker review — restricted goods | description contains "firearm" | `broker_review_required=True`; restricted_goods populated | `test_restricted_goods_requires_broker_review` |
| BL-10 | Broker review — missing origin | `country_of_origin=""` | `broker_review_required=True` | `test_missing_country_of_origin_requires_broker_review` |
| BL-11 | Routine consignment | `_VALID_PAYLOAD` shape | `broker_review_required=False`; flags empty | `test_clean_routine_requires_no_broker_review` |
| BL-12 | Document assembly | 5 rendered sections + compliance flags | all 5 headers + `REGULATORY COMPLIANCE NOTE`; the broker-review note reflects the flag | `TestOutputFormatNode` |
| BL-13 | Graph key coupling | inner `get_output` ↔ outer `merge_output` | 5 coupled keys mapped; `merge_output` returns changed keys only | `TestOuterGraphComposition`, `TestInnerDomainGraph` |
| BL-14 | Declared runtime config is live | `config/config.yaml` | `max_retry` / `timeout_s` on the agent AND on the inner graph | `test_declared_runtime_config_*` |
| BL-15 | Caller reference rendering | `declaration_reference` | rendered on the header when supplied, absent when not | `TestOutputFormatNode`, `TestInvokeEndToEnd` |

## 5. Input-contract tests (`test_caller_contract.py`)

Every case below is a **caller-correctable** rejection: the run COMPLETES with
`status=success` and no declaration, carrying an internal `error_code` that
`PostProcessNode` renders as the correction sentence the caller reads. The rejection
contract — which conditions complete this way and which terminate — is specified in
`docs/02_design.md` → *Rejection contract*; the assertions below pin the completing
status and the field naming.

| Case | Field(s) | Expected |
|------|----------|----------|
| non-finite matrix (`"NaN"`, `"Infinity"`, `"-Infinity"`, raw `nan`, raw `±inf`) | every goods numeric; `broker_review_threshold`; each `value_tier_thresholds` entry | `status=success`, error_log names the field |
| out-of-range / wrong-type magnitudes | quantity, unit_value, net_weight_kg, thresholds | `status=success` |
| inverted / non-object tier table | `value_tier_thresholds` | `status=success` |
| non-inert reference (spaces, punctuation, bare digits, >64 chars, non-string) | `declaration_reference`, `shipment_id` | `status=success`, error_log names the field |
| non-inert `channel` tag | `input_context.channel` | `status=success`, error_log names the field, value never echoed (`test_free_text_channel_is_rejected`) |
| invalid code | `currency`, `incoterms` | `status=success`, error_log names the field |
| empty or non-object party / transport / goods list / identifier | `exporter`, `transport`, `goods`, `shipment_id` | `status=success` (the `goods` and `shipment_id` cases also assert the field is named) |
| entry cap exceeded | `goods` (>200 lines) | `status=success`, error_log cites the line limit |
| over-long free text | goods description | accepted, capped (not rejected) |
| credential-bearing free text | goods description | `status=success`, error_log names `goods[].description` — the node-level rejection. End to end the framework's credential scan reaches the normalised payload first, so the same request terminates with `status=error` and releases nothing (`test_credential_shaped_caller_data_releases_nothing`) |
| rejected value in the error message | any | never echoed — the field is named, the value is not |

### 5a. Rejections that TERMINATE (`status=error`)

These are refusals and broken invariants, not correctable values: resending the same
request cannot succeed, so no correction sentence is produced and nothing is released.

| Case | Expected | Where |
|------|----------|-------|
| caller trust below `VERIFIED_EXTERNAL` | refused before `execute()`; error_log carries the trust-gate denial | `TestTrustGate` (PB-6d) |
| required shipment field absent | `status=error`, field named | `test_missing_required_field_returns_error`, `test_missing_required_field_is_rejected` (e2e) |
| instruction-override directive in the payload | `status=error`; no document, no `output`, `compliance_flags` withheld | `TestInstructionOverrideScreen`, `test_prompt_injection_is_refused_with_nothing_published` |
| caller value carrying a credential shape (`sk_live_`, `AKIA`, dotless `eyJ`, connection string) | `status=error`; `output`, `result` and `customs_document` all empty; the shape never appears in the envelope | `test_credential_shaped_caller_data_releases_nothing` |
| upstream invariant broken — `shipment_data` absent mid-pipeline | `status=error` at the node that needed it | `test_missing_shipment_data_returns_error` (parse / compliance / sections) |
| upstream invariant broken — `customs_sections` absent | `status=error` | `test_missing_sections_returns_error` |
| document carrying a credential / restricted-identifier pattern | `status=error`, every outward field replaced with the same stub | §6 *withholding* |

### 5b. The completing path end to end (`test_invoke_e2e.py`)

| Case | Expected |
|------|----------|
| empty input through `POST /invoke` | `status=success`, no `customs_document` (`test_empty_input_is_rejected`) |
| non-inert `channel` through `POST /invoke` | `status=success`, no `customs_document` (`test_invalid_channel_is_rejected`) |
| non-finite `broker_review_threshold` / goods `quantity` (full matrix) | `status=success`, no `customs_document` |
| structured fields on a completing rejection | `customs_sections`, `compliance_flags`, `broker_review_required` all withheld (`test_structured_fields_are_withheld_on_a_rejection`) |

## 6. Output-boundary tests (`test_output_gate.py`)

| Direction | Cases | Expected |
|-----------|-------|----------|
| leaks snap | comma-grouped, 5+-digit runs, marker before/after, code or symbol (incl. ￥/円/₩/$), attached or separated by any whitespace run, signed, small values in currency context | snapped onto the 1,000 grid, marker/delimiter/sign preserved |
| on-grid values | `JPY 1,000`, `100,000 USD`, `1,000,000` | byte-identical, no redaction |
| structural tokens | `LOG-SHIP-20260712-001`, `AWB-125-12345675`, `HS 6109.10`, `6109`, `1500pcs`, `12500kg`, `STAR 2026`, `in 2026` | byte-identical |
| layer order | `SSN 123-45-6789`, `TAX 987-65-4321` | not mangled by the numeric layer; detected and withheld by the pattern scan |
| withholding | national-ID / payment-card / credential patterns | `status=error`, every outward field replaced with the same stub |
| detector parity | every shape the framework's `detect_credentials()` refuses (`sk_live_`/`sk_test_`, `sk-`, dotless `eyJ`, `AKIA`, `Bearer`, connection strings) | refused by the domain gate too, value never echoed; ordinary declaration content not flagged |
| block containment | a blocked declaration | `customs_document` / `customs_sections` / `compliance_flags` cleared; replacement notice TRUTHY so `formatted_output or result` cannot resolve to the pre-gate document; surviving key set pinned by an inventory guard |
| envelope containment | every non-success status (error / timeout / cancelled / retry / pending / awaiting_human) | `result` and the structured keys withheld; `output` re-resolved so an absent gate output stays absent; no declaration text anywhere in the envelope |
| end-to-end scan | the rendered declaration | every monetary-form token on the grid; no per-line amounts |

## 7. Test Execution Summary

- Execution: `pytest tests/`.
- Execution date: 2026-08-31 — 348 passed, 1 skipped (PB-7 skip stub), 0 failed.
- Runner: pytest under the real framework wheel (`agenticstar-agentcore==1.0.2`).
- Gates: dependency pinning, stub check, category consistency, import isolation,
  composition, invoke chain, credential scan, trust level, project integrity — all PASS.
- Coverage: node + graph modules exercised on both success and error paths.
- PB-7 ships as a skip stub by design (no cross-boundary HITL propagation).

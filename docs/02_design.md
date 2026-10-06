# Template Design Specification — LOG-C2-011 Customs Documentation Generator

## Position in AgentCore Architecture

- **Agent Class**: CustomsDocumentationGeneratorAgent
- **L1 Base (framework base class)**: AgentBaseGraph — direct framework inheritance
- **Category**: Cat 2 (multi-step domain workflow — document-generation pattern)
- **Three-Layer Separation**:
  - State: flat TypedDict composition (never Pydantic — the checkpoint serializer is msgpack); dict/list fields are stored as JSON-serialised strings
  - Node: framework inheritance (Template Method: `execute(self, state: dict) -> dict` override only)
  - Graph: composition (`register_nodes()` for node substitution; two-layer nesting via `GraphNode`)

## Domain Context

A customs documentation generator for freight forwarders, importers/exporters and
customs brokers. It turns a shipment manifest supplied by a logistics coordinator
into a structured customs declaration draft.

**Regulatory basis**: 関税法 (Japan Customs Act) import/export procedures. Customs
digitisation is increasing demand for structured, machine-prepared declarations, and
manual declaration preparation is a significant bottleneck for smaller exporters
(1–2 hours per shipment).

**Important**: the generated document is a DRAFT and must be verified and certified
by a licensed customs broker before official submission.

## Architecture Overview

### Backbone (outer AgentBaseGraph — fixed 5-node pipeline)

```
START → initialize → pre_process → main(GraphNode) → post_process → finalize → END
                                         ↓ (retry, up to max_retry)
                                       pre_process
```

### Inner Domain Workflow (DomainWorkflowGraph — linear 5-node pipeline)

```
START → input_validate → parse_shipment_data → generate_customs_sections
          → compliance_check → output_format → END
```

### Node Configuration

| Node | Class | File | Trust | Responsibility | Input Keys | Output Keys |
|------|-------|------|-------|---------------|------------|-------------|
| initialize | InitializeNode | framework | — | session init | — | session_id, schema_version |
| pre_process | PreProcessNode | src/nodes/pre_process_node.py | VERIFIED_EXTERNAL | caller trust gate + structural JSON/size validation | user_input, input_context | validated_input, enriched_context, error_code |
| main | CustomsDocumentationGraphNode | src/graph/graph.py | — | delegates to DomainWorkflowGraph, bridges input_context | validated_input | customs_document, customs_sections, compliance_flags, broker_review_required |
| post_process | PostProcessNode | src/nodes/post_process_node.py | ANONYMOUS | external output gate + set formatted_output | customs_document | formatted_output, result |
| finalize | FinalizeNode | framework | — | response metadata | — | response_metadata, total_time_ms |
| input_validate (inner) | InputValidateNode | src/nodes/input_validate_node.py | ANONYMOUS | domain field validation, bounded numerics, country/HS normalisation, caller options | validated_input, input_context | shipment_data, declaration_options, error_code |
| parse_shipment_data (inner) | ParseShipmentDataNode | src/nodes/parse_shipment_data_node.py | ANONYMOUS | value/weight aggregation + HS-chapter + value-tier | shipment_data, declaration_options | shipment_data (enriched) |
| generate_customs_sections (inner) | GenerateCustomsSectionsNode | src/nodes/generate_customs_sections_node.py | ANONYMOUS | render the 5 declaration sections (deterministic) | shipment_data | customs_sections |
| compliance_check (inner) | ComplianceCheckNode | src/nodes/compliance_check_node.py | ANONYMOUS | Japan Customs Act broker-review determination | shipment_data, declaration_options | compliance_flags, broker_review_required |
| output_format (inner) | OutputFormatNode | src/nodes/output_format_node.py | ANONYMOUS | assemble the final declaration document | customs_sections, compliance_flags | customs_document, result |

### Implementation Note — deterministic generation

The five declaration sections are rendered by fixed templates from the validated
shipment data; no model is invoked. `config/config.yaml` declares an `llm` block
(`system_prompt_template` → `prompts/customs_declaration.j2`, `temperature`,
`max_tokens`) that is **reserved** for a build that wires a real model into
`GenerateCustomsSectionsNode`. Those keys are forwarded to the inner graph so they
are reachable, but the shipped node neither loads the Jinja2 template nor calls a
model.

### Runtime configuration

`config/agent.yaml` is the **flat registration manifest** (root-level keys only —
identity, entry point, trust level, compile-time requires). Every runtime parameter
lives in `config/config.yaml`, which the platform registry loads and passes as
`Graph(config=...)`; the standalone server (`src/api/server.py`) constructs the agent
with no explicit config, and `CustomsDocumentationGeneratorAgent.__init__` then loads
the same file via `_runtime_config()`, so both deployments see identical
configuration. The outer `AgentBaseGraph` consumes `max_retry` from it (retry
routing), and `CustomsDocumentationGraphNode._parent_config()` forwards `max_retry`
and `timeout_s` to the inner graph, where `DomainWorkflowGraph._validate_config()`
rejects a non-finite or out-of-range declared value at compile time rather than
mid-run.

### Caller-data contract (`input_context`)

`POST /invoke` accepts an optional `input_context` object (adapter-capped at 256 KB
serialized; the shipment payload itself is capped at 256 K characters). Every
declared field is validated before the declaration is generated; violations fail
CLOSED, naming the field in the internal audit trail and never echoing the value, and
end the run without a declaration — see *Rejection contract* below for how the run
ends. Undeclared keys are ignored.

| Field | Type / bounds | Effect |
|-------|---------------|--------|
| `channel` | str, inert identifier `[A-Za-z0-9._-]{1,64}` with at least one non-digit | recorded in `enriched_context` audit metadata (absent → `"unspecified"`) |
| `broker_review_threshold` | finite number, 0 … 1e12 | declared value at/above which licensed-broker review is mandatory (absent → 100,000) |
| `value_tier_thresholds.high` / `.standard` | finite numbers, 0 … 1e12, `high ≥ standard` | consignment value-tier classification (absent → 100,000 / 10,000) |
| `declaration_reference` | str, inert identifier as above | rendered on the declaration header (absent → not rendered) |

Every caller-supplied **number** — the options above and each goods line's
`quantity`, `unit_value` and `net_weight_kg` — is parsed by `finite_in_range()`,
which rejects bools, non-numerics, `NaN`/`±Infinity` and out-of-range magnitudes.
This is not defensive decoration: `float("NaN")` parses, Python's `json` accepts a
bare `NaN` in a request body, and every comparison against NaN is False — so an
unchecked NaN threshold would silently suppress the broker-review determination this
template exists to make.

Every caller-supplied **string that renders into the declaration** is either locked
to the inert identifier grammar (references, currency and delivery-terms codes) or
sanitised as free text (control characters stripped, whitespace collapsed, length
capped) — and in both cases refused if the external output boundary would rewrite or
withhold it (see below). The goods list is capped at 200 lines.

The framework's own input gate masks personal-name patterns in `user_input` /
`validated_input` before any node sees them, so a party name that matches that
heuristic is already masked in the rendered draft. It does **not** scan
`input_context`; this template accepts no free text on that channel (numbers and
inert identifiers only), so there is no unscanned free-text path into the document.

**Context bridge.** The framework's `GraphNode.execute()` does not forward the outer
state's `input_context` into `subgraph.invoke()`, so the outer graph stashes it in a
ContextVar (`CustomsDocumentationGraphNode.extract_input`) and the inner graph
re-seeds it (`DomainWorkflowGraph._extra_initial_state`) — see
`src/graph/context_bridge.py`. Verified end to end: a caller
`broker_review_threshold` override flips the broker-review determination through the
full nested graph.

### Rejection contract — what completes, what terminates

A request that produces no declaration ends the run in one of two ways, and the
difference is whether the caller can do anything about it.

**The caller can correct it → the run COMPLETES.** A malformed or out-of-contract
caller value ends the run with `status = success` and an `error_code` in State
(`EMPTY_INPUT`, `QUESTION_TOO_LONG`, `INVALID_REQUEST`). Nothing is generated:
`PostProcessNode` renders the matching plain sentence from
`src/services/failure_message.py` as the entire caller-facing body, and `get_output()`
returns the base envelope alone — no `customs_document`, no `customs_sections`, no
`compliance_flags`, no `broker_review_required`. Terminating here would end the calling
surface's conversational turn and leave the reason reachable only from the audit trail;
completing lets the caller fix the field and send the request again on the same
conversation. The conditions that take this path:

| Condition | Node | `error_code` | Sentence the caller reads |
|-----------|------|--------------|---------------------------|
| `user_input` empty, missing or whitespace-only | PreProcessNode | `EMPTY_INPUT` | "No question was received. Send the question you want answered." |
| payload over the 256 K-character request limit | PreProcessNode | `QUESTION_TOO_LONG` | "The request is too long. Shorten it and send it again." |
| body is not JSON, or its JSON root is not an object | PreProcessNode | `INVALID_REQUEST` | "A value in the request could not be accepted. Check it against the documented format." |
| `input_context` is not an object, or `channel` is not an inert identifier | PreProcessNode | `INVALID_REQUEST` | same |
| any domain-validation failure — non-finite or out-of-range numeric, inverted or non-object tier table, non-inert `shipment_id` / `declaration_reference`, invalid `currency` / `incoterms`, empty or over-cap goods list, a goods line the output boundary would rewrite or withhold, non-object party or transport block | InputValidateNode | `INVALID_REQUEST` | same |

Once `error_code` is set it is the settled reason. `CustomsDocumentationGraphNode`
skips the inner graph entirely, each inner domain node passes the marker through
without doing work on input that was already declined, `merge_output()` prefers the
outer reason over anything the inner graph reports, and `PostProcessNode` turns it into
the sentence — so a second, vaguer reason can never overwrite the specific one.
`error_code` itself is internal and is never surfaced in the envelope: the reason
reaches the caller through the sentence only, which is why each sentence must name what
to correct without echoing the rejected value, a field path or a gate message.

**The agent refuses, or an invariant broke → the run TERMINATES** with
`status = error`. There is no caller-facing body — `output` is empty, and every
document-bearing key is withheld. Resending the same request unchanged cannot succeed,
so no correction sentence is offered:

| Condition | Where it is decided |
|-----------|---------------------|
| caller trust below `VERIFIED_EXTERNAL` | S-1 trust gate, before `PreProcessNode.execute()` runs |
| a required shipment field is absent (`shipment_id`, `goods`, `exporter`, `importer`) | PreProcessNode |
| a payload string carries an instruction-override directive | PreProcessNode's own screen (and the framework input gate ahead of it) |
| a caller value carries a credential shape | the framework's credential scan, on the node result that carries the normalised payload — before domain validation runs |
| the assembled document carries a credential or restricted-identifier pattern | PostProcessNode output gate (layers 2 and 4 below) |
| an upstream invariant broke — `shipment_data` or `customs_sections` absent mid-pipeline | the inner domain node that needed it |

The domain input boundary's own `is_publishable()` check still refuses a
credential-bearing goods description as a caller-correctable rejection; end to end the
framework's credential scan reaches the normalised payload first, so such a request
terminates. The two agree on *refuse* and differ only on which layer gets there first,
which is why the domain check is retained rather than deferred to the framework.

### External output schema

The declaration draft is rendered section by section from validated fields — the raw
caller payload is never embedded. Two rules define what its numbers mean:

- **Monetary figures are aggregates on a 1,000 grid.** Only totals are rendered
  (per-line amounts never are), rounded to the nearest 1,000 of the declaration
  currency, with the precision note printed inside the Declared Value section. The
  broker-review threshold is applied on the same grid, so the threshold the document
  states is exactly the threshold applied. Every monetary representation uses the
  grid, including the `compliance_flags` structure returned alongside the document.
- **Non-monetary measurements are unit-suffixed tokens** (`1500pcs`, `300kg`,
  `6109.10`, `LOG-SHIP-20260712-001`), which are structurally distinct from monetary
  figures and pass the output boundary byte-identical.

`PostProcessNode` enforces this at the boundary in four ordered layers:

1. **Verbatim payload redaction** — a verbatim embedding of the caller's raw payload
   in the document is replaced with `[REDACTED]`, with an audit event.
2. **Credential / restricted-identifier scan** — API-key, JWT, bearer-token and
   password-assignment patterns, plus national-ID and payment-card number forms,
   withhold the whole document (sanitised stub, `status=error`, every outward field
   replaced). This runs **before** the numeric layer on purpose: the precision
   grammar treats a standalone 3-letter uppercase word as a currency marker, so
   snapping first could rewrite digits inside a labelled sequence such as
   `SSN 123-45-6789` and hide it from this scan.
3. **Monetary precision grid** — every monetary-form token (comma-grouped, 5+-digit
   runs, or short values in currency context — ISO code or symbol, either side, any
   whitespace run, signed) is snapped onto the grid, with an audit event. The
   grammar is wrapped in single-character identifier guards, so a digit run inside a
   longer alphanumeric token (a shipment reference, an HS code) is never touched.
4. **Re-scan** — layer 2 runs again over the rewritten text, so the bytes that leave
   the agent are never an unscanned surface.

The same grammar is exposed to the input boundary (`is_publishable()`), so a caller
value this gate *would* rewrite or withhold is refused at the door — naming the field
in the audit trail — instead of being silently mangled in the document.

### Data Flow

```
user_input (JSON shipment payload) + input_context (declaration options)
    │
    ▼ PreProcessNode (VERIFIED_EXTERNAL — trust + structural gate)
validated_input (normalised JSON string)
enriched_context (JSON string)
    │
    ▼ CustomsDocumentationGraphNode → DomainWorkflowGraph (input_context bridged)
    │   InputValidateNode            → shipment_data, declaration_options (JSON strings)
    │   ParseShipmentDataNode        → shipment_data (enriched)
    │   GenerateCustomsSectionsNode  → customs_sections (JSON string)
    │   ComplianceCheckNode          → compliance_flags (JSON string), broker_review_required (bool)
    │   OutputFormatNode             → customs_document (str), result (str)
    ▼ merge_output
customs_document, customs_sections, compliance_flags, broker_review_required → outer state
    │
    ▼ PostProcessNode (ANONYMOUS — external output gate)
formatted_output (gated customs_document), result
```

A caller-correctable rejection short-circuits this flow at the node that detected it:
`error_code` is written instead of the node's normal output, the inner graph is skipped,
and `PostProcessNode` returns the correction sentence as `formatted_output` / `result`
with `status = success`. A refusal or a broken invariant instead sets `status = error`:
decided before `post_process`, the backbone routes straight to `finalize` and no body is
produced; decided at the output gate itself, every outward field is replaced with the
withholding notice.

### State Definition

| Field | Type | Purpose | Producer |
|-------|------|---------|----------|
| validated_input | NotRequired[Optional[str]] | Normalised shipment JSON string | PreProcessNode |
| enriched_context | NotRequired[Optional[str]] | JSON: {source, channel, shipment_id} | PreProcessNode |
| declaration_options | NotRequired[Optional[str]] | JSON: validated caller declaration options | InputValidateNode |
| shipment_data | NotRequired[Optional[str]] | JSON: parsed + enriched shipment payload | InputValidateNode / ParseShipmentDataNode |
| customs_sections | NotRequired[Optional[str]] | JSON: {section_name: text, ...} × 5 sections | GenerateCustomsSectionsNode |
| compliance_flags | NotRequired[Optional[str]] | JSON: Japan Customs Act check result | ComplianceCheckNode |
| broker_review_required | NotRequired[Optional[bool]] | True if licensed-broker review required | ComplianceCheckNode |
| customs_document | NotRequired[Optional[str]] | Final formatted declaration text | OutputFormatNode |
| result | NotRequired[Optional[str]] | Same as customs_document (backbone convention) | OutputFormatNode / PostProcessNode |
| error_code | Optional[str] | Reason marker for a caller-correctable rejection; internal only, never surfaced in the envelope | PreProcessNode / InputValidateNode (passed through unchanged by every later node) |

**Serialisation constraint**: all dict/list-valued fields use JSON-serialised
`Optional[str]`. `to_json()` / `from_json()` are defined in `src/schemas/state.py`
and used at every producer/consumer boundary — one contract end to end.

**Prohibited**: re-declaring `formatted_output` (inherited from AgentState),
credentials in State, Pydantic models.

### Output-gate containment (both halves)

`AgentBaseGraph.get_output()` resolves its `output` key as
`formatted_output or result` **without consulting status**, so refusing a
document is not a status flip — a gate that returns ERROR while leaving the
document-bearing fields populated still ships the refused declaration inside
the error envelope. Both halves are therefore closed:

* **The gate** (`PostProcessNode._blocked`) overwrites `customs_document`,
  `customs_sections` and `compliance_flags` as well as `formatted_output` /
  `result`. The replacement notice is deliberately **non-empty**: a falsy
  value (`""` / `{}`) is exactly what re-activates the `or result` fallback.
  `broker_review_required` is left as-is — an inert boolean determination
  carrying no declaration text, withheld by the accessor on every non-success
  path; the surviving key set is pinned by an inventory guard in the tests.
* **The accessor** (`CustomsDocumentationGeneratorAgent.get_output`) surfaces
  `result` and the structured keys only when `status == SUCCESS`, and on any
  non-success outcome re-resolves `output` as `formatted_output or None`, so an
  absent gate output stays absent instead of becoming the pre-gate document.
  Without this the gate's own clearing was the only thing containing the
  declaration, which made the accessor unfalsifiable rather than safe: measured
  before the fix, a status flip after the gate passed surfaced the full
  declaration for every non-success status.

Violation messages name the matched **pattern label only**, never the value:
echoing it would put the refused string back into the node's own result, where
the framework's output-side credential scan raises — and a raise makes
`BaseNode.__call__` discard the whole return, taking the clearing with it.

### Input Payload Schema (user_input JSON)

```json
{
  "shipment_id": "SHP-2026-001234",
  "exporter": {"name": "Tokyo Trading Co.", "address": "1-1 Chuo, Tokyo", "country": "JP"},
  "importer": {"name": "LA Imports Inc.", "address": "100 Main St, Los Angeles", "country": "US"},
  "goods": [
    {
      "description": "Cotton T-shirts",
      "hs_code": "6109.10",
      "quantity": 500,
      "unit_value": 5.0,
      "net_weight_kg": 120.0,
      "country_of_origin": "JP"
    }
  ],
  "transport": {"mode": "sea", "port_of_loading": "Tokyo", "port_of_discharge": "Los Angeles"},
  "incoterms": "FOB",
  "currency": "USD"
}
```

`shipment_id` must be an inert identifier (1–64 characters of `[A-Za-z0-9._-]` with
at least one non-digit): a bare digit run is indistinguishable from a monetary
figure at the output boundary, so it is not a valid reference. `currency` and
`incoterms` are 3-letter uppercase codes. A digits-only `hs_code` is canonicalised
to the dotted form (`610910` → `6109.10`) before it can render.

### Output Declaration Sections (customs-declaration format)

1. **HS Code Classification** — per-line HS code + description + quantity; HS chapters covered
2. **Country of Origin** — per-line origin + exporting/importing countries
3. **Declared Value** — total declared value on the 1,000 grid, currency, Incoterms, precision note
4. **Package Details** — package count, quantity, net weight, transport mode, ports
5. **Compliance Declarations** — declarant statement of truth + licensed-broker disclaimer

## Security Configuration

| Concern | Gate | Implementation |
|---------|------|----------------|
| Caller trust | Trust enforcement | PreProcessNode `required_trust_level = VERIFIED_EXTERNAL`; the standalone server elevates a valid Bearer caller to that level and never demotes middleware-established trust |
| Input validation | Structural + domain | PreProcessNode (size, JSON shape, required fields, channel) + InputValidateNode (bounded numerics, entry caps, inert identifiers, free-text sanitisation). A correctable value completes with an `error_code` and a correction sentence; a refusal (trust, instruction-override, credential shape, missing required field) terminates — see *Rejection contract* |
| Output gate | External boundary | PostProcessNode `_security_gate_output()` — a module-level function re-exported on the agent class; four ordered layers as described above. Recognition is the UNION of the framework's own `detect_credentials()` and the domain-only table (national-ID / payment-card / credential-assignment forms), never a local set alone |
| Blocked-output containment | Envelope | A block clears every document-bearing field (`customs_document`, `customs_sections`, `compliance_flags`) and replaces `formatted_output` / `result` with a TRUTHY withholding notice; `get_output()` surfaces `result` and the structured keys only on the gated success path and re-resolves the base envelope's `formatted_output or result` fallback on any non-success outcome |
| Audit logging | Trace events | `emit_trace_event()` in every node's `execute()` (at least one domain-specific event), including one per gate action |
| Credential handling | No secrets in State | No credentials in State; secrets reach nodes via InvocationContext only. This template requires none (`requires.secrets: []`) |

**config/config.yaml:**
```yaml
max_retry: 3
timeout_s: 30
llm:
  system_prompt_template: prompts/customs_declaration.j2
  temperature: 0.0
  max_tokens: 4000
security:
  s3_gate_enabled: true
```

## Framework Utilization

### Shared Components Used
- [x] InvocationContext (`config["configurable"]` — session_id, trust_level)
- [x] Module-level `_security_gate_output()` in `post_process_node.py`, re-exported on `CustomsDocumentationGeneratorAgent`
- [x] `emit_trace_event()` — at least one domain-specific event per node `execute()`
- [x] `to_json()` / `from_json()` / `finite_in_range()` / `to_external_grid()` in `src/schemas/state.py`

### Composition Pattern

- **Pattern**: two-layer nesting — a GraphNode wrapping an inner BaseGraph
- **Outer graph**: `CustomsDocumentationGeneratorAgent(AgentBaseGraph)` — fixed 5-node backbone
- **Inner graph**: `DomainWorkflowGraph(BaseGraph)` — 5-node linear domain pipeline
- **Error propagation**: propagate (SubgraphError on inner failure; the outer backbone retries pre_process)

## Import Isolation Confirmation
- [x] The template imports no platform-internal SDK package
- [x] Import targets: `framework/` and `shared/` only

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| Framework base class | AgentBaseGraph | AutonomousBaseGraph | AgentBaseGraph | Fixed sequential pipeline; no reasoning loop required |
| Composition pattern | flat backbone | nested GraphNode | nested | 5 sequential domain steps behind one `main` slot |
| Customs compliance | inline in generation | separate ComplianceCheckNode | separate node | Single responsibility: determination is auditable on its own |
| State dict fields | bare dict | JSON-serialised str | JSON-serialised str | Checkpoint serialisation safety |
| Generation | live model | deterministic templates | deterministic | Reproducible declarations; the model seam is documented and reserved |
| Country normalisation | in InputValidate | separate node | in InputValidateNode | Normalisation is validation scope; fewer nodes |
| Monetary precision | full precision | aggregates on a 1,000 grid | grid | The declaration is a draft for broker certification, not a settlement record; a single stated precision is enforceable end to end |
| Unsafe caller identifiers | mangle at the gate | reject at the input boundary | reject | A silently rewritten shipment reference is worse than a refused request |
| Caller-correctable rejection | terminate with an error status | complete with a reason code and a correction sentence | complete | Terminating ends the calling surface's turn and leaves the reason only in the audit trail; completing tells the caller what to fix and keeps the conversation open. Refusals and broken invariants still terminate — they are not correctable by resending |

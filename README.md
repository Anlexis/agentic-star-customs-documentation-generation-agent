# Customs Documentation Generation Agent

AI agent for generating customs documentation, built with Agentic Star.

> **Category**: Cat 2 (domain-specific pipeline)
> **Industry**: Logistics
> **Template ID**: LOG-C2-011

## Overview

Turns a shipment manifest into a structured customs declaration draft. The agent
validates the manifest field by field, normalises country codes and HS codes,
aggregates declared value, quantity and net weight, renders the five sections of a
customs declaration, and runs a Japan Customs Act (関税法) pre-check that decides
whether a licensed customs broker must review the declaration before submission —
flagging invalid HS-code formats, missing country of origin, restricted or
controlled goods, and consignments at or above the review threshold.

The generated document is explicitly a **DRAFT**: it carries the determination, the
reason behind it and a standing disclaimer that a licensed customs broker must verify
and certify it before official submission. Generation is deterministic — the sections
are rendered from the validated manifest by fixed templates, so the same shipment
always produces the same declaration and the pipeline runs end to end with no model
or external service. `prompts/customs_declaration.j2` documents the prompt contract
for a build that wires a real model into the generation step.

The external draft states monetary figures as aggregates rounded to the nearest 1,000
of the declaration currency (per-line amounts are never rendered), and the output gate
independently enforces that grid on the way out, alongside a credential and
restricted-identifier scan.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform is unreachable or the SDK version does not match, the agent
fails at graph compile / start-up preflight rather than starting in a partially
working state. This is intentional — a half-running agent is worse than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Using it

`POST /invoke` takes the shipment manifest as a JSON string in `input`, plus an
optional `input_context` object of per-invocation declaration options:

```json
{
  "input": "{\"shipment_id\": \"LOG-SHIP-20260712-001\", \"exporter\": {...}, \"importer\": {...}, \"goods\": [...], \"transport\": {...}, \"incoterms\": \"FOB\", \"currency\": \"USD\"}",
  "input_context": {
    "broker_review_threshold": 100000,
    "declaration_reference": "REF-DEMO-01"
  }
}
```

Every caller-supplied number is parsed by a finite, bounded parser and every
caller-supplied string that renders into the draft is locked to an inert identifier
or sanitised as free text; a violation fails closed with an error naming the field.
`docs/02_design.md` documents the full contract, the bounds and the output schema.

## Project Structure

```
src/          agent implementation (nodes, graphs, schemas, HTTP adapter)
src/examples/ annotated samples of the framework's agent patterns
tests/        unit and boundary tests
config/       agent manifest + runtime configuration
prompts/      system prompt template for a live-model build
docs/         design and test documentation
```

See `docs/` for the design specification and test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Point the pipeline at your own manifest source and reference data.
3. Review the node implementations under `src/nodes/` for domain-specific logic —
   the restricted-goods keywords, the review thresholds and the section templates are
   the usual first things to localise.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.

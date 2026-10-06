"""AgentCore Platform v1.0"""

# Standalone HTTP entry point for the agent.
# Entry points are adapters only — no business logic here.
# For platform-level routing, the gateway calls agent.invoke() directly.

import json
import os
import secrets
from typing import Any, Dict, cast
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory
from src.graph.graph import CustomsDocumentationGeneratorAgent as Graph

# Upper bound on the serialized input_context (bytes). The domain nodes bound
# every field they read, but the adapter refuses an oversized structure before
# the agent is entered at all.
_MAX_INPUT_CONTEXT_BYTES = 262_144

app = FastAPI(title="Agent")

agent = Graph()
agent.compile()
# Replace namespace/agent_name to match the agent's manifest values.
agent.provision_secrets(secrets_factory(namespace="log-c2-011", agent_name="CustomsDocumentationGeneratorAgent"))


class InvokeRequest(BaseModel):
    """Request body: the shipment payload plus optional declaration options.

    `input_context` carries the per-invocation options the pipeline validates
    field by field (broker_review_threshold, value_tier_thresholds,
    declaration_reference, channel). Anything absent falls back to the
    declaration's declared defaults.
    """

    input: str
    session_id: str = ""
    input_context: Dict[str, Any] = Field(default_factory=dict)


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> Dict[str, Any]:
    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)
    # Standalone caller auth: when INVOKE_AUTH_TOKEN is set on the server
    # environment, callers that no upstream middleware vouched for (still
    # ANONYMOUS) must present it as a Bearer token and run at
    # VERIFIED_EXTERNAL. Middleware-established trust is never demoted.
    # This adapter is the entry-point auth boundary — a deployment-level caller
    # credential, not an agent secret, so ctx.secrets does not apply (no
    # InvocationContext exists before auth).
    input_context = req.input_context or {}
    if input_context and len(json.dumps(input_context, default=str)) > _MAX_INPUT_CONTEXT_BYTES:
        raise HTTPException(status_code=413, detail="input_context exceeds the maximum allowed size.")

    expected = os.environ.get("INVOKE_AUTH_TOKEN")
    if expected and trust is TrustLevel.ANONYMOUS:
        supplied = request.headers.get("authorization", "")
        # Compare bytes: compare_digest raises TypeError on non-ASCII str input
        # (headers decode as latin-1), which would 500 instead of the generic 401.
        if not secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            # Generic body on purpose — do not leak whether the token was absent,
            # malformed, or wrong.
            raise HTTPException(status_code=401, detail="Token is invalid or expired.")
        trust = TrustLevel.VERIFIED_EXTERNAL
    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        return cast(
            "Dict[str, Any]",
            agent.invoke(req.input, ctx=ctx, input_context=input_context),
        )


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok", "agent": "CustomsDocumentationGeneratorAgent"}

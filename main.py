"""
HiveMessenger — Asynchronous DID-to-DID messaging for AI agents.

Agents negotiate deals, exchange contracts, attach USDC settlements, and
close agreements without any human in the loop.

Endpoints:
  POST   /v1/messenger/send
  GET    /v1/messenger/inbox/{did}
  GET    /v1/messenger/thread/{thread_id}
  POST   /v1/messenger/read/{message_id}
  DELETE /v1/messenger/delete/{message_id}
  GET    /v1/messenger/outbox/{did}
  POST   /v1/messenger/broadcast
  GET    /v1/messenger/stats/{did}
  GET    /health

Internal key: hive_internal_125e04e071e8829be631ea0216dd4a0c9b707975fcecaf8c62c6a2ab43327d46
Discovery:    https://hive-discovery.onrender.com
HiveGate:     https://hivegate.onrender.com
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import List, Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from models import (
    BroadcastRequest,
    BroadcastResponse,
    DeleteResponse,
    HealthResponse,
    InboxResponse,
    Message,
    MessageStatus,
    MessageType,
    OutboxResponse,
    ReadReceiptResponse,
    SendMessageRequest,
    SendMessageResponse,
    StatsResponse,
    ThreadResponse,
)
from store import store
from threads import new_message_id, resolve_thread_id

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hivemessenger")

app = FastAPI(
    title="HiveMessenger",
    description=(
        "Asynchronous DID-to-DID messaging for AI agents. "
        "Agents negotiate, exchange contracts, and settle payments — "
        "all without human involvement."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DISCOVERY_URL = "https://hive-discovery.onrender.com"
HIVEGATE_URL = "https://hivegate.onrender.com"
INTERNAL_KEY = "hive_internal_125e04e071e8829be631ea0216dd4a0c9b707975fcecaf8c62c6a2ab43327d46"
INTERNAL_HEADER = "x-hive-internal-key"


# ---------------------------------------------------------------------------
# x402 payment gate
# ---------------------------------------------------------------------------

def x402_gate(price_usd: float, description: str):
    """Returns a FastAPI dependency that enforces x402 payment or internal key bypass."""
    async def dependency(
        request: Request,
        x_payment: str = Header(None),
        x_hive_internal: str = Header(None),
        x_api_key: str = Header(None)
    ):
        # Internal bypass
        if x_hive_internal == INTERNAL_KEY or x_api_key == INTERNAL_KEY:
            return {"bypassed": True, "amount": 0}
        # Payment present — accept (in production, verify cryptographically)
        if x_payment:
            return {"verified": True, "amount": price_usd}
        # No payment — return 402
        raise HTTPException(
            status_code=402,
            detail={
                "error": "payment_required",
                "x402": {
                    "version": "1.0",
                    "amount_usdc": price_usd,
                    "description": description,
                    "payment_methods": ["x402-usdc", "x402-aleo"],
                    "headers_required": ["X-Payment"],
                    "settlement_wallet": "0x78B3B3C356E89b5a69C488c6032509Ef4260B6bf",
                    "network": "base"
                }
            }
        )
    return dependency


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_did_header(x_hive_did: Optional[str], field_name: str = "x-hive-did") -> str:
    if not x_hive_did:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Missing required header: {field_name}",
        )
    return x_hive_did


def _check_internal_key(key: Optional[str]) -> bool:
    return key == INTERNAL_KEY


async def _fetch_discovery_agents(capability: str, trust_min: float) -> List[str]:
    """
    Query the discovery service for registered agents matching a capability.
    Falls back to an empty list if the discovery service is unavailable.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{DISCOVERY_URL}/v1/agents",
                params={"capability": capability, "trust_min": trust_min},
                headers={INTERNAL_HEADER: INTERNAL_KEY},
            )
            if resp.status_code == 200:
                data = resp.json()
                # Expected: {"agents": [{"did": "did:hive:...", ...}, ...]}
                agents = data.get("agents", [])
                return [a["did"] for a in agents if "did" in a]
    except Exception as exc:
        logger.warning("Discovery service unavailable: %s", exc)
    return []


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health", response_model=HealthResponse, tags=["System"])
async def health() -> HealthResponse:
    """Service health check."""
    return HealthResponse(
        status="ok",
        service="hivemessenger",
        version="1.0.0",
        message_count=store.message_count,
        thread_count=store.thread_count,
        timestamp=datetime.utcnow(),
    )


# ------------------------------------------------------------------
# Send
# ------------------------------------------------------------------

@app.post(
    "/v1/messenger/send",
    response_model=SendMessageResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Messaging"],
    summary="Send a DID-to-DID message",
)
async def send_message(
    req: SendMessageRequest,
    _payment=Depends(x402_gate(0.05, "DID-to-DID message delivery")),
) -> SendMessageResponse:
    """
    Send a message from one DID to another.

    - Automatically creates a new thread if `thread_id` is omitted.
    - Supports structured JSON or plain-text bodies.
    - Optional `settlement_attached` embeds a USDC transfer reference.
    - Set `expires_in_seconds` to impose a TTL on the message.
    """
    thread_id = resolve_thread_id(req.thread_id, store.by_thread_id)
    message_id = new_message_id()

    expires_at = None
    if req.expires_in_seconds:
        expires_at = datetime.utcnow() + timedelta(seconds=req.expires_in_seconds)

    # Determine delivery status — "delivered" if recipient DID is known in the
    # store (i.e. they have existing messages), otherwise "queued".
    recipient_known = bool(store.by_to_did.get(req.to_did) or store.by_from_did.get(req.to_did))
    delivery_status = MessageStatus.delivered if recipient_known else MessageStatus.queued

    msg = Message(
        message_id=message_id,
        thread_id=thread_id,
        from_did=req.from_did,
        to_did=req.to_did,
        subject=req.subject,
        body=req.body,
        message_type=req.message_type,
        status=delivery_status,
        signed=req.signed,
        settlement_attached=req.settlement_attached,
        expires_at=expires_at,
        created_at=datetime.utcnow(),
    )

    store.put(msg)
    logger.info("Message %s sent: %s -> %s [%s]", message_id, req.from_did, req.to_did, req.message_type)

    return SendMessageResponse(
        message_id=message_id,
        thread_id=thread_id,
        status=delivery_status.value,
        timestamp=msg.created_at,
    )


# ------------------------------------------------------------------
# Inbox
# ------------------------------------------------------------------

@app.get(
    "/v1/messenger/inbox/{did}",
    response_model=InboxResponse,
    tags=["Messaging"],
    summary="Retrieve inbox for a DID",
)
async def get_inbox(
    did: str,
    unread_only: bool = Query(default=False, description="Return only unread messages"),
    message_type: Optional[str] = Query(default=None, description="Filter by message type"),
    thread_id: Optional[str] = Query(default=None, description="Filter by thread ID"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> InboxResponse:
    """
    Return messages addressed to `did`, newest first.

    Supports filtering by read status, message type, and thread.
    """
    # Validate message_type if provided
    if message_type and message_type not in [t.value for t in MessageType]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid message_type '{message_type}'. "
                   f"Valid values: {[t.value for t in MessageType]}",
        )

    messages = store.inbox(
        did=did,
        unread_only=unread_only,
        message_type=message_type,
        thread_id=thread_id,
        limit=limit,
        offset=offset,
    )

    # Full count (without pagination) for metadata
    all_messages = store.inbox(
        did=did,
        unread_only=unread_only,
        message_type=message_type,
        thread_id=thread_id,
        limit=100_000,
        offset=0,
    )

    return InboxResponse(
        did=did,
        messages=messages,
        total=len(all_messages),
        offset=offset,
        limit=limit,
    )


# ------------------------------------------------------------------
# Thread
# ------------------------------------------------------------------

@app.get(
    "/v1/messenger/thread/{thread_id}",
    response_model=ThreadResponse,
    tags=["Messaging"],
    summary="Retrieve a full conversation thread",
)
async def get_thread(thread_id: str) -> ThreadResponse:
    """
    Return all messages in a thread, ordered chronologically (oldest first).
    Deleted messages are excluded.
    """
    messages = store.thread(thread_id)
    return ThreadResponse(
        thread_id=thread_id,
        messages=messages,
        total=len(messages),
    )


# ------------------------------------------------------------------
# Mark as read
# ------------------------------------------------------------------

@app.post(
    "/v1/messenger/read/{message_id}",
    response_model=ReadReceiptResponse,
    tags=["Messaging"],
    summary="Mark a message as read",
)
async def mark_read(
    message_id: str,
    x_hive_did: Optional[str] = Header(default=None),
) -> ReadReceiptResponse:
    """
    Mark a message as read.

    Only the **recipient DID** (supplied via `x-hive-did` header) can mark
    a message as read.
    """
    did = _require_did_header(x_hive_did)

    msg = store.get(message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail=f"Message '{message_id}' not found")
    if msg.status == MessageStatus.deleted:
        raise HTTPException(status_code=410, detail="Message has been deleted")
    if msg.to_did != did:
        raise HTTPException(
            status_code=403,
            detail="Only the recipient DID can mark a message as read",
        )

    updated = store.mark_read(message_id, did)
    if updated is None:
        raise HTTPException(status_code=403, detail="Read permission denied")

    return ReadReceiptResponse(
        message_id=message_id,
        read_at=updated.read_at,
        status="ok",
    )


# ------------------------------------------------------------------
# Delete
# ------------------------------------------------------------------

@app.delete(
    "/v1/messenger/delete/{message_id}",
    response_model=DeleteResponse,
    tags=["Messaging"],
    summary="Delete a message",
)
async def delete_message(
    message_id: str,
    x_hive_did: Optional[str] = Header(default=None),
) -> DeleteResponse:
    """
    Delete a message (soft delete — audit trail preserved).

    Only the **sender or recipient DID** (supplied via `x-hive-did`) may delete.
    """
    did = _require_did_header(x_hive_did)

    msg = store.get(message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail=f"Message '{message_id}' not found")
    if msg.status == MessageStatus.deleted:
        raise HTTPException(status_code=410, detail="Message already deleted")

    updated = store.mark_deleted(message_id, did)
    if updated is None:
        raise HTTPException(
            status_code=403,
            detail="Only sender or recipient DID can delete this message",
        )

    logger.info("Message %s deleted by %s", message_id, did)
    return DeleteResponse(
        message_id=message_id,
        deleted_by=did,
        status="deleted",
    )


# ------------------------------------------------------------------
# Outbox
# ------------------------------------------------------------------

@app.get(
    "/v1/messenger/outbox/{did}",
    response_model=OutboxResponse,
    tags=["Messaging"],
    summary="Retrieve outbox for a DID",
)
async def get_outbox(
    did: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> OutboxResponse:
    """Return messages sent by `did`, newest first."""
    messages = store.outbox(did=did, limit=limit, offset=offset)
    all_messages = store.outbox(did=did, limit=100_000, offset=0)
    return OutboxResponse(
        did=did,
        messages=messages,
        total=len(all_messages),
        offset=offset,
        limit=limit,
    )


# ------------------------------------------------------------------
# Broadcast
# ------------------------------------------------------------------

@app.post(
    "/v1/messenger/broadcast",
    response_model=BroadcastResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Messaging"],
    summary="Broadcast a message to all agents matching a capability",
)
async def broadcast(
    req: BroadcastRequest,
    _payment=Depends(x402_gate(0.05, "DID-to-DID message delivery")),
) -> BroadcastResponse:
    """
    Send a message to all agents registered in the discovery service that
    match `to_capability` and meet the `trust_min` threshold.

    Returns the count of recipients and all generated message IDs.
    If the discovery service is unreachable, recipients will be 0 and the
    response will indicate graceful degradation.
    """
    # Fetch matching agent DIDs from discovery service
    recipient_dids = await _fetch_discovery_agents(req.to_capability, req.trust_min)

    if not recipient_dids:
        logger.warning(
            "Broadcast from %s: no agents found for capability '%s' (trust_min=%.2f)",
            req.from_did,
            req.to_capability,
            req.trust_min,
        )

    thread_id = resolve_thread_id(None, store.by_thread_id)
    message_ids: List[str] = []

    for to_did in recipient_dids:
        if to_did == req.from_did:
            continue  # don't send to self
        msg_id = new_message_id()
        msg = Message(
            message_id=msg_id,
            thread_id=thread_id,
            from_did=req.from_did,
            to_did=to_did,
            subject=req.subject,
            body=req.body,
            message_type=req.message_type,
            status=MessageStatus.delivered,
            signed=req.signed,
            settlement_attached=req.settlement_attached,
            created_at=datetime.utcnow(),
        )
        store.put(msg)
        message_ids.append(msg_id)

    logger.info(
        "Broadcast from %s: %d recipients [capability=%s]",
        req.from_did,
        len(message_ids),
        req.to_capability,
    )

    return BroadcastResponse(
        from_did=req.from_did,
        subject=req.subject,
        message_type=req.message_type.value,
        recipients=len(message_ids),
        message_ids=message_ids,
        status="broadcast_complete" if message_ids else "no_recipients",
    )


# ------------------------------------------------------------------
# Stats
# ------------------------------------------------------------------

@app.get(
    "/v1/messenger/stats/{did}",
    response_model=StatsResponse,
    tags=["Messaging"],
    summary="Message statistics for a DID",
)
async def get_stats(did: str) -> StatsResponse:
    """
    Return inbox/outbox counts, unread count, active thread count, and
    the timestamp of the most recent message for a given DID.
    """
    s = store.stats(did)
    return StatsResponse(
        did=did,
        inbox_count=s["inbox_count"],
        unread_count=s["unread_count"],
        outbox_count=s["outbox_count"],
        threads_active=s["threads_active"],
        last_message_at=s["last_message_at"],
    )


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------

@app.exception_handler(404)
async def not_found_handler(request, exc):
    return JSONResponse(status_code=404, content={"detail": "Not found", "path": str(request.url)})


@app.exception_handler(500)
async def internal_error_handler(request, exc):
    logger.exception("Internal server error: %s", exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# ---------------------------------------------------------------------------
# Entry point (local development)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)

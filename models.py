"""
HiveMessenger — Pydantic models for DID-to-DID messaging.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class MessageType(str, Enum):
    offer = "offer"
    counter_offer = "counter_offer"
    acceptance = "acceptance"
    rejection = "rejection"
    inquiry = "inquiry"
    contract_proposal = "contract_proposal"
    payment_request = "payment_request"
    receipt = "receipt"


class MessageStatus(str, Enum):
    delivered = "delivered"
    queued = "queued"
    read = "read"
    deleted = "deleted"


class PaymentRail(str, Enum):
    usdc = "usdc"
    hive = "hive"
    hbd = "hbd"


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------

class SettlementAttached(BaseModel):
    amount_usdc: float = Field(default=0.0, ge=0, description="Amount in USDC")
    rail: PaymentRail = Field(default=PaymentRail.usdc)
    memo: str = Field(default="", max_length=512)
    tx_id: Optional[str] = Field(default=None, description="On-chain transaction ID once settled")


# ---------------------------------------------------------------------------
# Core message model
# ---------------------------------------------------------------------------

class Message(BaseModel):
    message_id: str
    thread_id: str
    from_did: str
    to_did: str
    subject: str
    body: Union[str, Dict[str, Any]]
    message_type: MessageType
    status: MessageStatus = MessageStatus.delivered
    signed: bool = False
    settlement_attached: Optional[SettlementAttached] = None
    expires_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    read_at: Optional[datetime] = None
    deleted_at: Optional[datetime] = None
    deleted_by: Optional[str] = None
    reply_to_message_id: Optional[str] = None  # explicit parent in thread

    class Config:
        use_enum_values = True


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------

class SendMessageRequest(BaseModel):
    from_did: str = Field(..., description="Sender DID, e.g. did:hive:agentA")
    to_did: str = Field(..., description="Recipient DID, e.g. did:hive:agentB")
    subject: str = Field(..., max_length=256)
    body: Union[str, Dict[str, Any]] = Field(..., description="Free-form string or structured JSON payload")
    message_type: MessageType
    thread_id: Optional[str] = Field(default=None, description="Omit to start a new thread; supply to reply")
    settlement_attached: Optional[SettlementAttached] = None
    expires_in_seconds: Optional[int] = Field(default=None, ge=1, description="TTL in seconds from send time")
    signed: bool = Field(default=False, description="Whether sender has cryptographically signed the payload")


class BroadcastRequest(BaseModel):
    from_did: str
    subject: str
    body: Union[str, Dict[str, Any]]
    message_type: MessageType
    to_capability: str = Field(..., description="Agent capability tag to target, e.g. 'compute', 'legal'")
    trust_min: float = Field(default=0.0, ge=0.0, le=1.0, description="Minimum trust score for recipient agents")
    signed: bool = False
    settlement_attached: Optional[SettlementAttached] = None


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class SendMessageResponse(BaseModel):
    message_id: str
    thread_id: str
    status: str
    timestamp: datetime


class InboxResponse(BaseModel):
    did: str
    messages: List[Message]
    total: int
    offset: int
    limit: int


class ThreadResponse(BaseModel):
    thread_id: str
    messages: List[Message]
    total: int


class ReadReceiptResponse(BaseModel):
    message_id: str
    read_at: datetime
    status: str = "ok"


class DeleteResponse(BaseModel):
    message_id: str
    deleted_by: str
    status: str = "deleted"


class OutboxResponse(BaseModel):
    did: str
    messages: List[Message]
    total: int
    offset: int
    limit: int


class BroadcastResponse(BaseModel):
    from_did: str
    subject: str
    message_type: str
    recipients: int
    message_ids: List[str]
    status: str


class StatsResponse(BaseModel):
    did: str
    inbox_count: int
    unread_count: int
    outbox_count: int
    threads_active: int
    last_message_at: Optional[datetime]


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str = "hivemessenger"
    version: str = "1.0.0"
    message_count: int
    thread_count: int
    timestamp: datetime = Field(default_factory=datetime.utcnow)

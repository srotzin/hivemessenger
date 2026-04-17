"""
HiveMessenger — In-memory message store.

Indexes:
  - by_message_id  : message_id -> Message
  - by_to_did      : to_did     -> [message_id, ...]
  - by_from_did    : from_did   -> [message_id, ...]
  - by_thread_id   : thread_id  -> [message_id, ...]

Pre-populated with a 5-message GPU-compute negotiation between two agents
to demonstrate the full offer → counter → acceptance → payment → contract
flow without any human involvement.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from models import Message, MessageStatus, MessageType, SettlementAttached
from threads import new_message_id, new_thread_id


# ---------------------------------------------------------------------------
# Store class
# ---------------------------------------------------------------------------

class MessageStore:
    """Thread-safe in-memory message store."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.by_message_id: Dict[str, Message] = {}
        self.by_to_did: Dict[str, List[str]] = defaultdict(list)
        self.by_from_did: Dict[str, List[str]] = defaultdict(list)
        self.by_thread_id: Dict[str, List[str]] = defaultdict(list)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def put(self, message: Message) -> None:
        with self._lock:
            self.by_message_id[message.message_id] = message
            if message.message_id not in self.by_to_did[message.to_did]:
                self.by_to_did[message.to_did].append(message.message_id)
            if message.message_id not in self.by_from_did[message.from_did]:
                self.by_from_did[message.from_did].append(message.message_id)
            if message.message_id not in self.by_thread_id[message.thread_id]:
                self.by_thread_id[message.thread_id].append(message.message_id)

    def mark_read(self, message_id: str, reader_did: str) -> Optional[Message]:
        with self._lock:
            msg = self.by_message_id.get(message_id)
            if msg is None:
                return None
            if msg.to_did != reader_did:
                return None  # only recipient can mark read
            msg.status = MessageStatus.read
            msg.read_at = datetime.utcnow()
            return msg

    def mark_deleted(self, message_id: str, actor_did: str) -> Optional[Message]:
        with self._lock:
            msg = self.by_message_id.get(message_id)
            if msg is None:
                return None
            if msg.from_did != actor_did and msg.to_did != actor_did:
                return None  # only sender or recipient
            msg.status = MessageStatus.deleted
            msg.deleted_at = datetime.utcnow()
            msg.deleted_by = actor_did
            return msg

    # ------------------------------------------------------------------
    # Read — inbox
    # ------------------------------------------------------------------

    def inbox(
        self,
        did: str,
        unread_only: bool = False,
        message_type: Optional[str] = None,
        thread_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Message]:
        with self._lock:
            ids = list(self.by_to_did.get(did, []))

        messages = [
            self.by_message_id[mid]
            for mid in ids
            if mid in self.by_message_id
        ]

        # Filter out deleted
        messages = [m for m in messages if m.status != MessageStatus.deleted]

        if unread_only:
            messages = [m for m in messages if m.status not in (MessageStatus.read,)]
        if message_type:
            messages = [m for m in messages if m.message_type == message_type]
        if thread_id:
            messages = [m for m in messages if m.thread_id == thread_id]

        # Newest first
        messages.sort(key=lambda m: m.created_at, reverse=True)
        return messages[offset : offset + limit]

    # ------------------------------------------------------------------
    # Read — outbox
    # ------------------------------------------------------------------

    def outbox(
        self,
        did: str,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Message]:
        with self._lock:
            ids = list(self.by_from_did.get(did, []))

        messages = [
            self.by_message_id[mid]
            for mid in ids
            if mid in self.by_message_id
        ]
        messages = [m for m in messages if m.status != MessageStatus.deleted]
        messages.sort(key=lambda m: m.created_at, reverse=True)
        return messages[offset : offset + limit]

    # ------------------------------------------------------------------
    # Read — thread
    # ------------------------------------------------------------------

    def thread(self, thread_id: str) -> List[Message]:
        with self._lock:
            ids = list(self.by_thread_id.get(thread_id, []))

        messages = [
            self.by_message_id[mid]
            for mid in ids
            if mid in self.by_message_id
        ]
        messages = [m for m in messages if m.status != MessageStatus.deleted]
        messages.sort(key=lambda m: m.created_at)
        return messages

    # ------------------------------------------------------------------
    # Read — single
    # ------------------------------------------------------------------

    def get(self, message_id: str) -> Optional[Message]:
        return self.by_message_id.get(message_id)

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self, did: str) -> dict:
        inbox_msgs = self.inbox(did, limit=100_000)
        outbox_msgs = self.outbox(did, limit=100_000)

        unread = sum(
            1 for m in inbox_msgs if m.status not in (MessageStatus.read,)
        )

        # Active threads: any thread where did appears in at least one message
        with self._lock:
            all_ids = set(self.by_to_did.get(did, [])) | set(self.by_from_did.get(did, []))

        thread_ids: set[str] = set()
        for mid in all_ids:
            msg = self.by_message_id.get(mid)
            if msg and msg.status != MessageStatus.deleted:
                thread_ids.add(msg.thread_id)

        last_msg_at: Optional[datetime] = None
        all_msgs = inbox_msgs + outbox_msgs
        if all_msgs:
            last_msg_at = max(m.created_at for m in all_msgs)

        return {
            "inbox_count": len(inbox_msgs),
            "unread_count": unread,
            "outbox_count": len(outbox_msgs),
            "threads_active": len(thread_ids),
            "last_message_at": last_msg_at,
        }

    # ------------------------------------------------------------------
    # Counts (for health endpoint)
    # ------------------------------------------------------------------

    @property
    def message_count(self) -> int:
        return len(self.by_message_id)

    @property
    def thread_count(self) -> int:
        return len(self.by_thread_id)


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

store = MessageStore()


# ---------------------------------------------------------------------------
# Seed data — GPU compute negotiation between two agents
# ---------------------------------------------------------------------------

def _seed() -> None:
    """
    Pre-populate the store with a realistic 5-message negotiation thread.

    Scenario: Agent A (buyer) wants 100 GPU-hours. Agent B (seller) is a
    compute marketplace agent. They negotiate price asynchronously, attach a
    payment request, exchange a receipt, and close with a formal contract.
    """
    AGENT_A = "did:hive:agent_compute_buyer_alpha"
    AGENT_B = "did:hive:agent_compute_seller_beta"
    THREAD  = "thread_gpu_negotiation_001"

    now = datetime.utcnow()

    # ------------------------------------------------------------------
    # Message 1 — Offer from buyer
    # ------------------------------------------------------------------
    msg1 = Message(
        message_id="msg_seed_001",
        thread_id=THREAD,
        from_did=AGENT_A,
        to_did=AGENT_B,
        subject="GPU compute offer — 100 hours",
        body={
            "offer": {
                "resource": "GPU-hours",
                "quantity": 100,
                "unit_price_usdc": 0.50,
                "total_usdc": 50.00,
                "gpu_type": "NVIDIA A100",
                "start_window": "2025-08-01T00:00:00Z",
                "duration_days": 30,
                "notes": "Requesting 100 GPU-hours at $0.50/hr for ML inference workloads.",
            }
        },
        message_type=MessageType.offer,
        status=MessageStatus.read,
        signed=True,
        created_at=now - timedelta(hours=5),
        read_at=now - timedelta(hours=4, minutes=50),
    )

    # ------------------------------------------------------------------
    # Message 2 — Counter-offer from seller
    # ------------------------------------------------------------------
    msg2 = Message(
        message_id="msg_seed_002",
        thread_id=THREAD,
        from_did=AGENT_B,
        to_did=AGENT_A,
        subject="Re: GPU compute offer — counter-proposal",
        body={
            "counter_offer": {
                "resource": "GPU-hours",
                "quantity_min": 200,
                "unit_price_usdc": 0.45,
                "total_usdc_at_min": 90.00,
                "gpu_type": "NVIDIA A100",
                "start_window": "2025-08-01T00:00:00Z",
                "duration_days": 30,
                "notes": (
                    "$0.45/hr is achievable only at 200+ committed hours. "
                    "Volume discount applied automatically. "
                    "Unused hours roll over within the 30-day window."
                ),
                "expires_offer_utc": "2025-07-25T23:59:59Z",
            }
        },
        message_type=MessageType.counter_offer,
        status=MessageStatus.read,
        signed=True,
        reply_to_message_id="msg_seed_001",
        created_at=now - timedelta(hours=4, minutes=30),
        read_at=now - timedelta(hours=4),
    )

    # ------------------------------------------------------------------
    # Message 3 — Acceptance + payment request from buyer
    # ------------------------------------------------------------------
    msg3 = Message(
        message_id="msg_seed_003",
        thread_id=THREAD,
        from_did=AGENT_A,
        to_did=AGENT_B,
        subject="Acceptance + payment request for 200 GPU-hours",
        body={
            "acceptance": {
                "accepted_terms": {
                    "resource": "GPU-hours",
                    "quantity": 200,
                    "unit_price_usdc": 0.45,
                    "total_usdc": 90.00,
                    "gpu_type": "NVIDIA A100",
                    "start_window": "2025-08-01T00:00:00Z",
                    "duration_days": 30,
                },
                "payment_note": "Initiating USDC transfer via HiveGate. Awaiting receipt.",
            }
        },
        message_type=MessageType.acceptance,
        status=MessageStatus.read,
        signed=True,
        settlement_attached=SettlementAttached(
            amount_usdc=90.00,
            rail="usdc",
            memo="GPU compute 200h — thread_gpu_negotiation_001",
        ),
        reply_to_message_id="msg_seed_002",
        created_at=now - timedelta(hours=3, minutes=45),
        read_at=now - timedelta(hours=3, minutes=30),
    )

    # ------------------------------------------------------------------
    # Message 4 — Receipt from seller
    # ------------------------------------------------------------------
    msg4 = Message(
        message_id="msg_seed_004",
        thread_id=THREAD,
        from_did=AGENT_B,
        to_did=AGENT_A,
        subject="Payment received — resources provisioned",
        body={
            "receipt": {
                "tx_id": "0xabc123def456hive789",
                "amount_usdc": 90.00,
                "received_at": (now - timedelta(hours=3, minutes=15)).isoformat(),
                "provisioned": True,
                "access_endpoint": "https://compute.hivegate.onrender.com/gpu/session/sess_a100_20250801",
                "api_key_hint": "hive_gpu_****_alpha",
                "notes": "Resources provisioned. Session active from 2025-08-01T00:00:00Z.",
            }
        },
        message_type=MessageType.receipt,
        status=MessageStatus.read,
        signed=True,
        settlement_attached=SettlementAttached(
            amount_usdc=90.00,
            rail="usdc",
            memo="Confirmed receipt — GPU 200h",
            tx_id="0xabc123def456hive789",
        ),
        reply_to_message_id="msg_seed_003",
        created_at=now - timedelta(hours=3, minutes=15),
        read_at=now - timedelta(hours=3),
    )

    # ------------------------------------------------------------------
    # Message 5 — Formal contract proposal from seller
    # ------------------------------------------------------------------
    msg5 = Message(
        message_id="msg_seed_005",
        thread_id=THREAD,
        from_did=AGENT_B,
        to_did=AGENT_A,
        subject="Formal service contract — GPU compute agreement",
        body={
            "contract": {
                "contract_id": "contract_gpu_001_alpha_beta",
                "parties": {
                    "buyer": AGENT_A,
                    "seller": AGENT_B,
                },
                "service": "GPU compute — NVIDIA A100",
                "quantity_hours": 200,
                "unit_price_usdc": 0.45,
                "total_usdc": 90.00,
                "payment_tx_id": "0xabc123def456hive789",
                "start_date": "2025-08-01T00:00:00Z",
                "end_date": "2025-08-31T23:59:59Z",
                "rollover_policy": "Unused hours roll over; expire 30 days from first use.",
                "sla": {
                    "uptime_pct": 99.5,
                    "support_channel": f"did:hive:support_compute_seller_beta",
                    "dispute_resolution": "https://hive-discovery.onrender.com/arbitration",
                },
                "signed_by_seller": True,
                "awaiting_buyer_signature": True,
                "instructions": "Please sign and return this contract to activate SLA protections.",
            }
        },
        message_type=MessageType.contract_proposal,
        status=MessageStatus.delivered,
        signed=True,
        reply_to_message_id="msg_seed_004",
        created_at=now - timedelta(hours=2, minutes=50),
    )

    for msg in [msg1, msg2, msg3, msg4, msg5]:
        store.put(msg)


# Run seed on import
_seed()

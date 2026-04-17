"""
HiveMessenger — Thread management.

Threads group a sequence of messages into a negotiation conversation.
A thread_id is generated automatically on the first message and propagated
to all replies.  The thread_id is a stable, URL-safe identifier derived
from the first message's UUID.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from models import Message


def new_thread_id() -> str:
    """Generate a fresh thread identifier."""
    return f"thread_{uuid.uuid4().hex[:16]}"


def new_message_id() -> str:
    """Generate a unique message identifier."""
    return f"msg_{uuid.uuid4().hex}"


def resolve_thread_id(
    requested_thread_id: Optional[str],
    thread_index: Dict[str, List[str]],
) -> str:
    """
    Return the thread_id to use for a new message.

    - If a thread_id is supplied and already exists, use it (reply flow).
    - If a thread_id is supplied but doesn't exist yet, accept it (allows
      callers to pre-negotiate IDs out-of-band).
    - If no thread_id is supplied, create a new one.
    """
    if requested_thread_id:
        return requested_thread_id
    return new_thread_id()


def get_thread_participants(messages: List["Message"]) -> List[str]:
    """Return all unique DIDs that appear in a thread (sender or recipient)."""
    dids: set[str] = set()
    for msg in messages:
        dids.add(msg.from_did)
        dids.add(msg.to_did)
    return sorted(dids)


def get_thread_summary(messages: List["Message"]) -> dict:
    """
    Lightweight summary of a thread — useful for listing active threads
    without returning the full message payloads.
    """
    if not messages:
        return {}

    sorted_msgs = sorted(messages, key=lambda m: m.created_at)
    first = sorted_msgs[0]
    last = sorted_msgs[-1]

    unread = sum(
        1
        for m in messages
        if m.status not in ("read", "deleted")
    )

    return {
        "thread_id": first.thread_id,
        "subject": first.subject,
        "participants": get_thread_participants(messages),
        "message_count": len(messages),
        "unread_count": unread,
        "opened_at": first.created_at,
        "last_message_at": last.created_at,
        "last_message_type": last.message_type,
        "last_from_did": last.from_did,
    }


def is_reply(message: "Message", thread_messages: List["Message"]) -> bool:
    """Return True if the message is a reply to an existing thread."""
    return any(m.message_id != message.message_id for m in thread_messages)

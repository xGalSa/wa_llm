from typing import List

from src.models import Message
from src.whatsapp.jid import parse_jid


def chat2text(history: List[Message]) -> str:
    """Convert message history to readable text format for AI processing."""
    formatted_messages = []
    for message in history:
        # Format timestamp to be more readable
        timestamp_str = message.timestamp.strftime("%Y-%m-%d %H:%M")
        username = parse_jid(message.sender_jid).user
        # Clean and format the message text
        text = (message.text or "").strip()
        formatted_messages.append(f"[{timestamp_str}] @{username}: {text}")
    
    return "\n".join(formatted_messages)

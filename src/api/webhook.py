from typing import Annotated
import logging

from fastapi import APIRouter, Depends

from src.api.deps import get_handler
from src.handler import MessageHandler
from src.models.webhook import WhatsAppWebhookPayload

# Create router for webhook endpoints
router = APIRouter(tags=["webhook"])


@router.post("/webhook")
async def webhook(
    payload: WhatsAppWebhookPayload,
    handler: Annotated[MessageHandler, Depends(get_handler)],
) -> str:
    """
    WhatsApp webhook endpoint for receiving incoming messages.
    Returns:
        Simple "ok" response to acknowledge receipt
    """
    try:
        # Remove privacy-sensitive logging
        # print(f"=== WEBHOOK RECEIVED ===")
        # print(f"From: {payload.from_}")
        # print(f"Message text: {payload.message.text if payload.message else 'No message'}")
        
        # Only process messages that have a sender (from_ field)
        if payload.from_:
            # print("Calling handler...")
            await handler(payload)
            # print("=== HANDLER COMPLETED ===")
        return "ok"
    except Exception as e:
        print(f"Webhook error: {e}")
        import traceback
        print(f"Traceback: {traceback.format_exc()}")
        # Still return "ok" to avoid WhatsApp retrying
        return "ok"

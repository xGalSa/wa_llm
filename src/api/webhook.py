from typing import Annotated
import logging

from fastapi import APIRouter, Depends

from api.deps import get_handler
from handler import MessageHandler
from models.webhook import WhatsAppWebhookPayload

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
        # Only process messages that have a sender (from_ field)
        if payload.from_:
            await handler(payload)
        return "ok"
    except Exception as e:
        logging.error(f"Webhook error: {e}")
        import traceback
        logging.error(f"Traceback: {traceback.format_exc()}")
        # Still return "ok" to avoid WhatsApp retrying
        return "ok"

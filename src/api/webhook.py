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
    logger = logging.getLogger(__name__)
    
    try:
        msg_id = payload.message.id if payload.message else "<none>"
        text = payload.message.text if payload.message and payload.message.text else ""
        logger.info(
            f"webhook received from={payload.from_} msg_id={msg_id} ts={payload.timestamp} text_len={len(text)}"
        )
        
        # Only process messages that have a sender (from_ field)
        if payload.from_:
            logger.info("webhook dispatching to handler")
            await handler(payload)
            logger.info("webhook handler completed")
        else:
            logger.info("webhook missing from_, skipping")
            
        return "ok"
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        # Still return "ok" to avoid WhatsApp retrying
        return "ok"

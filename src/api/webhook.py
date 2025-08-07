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
        logger.info(f"=== WEBHOOK RECEIVED ===")
        logger.info(f"From: {payload.from_}")
        logger.info(f"Message text: {payload.message.text if payload.message else 'No message'}")
        logger.info(f"Timestamp: {payload.timestamp}")
        
        # Only process messages that have a sender (from_ field)
        if payload.from_:
            logger.info("Calling handler...")
            await handler(payload)
            logger.info("=== HANDLER COMPLETED ===")
        else:
            logger.info("No sender (from_ field), skipping processing")
            
        return "ok"
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        # Still return "ok" to avoid WhatsApp retrying
        return "ok"

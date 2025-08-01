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
        # Add comprehensive debug logging
        print(f"=== WEBHOOK RECEIVED ===")
        print(f"From: {payload.from_}")
        print(f"Message text: {payload.message.text if payload.message else 'No message'}")
        
        # Force send a test message to see if the bot can respond
        if payload.from_ and payload.message and "972515004420" in payload.message.text:
            print("Bot mentioned! Sending test response...")
            try:
                await handler.send_message(
                    "120363401598328725@g.us",
                    "🤖 Bot is working! This is a test response.",
                    payload.message.id
                )
                print("Test message sent successfully!")
            except Exception as e:
                print(f"Failed to send test message: {e}")
                import traceback
                print(f"Traceback: {traceback.format_exc()}")
        
        # Only process messages that have a sender (from_ field)
        if payload.from_:
            print("Calling handler...")
            await handler(payload)
            print("=== HANDLER COMPLETED ===")
        return "ok"
    except Exception as e:
        print(f"Webhook error: {e}")
        import traceback
        print(f"Traceback: {traceback.format_exc()}")
        # Still return "ok" to avoid WhatsApp retrying
        return "ok"

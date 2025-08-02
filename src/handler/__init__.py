import logging
import httpx
import traceback

from sqlmodel.ext.asyncio.session import AsyncSession
from voyageai.client_async import AsyncClient

from handler.router import Router
from handler.whatsapp_group_link_spam import WhatsappGroupLinkSpamHandler
from models import (
    WhatsAppWebhookPayload,
    Message,
)
from whatsapp import WhatsAppClient
from .base_handler import BaseHandler

logger = logging.getLogger(__name__)

# Global variable to store access state
_bot_access_enabled = False

class MessageHandler(BaseHandler):
    def __init__(
        self,
        session: AsyncSession,
        whatsapp: WhatsAppClient,
        embedding_client: AsyncClient,
    ):
        self.router = Router(session, whatsapp, embedding_client)
        self.whatsapp_group_link_spam = WhatsappGroupLinkSpamHandler(
            session, whatsapp, embedding_client
        )
        super().__init__(session, whatsapp, embedding_client)

    async def __call__(self, payload: WhatsAppWebhookPayload):
        print("=== MESSAGE HANDLER START ===")
        
        try:
            message = await self.store_message(payload)
            print(f"Message stored: {message is not None}")
            print(f"Message text: {message.text if message else 'None'}")
            print(f"Message sender: {message.sender_jid if message else 'None'}")
            print(f"Message group: {message.group_jid if message else 'None'}")

            if (
                message
                and message.group
                and message.group.managed
                and message.group.forward_url
            ):
                await self.forward_message(payload, message.group.forward_url)

            # ignore messages that don't exist or don't have text
            if not message or not message.text:
                print("No message or no text - returning")
                return

            if message.sender_jid.endswith("@lid"):
                logging.info(
                    f"Received message from {message.sender_jid}: {payload.model_dump_json()}"
                )

            # ignore messages from unmanaged groups
            # TEMPORARILY DISABLED FOR TESTING
            # if message and message.group and not message.group.managed:
            #     return

            # NEW FEATURE: Check for @כולם mentions
            if "@כולם" in message.text:
                print("Found @כולם mention - tagging all participants")
                await self.tag_all_participants(message)
                return  # Exit early, don't process bot mentions

            print("Checking if bot was mentioned...")
            my_jid = await self.whatsapp.get_my_jid()
            print(f"My JID: {my_jid}")
            print(f"Message text: {message.text}")
            print(f"Looking for: @{my_jid.user}")
            
            # If bot was mentioned
            if message.has_mentioned(my_jid):
                print("Bot was mentioned!")
                
                global _bot_access_enabled

                # Admin command - check if message contains "allow"
                if message.sender_jid.startswith("972532741041") and "allow" in message.text.lower():
                    _bot_access_enabled = not _bot_access_enabled
                    await self.send_message(message.chat_jid, f"🔐 *מצב גישה:* {'מופעל' if _bot_access_enabled else 'מושבתת'}", message.message_id)
                    return
                
                # Simple access check - either access is enabled OR user is admin
                if _bot_access_enabled or message.sender_jid.startswith("972532741041"):
                    await self.router(message)
                else:
                    await self.send_message(message.chat_jid, "הלו גברתי אדוני, רק המק״ס יכול לדבר איתי", message.message_id)
            else:
                print("Bot was not mentioned")

            print("=== MESSAGE HANDLER END ===")

        except Exception as e:
            print(f"Error in message handler: {e}")
            import traceback
            print(f"Traceback: {traceback.format_exc()}")

    async def forward_message(
        self, payload: WhatsAppWebhookPayload, forward_url: str
    ) -> None:
        """
        Forward a message to the group's configured forward URL using HTTP POST.

        :param payload: The WhatsApp webhook payload to forward
        :param forward_url: The URL to forward the message to
        """
        # Ensure we have a forward URL
        if not forward_url:
            return

        try:
            # Create an async HTTP client and forward the message
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    forward_url,
                    json=payload.model_dump(mode="json"),  # Convert Pydantic model to dict for JSON serialization
                    headers={"Content-Type": "application/json"},
                )
                response.raise_for_status()

        except httpx.HTTPError as exc:
            # Log the error but don't raise it to avoid breaking message processing
            logger.error(f"Failed to forward message to {forward_url}: {exc}")
        except Exception as exc:
            # Catch any other unexpected errors
            logger.error(f"Unexpected error forwarding message to {forward_url}: {exc}")

    async def tag_all_participants(self, message: Message):
        """
        Tag all participants in the group when @כולם is mentioned
        """
        try:
            # Get bot's phone number to exclude it
            my_jid = await self.whatsapp.get_my_jid()
            bot_phone = my_jid.user
            
            # Get all groups and find this one
            groups_response = await self.whatsapp.get_user_groups()
            
            # Find the target group first
            target_group = next(
                (group for group in groups_response.results.data if group.JID == message.chat_jid),
                None
            )
            
            if target_group:
                # Tag everyone except the bot
                tagged_message = ""
                for participant in target_group.Participants:
                    # Only tag participants with actual phone numbers
                    if participant.JID.endswith('@s.whatsapp.net') or participant.JID.endswith('@c.us'):
                        phone = participant.JID.split('@')[0]
                        if phone != bot_phone:
                            tagged_message += f"@{phone} "
                
                # Send either the tagged message or fallback
                response_text = tagged_message.strip() or "📢 כולם מוזמנים! 🎉"
                await self.send_message(message.chat_jid, response_text, message.message_id)
                return
                    
        except Exception as e:
            logger.error(f"Error tagging participants: {e}")
        
        # Fallback
        await self.send_message(message.chat_jid, "📢 כולם מוזמנים!", message.message_id)
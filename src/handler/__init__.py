import logging
import httpx

from sqlmodel.ext.asyncio.session import AsyncSession
from voyageai.client_async import AsyncClient

from handler.router import Router
from handler.whatsapp_group_link_spam import WhatsappGroupLinkSpamHandler
from models import (
    WhatsAppWebhookPayload,
)
from whatsapp import WhatsAppClient
from .base_handler import BaseHandler
from whatsapp.models import Message

logger = logging.getLogger(__name__)


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
                # Check if the message is from the authorized user (972532741041)
                if message.sender_jid.startswith("972532741041"):
                    print("Authorized user - calling router")
                    # Full functionality for the makas
                    await self.router(message)
                    print("Router completed")
                else:
                    print("Unauthorized user - sending restricted message")
                    # Predefined message for everyone else
                    await self.send_message(
                        message.chat_jid,
                        "הלו גברתי אדוני, רק המק״ס יכול לדבר איתי",
                        message.message_id,
                    )
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
            # Extract group ID from the chat JID
            group_id = message.chat_jid.split('@')[0]
            
            # Get participants from the API
            participants_data = await self.whatsapp.get_group_participants(group_id)
            
            # Get the participants list
            participants = []
            if isinstance(participants_data, list):
                participants = participants_data
            elif isinstance(participants_data, dict) and 'participants' in participants_data:
                participants = participants_data['participants']
            
            if participants:
                # Create a message with all participants tagged
                tagged_message = ""
                
                for participant in participants:
                    if isinstance(participant, dict):
                        phone = participant.get('JID') or participant.get('phone')
                        if phone and '@' in phone:
                            phone_number = phone.split('@')[0]
                            tagged_message += f"@{phone_number} "
                
                # Send the tagged message
                await self.send_message(
                    message.chat_jid,
                    tagged_message,
                    message.message_id,
                )
                
        except Exception as e:
            print(f"Error tagging participants: {e}")
            await self.send_message(
                message.chat_jid,
                "📢 כולם מוזמנים!",
                message.message_id,
            )
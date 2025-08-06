import asyncio
import logging
import httpx
import traceback
from datetime import datetime
from typing import Optional

from sqlmodel.ext.asyncio.session import AsyncSession
from voyageai.client_async import AsyncClient

from handler.router import Router
from handler.whatsapp_group_link_spam import WhatsappGroupLinkSpamHandler
from models import (
    WhatsAppWebhookPayload,
    Message,
)
from utils.phone_mapper import phone_mapper
from whatsapp import WhatsAppClient
from .base_handler import BaseHandler

logger = logging.getLogger(__name__)

# Global bot access control
_bot_access_enabled = False


async def get_user_groups_with_retry(whatsapp: WhatsAppClient, max_retries: int = 3):
    """Get user groups with simple retry logic for rate limiting and server errors"""
    for attempt in range(max_retries):
        try:
            return await whatsapp.get_user_groups()
        except httpx.HTTPStatusError as exc:
            # Retry on both 429 (rate limit) and 500 (server error)
            if exc.response.status_code in [429, 500]:
                wait_time = 2 ** attempt  # Exponential backoff: 1, 2, 4 seconds
                logger.warning(f"HTTP {exc.response.status_code} error (attempt {attempt + 1}/{max_retries}). Waiting {wait_time}s...")
                await asyncio.sleep(wait_time)
                continue
            else:
                raise
        except Exception as e:
            logger.error(f"Unexpected error fetching groups: {e}")
            raise
    
    # If we get here, all retries failed
    logger.error("All retries failed for get_user_groups")
    raise httpx.HTTPStatusError("Request failed after all retries", request=None, response=None)


def extract_phone_from_participant(participant):
    """Extract phone number from participant data"""
    try:
        # Try to get PhoneNumber directly from the model
        if participant.PhoneNumber:
            phone = participant.PhoneNumber
            # Extract phone number from format "972585277785@s.whatsapp.net"
            return phone.split('@')[0] if '@' in phone else phone
        
        # Fallback to phone mapper using JID
        return phone_mapper.get_phone(participant.JID)
        
    except Exception as e:
        logger.warning(f"Error extracting phone from participant: {e}")
        return None


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

            # Handle message forwarding
            if (
                message
                and message.group
                and message.group.managed
                and message.group.forward_url
            ):
                await self.forward_message(payload, message.group.forward_url)

            # Early return if no message or no text
            if not message or not message.text:
                print("No message or no text - returning")
                return

            # Update phone database
            await self.update_global_phone_database(message)

            # Handle @כולם mentions
            if "@כולם" in message.text and not payload.forwarded:
                print("Found @כולם mention - tagging all participants")
                await self.tag_all_participants(message)
                return

            # Check if bot was mentioned
            print("Checking if bot was mentioned...")
            my_jid = await self.whatsapp.get_my_jid()
            
            if message.has_mentioned(my_jid):
                print("Bot was mentioned!")
                await self._handle_bot_command(message)
            else:
                print("Bot was not mentioned")

            print("=== MESSAGE HANDLER END ===")

        except Exception as e:
            print(f"Error in message handler: {e}")
            import traceback
            print(f"Traceback: {traceback.format_exc()}")

    async def _handle_bot_command(self, message: Message):
        """Handle bot commands with simplified logic"""
        global _bot_access_enabled

        # Admin command
        if message.sender_jid.startswith("972532741041") and "allow" in message.text.lower():
            _bot_access_enabled = not _bot_access_enabled
            status = "מופעל" if _bot_access_enabled else "מושבתת"
            await self.send_message(message.chat_jid, f"*מצב גישה:* {status}", message.message_id)
            return

        # Check access permissions
        is_admin = message.sender_jid.startswith("972532741041")
        if not (_bot_access_enabled or is_admin):
            await self.send_message(message.chat_jid, "הלו גברתי אדוני, רק המק״ס יכול לדבר איתי", message.message_id)
            return

        # Route to appropriate handler using Router's __call__ method
        print("Routing message to appropriate handler")
        await self.router(message)  # This calls Router.__call__(message)

    async def update_global_phone_database(self, message: Message):
        """Update the global phone number database when messages come in"""
        try:
            if message.sender_jid and '@' in message.sender_jid:
                phone = message.sender_jid.split('@')[0]
                jid = message.sender_jid
                
                # Store JID -> phone mapping
                phone_mapper.add_jid_mapping(jid, phone)
                
                # Also analyze all groups to find LID mappings for this phone
                await self.analyze_groups_for_lid_mappings(phone, jid)
                
        except Exception as e:
            logger.error(f"Error updating global phone database: {e}")

    async def analyze_groups_for_lid_mappings(self, phone: str, jid: str):
        """Analyze all groups to find LID mappings for a known phone number"""
        try:
            # Get all groups with retry logic
            groups_response = await get_user_groups_with_retry(self.whatsapp)
            
            for group in groups_response.results.data:
                for participant in group.Participants:
                    # If this participant has the same phone in their JID, 
                    # but appears as LID in this group, create the mapping
                    if participant.JID.endswith('@lid'):
                        # We can't directly match, but we can build mappings over time
                        # This is a limitation - we need other logic to connect LIDs to phones
                        pass
                    elif participant.JID == jid:
                        # This person appears with phone JID in this group
                        # Look for other groups where they might appear as LID
                        pass
                        
        except Exception as e:
            logger.error(f"Error analyzing groups for LID mappings: {e}")

    async def find_lid_for_phone_across_groups(self, phone: str, jid: str):
        """Try to find LID representations of this phone across groups"""
        # This is complex because we can't directly match LID to phone
        # We would need additional logic or data to make this connection
        # For now, let's focus on building mappings from known data
        pass

    async def tag_all_participants(self, message: Message):
        """Tag all participants in the group when @כולם is mentioned"""
        try:
            # Get bot's phone number to exclude it
            my_jid = await self.whatsapp.get_my_jid()
            bot_phone = my_jid.user
            logger.info(f"Bot phone: {bot_phone}")
            
            # Get all groups with retry logic for rate limiting
            groups_response = await get_user_groups_with_retry(self.whatsapp)
            
            # Add null check for results
            if not groups_response.results or not groups_response.results.data:
                logger.info("No groups data found")
                await self.send_message(message.chat_jid, "📢 כולם מוזמנים!", message.message_id)
                return
            
            # Find the target group first
            target_group = next(
                (group for group in groups_response.results.data if group.JID == message.chat_jid),
                None
            )
            
            if target_group:
                logger.info(f"Found target group with {len(target_group.Participants)} participants")
                
                # Tag everyone except the bot
                tagged_message = ""
                for participant in target_group.Participants:
                    logger.info(f"Processing participant: {participant.JID}")
                    
                    # Extract phone number using our helper function
                    phone = extract_phone_from_participant(participant)
                    logger.info(f"Got phone: {phone} for JID: {participant.JID}")
                    
                    # Only tag if we have a real phone number and it's not the bot
                    if phone and phone != bot_phone:
                        tagged_message += f"@{phone} "
                        logger.info(f"Added to tagged message: @{phone}")
                
                logger.info(f"Tagged message so far: '{tagged_message}'")
                
                # If no phone numbers found, use all known phones from other groups
                if not tagged_message.strip():
                    logger.info("No participants tagged, checking all known phones")
                    all_phones = phone_mapper.get_all_phones()
                    logger.info(f"All known phones: {all_phones}")
                    
                    for phone in all_phones:
                        if phone != bot_phone:
                            tagged_message += f"@{phone} "
                            logger.info(f"Added from known phones: @{phone}")
                
                logger.info(f"Final tagged message: '{tagged_message}'")
                
                # Send either the tagged message or fallback
                response_text = tagged_message.strip() or " כולם מוזמנים! 🎉"
                logger.info(f"Sending response: '{response_text}'")
                await self.send_message(message.chat_jid, response_text, message.message_id)
                return
            else:
                logger.info("Target group not found")
                    
        except Exception as e:
            logger.error(f"Error tagging participants: {e}")
        
        # Fallback
        await self.send_message(message.chat_jid, "📢 כולם מוזמנים!", message.message_id)

    async def forward_message(
        self, payload: WhatsAppWebhookPayload, forward_url: str
    ) -> None:
        """Forward a message to the group's configured forward URL using HTTP POST."""
        if not forward_url:
            return

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    forward_url,
                    json=payload.model_dump(mode="json"),
                    headers={"Content-Type": "application/json"},
                )
                response.raise_for_status()

        except httpx.HTTPError as exc:
            logger.error(f"Failed to forward message to {forward_url}: {exc}")
        except Exception as exc:
            logger.error(f"Unexpected error forwarding message to {forward_url}: {exc}")
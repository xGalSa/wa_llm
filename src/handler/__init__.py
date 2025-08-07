import asyncio
import logging
import httpx
import traceback
from collections import deque
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Optional

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from voyageai.client_async import AsyncClient

from src.handler.router import Router
from src.models.message import Message
from src.models.sender import Sender
from src.models.webhook import WhatsAppWebhookPayload
from src.whatsapp.client import WhatsAppClient
from src.whatsapp.jid import JID, parse_jid
from src.utils.phone_mapper import phone_mapper

logger = logging.getLogger(__name__)

# Global cache for processed message IDs across all webhook calls
_processed_messages_cache: deque[str] = deque(maxlen=1000)
_cache_lock = Lock()

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
    raise httpx.HTTPStatusError("Request failed after all retries", request=httpx.Request("GET", ""), response=httpx.Response(500))


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


class MessageHandler:
    def __init__(
        self,
        session: AsyncSession,
        whatsapp: WhatsAppClient,
        embedding_client: AsyncClient,
    ):
        self.session = session
        self.whatsapp = whatsapp
        self.embedding_client = embedding_client
        self.router = Router(session, whatsapp, embedding_client)
        # Instance-level cache for additional safety
        self.processed_messages = set()

    async def __call__(self, payload: WhatsAppWebhookPayload) -> None:
        """Handle incoming webhook payload."""
        logger.info("=== MESSAGE HANDLER START ===")
        
        try:
            # Extract message from payload
            message = Message.from_webhook(payload)
            if not message:
                logger.info("No message found in payload, skipping")
                return

            # Create unique message identifier
            message_id = f"{message.chat_jid}_{message.message_id}_{message.timestamp}"
            
            # Check global cache first
            if message_id in _processed_messages_cache:
                logger.info(f"Message {message_id} already processed (global cache), skipping")
                return
            _processed_messages_cache.append(message_id)
            
            # Check instance cache as additional safety
            if message_id in self.processed_messages:
                logger.info(f"Message {message_id} already processed (instance cache), skipping")
                return
            
            self.processed_messages.add(message_id)
            
            # Clear instance cache if it gets too large
            if len(self.processed_messages) > 100:
                self.processed_messages.clear()

            # Check message age (skip messages older than 5 minutes)
            message_age = datetime.now(timezone.utc) - message.timestamp
            if message_age.total_seconds() > 300:
                logger.info(f"Message {message_id} is too old ({message_age.total_seconds():.1f}s), skipping")
                return

            logger.info(f"Processing message: {message_id}")
            logger.info(f"Message text: {message.text}")
            logger.info(f"Chat JID: {message.chat_jid}")
            logger.info(f"Sender JID: {message.sender_jid}")

            # Store message in database
            await self._store_message(message)

            # Check if message is from bot itself
            if await self._is_bot_message(message.sender_jid):
                logger.info("Message is from bot itself, skipping")
                return

            # Handle bot commands
            await self._handle_bot_command(message)

        except Exception as e:
            logger.error(f"Error in message handler: {e}", exc_info=True)
        finally:
            logger.info("=== MESSAGE HANDLER END ===")

    async def _is_bot_message(self, sender_jid: str) -> bool:
        """Check if message is from the bot itself."""
        try:
            my_jid = await self.whatsapp.get_my_jid()
            bot_jids = [
                str(my_jid),
                my_jid.normalize_str(),
                f"{my_jid.user}@s.whatsapp.net",
                f"{my_jid.user}@c.us",
            ]
            return sender_jid in bot_jids
        except Exception as e:
            logger.error(f"Error checking if message is from bot: {e}")
            return False

    async def _store_message(self, message: Message) -> None:
        """Store message in database."""
        try:
            # Check if message already exists
            existing_message = await self.session.get(Message, message.message_id)
            
            if existing_message:
                logger.info(f"Message {message.message_id} already exists in database")
                return

            # Store sender if not exists
            sender = await self.session.get(Sender, message.sender_jid)
            
            if not sender:
                sender = Sender(jid=message.sender_jid)
                self.session.add(sender)
                await self.session.commit()
                await self.session.refresh(sender)

            # Store message
            self.session.add(message)
            await self.session.commit()
            logger.info(f"Stored message {message.message_id} in database")
            
        except Exception as e:
            logger.error(f"Error storing message: {e}", exc_info=True)
            await self.session.rollback()

    async def _handle_bot_command(self, message: Message) -> None:
        """Handle bot commands and mentions."""
        logger.info("=== HANDLE BOT COMMAND START ===")
        
        try:
            # Check if bot is mentioned
            my_jid = await self.whatsapp.get_my_jid()
            is_mentioned = message.has_mentioned(my_jid)
            
            logger.info(f"Bot mentioned: {is_mentioned}")
            logger.info(f"Message text: {message.text}")
            logger.info(f"Bot JID: {my_jid}")
            
            if is_mentioned:
                logger.info("Bot is mentioned, routing to handler")
                await self.router(message)
                logger.info("Handler completed successfully")
            else:
                logger.info("Bot not mentioned, skipping")
                
        except Exception as e:
            logger.error(f"Error in bot command handler: {e}", exc_info=True)
        finally:
            logger.info("=== HANDLE BOT COMMAND END ===")

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
            
            if not groups_response.results or not groups_response.results.data:
                return
                
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

    async def send_message(self, to_jid: str, message: str, in_reply_to: str | None = None):
        """Send a message to a JID over WhatsApp"""
        from src.whatsapp.models import SendMessageRequest
        
        resp = await self.whatsapp.send_message(
            SendMessageRequest(
                phone=to_jid,
                message=message,
                reply_message_id=in_reply_to,
            )
        )
        return resp

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
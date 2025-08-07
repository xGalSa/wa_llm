import asyncio
import logging
import httpx
import traceback

from datetime import datetime, timezone

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


logger = logging.getLogger(__name__)



# Global bot access control
_bot_access_enabled = False

async def get_user_groups(whatsapp: WhatsAppClient):
    """Get user groups - single attempt only"""
    try:
        return await whatsapp.get_user_groups()
    except Exception as e:
        logger.error(f"Error fetching groups: {e}")
        raise


def extract_phone_from_participant(participant):
    """Extract phone number from participant data"""
    try:
        # Try to get PhoneNumber directly from the model
        if participant.PhoneNumber:
            phone = participant.PhoneNumber
            # Extract phone number from format "972585277785@s.whatsapp.net"
            return phone.split('@')[0] if '@' in phone else phone
        
        # No fallback - return None if no phone number found
        return None
        
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

    async def __call__(self, payload: WhatsAppWebhookPayload) -> None:
        """Handle incoming webhook payload."""
        logger.info("=== MESSAGE HANDLER START ===")
        logger.info(f"Payload from: {payload.from_}")
        logger.info(f"Payload timestamp: {payload.timestamp}")
        logger.info(f"Payload has message: {payload.message is not None}")
        logger.info(f"Payload has reaction: {payload.reaction is not None}")
        
        try:
            # Extract message from payload
            message = Message.from_webhook(payload)
            if not message:
                logger.info("No message found in payload, skipping")
                return

            # Create unique message identifier using WhatsApp's message ID and sender
            message_id = f"{message.chat_jid}_{message.message_id}_{message.sender_jid}"

            logger.info(f"Processing message: {message_id}")
            logger.info(f"Message text: {message.text}")
            logger.info(f"Chat JID: {message.chat_jid}")
            logger.info(f"Sender JID: {message.sender_jid}")
            logger.info(f"Message timestamp: {message.timestamp}")

            # Store message in database
            await self._store_message(message)
            logger.info(f"Message stored in database")

            # Check if message is from bot itself
            if await self._is_bot_message(message.sender_jid):
                logger.info("Message is from bot itself, skipping")
                return

            # Handle bot commands
            logger.info("About to handle bot command...")
            await self._handle_bot_command(message)
            logger.info("Bot command handling completed")

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
            is_bot = sender_jid in bot_jids
            logger.info(f"Bot message check: sender={sender_jid}, my_jid={my_jid}, is_bot={is_bot}")
            logger.info(f"Bot JIDs checked: {bot_jids}")
            return is_bot
        except Exception as e:
            logger.error(f"Error checking if message is from bot: {e}")
            # If we can't determine, assume it's not from bot to be safe
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
            logger.info(f"Message sender JID: {message.sender_jid}")
            logger.info(f"Message chat JID: {message.chat_jid}")
            
            if is_mentioned:
                logger.info("Bot is mentioned, routing to handler")
                logger.info("About to call router...")
                await self.router(message)
                logger.info("Router completed successfully")
            else:
                logger.info("Bot not mentioned, skipping")
                
        except Exception as e:
            logger.error(f"Error in bot command handler: {e}", exc_info=True)
        finally:
            logger.info("=== HANDLE BOT COMMAND END ===")



    async def tag_all_participants(self, message: Message):
        """Tag all participants in the group when @כולם is mentioned"""
        try:
            # Get bot's phone number to exclude it
            my_jid = await self.whatsapp.get_my_jid()
            bot_phone = my_jid.user
            logger.info(f"Bot phone: {bot_phone}")
            
            # Get all groups - single attempt only
            groups_response = await get_user_groups(self.whatsapp)
            
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
                
                # If no phone numbers found, just use the fallback message
                if not tagged_message.strip():
                    logger.info("No participants tagged, will use fallback message")
                
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
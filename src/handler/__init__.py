import logging
import httpx

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
        logger.info(
            f"handler start from={payload.from_} msg_id={(payload.message.id if payload.message else '<none>')} ts={payload.timestamp} has_msg={bool(payload.message)} has_reaction={bool(payload.reaction)}"
        )
        
        try:
            # Extract message from payload
            message = Message.from_webhook(payload)
            if not message:
                logger.info("handler no message after parse, skip")
                return

            # Create unique message identifier using WhatsApp's message ID and sender
            message_id = f"{message.chat_jid}_{message.message_id}_{message.sender_jid}"

            logger.info(
                f"handler msg id={message_id} chat={message.chat_jid} sender={message.sender_jid} text_len={(len(message.text) if message.text else 0)} ts={message.timestamp}"
            )

            # Skip storing and handling for messages without text unless they include special commands or mentions
            if message.text is None or message.text.strip() == "":
                logger.info("handler empty text, skip")
                return

            # Store message in database; only continue if this is the first time we see it
            is_new_message = await self._store_message(message)
            logger.info(
                f"handler stored is_new={is_new_message} key={message.message_id}"
            )
            if not is_new_message:
                logger.info("handler duplicate detected, skip routing")
                return

            # Check if message is from bot itself
            if await self._is_bot_message(message.sender_jid):
                logger.info("handler message from self, skip")
                return

            # Handle bot commands
            logger.info("handler routing to bot command handler")
            await self._handle_bot_command(message)
            logger.info("handler bot command handler done")

        except Exception as e:
            logger.error(f"handler error: {e}", exc_info=True)
        finally:
            logger.info("handler end")

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
            logger.info(
                f"handler is_bot_check sender={sender_jid} my={my_jid} is_bot={is_bot}"
            )
            return is_bot
        except Exception as e:
            logger.error(f"Error checking if message is from bot: {e}")
            # If we can't determine, assume it's not from bot to be safe
            return False

    async def _store_message(self, message: Message) -> bool:
        """Store message in database.
        Returns True if newly stored, False if it already existed or failed.
        """
        try:
            # Do not store messages without text to avoid DB noise
            if not message.text or message.text.strip() == "":
                logger.info("handler skip store: empty text")
                return False
            # Check if message already exists
            existing_message = await self.session.get(Message, message.message_id)
            
            if existing_message:
                logger.info(f"handler already exists id={message.message_id}")
                return False

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
            logger.info(f"handler stored id={message.message_id}")
            return True
            
        except Exception as e:
            logger.error(f"handler store error: {e}", exc_info=True)
            await self.session.rollback()
            return False

    async def _handle_bot_command(self, message: Message) -> None:
        """Handle bot commands and mentions."""
        logger.info("handler bot_command start")
        
        try:
            # Check if bot is mentioned
            my_jid = await self.whatsapp.get_my_jid()
            is_mentioned = message.has_mentioned(my_jid)
            
            # Check for special commands that should be processed even without mention
            is_special_command = False
            if message.text:
                special_commands = ["@כולם", "@everyone"]
                is_special_command = any(cmd in message.text for cmd in special_commands)
            
            logger.info(
                f"handler bot_command mentioned={is_mentioned} special={is_special_command} bot={my_jid} sender={message.sender_jid} chat={message.chat_jid}"
            )
            
            if is_mentioned or is_special_command:
                logger.info("handler bot_command -> router")
                await self.router(message)
                logger.info("handler bot_command router done")
            else:
                logger.info("handler bot_command not mentioned and no special, skip")
                
        except Exception as e:
            logger.error(f"handler bot_command error: {e}", exc_info=True)
        finally:
            logger.info("handler bot_command end")

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
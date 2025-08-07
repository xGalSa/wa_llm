import logging
from sqlmodel.ext.asyncio.session import AsyncSession
from voyageai.client_async import AsyncClient

from src.models import (
    WhatsAppWebhookPayload,
    BaseGroup,
    BaseSender,
    Message,
    Sender,
    Group,
    BaseMessage,
    upsert,
)
from src.whatsapp.jid import normalize_jid
from src.whatsapp.client import WhatsAppClient
from src.whatsapp.models import SendMessageRequest

logger = logging.getLogger(__name__)


class BaseHandler:
    def __init__(
        self,
        session: AsyncSession,
        whatsapp: WhatsAppClient,
        embedding_client: AsyncClient,
    ):
        self.session = session
        self.whatsapp = whatsapp
        self.embedding_client = embedding_client

    async def store_message(
        self,
        message: Message | BaseMessage | WhatsAppWebhookPayload,
        sender_pushname: str | None = None,
    ) -> Message | None:
        """
        Store a message in the database
        :param message:  Message to store - can be a Message, BaseMessage or WhatsAppWebhookPayload
        :param sender_pushname:  Pushname of the sender [Optional]
        :return: The stored message
        """
        if isinstance(message, WhatsAppWebhookPayload):
            sender_pushname = message.pushname
            message = Message.from_webhook(message)
        if isinstance(message, BaseMessage):
            message = Message(**message.model_dump())

        if not message.text:
            return message  # Don't store messages without text

        async with self.session.begin_nested():
            # Ensure sender exists and is committed
            sender = await self.session.get(Sender, message.sender_jid)
            if sender is None:
                sender = Sender(
                    **BaseSender(
                        jid=message.sender_jid,  # Use normalized JID from message
                        push_name=sender_pushname,
                    ).model_dump()
                )
                await self.upsert(sender)
                await (
                    self.session.flush()
                )  # Ensure sender is visible in this transaction

            if message.group_jid:
                group = await self.session.get(Group, message.group_jid)
                if group is None:
                    group = Group(**BaseGroup(group_jid=message.group_jid).model_dump())
                    await self.upsert(group)
                    await self.session.flush()

            # Finally add the message
            stored_message = await self.upsert(message)
            if isinstance(stored_message, Message):
                return stored_message
            return None

    async def send_message(
        self, to_jid: str, message: str, in_reply_to: str | None = None
    ) -> Message:
        """
        Send a message to a JID over WhatsApp, and store the message in the database
        :param to_jid: The JID to send the message to
        :param message: The message text to send
        :param in_reply_to: The JID of the message to reply to [Optional]
        :return: The stored message
        """
        logger.info(f"=== SEND MESSAGE START ===")
        logger.info(f"Sending message to: {to_jid}")
        logger.info(f"Message length: {len(message)} characters")
        logger.info(f"Reply to message ID: {in_reply_to}")
        logger.info(f"Message preview: {message[:100]}...")
        
        assert to_jid, "to_jid is required"
        assert message, "message is required"
        to_jid = normalize_jid(to_jid)
        if in_reply_to:
            in_reply_to = normalize_jid(in_reply_to)

        resp = await self.whatsapp.send_message(
            SendMessageRequest(
                phone=to_jid,
                message=message,
                reply_message_id=in_reply_to,
            )
        )
        
        if resp.results is None:
            raise RuntimeError("WhatsApp API returned no results")
        
        logger.info(f"WhatsApp API response message ID: {resp.results.message_id}")
        
        my_number = await self.whatsapp.get_my_jid()
        new_message = BaseMessage(
            message_id=resp.results.message_id,
            text=message,
            sender_jid=str(my_number),
            chat_jid=to_jid,
        )
        
        stored_message = await self.store_message(Message(**new_message.model_dump()))
        if stored_message is None:
            raise RuntimeError("Failed to store message in database")
        
        logger.info(f"Message stored in database with ID: {stored_message.message_id}")
        logger.info("=== SEND MESSAGE END ===")
        
        return stored_message

    async def upsert(self, model):
        return await upsert(self.session, model)

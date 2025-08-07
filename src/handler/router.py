import logging
from datetime import datetime, timedelta, date
from enum import Enum

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from sqlmodel import desc, select
from sqlmodel.ext.asyncio.session import AsyncSession
from voyageai.client_async import AsyncClient

from src.handler.knowledge_base_answers import KnowledgeBaseAnswers
from src.models import Message
from src.whatsapp.jid import parse_jid
from src.utils.chat_text import chat2text
from src.whatsapp.client import WhatsAppClient
from .base_handler import BaseHandler

# Creating an object
logger = logging.getLogger(__name__)


class IntentEnum(str, Enum):
    summarize = "summarize"
    ask_question = "ask_question"
    about = "about"
    tag_all = "tag_all"
    other = "other"


class Intent(BaseModel):
    intent: IntentEnum = Field(
        description="The intent of the user's message"
    )


class Router(BaseHandler):
    def __init__(
        self,
        session: AsyncSession,
        whatsapp: WhatsAppClient,
        embedding_client: AsyncClient,
    ):
        self.ask_knowledge_base = KnowledgeBaseAnswers(
            session, whatsapp, embedding_client
        )
        super().__init__(session, whatsapp, embedding_client)

    async def _route(self, message: str) -> IntentEnum:
        """Route message to appropriate handler based on content"""
        message_lower = message.lower()
        logger.info(f"Routing message: '{message}' (lower: '{message_lower}')")
        
        # Check for summarize intent
        if any(phrase in message_lower for phrase in ["סיכום יומי", "daily summary", "summarize", "סיכום"]):
            logger.info("Routing to summarize")
            return IntentEnum.summarize
            
        # Check for about intent
        if any(phrase in message_lower for phrase in ["about", "אודות", "מי אתה", "what are you", "help", "עזרה"]):
            logger.info("Routing to about")
            return IntentEnum.about
            
        # Check for tag_all intent (@כולם)
        if any(phrase in message_lower for phrase in ["@כולם", "@everyone"]):
            logger.info("Routing to tag_all")
            return IntentEnum.tag_all
            
        # Default to ask_question for everything else
        logger.info("Routing to ask_question (default)")
        return IntentEnum.ask_question

    async def __call__(self, message: Message):
        """Route message to appropriate handler"""
        logger.info(f"Router.__call__ called with message from {message.sender_jid}")
        logger.info(f"Router message text: '{message.text}'")
        logger.info(f"Router message chat JID: {message.chat_jid}")
        
        # Ensure message.text is not None before routing
        if message.text is None:
            logger.warning("Received message with no text, skipping routing")
            return
            
        route = await self._route(message.text)
        logger.info(f"Router determined intent: {route}")
        
        match route:
            case IntentEnum.summarize:
                logger.info("Calling summarize handler")
                await self.summarize(message)
                logger.info("Summarize handler completed")
            case IntentEnum.ask_question:
                logger.info("Calling ask_knowledge_base handler")
                await self.ask_knowledge_base(message)
                logger.info("Knowledge base handler completed")
            case IntentEnum.about:
                logger.info("Calling about handler")
                await self.about(message)
                logger.info("About handler completed")
            case IntentEnum.tag_all:
                logger.info("Calling tag_all_participants handler")
                await self.tag_all_participants(message)
                logger.info("Tag all participants handler completed")
            case IntentEnum.other:
                logger.info("Calling default_response handler")
                await self.default_response(message)
                logger.info("Default response handler completed")

    async def summarize(self, message: Message):
        logger.info("=== SUMMARIZE METHOD START ===")
        today_start = datetime.combine(date.today(), datetime.min.time())
        my_jid = await self.whatsapp.get_my_jid()
        stmt = (
            select(Message)
            .where(Message.chat_jid == message.chat_jid) # From the same group
            .where(Message.timestamp >= today_start) # From today
            .where(Message.sender_jid != my_jid.normalize_str())  # Exclude self messages
            .order_by(desc(Message.timestamp)) # Newest to oldest
            .limit(200)  # Capture more messages for better filtering
        )
        res = await self.session.exec(stmt)
        messages: list[Message] = list(res.all())

        if len(messages) > 50:
            await self.send_message(
                message.chat_jid,
                f"מעבד {len(messages)} הודעות... זה יכול לקחת דקה.",
                message.message_id,
            )

        agent = Agent(
            model="anthropic:claude-4-sonnet-20250514",
            system_prompt=f"""Create a comprehensive summary of TODAY's important discussions from the group chat.

            CURRENT TIME: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

            INCLUDE:
            - Important decisions, announcements, action items
            - New information learned or insights gained
            - Key discussions that impact the group
            - Important questions and answers
            - Administrative announcements

            EXCLUDE:
            - Casual small talk, greetings, jokes
            - Repetitive discussions
            - Temporary information

            FORMAT:
            - Start with: "📋 Summary of Today's Important Discussions"
            - Use headers like "🎯 Key Decisions", "📚 New Information", "⚡ Action Items"
            - Tag users with @number when mentioning them
            - Use *bold* for headers, _italic_ for quotes, emojis for organization
            - Keep response under 3000 characters
            - Respond in the same language as the request
            """,
        )

        response = await agent.run(
            f"@{parse_jid(message.sender_jid).user}: {message.text}\n\n # History:\n {chat2text(messages)}"
        )

        # If pydantic_ai provides usage info
        if hasattr(response, 'usage'):
            logger.info(f"Tokens used: {response.usage}")

        response_text = response.output
        logger.info(f"Sending summary response (length: {len(response_text)} characters)")
        
        # Check if response is too long (WhatsApp has a limit of ~4096 characters)
        if len(response_text) > 4000:
            logger.warning(f"Response too long ({len(response_text)} chars), truncating to 4000 chars")
            response_text = response_text[:4000] + "...\n\n[Response truncated due to length]"
        
        await self.send_message(
            message.chat_jid,
            response_text,
            message.message_id,
        )
        logger.info("=== SUMMARIZE METHOD END ===")

    async def about(self, message):
        await self.send_message(
            message.chat_jid,
            "I'm an open-source bot created for the GenAI Israel community - https://llm.org.il.\nI can help you catch up on the chat messages and answer questions based on the group's knowledge.\nPlease send me PRs and star me at https://github.com/ilanbenb/wa_llm ⭐️",
            message.message_id,
        )

    async def default_response(self, message):
        await self.send_message(
            message.chat_jid,
            "מצטער, אבל אני לא חושב שאני יכול לעזור עם זה כרגע 😅.\n אני יכול לעזור לך להתעדכן בהודעות הצ'אט או לענות על שאלות בהתבסס על הידע של הקבוצה.",
            message.message_id,
        )

    async def tag_all_participants(self, message: Message):
        """Tag all participants in the group when @כולם is mentioned"""
        try:
            # Get bot's phone number to exclude it
            my_jid = await self.whatsapp.get_my_jid()
            bot_phone = my_jid.user
            logger.info(f"Bot phone: {bot_phone}")
            
            # Get all groups - single attempt only
            from src.handler import get_user_groups
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
                    from src.handler import extract_phone_from_participant
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

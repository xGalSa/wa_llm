import logging
from datetime import datetime, timedelta, date
from enum import Enum

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from sqlmodel import desc, select
from sqlmodel.ext.asyncio.session import AsyncSession
from voyageai.client_async import AsyncClient

from handler.knowledge_base_answers import KnowledgeBaseAnswers
from models import Message
from whatsapp.jid import parse_jid
from utils.chat_text import chat2text
from whatsapp import WhatsAppClient
from .base_handler import BaseHandler

# Creating an object
logger = logging.getLogger(__name__)


class IntentEnum(str, Enum):
    summarize = "summarize"
    ask_question = "ask_question"
    about = "about"
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
        
        # Check for summarize intent
        if any(phrase in message_lower for phrase in ["סיכום יומי", "daily summary", "summarize", "סיכום"]):
            return IntentEnum.summarize
            
        # Check for about intent
        if any(phrase in message_lower for phrase in ["about", "אודות", "מי אתה", "what are you", "help", "עזרה"]):
            return IntentEnum.about
            
        # Default to ask_question for everything else
        return IntentEnum.ask_question

    async def __call__(self, message: Message):
        """Route message to appropriate handler"""
        # Ensure message.text is not None before routing
        if message.text is None:
            logger.warning("Received message with no text, skipping routing")
            return
            
        route = await self._route(message.text)
        match route:
            case IntentEnum.summarize:
                await self.summarize(message)
            case IntentEnum.ask_question:
                await self.ask_knowledge_base(message)
            case IntentEnum.about:
                await self.about(message)
            case IntentEnum.other:
                await self.default_response(message)

    async def summarize(self, message: Message):
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
        messages: list[Message] = res.all()

        if len(messages) > 50:
            await self.send_message(
                message.chat_jid,
                f"מעבד {len(messages)} הודעות... זה יכול לקחת דקה.",
                message.message_id,
            )

        agent = Agent(
            model="anthropic:claude-4-sonnet-20250514",
            system_prompt=f"""Create a comprehensive, detailed summary of TODAY's important and relevant discussions from the group chat.

            CURRENT TIME CONTEXT: It is now {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (local time).
            Messages from earlier today may be outdated if they've been superseded by newer information.

            CONTEXT: You are summarizing a military/educational group chat into a WhatsApp message. Focus on operational, educational, and organizational content.

            PRIORITY CONTENT - Include these with full details:
            - Important decisions, announcements, or action items
            - New information learned or insights gained
            - Relevant for future reference or follow-up
            - Significant developments or changes
            - Key discussions that impact the group or individuals
            - Important questions asked and their answers
            - Administrative announcements or procedures

            EXCLUDE:
            - Casual small talk, greetings, or social pleasantries, irrelevant jokes or memes
            - Repetitive or redundant discussions.
            - Temporary or time-sensitive information that's no longer relevant

            SUMMARY STRUCTURE:
            - Start with: "📋 Comprehensive Summary of Today's Important Discussions"
            - Use clear section headers like "🎯 Key Decisions", "📚 New Information", "⚡ Action Items"
            - Include specific details, quotes, and key phrases when relevant
            - Tag ALL users when mentioning them (e.g., @972536150150)
            - Mention timing/chronology when it adds context
            - Include any action items, decisions made, or follow-ups needed
            - End with a "📝 Summary" section of key takeaways
    
            FORMATTING: Your output is a WhatsApp message! *bold* for headers/emphasis, _italic_ for quotes, emojis for organization, bullet points for lists.

            QUALITY REQUIREMENTS:
            - Be thorough and comprehensive - include ALL important content. Don't get stuck on one topic
            - Focus on lasting value and future relevance
            - Maintain readability and clear organization
            - Use A LOT of emojis and formatting to improve readability
            - You MUST respond with the same language as the request
            - RESPONSE LENGTH: Keep the summary comprehensive but concise. Aim for 1200 characters for most summaries, but make sure words don't get cut in the middle of the output prompt.
            - GENERAL HIGHLIGHTS: Only include if there's space and only in a summarized, non-specific way
            """,
            output_type=str,
            max_tokens=30000,
        )

        response = await agent.run(
            f"@{parse_jid(message.sender_jid).user}: {message.text}\n\n # History:\n {chat2text(messages)}"
        )

        # If pydantic_ai provides usage info
        if hasattr(response, 'usage'):
            logger.info(f"Tokens used: {response.usage}")
            print(f"Tokens used: {response.usage}")

        await self.send_message(
            message.chat_jid,
            response.output,
            message.message_id,
        )

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

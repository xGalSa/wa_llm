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
        description="""The intent of the message.
- summarize: Summarize TODAY's chat messages, or catch up on the chat messages FROM TODAY ONLY. This will trigger the summarization of the chat messages. This is only relevant for queries about TODDAY chat. A query across a broader timespan is classified as ask_question
- ask_question: Ask a question or learn from the collective knowledge of the group. This will trigger the knowledge base to answer the question.
- about: Learn about me(bot) and my capabilities. This will trigger the about section.
- other:  something else. This will trigger the default response."""
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

    async def __call__(self, message: Message):
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

    async def _route(self, message: str) -> IntentEnum:
        agent = Agent(
            model="anthropic:claude-4-sonnet-20250514",
            system_prompt="What is the intent of the message? What does the user want us to help with?",
            output_type=Intent,
        )

        result = await agent.run(message)
        return result.data.intent

    async def summarize(self, message: Message):
        today_start = datetime.combine(date.today(), datetime.min.time())
        my_jid = await self.whatsapp.get_my_jid()
        stmt = (
            select(Message)
            .where(Message.chat_jid == message.chat_jid) # From the same group
            .where(Message.timestamp >= today_start) # From today
            .where(Message.sender_jid != my_jid.normalize_str())  # Exclude self messages
            .order_by(desc(Message.timestamp)) # Newest to oldest
            .limit(300)  # Capture more messages for better filtering
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
            system_prompt=f"""Create a comprehensive summary of TODAY's important discussions from this military/educational WhatsApp group chat. Output is a WhatsApp message.

            Current time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} .
            Messages from earlier today may be outdated if superseded by newer information.

            INCLUDE: Important decisions, announcements, action items, new information, key discussions, questions/answers, achievements, operational updates, educational content. Be detailed and informative while staying focused on relevance. Include any action items, decisions made, or follow-ups needed.

            EXCLUDE: Small talk, jokes, personal conversations, repetitive discussions. temporary or time-sensitive information that's no longer relevant.

            FORMATTING: Use WhatsApp formatting: *bold* for headers/emphasis, _italic_ for quotes, emojis for organization, bullet points for lists.

            STRUCTURE:
            *Today's Summary*
            *Key Decisions*
            📚 *New Information* 
            ⚡ *Action Items and follow-ups*
            📝 *Summary*

            Tag users with @number. Respond in the same language as the request. Aim for 1200 words.
            """,
            output_type=str,
            max_tokens=25000,
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
            response.data,
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

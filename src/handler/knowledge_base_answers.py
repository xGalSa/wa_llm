import logging
from typing import List

from pydantic_ai import Agent
from pydantic_ai.agent import AgentRunResult
from sqlmodel import select, cast, String, desc


from src.models import Message, KBTopic
from src.whatsapp.jid import parse_jid
from src.utils.chat_text import chat2text
from src.utils.voyage_embed_text import voyage_embed_text
from .base_handler import BaseHandler

# Creating an object
logger = logging.getLogger(__name__)


class KnowledgeBaseAnswers(BaseHandler):
    async def __call__(self, message: Message):
        logger.info("=== KNOWLEDGE BASE ANSWERS START ===")
        logger.info(f"Knowledge base processing message from: {message.sender_jid}")
        logger.info(f"Knowledge base message text: '{message.text}'")
        logger.info(f"Knowledge base chat JID: {message.chat_jid}")
        
        # Ensure message.text is not None before passing to generation_agent
        if message.text is None:
            logger.warning("Received message with no text, skipping knowledge base processing")
            return
        # get the last 7 messages
        stmt = (
            select(Message)
            .where(Message.chat_jid == message.chat_jid)
            .order_by(desc(Message.timestamp))
            .limit(400)
        )
        res = await self.session.exec(stmt)
        history: list[Message] = list(res.all())

        rephrased_response = await self.rephrasing_agent(
            (await self.whatsapp.get_my_jid()).user, message, history
        )
        # Get query embedding
        embedded_question = (
            await voyage_embed_text(self.embedding_client, [rephrased_response.output])
        )[0]

        select_from = None
        if message.group:
            select_from = [message.group]
            if message.group.community_keys:
                select_from.extend(
                    await message.group.get_related_community_groups(self.session)
                )

        # Consider adding cosine distance threshold
        # cosine_distance_threshold = 0.8
        limit_topics = 10
        # query for user query
        q = (
            select(
                KBTopic,
                KBTopic.embedding.cosine_distance(embedded_question).label(
                    "cosine_distance"
                ),
            )
            .order_by(KBTopic.embedding.cosine_distance(embedded_question))
            # .where(KBTopic.embedding.cosine_distance(embedded_question) < cosine_distance_threshold)
            .limit(limit_topics)
        )
        if select_from:
            q = q.where(
                cast(KBTopic.group_jid, String).in_(
                    [group.group_jid for group in select_from]
                )
            )
        retrieved_topics = await self.session.exec(q)

        similar_topics = []
        similar_topics_distances = []
        for kb_topic, topic_distance in retrieved_topics:  # Unpack the tuple
            similar_topics.append(f"{kb_topic.subject} \n {kb_topic.summary}")
            similar_topics_distances.append(f"topic_distance: {topic_distance}")

        sender_number = parse_jid(message.sender_jid).user
        generation_response = await self.generation_agent(
            message.text, similar_topics, message.sender_jid, history
        )
        # Remove privacy-sensitive logging
        # logger.info(
        #     "RAG Query Results:\n"
        #     f"Sender: {sender_number}\n"
        #     f"Question: {message.text}\n"
        #     f"Rephrased Question: {rephrased_response.output}\n"
        #     f"Chat JID: {message.chat_jid}\n"
        #     f"Retrieved Topics: {len(similar_topics)}\n"
        #     f"Similarity Scores: {similar_topics_distances}\n"
        #     "Topics:\n"
        #     + "\n".join(f"- {topic[:100]}..." for topic in similar_topics)
        #     + "\n"
        #     f"Generated Response: {generation_response.output}"
        # )

        logger.info(f"About to send knowledge base response to {message.chat_jid}")
        logger.info(f"Response length: {len(generation_response.output)} characters")
        logger.info(f"Response preview: {generation_response.output[:100]}...")
        
        await self.send_message(
            message.chat_jid,
            generation_response.output,
            message.message_id,
        )
        
        logger.info("Knowledge base response sent successfully")
        logger.info("=== KNOWLEDGE BASE ANSWERS END ===")

    async def generation_agent(
        self, query: str, topics: list[str], sender: str, history: List[Message]
    ) -> AgentRunResult[str]:
        agent = Agent(
            model="anthropic:claude-4-sonnet-20250514",
            system_prompt="""Answer the user's question based on the attached knowledge base topics.

            FORMATTING: Use WhatsApp formatting - *bold* for emphasis, _italic_ for quotes, emojis for organization.

            GUIDELINES:
            - Answer in the same language as the question
            - Be conversational and concise (this is a WhatsApp chat)
            - Only use information from the attached topics
            - Tag users with @number when mentioning them
            - If no relevant topics found, say "לא מצאתי מידע רלוונטי על זה" (Hebrew) or "I couldn't find relevant information about this" (English)

            CONTEXT: Recent chat history is provided for context. Use it if relevant, ignore if not.""",
            max_tokens=25000,
        )

        prompt_template = f"""
        {f"@{sender}"}: {query}
        
        # Recent chat history:
        {chat2text(history)}
        
        # Related Topics:
        {"\n---\n".join(topics) if len(topics) > 0 else "No related topics found."}
        """

        return await agent.run(prompt_template)

    async def rephrasing_agent(
        self, my_jid: str, message: Message, history: List[Message]
    ) -> AgentRunResult[str]:
        rephrased_agent = Agent(
            model="anthropic:claude-4-sonnet-20250514",
            system_prompt=f"""Phrase the following message as a short paragraph describing a query from the knowledge base.
            - Use English only!
            - Ensure only to include the query itself. The message that includes a lot of information - focus on what the user asks you.
            - Your name is @{my_jid}
            - Attached is the recent chat history. You can use it to understand the context of the query. If the context is not clear or irrelevant to the query, ignore it.
            - ONLY answer with the new phrased query, no other text!""",
            max_tokens=25000,
        )

        # We obviously need to translate the question and turn the question vebality to a title / summary text to make it closer to the questions in the rag
        return await rephrased_agent.run(
            f"{message.text}\n\n## Recent chat history:\n {chat2text(history)}"
        )

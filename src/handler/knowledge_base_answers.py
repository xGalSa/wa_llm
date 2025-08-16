import logging
import time
from typing import List

from pydantic_ai import Agent
from pydantic_ai.agent import AgentRunResult
from sqlmodel import select, desc

from src.models import Message
from src.utils.chat_text import chat2text
from .base_handler import BaseHandler

# Creating an object
logger = logging.getLogger(__name__)


class KnowledgeBaseAnswers(BaseHandler):
    # Privacy configuration: matches MessageHandler's privacy limit
    MAX_CONTEXT_MESSAGES = 400  # Use last 400 messages as context (privacy limit)
    MAX_QUERY_LENGTH = 500  # Prevent extremely long queries
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Log current configuration for debugging
        logger.info(f"KnowledgeBaseAnswers initialized with full-context approach, max_context={self.MAX_CONTEXT_MESSAGES}")
        logger.debug(f"Logger level: {logger.getEffectiveLevel()}, Debug enabled: {logger.isEnabledFor(logging.DEBUG)}")
    
    async def get_recent_messages(self, group_jid: str, limit: int | None = None) -> List[Message]:
        """Get recent messages from a group for context."""
        if limit is None:
            limit = self.MAX_CONTEXT_MESSAGES
            
        try:
            # Get recent messages from this group only (security boundary)
            my_jid = await self.whatsapp.get_my_jid()
            stmt = (
                select(Message)
                .where(Message.group_jid == group_jid)
                .where(Message.sender_jid != my_jid.normalize_str())  # Exclude bot messages
                .where(Message.text != None)  # Only messages with text
                .order_by(desc(Message.timestamp))
                .limit(limit)
            )
            result = await self.session.exec(stmt)
            messages = list(result.all())
            
            # Reverse to get chronological order (oldest first)
            messages.reverse()
            
            logger.info(f"Retrieved {len(messages)} recent messages from group {group_jid} for context")
            return messages
            
        except Exception as e:
            logger.error(f"Error retrieving recent messages for group {group_jid}: {e}")
            return []



    async def full_context_agent(self, conversation_context: str, question: str) -> AgentRunResult[str]:
        """
        AI agent that answers questions using full conversation context.
        This is much more cost-efficient than pre-processing topics.
        """
        try:
            logger.info(f"Creating AI agent with model: anthropic:claude-4-sonnet-20250514")
            agent = Agent(
                model="anthropic:claude-4-sonnet-20250514",
                system_prompt="""You are a helpful assistant that answers questions based on WhatsApp group conversation history.

You will be provided with:
1. The recent conversation history from the group
2. A specific question from a user

Your task:
- Analyze the conversation history to find relevant information
- Answer the question based ONLY on information from the conversation
- If the information isn't in the conversation, say so clearly
- Provide specific details and context when available
- Credit insights to specific participants when relevant (use @username format)
- Respond in the same language as the question (Hebrew/English/etc.)

Guidelines:
- Be concise but thorough
- Use direct quotes when helpful
- If multiple perspectives exist, mention them
- Focus on recent/relevant discussions first
- Maintain conversational tone appropriate for WhatsApp
""",
                retries=3,
            )
            logger.info(f"AI agent created successfully")

            prompt = f"""## Recent Group Conversation History:
```
{conversation_context}
```

## User Question:
{question}

## Instructions:
Please analyze the conversation history above and answer the user's question. Base your response ONLY on information found in the conversation. If the answer isn't in the conversation history, clearly state that."""

            logger.info(f"About to run agent with prompt length: {len(prompt)}")
            result = await agent.run(prompt)
            logger.info(f"Agent completed successfully, response length: {len(result.output) if result.output else 0}")
            return result
            
        except Exception as e:
            logger.error(f"Error in full_context_agent: {type(e).__name__}: {e}", exc_info=True)
            raise







    async def __call__(self, message: Message):
        start_time = time.time()
        logger.info("=== FULL-CONTEXT KNOWLEDGE BASE START ===")
        logger.info(f"Processing question from: {message.sender_jid}")
        logger.info(f"Question: '{message.text}'")
        logger.info(f"Group: {message.group_jid}")
        
        # Ensure message.text is not None and not too long
        if message.text is None:
            logger.warning("Received message with no text, skipping knowledge base processing")
            return
            
        if len(message.text) > self.MAX_QUERY_LENGTH:
            logger.warning(f"Message too long ({len(message.text)} chars), truncating")
            message.text = message.text[:self.MAX_QUERY_LENGTH]
            
        # Security: Only process group messages (no private chats)
        if not message.group_jid:
            logger.warning("Private message received - knowledge base only available in groups")
            await self.send_message(
                message.chat_jid,
                "מאגר הידע זמין רק בקבוצות 📚" if any(ord(c) > 127 for c in message.text)
                else "Knowledge base is only available in groups 📚",
                message.message_id,
            )
            return
        
        logger.info(f"Processing group message - group_jid: {message.group_jid}")
            
        # Get recent message history for context (security: only from this group)
        logger.info(f"About to fetch recent messages for group: {message.group_jid}")
        recent_messages = await self.get_recent_messages(message.group_jid, limit=self.MAX_CONTEXT_MESSAGES)
        logger.info(f"Retrieved {len(recent_messages)} recent messages successfully")
        
        if len(recent_messages) < 5:
            logger.warning(f"Not enough message history ({len(recent_messages)} messages) for meaningful context")
            await self.send_message(
                message.chat_jid,
                "אין מספיק היסטוריית הודעות עדיין כדי לענות על שאלות 📚" if any(ord(c) > 127 for c in message.text)
                else "Not enough message history yet to answer questions 📚",
                message.message_id,
            )
            return
        
        # Convert message history to conversational text
        conversation_context = chat2text(recent_messages)
        logger.info(f"Using {len(recent_messages)} messages as context ({len(conversation_context)} chars)")
        
        # Validate context length (prevent extremely large contexts)
        MAX_CONTEXT_CHARS = 100000  # ~25k tokens limit
        if len(conversation_context) > MAX_CONTEXT_CHARS:
            logger.warning(f"Context too long ({len(conversation_context)} chars), truncating to {MAX_CONTEXT_CHARS}")
            conversation_context = conversation_context[-MAX_CONTEXT_CHARS:]  # Keep most recent part
        
        # Process question with full conversation context (only pay when asked!)
        try:
            logger.info(f"Sending to AI: question='{message.text}' context_chars={len(conversation_context)}")
            logger.info(f"About to call full_context_agent with context preview: {conversation_context[:200]}...")
            response = await self.full_context_agent(conversation_context, message.text)
            logger.info(f"AI agent returned successfully")
            
            if not response.output or not response.output.strip():
                logger.warning("AI returned empty response")
                await self.send_message(
                    message.chat_jid,
                    "מצטער, לא הצלחתי למצוא מידע רלוונטי בהיסטוריית הקבוצה" if any(ord(c) > 127 for c in message.text)
                    else "Sorry, I couldn't find relevant information in the group history",
                    message.message_id,
                )
                return
            
            logger.info(f"Generated response ({len(response.output)} chars): {response.output[:100]}...")
            
            # Check WhatsApp message length limit
            if len(response.output) > 4000:
                logger.warning(f"Response too long ({len(response.output)} chars), truncating")
                response_text = response.output[:4000] + "\n\n[...תשובה קוצרה בגלל אורך]"
            else:
                response_text = response.output
            
            await self.send_message(
                message.chat_jid,
                response_text,
                message.message_id,
            )
            
            # Log successful completion
            total_time = time.time() - start_time
            logger.info(f"Full-context response sent successfully in {total_time:.2f} seconds")
            logger.info("=== FULL-CONTEXT KNOWLEDGE BASE END ===")
            
        except Exception as e:
            logger.error(f"Failed to generate full-context response: {e}", exc_info=True)
            
            # Check for specific API key error
            error_msg = str(e)
            if "ANTHROPIC_API_KEY" in error_msg or "api_key" in error_msg.lower():
                logger.error("ANTHROPIC_API_KEY environment variable not set!")
                await self.send_message(
                    message.chat_jid,
                    "מאגר הידע לא מוגדר כרגע - חסר מפתח API 🔑" if any(ord(c) > 127 for c in message.text)
                    else "Knowledge base not configured - missing API key 🔑",
                    message.message_id,
                )
            else:
                await self.send_message(
                    message.chat_jid,
                    "מצטער, יש לי בעיה טכנית בעיבוד השאלה" if any(ord(c) > 127 for c in message.text)
                    else "Sorry, I'm having a technical issue processing your question",
                    message.message_id,
                )

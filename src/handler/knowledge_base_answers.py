import logging
import time
from typing import List, Tuple

from pydantic_ai import Agent
from pydantic_ai.agent import AgentRunResult
from sqlmodel import select, cast, String, desc, or_, func


from src.models import Message, KBTopic, Group
from src.whatsapp.jid import parse_jid
from src.utils.chat_text import chat2text
from src.utils.voyage_embed_text import voyage_embed_text
from .base_handler import BaseHandler

# Creating an object
logger = logging.getLogger(__name__)


class KnowledgeBaseAnswers(BaseHandler):
    # Similarity threshold for relevant topics
    COSINE_DISTANCE_THRESHOLD = 0.4
    MIN_RELEVANT_TOPICS = 2
    MAX_QUERY_LENGTH = 500  # Prevent extremely long queries
    MAX_TOPICS_TO_RETRIEVE = 15  # Balance between quality and performance
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Log current configuration for debugging
        logger.info(f"KnowledgeBaseAnswers initialized with threshold={self.COSINE_DISTANCE_THRESHOLD}, max_topics={self.MAX_TOPICS_TO_RETRIEVE}")
        logger.debug(f"Logger level: {logger.getEffectiveLevel()}, Debug enabled: {logger.isEnabledFor(logging.DEBUG)}")
    
    async def validate_rephrased_query(self, original: str | None, rephrased: str | None) -> bool:
        """Validate that the rephrased query maintains the intent of the original."""
        # Basic validations
        if not original or not rephrased or len(rephrased.strip()) < 3:
            return False
        
        # Reject if rephrased is just generic responses
        generic_responses = {
            "i don't understand", "unclear", "no query", "invalid", 
            "not clear", "cannot determine", "i cannot", "unable to"
        }
        if rephrased.lower().strip() in generic_responses:
            return False
        
        # Check if rephrased query is too different from original (basic heuristic)
        original_words = set(word.lower() for word in original.split() if len(word) > 2)
        rephrased_words = set(word.lower() for word in rephrased.split() if len(word) > 2)
        
        # If there's some overlap or the rephrased is reasonable length, consider it valid
        overlap = len(original_words.intersection(rephrased_words))
        return overlap > 0 or (len(rephrased.split()) >= 3 and len(rephrased) >= 10)

    async def hybrid_search(self, embedded_question: List[float], original_text: str, select_from=None) -> List[Tuple[KBTopic, float]]:
        """Perform hybrid search combining semantic similarity and keyword matching.
        
        IMPORTANT: select_from MUST be provided to limit search to specific groups.
        This prevents cross-group data leakage and ensures privacy.
        """
        if not select_from:
            logger.error("SECURITY: hybrid_search called without group filtering - this should never happen!")
            raise ValueError("Group filtering is required for knowledge base search")
        # Semantic search - SECURITY: Always exclude NULL group_jid topics
        semantic_query = (
            select(
                KBTopic,
                KBTopic.embedding.cosine_distance(embedded_question).label("cosine_distance"),
            )
            .where(KBTopic.embedding.cosine_distance(embedded_question) < self.COSINE_DISTANCE_THRESHOLD)
            .where(KBTopic.group_jid != None)  # SECURITY: Never return orphaned topics
            .order_by(KBTopic.embedding.cosine_distance(embedded_question))
            .limit(self.MAX_TOPICS_TO_RETRIEVE)  # Get more candidates for filtering
        )
        
        # Add keyword search for better recall
        # Filter out common stop words and short words
        stop_words = {'the', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by', 'what', 'how', 'when', 'where', 'why', 'who'}
        keywords = [
            word.strip().lower() 
            for word in original_text.split() 
            if len(word.strip()) > 2 and word.lower() not in stop_words
        ]
        logger.debug(f"Extracted keywords for search: {keywords}")
        if keywords:
            keyword_conditions = []
            for keyword in keywords[:5]:  # Limit to avoid too complex queries
                # Escape special SQL characters
                safe_keyword = keyword.replace('%', r'\%').replace('_', r'\_')
                keyword_conditions.extend([
                    func.lower(KBTopic.subject).like(f"%{safe_keyword}%"),
                    func.lower(KBTopic.summary).like(f"%{safe_keyword}%")
                ])
            
            keyword_query = (
                select(
                    KBTopic,
                    func.literal(0.3).label("cosine_distance")  # Give keyword matches good score
                )
                .where(or_(*keyword_conditions))
                .where(KBTopic.group_jid != None)  # SECURITY: Never return orphaned topics
                .limit(5)
            )
        else:
            keyword_query = None
        
        # Apply required group filtering 
        group_jids = [group.group_jid for group in select_from]
        logger.debug(f"Filtering search to groups: {group_jids}")
        semantic_query = semantic_query.where(
            cast(KBTopic.group_jid, String).in_(group_jids)
        )
        if keyword_query is not None:
            keyword_query = keyword_query.where(
                cast(KBTopic.group_jid, String).in_(group_jids)
            )
        
        # Execute queries
        logger.debug(f"Executing semantic search with threshold < {self.COSINE_DISTANCE_THRESHOLD}")
        semantic_results = await self.session.exec(semantic_query)
        semantic_topics = list(semantic_results)
        logger.debug(f"Semantic search returned {len(semantic_topics)} topics")
        
        keyword_topics = []
        if keyword_query is not None:
            logger.debug("Executing keyword search")
            keyword_results = await self.session.exec(keyword_query)
            keyword_topics = list(keyword_results)
            logger.debug(f"Keyword search returned {len(keyword_topics)} topics")
        else:
            logger.debug("No keyword search (no valid keywords found)")
        
        # Combine and deduplicate results
        all_topics = {}
        for topic, distance in semantic_topics:
            all_topics[topic.id] = (topic, distance)
        
        # Add keyword results with slightly lower priority
        keyword_only_count = 0
        for topic, distance in keyword_topics:
            if topic.id not in all_topics:
                all_topics[topic.id] = (topic, distance + 0.1)  # Slight penalty for keyword-only
                keyword_only_count += 1
        
        logger.debug(f"Combined results: {len(semantic_topics)} semantic + {keyword_only_count} keyword-only = {len(all_topics)} total unique topics")
        
        # Sort by distance and return top results
        sorted_topics = sorted(all_topics.values(), key=lambda x: x[1])
        final_topics = sorted_topics[:10]
        
        if final_topics:
            distances = [distance for _, distance in final_topics]
            logger.debug(f"Final topic distances: min={min(distances):.3f}, max={max(distances):.3f}, avg={sum(distances)/len(distances):.3f}")
        
        return final_topics

    async def check_kb_health(self) -> bool:
        """Quick health check for the knowledge base."""
        try:
            # Check if we have any topics at all (only count valid group-associated topics)
            count_stmt = select(func.count()).select_from(KBTopic).where(KBTopic.group_jid != None)
            result = await self.session.exec(count_stmt)
            topic_count = result.one()
            
            # Also check for orphaned topics (security audit)
            orphaned_stmt = select(func.count()).select_from(KBTopic).where(KBTopic.group_jid == None)
            orphaned_result = await self.session.exec(orphaned_stmt)
            orphaned_count = orphaned_result.one()
            
            if orphaned_count > 0:
                logger.warning(f"SECURITY WARNING: Found {orphaned_count} orphaned topics with NULL group_jid")
            
            # Check if we have any groups set up for topic loading
            groups_stmt = select(func.count()).select_from(Group).where(Group.managed == True)
            groups_result = await self.session.exec(groups_stmt)
            managed_groups_count = groups_result.one()
            
            if topic_count == 0:
                if managed_groups_count == 0:
                    logger.warning("Knowledge base is empty - no managed groups configured for topic loading")
                else:
                    logger.warning(f"Knowledge base is empty - {managed_groups_count} managed groups found but no topics loaded yet")
                return False
                
            logger.info(f"Knowledge base health check: {topic_count} valid topics available from {managed_groups_count} managed groups")
            return True
        except Exception as e:
            logger.error(f"Knowledge base health check failed: {e}")
            return False

    async def filter_quality_topics(self, topics_with_distances: List[Tuple[KBTopic, float]]) -> Tuple[List[str], float]:
        """Filter topics and calculate confidence score."""
        quality_topics = []
        total_confidence = 0.0
        filtered_count = 0
        
        logger.debug(f"Filtering {len(topics_with_distances)} topics for quality")
        
        for topic, distance in topics_with_distances:
            # Skip topics that are too dissimilar
            if distance >= self.COSINE_DISTANCE_THRESHOLD:
                filtered_count += 1
                logger.debug(f"Filtered topic '{topic.subject[:50]}...' - distance {distance:.3f} >= threshold {self.COSINE_DISTANCE_THRESHOLD}")
                continue
                
            # Basic quality checks
            if not topic.subject or not topic.summary:
                filtered_count += 1
                logger.debug(f"Filtered topic - missing subject or summary")
                continue
                
            if len(topic.subject.strip()) < 3 or len(topic.summary.strip()) < 10:
                filtered_count += 1
                logger.debug(f"Filtered topic '{topic.subject}' - too short (subject: {len(topic.subject)}, summary: {len(topic.summary)})")
                continue
            
            quality_topics.append(f"{topic.subject}\n{topic.summary}")
            
            # Calculate confidence based on similarity (lower distance = higher confidence)
            topic_confidence = max(0, 1 - (distance / self.COSINE_DISTANCE_THRESHOLD))
            total_confidence += topic_confidence
            logger.debug(f"Accepted topic '{topic.subject[:50]}...' - distance: {distance:.3f}, confidence: {topic_confidence:.3f}")
        
        # Calculate average confidence
        avg_confidence = total_confidence / len(quality_topics) if quality_topics else 0.0
        
        logger.debug(f"Quality filtering results: {len(quality_topics)} passed, {filtered_count} filtered out, avg confidence: {avg_confidence:.3f}")
        
        return quality_topics, avg_confidence

    async def __call__(self, message: Message):
        start_time = time.time()
        logger.info("=== KNOWLEDGE BASE ANSWERS START ===")
        logger.info(f"Knowledge base processing message from: {message.sender_jid}")
        logger.info(f"Knowledge base message text: '{message.text}'")
        logger.info(f"Knowledge base chat JID: {message.chat_jid}")
        
        # Ensure message.text is not None and not too long
        if message.text is None:
            logger.warning("Received message with no text, skipping knowledge base processing")
            return
            
        if len(message.text) > self.MAX_QUERY_LENGTH:
            logger.warning(f"Message too long ({len(message.text)} chars), truncating")
            message.text = message.text[:self.MAX_QUERY_LENGTH]
            
        # Quick health check
        if not await self.check_kb_health():
            # Get more specific error message based on the actual issue
            try:
                # Check if we have any topics
                count_stmt = select(func.count()).select_from(KBTopic).where(KBTopic.group_jid != None)
                result = await self.session.exec(count_stmt)
                topic_count = result.one()
                
                # Check if we have managed groups
                groups_stmt = select(func.count()).select_from(Group).where(Group.managed == True)
                groups_result = await self.session.exec(groups_stmt)
                managed_groups_count = groups_result.one()
                
                if managed_groups_count == 0:
                    error_message = (
                        "מאגר הידע לא מוגדר עדיין. יש לקבוע קבוצות מנוהלות תחילה 🔧" 
                        if any(ord(c) > 127 for c in message.text)
                        else "Knowledge base not configured yet. Managed groups need to be set up first 🔧"
                    )
                elif topic_count == 0:
                    error_message = (
                        "מאגר הידע ריק. יש להפעיל את תהליך טעינת הנושאים 📚" 
                        if any(ord(c) > 127 for c in message.text)
                        else "Knowledge base is empty. Topic loading process needs to be run 📚"
                    )
                else:
                    error_message = (
                        "מאגר הידע לא זמין כרגע 😔" 
                        if any(ord(c) > 127 for c in message.text)
                        else "Knowledge base is not available right now 😔"
                    )
            except Exception:
                error_message = (
                    "מאגר הידע לא זמין כרגע 😔" 
                    if any(ord(c) > 127 for c in message.text)
                    else "Knowledge base is not available right now 😔"
                )
            
            await self.send_message(
                message.chat_jid,
                error_message,
                message.message_id,
            )
            return
            
        # get the last 200 messages
        my_jid = await self.whatsapp.get_my_jid()
        stmt = (
            select(Message)
            .where(Message.chat_jid == message.chat_jid)
            .where(Message.sender_jid != my_jid.normalize_str())  # Exclude self messages
            .order_by(desc(Message.timestamp))
            .limit(200)
        )
        res = await self.session.exec(stmt)
        history: list[Message] = list(res.all())

        # Step 1: Rephrase and validate the query
        try:
            rephrased_response = await self.rephrasing_agent(
                (await self.whatsapp.get_my_jid()).user, message, history
            )
        except Exception as e:
            logger.warning(f"Failed to rephrase query, using original: {e}")
            # Fallback to using original query
            class MockResponse:
                def __init__(self, text):
                    self.output = text
            rephrased_response = MockResponse(message.text or "")
        
        # Step 2: Validate rephrased query
        if not await self.validate_rephrased_query(message.text, rephrased_response.output):
            logger.warning(f"Rephrased query validation failed. Original: '{message.text}', Rephrased: '{rephrased_response.output}'")
            # Fallback to original query
            query_for_embedding = message.text
        else:
            query_for_embedding = rephrased_response.output
            logger.info(f"Using rephrased query: '{query_for_embedding}'")
        
        # Step 3: Get query embedding
        try:
            embedded_question = (
                await voyage_embed_text(self.embedding_client, [query_for_embedding])
            )[0]
        except Exception as e:
            logger.error(f"Failed to generate embedding: {e}")
            await self.send_message(
                message.chat_jid,
                "מצטער, יש לי בעיה טכנית עם חיפוש במאגר הידע" if any(ord(c) > 127 for c in message.text)
                else "Sorry, I'm having a technical issue with the knowledge base search",
                message.message_id,
            )
            return

        # Step 4: Determine search scope - CRITICAL: Only search within message's group context
        logger.info(f"Message group context: group_jid={message.group_jid}, chat_jid={message.chat_jid}")
        select_from = None
        if message.group_jid:
            # Load the group if not already loaded
            if not message.group:
                message.group = await self.session.get(Group, message.group_jid)
            
            if message.group:
                select_from = [message.group]
                logger.debug(f"Searching within group: {message.group.group_jid}")
                
                # Include related community groups if they exist
                if message.group.community_keys:
                    related_groups = await message.group.get_related_community_groups(self.session)
                    select_from.extend(related_groups)
                    related_jids = [g.group_jid for g in related_groups]
                    logger.debug(f"Including {len(related_groups)} related community groups: {related_jids}")
                
                # Log final search scope for security audit
                all_group_jids = [g.group_jid for g in select_from]
                logger.info(f"Knowledge base search scope: {len(all_group_jids)} groups: {all_group_jids}")
            else:
                logger.warning(f"Could not load group {message.group_jid} for message")
                await self.send_message(
                    message.chat_jid,
                    "לא יכול לטעון את הקבוצה עבור החיפוש במאגר הידע" if any(ord(c) > 127 for c in message.text)
                    else "Could not load group for knowledge base search",
                    message.message_id,
                )
                return
        else:
            # Private message - don't search knowledge base as topics are group-specific
            logger.warning("Private message received - knowledge base search not available for private messages")
            await self.send_message(
                message.chat_jid,
                "מאגר הידע זמין רק בקבוצות 📚" if any(ord(c) > 127 for c in message.text)
                else "Knowledge base is only available in groups 📚",
                message.message_id,
            )
            return

        # Step 5: Perform hybrid search with similarity threshold
        try:
            topics_with_distances = await self.hybrid_search(
                embedded_question, message.text, select_from
            )
        except Exception as e:
            logger.error(f"Failed to search knowledge base: {e}")
            await self.send_message(
                message.chat_jid,
                "מצטער, יש לי בעיה בחיפוש במאגר הידע" if any(ord(c) > 127 for c in message.text)
                else "Sorry, I'm having trouble searching the knowledge base",
                message.message_id,
            )
            return
        
        # Step 6: Filter quality topics and calculate confidence
        similar_topics, confidence_score = await self.filter_quality_topics(topics_with_distances)
        
        # Step 7: Log search results for debugging
        logger.info(f"Retrieved {len(topics_with_distances)} total topics, {len(similar_topics)} quality topics")
        logger.info(f"Confidence score: {confidence_score:.3f}")
        
        if len(similar_topics) == 0:
            logger.warning("No relevant topics found above similarity threshold")
            await self.send_message(
                message.chat_jid,
                "לא מצאתי מידע רלוונטי על זה במאגר הידע שלי 🤔" if any(ord(c) > 127 for c in message.text) 
                else "I couldn't find relevant information about this in my knowledge base 🤔",
                message.message_id,
            )
            return
        
        # Step 8: Only proceed if we have sufficient confidence
        if confidence_score < 0.3:
            logger.warning(f"Low confidence score ({confidence_score:.3f}), sending cautious response")
            cautious_response = (
                "מצאתי מידע שעשוי להיות רלוונטי, אבל אני לא בטוח. האם תוכל לנסח את השאלה מחדש?" 
                if any(ord(c) > 127 for c in message.text)
                else "I found some potentially relevant information, but I'm not confident. Could you rephrase your question?"
            )
            await self.send_message(
                message.chat_jid,
                cautious_response,
                message.message_id,
            )
            return

        # Step 9: Generate response with improved context
        sender_number = parse_jid(message.sender_jid).user
        try:
            generation_response = await self.generation_agent(
                message.text, similar_topics, sender_number, history, confidence_score
            )
        except Exception as e:
            logger.error(f"Failed to generate response: {e}")
            await self.send_message(
                message.chat_jid,
                "מצטער, יש לי בעיה ביצירת התשובה" if any(ord(c) > 127 for c in message.text)
                else "Sorry, I had trouble generating a response",
                message.message_id,
            )
            return
        
        # Step 10: Enhanced logging for debugging and monitoring
        logger.info(f"RAG Query Summary:")
        logger.info(f"- Original Question: {message.text[:100]}...")
        logger.info(f"- Rephrased Query: {query_for_embedding[:100]}...")
        logger.info(f"- Topics Found: {len(similar_topics)}")
        logger.info(f"- Confidence Score: {confidence_score:.3f}")
        logger.info(f"- Response Length: {len(generation_response.output)} characters")
        logger.info(f"- Response Preview: {generation_response.output[:100]}...")
        
        await self.send_message(
            message.chat_jid,
            generation_response.output,
            message.message_id,
        )
        
        # Calculate and log total processing time
        total_time = time.time() - start_time
        logger.info(f"Knowledge base response sent successfully")
        logger.info(f"Total processing time: {total_time:.2f} seconds")
        logger.info("=== KNOWLEDGE BASE ANSWERS END ===")

    async def generation_agent(
        self, query: str, topics: list[str], sender: str, history: List[Message], confidence_score: float = 1.0
    ) -> AgentRunResult[str]:
        logger.debug(f"Starting generation agent with {len(topics)} topics, confidence: {confidence_score:.3f}")
        
        # Adjust system prompt based on confidence
        confidence_guidance = ""
        if confidence_score < 0.7:
            confidence_guidance = "\n- Since the topic matching confidence is moderate, be appropriately cautious in your response"
            logger.debug("Using moderate confidence guidance in system prompt")
        elif confidence_score < 0.5:
            confidence_guidance = "\n- Since the topic matching confidence is low, clearly indicate uncertainty and suggest the user provide more specific details"
            logger.debug("Using low confidence guidance in system prompt")
            
        agent = Agent(
            model="anthropic:claude-4-sonnet-20250514",
            model_settings={"max_tokens": 25000},
            system_prompt=f"""Answer the user's question based on the attached knowledge base topics.

            FORMATTING: Use WhatsApp formatting - *bold* for emphasis, _italic_ for quotes, emojis for organization.

            GUIDELINES:
            - Answer in the same language as the question
            - Be conversational and concise (this is a WhatsApp chat)
            - Only use information from the attached topics
            - Tag users with @number when mentioning them
            - If no relevant topics found, say "לא מצאתי מידע רלוונטי על זה" (Hebrew) or "I couldn't find relevant information about this" (English){confidence_guidance}

            CONTEXT: Recent chat history is provided for context. Use it if relevant, ignore if not.""",
        )

        prompt_template = f"""
        {f"@{sender}"}: {query}
        
        # Recent chat history:
        {chat2text(history)}
        
        # Related Topics (Confidence: {confidence_score:.2f}):
        {"\n---\n".join(topics) if len(topics) > 0 else "No related topics found."}
        """
        
        logger.debug(f"Generation prompt length: {len(prompt_template)} chars, history messages: {len(history)}")
        
        try:
            result = await agent.run(prompt_template)
            logger.debug(f"Generation completed successfully, response length: {len(result.output)} chars")
            return result
        except Exception as e:
            logger.error(f"Generation agent failed: {e}")
            raise

    async def rephrasing_agent(
        self, my_jid: str, message: Message, history: List[Message]
    ) -> AgentRunResult[str]:
        logger.debug(f"Starting rephrasing agent for message: '{(message.text or '')[:100]}...'")
        
        rephrased_agent = Agent(
            model="anthropic:claude-4-sonnet-20250514",
            model_settings={"max_tokens": 25000},
            system_prompt=f"""Phrase the following message as a short paragraph describing a query from the knowledge base.
            - Use English only!
            - Ensure only to include the query itself. The message that includes a lot of information - focus on what the user asks you.
            - Your name is @{my_jid}
            - Attached is the recent chat history. You can use it to understand the context of the query. If the context is not clear or irrelevant to the query, ignore it.
            - ONLY answer with the new phrased query, no other text!""",
        )

        rephrasing_input = f"{message.text}\n\n## Recent chat history:\n {chat2text(history)}"
        logger.debug(f"Rephrasing input length: {len(rephrasing_input)} chars, history messages: {len(history)}")
        
        try:
            result = await rephrased_agent.run(rephrasing_input)
            logger.debug(f"Rephrasing completed: '{result.output[:100]}...'")
            return result
        except Exception as e:
            logger.error(f"Rephrasing agent failed: {e}")
            raise

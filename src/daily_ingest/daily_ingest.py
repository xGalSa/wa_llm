import hashlib
import logging
from datetime import datetime
from typing import Dict, List

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.result import ResultDataT
from sqlmodel import desc, select
from sqlmodel.ext.asyncio.session import AsyncSession
from tenacity import retry, wait_random_exponential, stop_after_attempt, after_log
from voyageai.client_async import AsyncClient

from models import KBTopicCreate, Group, Message
from models.knowledge_base_topic import KBTopic
from models.upsert import bulk_upsert
from utils.voyage_embed_text import voyage_embed_text
from whatsapp import WhatsAppClient

logger = logging.getLogger(__name__)


class Topic(BaseModel):
    subject: str = Field(description="The subject of the summary")
    summary: str = Field(
        description="A concise summary of the topic discussed. Credit notable insights to the speaker by tagging him (e.g, @user_1)"
    )
    speakers: List[str] = Field(
        description="The speakers participated. e.g. [@user_1, @user_7, ...]"
    )


def _swap_numbers_tags_in_messages_to_user_tags(
    message: str, user_mapping: Dict[str, str]
) -> str:
    for k, v in user_mapping.items():
        message = message.replace(f"@{k}", "v")
    return message


def _remap_user_mapping_to_tagged_users(
    message: str, user_mapping: Dict[str, str]
) -> str:
    for k, v in user_mapping.items():
        message = message.replace(k, f"@{v}")
    return message


@retry(
    wait=wait_random_exponential(min=1, max=30),
    stop=stop_after_attempt(6),
    after=after_log(logger, logging.ERROR),
)
async def conversation_splitter_agent(content: str) -> ResultDataT:
    agent = Agent(
        model="anthropic:claude-3-5-sonnet-latest",
        system_prompt="""This conversation is a chain of messages that was uninterrupted by a break in the conversation of up to 3 hours.
Break the conversation into a list of topics.
""",
        result_type=List[Topic],
        retries=5,
    )

    return await agent.run(content)


async def get_conversation_topics(messages: list[Message]) -> List[Topic]:
    sender_jids = {msg.sender_jid for msg in messages}
    speaker_mapping = {
        sender_jid: f"@user_{i+1}" for i, sender_jid in enumerate(sender_jids)
    }

    # Create the reverse mapping: enumerated number -> original username
    reversed_speaker_mapping = {v: k for k, v in speaker_mapping.items()}

    # Format conversation as "{timestamp}: {participant_enumeration}: {message}"
    # Swap tags in message to user tags E.G. "@972536150150 please comment" to "@user_1 please comment"
    conversation_content = "\n".join(
        [
            f"{message.timestamp}: {speaker_mapping[message.sender_jid]}: {_swap_numbers_tags_in_messages_to_user_tags(message.text, speaker_mapping)}"
            for message in messages
            if message.text is not None
        ]
    )

    result = await conversation_splitter_agent(conversation_content)
    for topic in result.data:
        # If for some reason the speaker is not in the mapping, keep the original speaker
        # This case was needed when the speaker is not in the mapping because the user was not in the chat
        remaped_speakers = [
            reversed_speaker_mapping.get(speaker, speaker) for speaker in topic.speakers
        ]
        topic.speakers = remaped_speakers
        remaped_summary = _remap_user_mapping_to_tagged_users(
            topic.summary, reversed_speaker_mapping
        )
        topic.summary = remaped_summary
    return result.data


async def load_topics(
    db_session: AsyncSession,
    group: Group,
    embedding_client: AsyncClient,
    topics: List[Topic],
    start_time: datetime,
):
    documents = [f"# {topic.subject}\n{topic.summary}" for topic in topics]
    topics_embeddings = await voyage_embed_text(embedding_client, documents)

    doc_models = [
        # TODO: Replace topic.subject with something else that is deterministic.
        # topic.subject is not deterministic because it's the result of the LLM.
        KBTopicCreate(
            id=str(
                hashlib.sha256(
                    f"{group.group_jid}_{start_time}_{topic.subject}".encode()
                ).hexdigest()
            ),
            embedding=emb,
            group_jid=group.group_jid,
            start_time=start_time,
            speakers=",".join(topic.speakers),
            summary=topic.summary,
            subject=topic.subject,
        )
        for topic, emb in zip(topics, topics_embeddings)
    ]
    # Once we give a meaningfull ID, we should migrate to upsert!
    await bulk_upsert(db_session, [KBTopic(**doc.model_dump()) for doc in doc_models])

    # Update the group with the new last_ingest
    group.last_ingest = datetime.now()
    db_session.add(group)
    await db_session.commit()


class topicsLoader:
    async def load_topics(
        self,
        db_session: AsyncSession,
        group: Group,
        embedding_client: AsyncClient,
        whatsapp: WhatsAppClient,
    ):
        try:
            # Since yesterday at 12:00 UTC. Between 24 hours to 48 hours ago
            stmt = (
                select(Message)
                .where(Message.timestamp >= group.last_ingest)
                .where(Message.group_jid == group.group_jid)
                .where(
                    Message.sender_jid != (await whatsapp.get_my_jid()).normalize_str()
                )
                .order_by(desc(Message.timestamp))
            )
            res = await db_session.exec(stmt)
            # Convert Sequence to list explicitly
            messages = list(res.all())

            if len(messages) == 0:
                logger.info(f"No messages found for group {group.group_name}")
                return

            # The result is ordered by timestamp, so the first message is the oldest
            start_time = messages[0].timestamp
            daily_topics = await get_conversation_topics(messages)
            logger.info(
                f"Loaded {len(daily_topics)} topics for group {group.group_name}"
            )
            await load_topics(
                db_session, group, embedding_client, daily_topics, start_time
            )
            logger.info(f"topics loaded for group {group.group_name}")
        except Exception as e:
            logger.error(f"Error loading topics for group {group.group_name}: {str(e)}")
            raise

    async def load_topics_for_all_groups(
        self,
        session: AsyncSession,
        embedding_client: AsyncClient,
        whatsapp: WhatsAppClient,
    ):
        groups = await session.exec(select(Group).where(Group.managed == True))
        for group in list(groups.all()):
            await self.load_topics(session, group, embedding_client, whatsapp)

import logging
from datetime import datetime, timedelta, date, time, timezone
from enum import Enum
from typing import Any, Dict, Optional

import os, re, json, base64, asyncio
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from sqlmodel import desc, select
from sqlmodel.ext.asyncio.session import AsyncSession
from voyageai.client_async import AsyncClient

from src.handler.knowledge_base_answers import KnowledgeBaseAnswers
from src.models import Message
from src.models.group import Group
from src.models.sender import Sender
from src.whatsapp.jid import parse_jid
from src.utils.chat_text import chat2text
from src.whatsapp.client import WhatsAppClient
from .base_handler import BaseHandler

# Creating an object
logger = logging.getLogger(__name__)

# Centralized configuration
TZ = ZoneInfo("Asia/Jerusalem")  # Global timezone
MAX_MESSAGES_FOR_CONTEXT = 200  # Bound context upstream in SQL
MAX_HISTORY_CHARS = 12000       # ~3k tokens approximation (4 chars/token)
HISTORY_PROCESSING_NOTIFY_THRESHOLD = 50
DEFAULT_DUE_HOUR = 10
DEFAULT_DUE_MINUTE = 0
TARGET_TASK_LIST_NAME = "WhatsApp tasks"
SUMMARIZE_MODEL = "anthropic:claude-4-sonnet-20250514"

# Google Tasks integration helpers
SCOPES = ["https://www.googleapis.com/auth/tasks"]

def load_google_tasks_credentials():
    """Load Google OAuth credentials from base64 env variable only."""
    # Lazy import to avoid hard dependency during test collection or when tasks feature isn't used
    from google.oauth2.credentials import Credentials  # type: ignore[import-not-found]
    b64 = os.getenv("GOOGLE_TASKS_TOKEN_B64")
    if not b64:
        raise RuntimeError("GOOGLE_TASKS_TOKEN_B64 environment variable is required for Google Tasks integration")
    data = json.loads(base64.b64decode(b64).decode())
    return Credentials.from_authorized_user_info(data, SCOPES)

def get_tasks_service():
    # Lazy import to avoid hard dependency during test collection or when tasks feature isn't used
    from googleapiclient.discovery import build  # type: ignore[import-not-found]
    creds = load_google_tasks_credentials()
    return build("tasks", "v1", credentials=creds, cache_discovery=False)

def _get_tasklist_id_by_name_sync(name: str) -> Optional[str]:
    """Return Google Tasks list ID by its title, or None if not found."""
    svc = get_tasks_service()
    page_token: Optional[str] = None
    while True:
        resp = (
            svc.tasklists()
            .list(maxResults=100, pageToken=page_token)
            .execute()
        )
        for tasklist in resp.get("items", []) or []:
            if tasklist.get("title") == name:
                return tasklist.get("id")
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return None

def _parse_task(text: str):
    """
    Minimal parser: if the message contains "משימה חדשה", the task title is
    everything that comes after it on the same line. Returns the title string,
    or None if missing.
    """
    if not text:
        return None
    trigger = "משימה חדשה"
    idx = text.find(trigger)
    if idx == -1:
        return None
    title = text[idx + len(trigger):].strip(" \t-:")
    return title or None

def _parse_due_datetime(text: str, tz) -> Optional[datetime]:
    """
    Parse a due datetime from free text.
    - Date formats: DD.MM, DD.MM.YY, DD.MM.YYYY or DD/MM, DD/MM/YY, DD/MM/YYYY
    - Time formats: HH:MM
    Behavior:
      - If date present: use it. If time also present: use that time, else 10:00.
      - If only time present: use today at that time if still in future, else tomorrow.
      - If nothing present: return None.
    The returned datetime is timezone-aware if tz is provided; otherwise UTC.
    """
    if not text:
        return None

    logger.info(f"due_parse start: text='{text}'")
    date_match = re.search(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b", text)
    time_match = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", text)

    if date_match:
        logger.info(f"due_parse date_match: day={date_match.group(1)} month={date_match.group(2)} year_part={date_match.group(3)}")
    else:
        logger.info("due_parse no date match")
    
    if time_match:
        logger.info(f"due_parse time_match: hour={time_match.group(1)} minute={time_match.group(2)}")
    else:
        logger.info("due_parse no time match")

    now = datetime.now(tz) if tz else datetime.now(timezone.utc)

    parsed_hour = DEFAULT_DUE_HOUR
    parsed_minute = DEFAULT_DUE_MINUTE
    if time_match:
        parsed_hour = int(time_match.group(1))
        parsed_minute = int(time_match.group(2))

    if date_match:
        day = int(date_match.group(1))
        month = int(date_match.group(2))
        year_part = date_match.group(3)

        if year_part is None:
            year = now.year
            try:
                candidate = datetime(year, month, day, parsed_hour, parsed_minute)
            except ValueError:
                return None
            if candidate < now:
                year += 1
        else:
            if len(year_part) == 2:
                year = 2000 + int(year_part)
            else:
                year = int(year_part)

        try:
            dt = datetime(year, month, day, parsed_hour, parsed_minute)
        except ValueError:
            logger.info("due_parse invalid date components")
            return None
        dt_final = dt.replace(tzinfo=tz) if tz else dt.replace(tzinfo=timezone.utc)
        logger.info(f"due_parse result date+time -> {dt_final.isoformat()}")
        return dt_final

    # No date provided, but time may be
    if time_match:
        candidate = now.replace(hour=parsed_hour, minute=parsed_minute, second=0, microsecond=0)
        if candidate <= now:
            candidate = candidate + timedelta(days=1)
        logger.info(f"due_parse result time-only -> {candidate.isoformat()}")
        return candidate

    logger.info("due_parse no result -> None")
    return None

def _create_google_task_sync(
    title: str,
    notes: Optional[str] = None,
    list_id: Optional[str] = None,
    due: Optional[datetime] = None,
) -> Dict[str, Any]:
    svc = get_tasks_service()
    body: Dict[str, Any] = {"title": title}
    if notes:
        body["notes"] = notes
    if due is not None:
        # Google Tasks expects RFC3339 timestamp
        body["due"] = due.isoformat()
    tasklist = list_id or "@default"
    created: Dict[str, Any] = (
        svc.tasks().insert(tasklist=tasklist, body=body).execute()
    )
    return created


class IntentEnum(str, Enum):
    summarize = "summarize"
    ask_question = "ask_question"
    about = "about"
    tag_all = "tag_all"
    task = "task"
    admin_only = "admin_only"
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

    async def _route(self, message: str, allow_command_execution: bool = False) -> IntentEnum:
        """Route message to appropriate handler based on content"""
        message_lower = message.lower()

        # Check for tag_all intent (@כולם) - everyone can use it
        if any(phrase in message_lower for phrase in ["@כולם", "@everyone"]):
            logger.info("Routing to tag_all")
            return IntentEnum.tag_all

        # If enforcement is ON, only admin may proceed
        if not allow_command_execution:
            logger.info("Admin-only: rejecting non-admin request")
            return IntentEnum.admin_only

        logger.info(f"route msg_preview='{message[:60]}'")
        
        # Check for summarize intent
        if any(phrase in message_lower for phrase in ["סיכום יומי", "daily summary", "summarize", "סיכום"]):
            logger.info("Routing to summarize")
            return IntentEnum.summarize
            
        # Check for task intent (trigger phrase appears anywhere)
        # Already checked in __init__ if it's a task command and if admin only
        if "משימה חדשה" in message:
            logger.info("Routing to task")
            return IntentEnum.task
            
        # Default to ask_question for everything else
        logger.info("Routing to ask_question (default)")
        return IntentEnum.ask_question

    async def __call__(self, message: Message, allow_command_execution: bool = False):
        """Route message to appropriate handler"""
        logger.info(
            f"router sender={message.sender_jid} chat={message.chat_jid} text_len={(len(message.text) if message.text else 0)}"
        )
        
        # Ensure message.text is not None before routing
        if message.text is None:
            logger.warning("Received message with no text, skipping routing")
            return
            
        route = await self._route(message.text, allow_command_execution)
        logger.info(f"router intent={route}")
        
        match route:
            case IntentEnum.admin_only:
                logger.info("router -> admin_only")
                await self.admin_only(message)
                logger.info("Admin only handler completed")
            case IntentEnum.summarize:
                logger.info("router -> summarize")
                await self.summarize(message)
                logger.info("Summarize handler completed")
            case IntentEnum.ask_question:
                logger.info("router -> ask_knowledge_base")
                await self.ask_knowledge_base(message)
                logger.info("Knowledge base handler completed")
            case IntentEnum.about:
                logger.info("router -> about")
                await self.about(message)
                logger.info("About handler completed")
            case IntentEnum.tag_all:
                logger.info("router -> tag_all_participants")
                await self.tag_all_participants(message)
                logger.info("Tag all participants handler completed")
            case IntentEnum.task:
                logger.info("router -> task")
                await self.task(message)
                logger.info("Task handler completed")
            case IntentEnum.other:
                logger.info("router -> default_response")
                await self.default_response(message)
                logger.info("Default response handler completed")

    async def admin_only(self, message: Message):
        logger.info("admin_only start")
        await self.send_message(
            message.chat_jid,
            "Sorry, only admins can use this command.",
            message.message_id,
        )
        logger.info("admin_only end")

    async def summarize(self, message: Message):
        logger.info("summarize start")
        today_start = datetime.combine(date.today(), datetime.min.time())
        my_jid = await self.whatsapp.get_my_jid()

        stmt = (
            select(Message)
            .where(Message.chat_jid == message.chat_jid)  # From the same group
            .where(Message.timestamp >= today_start)  # From today
            .where(Message.sender_jid != my_jid.normalize_str())  # Exclude self messages
            .order_by(desc(Message.timestamp))  # Newest to oldest
            .limit(MAX_MESSAGES_FOR_CONTEXT)
        )
        res = await self.session.exec(stmt)
        messages_to_summarize: list[Message] = list(res.all())

        if len(messages_to_summarize) > HISTORY_PROCESSING_NOTIFY_THRESHOLD:
            await self.send_message(
                message.chat_jid,
                f"מעבד {len(messages_to_summarize)} הודעות... זה יכול לקחת דקה.",
                message.message_id,
            )

        agent = Agent(
            model=SUMMARIZE_MODEL,
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

        # Compose bounded input for the LLM (history only)
        history_text_full = chat2text(messages_to_summarize)
        history_text = history_text_full[:MAX_HISTORY_CHARS]

        response = await agent.run(
            f"# History (truncated):\n{history_text}"
        )

        # If pydantic_ai provides usage info
        if hasattr(response, 'usage'):
            logger.info(f"Tokens used: {response.usage}")

        response_text = response.output
        logger.info(f"summarize sending len={len(response_text)}")
        
        # Check if response is too long (WhatsApp has a limit of ~4096 characters)
        if len(response_text) > 4000:
            logger.warning(f"Response too long ({len(response_text)} chars), truncating to 4000 chars")
            response_text = response_text[:4000] + "...\n\n[Response truncated due to length]"
        
        await self.send_message(
            message.chat_jid,
            response_text,
            message.message_id,
        )
        logger.info("summarize end")

    async def about(self, message):
        await self.send_message(
            message.chat_jid,
            "I'm an open-source bot created for the GenAI Israel community - https://llm.org.il.\nI can help you catch up on the chat messages and answer questions based on the group's knowledge.\nPlease send me PRs and star me at https://github.com/ilanbenb/wa_llm ⭐️",
            message.message_id,
        )

    async def task(self, message: Message):
        logger.info("task start")
        try:
            text = message.text or ""
            title = _parse_task(text)
            if not title:
                logger.info("No 'משימה חדשה' keyword found or no text after trigger")
                await self.send_message(
                    message.chat_jid,
                    "לא מצאתי טקסט אחרי 'משימה חדשה'. נסה למשל: 'משימה חדשה לעבור על המצגת'",
                    message.message_id,
                )
                return

            # Choose list strictly by name TARGET_TASK_LIST_NAME; if not found, use default
            try:
                list_id = await asyncio.to_thread(_get_tasklist_id_by_name_sync, TARGET_TASK_LIST_NAME)
            except Exception as e:
                logger.exception(f"Failed to resolve Google Tasks list by name: {e}")
                list_id = None
            list_id = list_id or "@default"

            # Build notes using group name and sender name/phone
            group_name: str
            if message.group_jid:
                grp = await self.session.get(Group, message.group_jid)
                group_name = grp.group_name if (grp and grp.group_name) else message.chat_jid
            else:
                group_name = message.chat_jid

            snd = await self.session.get(Sender, message.sender_jid)
            sender_display = (snd.push_name if (snd and snd.push_name) else parse_jid(message.sender_jid).user)

            notes = f"Group: {group_name}\nSender: {sender_display}"

            # Determine due date: parse from text; if absent, default to next day 10:00
            tz = TZ
            due_dt = _parse_due_datetime(text, tz)
            if not due_dt:
                today_local = datetime.now(tz)
                next_day = (today_local + timedelta(days=1)).date()
                due_dt = datetime.combine(next_day, time(hour=DEFAULT_DUE_HOUR, minute=DEFAULT_DUE_MINUTE)).replace(tzinfo=tz)

            logger.info(f"Creating Google Task: title='{title}', list='{list_id}'")

            # Run blocking Google API call in a thread
            created = await asyncio.to_thread(
                _create_google_task_sync,
                title,
                notes,
                list_id,
                due_dt,
            )


            # Format due date for display
            due_str = ""
            if due_dt:
                # Format in local timezone for user-friendly display
                due_local = due_dt.astimezone(TZ)
                due_str = f"\nמועד יעד: {due_local.strftime('%d/%m/%Y %H:%M')}"
            
            response = f"המשימה '{created.get('title')}' נוספה ל-Google Tasks של המק״ס{due_str}"
            await self.send_message(message.chat_jid, response, message.message_id)
            logger.info("task created")

        except ModuleNotFoundError as e:
            # Provide a clearer message when the Google API client is not installed in the image
            logger.exception(f"task failed (missing dependency): {e}")
            await self.send_message(
                message.chat_jid,
                "התכונה 'יצירת משימות' אינה זמינה כרגע בשרת (תלות חסרה). בקש מהמנהל לפרוס גרסה מעודכנת.",
                message.message_id,
            )
        except Exception as e:
            logger.exception(f"task failed: {e}")
            error_msg = f"לא הצלחתי ליצור משימה כרגע.\nשגיאה: {type(e).__name__}: {str(e)}\nודא שהטוקן תקין ונסה שוב."
            await self.send_message(
                message.chat_jid,
                error_msg,
                message.message_id,
            )
        finally:
            logger.info("task end")

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
                response_text = tagged_message.strip() or "כולם מוזמנים! 🎉"
                logger.info(f"tag_all sending len={len(response_text)}")
                await self.send_message(message.chat_jid, response_text, message.message_id)
                return
            else:
                logger.info("Target group not found")
                    
        except Exception as e:
            logger.error(f"Error tagging participants: {e}")
        
        # Fallback
        await self.send_message(message.chat_jid, "📢 כולם מוזמנים!", message.message_id)

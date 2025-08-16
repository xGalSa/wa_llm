import logging
from typing import Annotated, Dict, Any
from fastapi import APIRouter, Depends
from sqlmodel import select, func
from sqlmodel.ext.asyncio.session import AsyncSession

from src.models import KBTopic, Group
from .deps import get_db_async_session

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/knowledge_base_status")
async def knowledge_base_status(
    session: Annotated[AsyncSession, Depends(get_db_async_session)],
) -> Dict[str, Any]:
    """
    Get detailed knowledge base status for debugging.
    Returns counts of groups, topics, and configuration status.
    """
    try:
        # Check total groups
        total_groups_stmt = select(func.count()).select_from(Group)
        total_groups_result = await session.exec(total_groups_stmt)
        total_groups = total_groups_result.one()
        
        # Check managed groups
        managed_groups_stmt = select(func.count()).select_from(Group).where(Group.managed == True)
        managed_groups_result = await session.exec(managed_groups_stmt)
        managed_groups = managed_groups_result.one()
        
        # Check total topics
        total_topics_stmt = select(func.count()).select_from(KBTopic)
        total_topics_result = await session.exec(total_topics_stmt)
        total_topics = total_topics_result.one()
        
        # Check valid topics (with group_jid)
        valid_topics_stmt = select(func.count()).select_from(KBTopic).where(KBTopic.group_jid != None)
        valid_topics_result = await session.exec(valid_topics_stmt)
        valid_topics = valid_topics_result.one()
        
        # Check orphaned topics
        orphaned_topics_stmt = select(func.count()).select_from(KBTopic).where(KBTopic.group_jid == None)
        orphaned_topics_result = await session.exec(orphaned_topics_stmt)
        orphaned_topics = orphaned_topics_result.one()
        
        # Get list of managed groups
        managed_groups_list_stmt = select(Group.group_jid, Group.group_name, Group.last_ingest).where(Group.managed == True)
        managed_groups_list_result = await session.exec(managed_groups_list_stmt)
        managed_groups_list = [
            {
                "group_jid": group_jid,
                "group_name": group_name or "Unknown",
                "last_ingest": last_ingest.isoformat() if last_ingest else None
            }
            for group_jid, group_name, last_ingest in managed_groups_list_result.all()
        ]
        
        # Determine status
        if managed_groups == 0:
            status = "no_managed_groups"
            issue = "No groups configured for topic loading"
            recommendation = "Set managed=true for groups in database"
        elif valid_topics == 0:
            status = "no_topics"
            issue = "No topics loaded yet"
            recommendation = "Run topic loading process via /load_new_kbtopics"
        else:
            status = "healthy"
            issue = None
            recommendation = None
        
        return {
            "status": status,
            "healthy": status == "healthy",
            "issue": issue,
            "recommendation": recommendation,
            "statistics": {
                "total_groups": total_groups,
                "managed_groups": managed_groups,
                "total_topics": total_topics,
                "valid_topics": valid_topics,
                "orphaned_topics": orphaned_topics
            },
            "managed_groups_list": managed_groups_list,
            "warnings": [
                f"Found {orphaned_topics} orphaned topics with NULL group_jid"
            ] if orphaned_topics > 0 else []
        }
        
    except Exception as e:
        logger.error(f"Error checking knowledge base status: {e}")
        return {
            "status": "error",
            "healthy": False,
            "issue": f"Database error: {str(e)}",
            "recommendation": "Check database connection and table structure",
            "statistics": {},
            "managed_groups_list": [],
            "warnings": []
        }

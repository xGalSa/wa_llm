# Ensure 'src.models' and 'models' resolve to the same module to avoid duplicate model definitions
import sys as _sys
_sys.modules.setdefault('models', _sys.modules.get(__name__, None) or _sys.modules[__name__])
_sys.modules.setdefault('src.models', _sys.modules[__name__])

from .group import Group, BaseGroup
from .knowledge_base_topic import KBTopic, KBTopicCreate
from .message import Message, BaseMessage
from .sender import Sender, BaseSender
from .upsert import upsert, bulk_upsert
from .webhook import WhatsAppWebhookPayload

__all__ = [
    "Group",
    "BaseGroup",
    "Message",
    "BaseMessage",
    "Sender",
    "BaseSender",
    "WhatsAppWebhookPayload",
    "upsert",
    "bulk_upsert",
    "KBTopic",
    "KBTopicCreate",
]

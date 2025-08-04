from datetime import datetime, timezone
from typing import Dict, Optional, Any


class PhoneMapper:
    """Global mapping service for JIDs to phone numbers"""
    
    def __init__(self):
        # Store phone number for each JID
        self._jid_to_phone: Dict[str, str] = {}
        # Store when we last saw each phone number
        self._phone_last_seen: Dict[str, datetime] = {}
    
    def add_mapping(self, jid: str, phone: str):
        """Add mapping from JID to phone number"""
        self._jid_to_phone[jid] = phone
        self._phone_last_seen[phone] = datetime.now(timezone.utc)
    
    def get_phone(self, jid: str) -> str:
        """Get phone number by JID, fallback to extracting from JID"""
        if jid in self._jid_to_phone:
            return self._jid_to_phone[jid]
        
        # Fallback: extract phone from JID format
        if '@' in jid:
            return jid.split('@')[0]
        
        return jid
    
    def get_all_phones(self) -> list[str]:
        """Get all known phone numbers"""
        return list(self._phone_last_seen.keys())
    
    def clear(self):
        """Clear all mappings"""
        self._jid_to_phone.clear()
        self._phone_last_seen.clear()


# Global instance
phone_mapper = PhoneMapper() 
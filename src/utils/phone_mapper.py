from datetime import datetime, timezone
from typing import Dict, Optional, Any


class PhoneMapper:
    """Global mapping service for JIDs, LIDs, and phone numbers"""
    
    def __init__(self):
        # Store phone number for each identifier (JID or LID -> phone)
        self._identifier_to_phone: Dict[str, str] = {}
        # Store when we last saw each phone number
        self._phone_last_seen: Dict[str, datetime] = {}
    
    def add_jid_mapping(self, jid: str, phone: str):
        """Add mapping from JID to phone number (from messages)"""
        self._identifier_to_phone[jid] = phone
        self._phone_last_seen[phone] = datetime.now(timezone.utc)
    
    def add_lid_mapping(self, lid: str, phone: str):
        """Add mapping from LID to phone number (from group analysis)"""
        self._identifier_to_phone[lid] = phone
        self._phone_last_seen[phone] = datetime.now(timezone.utc)
    
    def get_phone(self, identifier: str) -> Optional[str]:
        """Get phone number by any identifier (JID or LID)"""
        if identifier in self._identifier_to_phone:
            return self._identifier_to_phone[identifier]
        
        # Only extract phone from actual phone JIDs, not LIDs
        if '@' in identifier and not identifier.endswith('@lid'):
            phone = identifier.split('@')[0]
            # Store this mapping for future use
            self._identifier_to_phone[identifier] = phone
            return phone
        
        # Return None for unknown LIDs
        return None
    
    def get_all_phones(self) -> list[str]:
        """Get all known phone numbers"""
        return list(self._phone_last_seen.keys())
    
    def clear(self):
        """Clear all mappings"""
        self._identifier_to_phone.clear()
        self._phone_last_seen.clear()


# Global instance
phone_mapper = PhoneMapper() 
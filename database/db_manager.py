"""
Database Manager for Logiq
Handles async MongoDB operations with connection pooling
"""

import asyncio
import time
from typing import Optional, Dict, Any, List
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
import logging

logger = logging.getLogger(__name__)


class DatabaseManager:
    """Async MongoDB database manager with connection pooling"""

    def __init__(self, uri: str, database_name: str, pool_size: int = 10):
        """
        Initialize database manager

        Args:
            uri: MongoDB connection URI
            database_name: Name of the database
            pool_size: Maximum connection pool size
        """
        self.uri = uri
        self.database_name = database_name
        self.pool_size = pool_size
        self.client: Optional[AsyncIOMotorClient] = None
        self.db: Optional[AsyncIOMotorDatabase] = None
        self._connected = False

    async def connect(self) -> None:
        """Establish database connection"""
        try:
            self.client = AsyncIOMotorClient(
                self.uri,
                maxPoolSize=self.pool_size,
                minPoolSize=1,
                serverSelectionTimeoutMS=5000
            )
            self.db = self.client[self.database_name]
            # Test connection
            await self.client.admin.command('ping')
            self._connected = True
            logger.info(f"Connected to MongoDB database: {self.database_name}")
        except Exception as e:
            logger.error(f"Failed to connect to MongoDB: {e}")
            raise

    async def disconnect(self) -> None:
        """Close database connection"""
        if self.client:
            self.client.close()
            self._connected = False
            logger.info("Disconnected from MongoDB")

    @property
    def is_connected(self) -> bool:
        """Check if database is connected"""
        return self._connected

    # User operations
    async def get_user(self, user_id: int, guild_id: int) -> Optional[Dict[str, Any]]:
        """Get user document"""
        return await self.db.users.find_one({
            "user_id": user_id,
            "guild_id": guild_id
        })

    async def create_user(self, user_id: int, guild_id: int, data: Dict[str, Any] = None) -> Dict[str, Any]:
        """Create new user document"""
        user_data = {
            "user_id": user_id,
            "guild_id": guild_id,
            "xp": 0,
            "level": 0,
            "balance": 1000,
            "inventory": [],
            "warnings": [],
            "created_at": time.time()
        }
        if data:
            user_data.update(data)

        await self.db.users.insert_one(user_data)
        return user_data

    async def update_user(self, user_id: int, guild_id: int, data: Dict[str, Any]) -> bool:
        """Update user document"""
        result = await self.db.users.update_one(
            {"user_id": user_id, "guild_id": guild_id},
            {"$set": data}
        )
        return result.modified_count > 0

    async def increment_user_field(self, user_id: int, guild_id: int, field: str, amount: int = 1) -> bool:
        """Increment a numeric field in user document"""
        result = await self.db.users.update_one(
            {"user_id": user_id, "guild_id": guild_id},
            {"$inc": {field: amount}}
        )
        return result.modified_count > 0

    # Guild operations
    async def get_guild(self, guild_id: int) -> Optional[Dict[str, Any]]:
        """Get guild configuration"""
        return await self.db.guilds.find_one({"guild_id": guild_id})

    async def create_guild(self, guild_id: int, data: Dict[str, Any] = None) -> Dict[str, Any]:
        """Create new guild configuration"""
        guild_data = {
            "guild_id": guild_id,
            "prefix": "/",
            "modules": {},
            "log_channel": None,
            "welcome_channel": None,
            "verified_role": None,
            "created_at": time.time()
        }
        if data:
            guild_data.update(data)

        await self.db.guilds.insert_one(guild_data)
        return guild_data

    async def update_guild(self, guild_id: int, data: Dict[str, Any]) -> bool:
        """Update guild configuration"""
        result = await self.db.guilds.update_one(
            {"guild_id": guild_id},
            {"$set": data}
        )
        return result.modified_count > 0

    # Leveling operations
    async def get_leaderboard(self, guild_id: int, limit: int = 10) -> List[Dict[str, Any]]:
        """Get XP leaderboard for guild"""
        cursor = self.db.users.find(
            {"guild_id": guild_id}
        ).sort("xp", -1).limit(limit)
        return await cursor.to_list(length=limit)

    # Economy operations
    async def add_balance(self, user_id: int, guild_id: int, amount: int) -> bool:
        """Add to user balance"""
        return await self.increment_user_field(user_id, guild_id, "balance", amount)

    async def remove_balance(self, user_id: int, guild_id: int, amount: int) -> bool:
        """Remove from user balance"""
        user = await self.get_user(user_id, guild_id)
        if user and user.get("balance", 0) >= amount:
            return await self.increment_user_field(user_id, guild_id, "balance", -amount)
        return False

    async def add_item(self, user_id: int, guild_id: int, item: Dict[str, Any]) -> bool:
        """Add item to user inventory"""
        result = await self.db.users.update_one(
            {"user_id": user_id, "guild_id": guild_id},
            {"$push": {"inventory": item}}
        )
        return result.modified_count > 0

    # Moderation operations
    async def add_warning(self, user_id: int, guild_id: int, warning: Dict[str, Any]) -> bool:
        """Add warning to user"""
        result = await self.db.users.update_one(
            {"user_id": user_id, "guild_id": guild_id},
            {"$push": {"warnings": warning}}
        )
        return result.modified_count > 0

    async def get_warnings(self, user_id: int, guild_id: int) -> List[Dict[str, Any]]:
        """Get user warnings"""
        user = await self.get_user(user_id, guild_id)
        return user.get("warnings", []) if user else []

    # Tickets operations
    async def create_ticket(self, ticket_data: Dict[str, Any]) -> str:
        """Create support ticket"""
        result = await self.db.tickets.insert_one(ticket_data)
        return str(result.inserted_id)

    async def get_ticket(self, ticket_id: str) -> Optional[Dict[str, Any]]:
        """Get ticket by ID"""
        from bson import ObjectId
        return await self.db.tickets.find_one({"_id": ObjectId(ticket_id)})

    async def update_ticket(self, ticket_id: str, data: Dict[str, Any]) -> bool:
        """Update ticket"""
        from bson import ObjectId
        result = await self.db.tickets.update_one(
            {"_id": ObjectId(ticket_id)},
            {"$set": data}
        )
        return result.modified_count > 0

    async def list_tickets(self, guild_id: int, limit: int = 50) -> List[Dict[str, Any]]:
        """Ticket del server, dal più recente"""
        cursor = self.db.tickets.find({"guild_id": guild_id}).sort("_id", -1).limit(limit)
        return [doc async for doc in cursor]

    async def get_open_ticket_by_channel(self, guild_id: int, channel_id: int) -> Optional[Dict[str, Any]]:
        return await self.db.tickets.find_one({"guild_id": guild_id, "channel_id": channel_id, "status": "open"})

    async def close_ticket_by_channel(self, channel_id: int, closed_by: int, reason: str) -> None:
        await self.db.tickets.update_one(
            {"channel_id": channel_id},
            {"$set": {"status": "closed", "closed_by": closed_by, "close_reason": reason, "closed_at": time.time()}}
        )

    # Role menus (menu a tendina pubblicati nei canali)
    async def create_role_menu(self, doc: Dict[str, Any]) -> None:
        await self.db.role_menus.insert_one(doc)

    async def get_role_menus(self, guild_id: int) -> List[Dict[str, Any]]:
        return [d async for d in self.db.role_menus.find({"guild_id": guild_id}).sort("created_at", -1).limit(100)]

    async def get_all_role_menus(self) -> List[Dict[str, Any]]:
        return [d async for d in self.db.role_menus.find({})]

    async def delete_role_menu(self, guild_id: int, menu_id: str) -> bool:
        result = await self.db.role_menus.delete_one({"guild_id": guild_id, "menu_id": menu_id})
        return result.deleted_count > 0

    # Analytics operations
    async def log_event(self, event_type: str, data: Dict[str, Any]) -> None:
        """Log analytics event"""
        event = {
            "type": event_type,
            "timestamp": time.time(),
            **data
        }
        await self.db.analytics.insert_one(event)

    async def get_analytics(
        self,
        guild_id: int,
        event_type: Optional[str] = None,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None
    ) -> List[Dict[str, Any]]:
        """Get analytics events with filters"""
        query = {"guild_id": guild_id}
        if event_type:
            query["type"] = event_type
        if start_time or end_time:
            query["timestamp"] = {}
            if start_time:
                query["timestamp"]["$gte"] = start_time
            if end_time:
                query["timestamp"]["$lte"] = end_time

        cursor = self.db.analytics.find(query).sort("timestamp", -1)
        return await cursor.to_list(length=1000)

    # Reminder operations
    async def create_reminder(self, reminder_data: Dict[str, Any]) -> str:
        """Create reminder"""
        result = await self.db.reminders.insert_one(reminder_data)
        return str(result.inserted_id)

    async def get_due_reminders(self, current_time: float) -> List[Dict[str, Any]]:
        """Get reminders that are due"""
        cursor = self.db.reminders.find({
            "remind_at": {"$lte": current_time},
            "completed": False
        })
        return await cursor.to_list(length=100)

    async def complete_reminder(self, reminder_id: str) -> bool:
        """Mark reminder as completed"""
        from bson import ObjectId
        result = await self.db.reminders.update_one(
            {"_id": ObjectId(reminder_id)},
            {"$set": {"completed": True}}
        )
        return result.modified_count > 0

    # Shop operations
    async def get_shop_items(self, guild_id: int) -> List[Dict[str, Any]]:
        """Get shop items for guild"""
        cursor = self.db.shop.find({"guild_id": guild_id})
        return await cursor.to_list(length=100)

    async def create_shop_item(self, item_data: Dict[str, Any]) -> str:
        """Create shop item"""
        result = await self.db.shop.insert_one(item_data)
        return str(result.inserted_id)

    # ----- sessioni web (dashboard) -----
    async def ensure_web_indexes(self) -> None:
        """Indice TTL: MongoDB elimina da solo le sessioni scadute"""
        await self.db.web_sessions.create_index("expires_at", expireAfterSeconds=0)

    async def save_web_session(self, key: str, data: Dict[str, Any], exp: float, created: float) -> None:
        from datetime import datetime, timezone
        await self.db.web_sessions.replace_one(
            {"_id": key},
            {
                "_id": key,
                "data": data,
                "exp": exp,
                "created": created,
                "expires_at": datetime.fromtimestamp(exp, tz=timezone.utc),
            },
            upsert=True,
        )

    async def delete_web_session(self, key: str) -> None:
        await self.db.web_sessions.delete_one({"_id": key})

    async def load_web_sessions(self) -> list:
        rows = []
        async for doc in self.db.web_sessions.find({"exp": {"$gt": time.time()}}).limit(5000):
            rows.append((doc["_id"], doc.get("data"), doc.get("exp", 0), doc.get("created", 0)))
        return rows

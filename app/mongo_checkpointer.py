"""MongoDB-backed persistent LangGraph CheckpointSaver for Greeny AI.

Persists StateGraph checkpoints and task writes to a dedicated MongoDB database.
Supports cross-kiosk authorization checks and clean thread deletions.
"""

import asyncio
from datetime import datetime, timezone, timedelta
from pymongo.errors import DuplicateKeyError
from app.config import settings
from app.privacy import redact_contacts
from app.coordination import Coordinator
from typing import Any, AsyncIterator, Dict, Iterator, List, Optional, Sequence, Tuple
from bson import Binary
from pymongo.collection import Collection
from pymongo.database import Database

from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    WRITES_IDX_MAP,
    get_checkpoint_id,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langchain_core.runnables import RunnableConfig


class CrossKioskAccessError(PermissionError):
    """Raised when a kiosk attempts to access or mutate a session owned by another kiosk."""


class MongoCheckpointSaver(BaseCheckpointSaver):
    """Stores LangGraph thread checkpoints and intermediate writes in MongoDB."""

    def __init__(self, db: Database, checkpoints_collection: str = "checkpoints",
                 writes_collection: str = "checkpoint_writes") -> None:
        super().__init__(serde=JsonPlusSerializer())
        self.db = db
        self.coordinator = Coordinator(db)
        self.owners_col = db["checkpoint_owners"]
        self.checkpoints_col: Collection = db[checkpoints_collection]
        self.writes_col: Collection = db[writes_collection]
        self.blobs_col: Collection = db[f"{checkpoints_collection}_blobs"]

    def ensure_indexes(self) -> None:
        """Create indexes for performance and session cleanup."""
        self.checkpoints_col.create_index([("thread_id", 1), ("checkpoint_ns", 1), ("checkpoint_id", 1)], unique=True)
        self.writes_col.create_index([("thread_id", 1), ("checkpoint_ns", 1), ("checkpoint_id", 1), ("task_id", 1), ("idx", 1)], unique=True)
        self.blobs_col.create_index([("thread_id", 1), ("checkpoint_ns", 1), ("channel", 1), ("version", 1)], unique=True)
        for col in (self.checkpoints_col, self.writes_col, self.blobs_col):
            col.create_index('created_at', expireAfterSeconds=settings.greeny_ai_retention_days * 86400)
        for name in ('backend_tickets', 'backend_rates'):
            self.db[name].create_index('expires_at', expireAfterSeconds=0)

    def validate_indexes(self):
        required = (
            (self.checkpoints_col, ('thread_id', 'checkpoint_ns', 'checkpoint_id')),
            (self.writes_col, ('thread_id', 'checkpoint_ns', 'checkpoint_id', 'task_id', 'idx')),
            (self.blobs_col, ('thread_id', 'checkpoint_ns', 'channel', 'version')),
        )
        for collection, keys in required:
            indexes = list(collection.index_information().values())
            if not any(index.get('unique') and tuple(k for k, _ in index['key']) == keys for index in indexes):
                raise RuntimeError('Required checkpoint uniqueness index is missing.')
            if not any(index.get('expireAfterSeconds') == settings.greeny_ai_retention_days * 86400 for index in indexes):
                raise RuntimeError('Required checkpoint retention index is missing.')

    def _verify_kiosk_ownership(self, thread_id, request_kiosk_id):
        if not request_kiosk_id:
            raise CrossKioskAccessError('Kiosk identity is required.')
        doc = self.owners_col.find_one({'_id': thread_id})
        legacy = self.checkpoints_col.find_one({'thread_id': thread_id})
        owner = (doc or legacy or {}).get('kiosk_id')
        if owner and owner != request_kiosk_id:
            raise CrossKioskAccessError('Session ownership mismatch.')

    def claim(self, thread_id, kiosk_id):
        self._verify_kiosk_ownership(thread_id, kiosk_id)
        try:
            self.owners_col.insert_one({'_id': thread_id, 'kiosk_id': kiosk_id,
                                        'last_active': datetime.now(timezone.utc)})
        except DuplicateKeyError:
            self._verify_kiosk_ownership(thread_id, kiosk_id)

    def get_session_kiosk(self, thread_id):
        doc = self.owners_col.find_one({'_id': thread_id}) or self.checkpoints_col.find_one({'thread_id': thread_id})
        return doc.get('kiosk_id') if doc else None

    def touch_session(self, thread_id, kiosk_id, timeout):
        self.claim(thread_id, kiosk_id)
        owner = self.owners_col.find_one({'_id': thread_id})
        last = owner.get('last_active')
        now = datetime.now(timezone.utc)
        if last and (now - last.replace(tzinfo=timezone.utc)).total_seconds() > timeout:
            self.delete_thread(thread_id, request_kiosk_id=kiosk_id)
        self.owners_col.update_one({'_id': thread_id, 'kiosk_id': kiosk_id}, {'$set': {'last_active': now}})

    def get_tuple(self, config: RunnableConfig) -> Optional[CheckpointTuple]:
        thread_id: str = config["configurable"]["thread_id"]
        checkpoint_ns: str = config["configurable"].get("checkpoint_ns", "")
        request_kiosk_id: Optional[str] = config["configurable"].get("kiosk_id")
        self._verify_kiosk_ownership(thread_id, request_kiosk_id)

        checkpoint_id = get_checkpoint_id(config)
        query = {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns}
        if checkpoint_id:
            query["checkpoint_id"] = checkpoint_id
            doc = self.checkpoints_col.find_one(query)
        else:
            doc = self.checkpoints_col.find_one(query, sort=[("checkpoint_id", -1)])

        if not doc:
            return None
        created = doc.get("created_at")
        if created and (datetime.now(timezone.utc) - created.replace(tzinfo=timezone.utc)).total_seconds() > settings.greeny_ai_retention_days * 86400:
            return None

        # Fetch writes
        writes_cursor = self.writes_col.find({
            "thread_id": thread_id,
            "checkpoint_ns": checkpoint_ns,
            "checkpoint_id": doc["checkpoint_id"]
        }).sort([("idx", 1)])

        pending_writes = []
        for w in writes_cursor:
            val = redact_contacts(self.serde.loads_typed((w["value_type"], bytes(w["value"]))))
            pending_writes.append((w["task_id"], w["channel"], val))

        # Reconstruct checkpoint
        raw_cp = self.serde.loads_typed((doc["cp_type"], bytes(doc["checkpoint"])))
        meta = redact_contacts(self.serde.loads_typed((doc["meta_type"], bytes(doc["metadata"]))))

        # Load channel values from blobs
        channel_values = {}
        for k, v in raw_cp.get("channel_versions", {}).items():
            blob = self.blobs_col.find_one({
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "channel": k,
                "version": v,
            })
            if blob:
                channel_values[k] = redact_contacts(self.serde.loads_typed((blob["blob_type"], bytes(blob["data"]))))

        parent_config = None
        if doc.get("parent_checkpoint_id"):
            parent_config = {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": doc["parent_checkpoint_id"],
                    "kiosk_id": request_kiosk_id,
                }
            }

        return CheckpointTuple(
            config={
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": doc["checkpoint_id"],
                    "kiosk_id": request_kiosk_id,
                }
            },
            checkpoint={**raw_cp, "channel_values": channel_values},
            metadata=meta,
            parent_config=parent_config,
            pending_writes=pending_writes,
        )

    def list(self, config: Optional[RunnableConfig], *, filter: Optional[Dict[str, Any]] = None,
             before: Optional[RunnableConfig] = None, limit: Optional[int] = None) -> Iterator[CheckpointTuple]:
        if not config:
            return
        thread_id = config["configurable"]["thread_id"]
        request_kiosk_id = config["configurable"].get("kiosk_id")
        self._verify_kiosk_ownership(thread_id, request_kiosk_id)
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        query: Dict[str, Any] = {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns}
        if before:
            before_id = get_checkpoint_id(before)
            if before_id:
                query["checkpoint_id"] = {"$lt": before_id}

        cursor = self.checkpoints_col.find(query).sort([("checkpoint_id", -1)])
        remaining = limit

        for doc in cursor:
            sub_config = {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": doc["checkpoint_id"],
                    "kiosk_id": request_kiosk_id,
                }
            }
            res = self.get_tuple(sub_config)
            if res and (not filter or all(res.metadata.get(k) == v for k, v in filter.items())):
                if remaining is not None and remaining <= 0:
                    break
                yield res
                if remaining is not None:
                    remaining -= 1

    def put(self, config: RunnableConfig, checkpoint: Checkpoint, metadata: CheckpointMetadata,
            new_versions: ChannelVersions) -> RunnableConfig:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        kiosk_id = config["configurable"].get("kiosk_id")
        self.claim(thread_id, kiosk_id)

        c = checkpoint.copy()
        values: dict[str, Any] = c.pop("channel_values", {})

        # Save blobs for new versions
        for k, v in c.get("channel_versions", {}).items():
            if k in values:
                b_type, b_bytes = self.serde.dumps_typed(redact_contacts(values[k]))
                self.blobs_col.replace_one(
                    {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns, "channel": k, "version": v},
                    {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns, "channel": k, "version": v,
                     "blob_type": b_type, "data": Binary(b_bytes), "created_at": datetime.now(timezone.utc)},
                    upsert=True,
                )

        cp_type, cp_bytes = self.serde.dumps_typed(c)
        meta_type, meta_bytes = self.serde.dumps_typed(redact_contacts(metadata))
        parent_id = config["configurable"].get("checkpoint_id")

        self.checkpoints_col.replace_one(
            {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns, "checkpoint_id": checkpoint["id"]},
            {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint["id"],
                "parent_checkpoint_id": parent_id,
                "kiosk_id": kiosk_id,
                "cp_type": cp_type,
                "checkpoint": Binary(cp_bytes),
                "meta_type": meta_type,
                "metadata": Binary(meta_bytes),
                "created_at": datetime.now(timezone.utc),
            },
            upsert=True,
        )

        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint["id"],
                "kiosk_id": kiosk_id,
            }
        }

    def put_writes(self, config: RunnableConfig, writes: Sequence[Tuple[str, Any]], task_id: str,
                   task_path: str = "") -> None:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = config["configurable"]["checkpoint_id"]
        self.claim(thread_id, config["configurable"].get("kiosk_id"))

        for idx, (channel, val) in enumerate(writes):
            val_type, val_bytes = self.serde.dumps_typed(redact_contacts(val))
            write_index = WRITES_IDX_MAP.get(channel, idx)
            if write_index >= 0 and self.writes_col.find_one({"thread_id": thread_id, "checkpoint_ns": checkpoint_ns, "checkpoint_id": checkpoint_id, "task_id": task_id, "idx": write_index}):
                continue
            self.writes_col.replace_one(
                {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": checkpoint_id,
                    "task_id": task_id,
                    "idx": WRITES_IDX_MAP.get(channel, idx),
                },
                {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": checkpoint_id,
                    "task_id": task_id,
                    "idx": WRITES_IDX_MAP.get(channel, idx),
                    "channel": channel,
                    "value_type": val_type,
                    "value": Binary(val_bytes),
                    "task_path": task_path,
                    "created_at": datetime.now(timezone.utc),
                },
                upsert=True,
            )

    def delete_thread(self, thread_id: str, request_kiosk_id: Optional[str] = None) -> None:
        """Clear thread checkpoints, blobs and writes. Verifies kiosk authorization if provided."""
        self._verify_kiosk_ownership(thread_id, request_kiosk_id)
        self.checkpoints_col.delete_many({"thread_id": thread_id})
        self.writes_col.delete_many({"thread_id": thread_id})
        self.blobs_col.delete_many({"thread_id": thread_id})

    async def aget_tuple(self, config: RunnableConfig) -> Optional[CheckpointTuple]:
        return await asyncio.to_thread(self.get_tuple, config)

    async def alist(
        self,
        config: Optional[RunnableConfig],
        *,
        filter: Optional[Dict[str, Any]] = None,
        before: Optional[RunnableConfig] = None,
        limit: Optional[int] = None,
    ) -> AsyncIterator[CheckpointTuple]:
        tuples = await asyncio.to_thread(lambda: list(self.list(config, filter=filter, before=before, limit=limit)))
        for t in tuples:
            yield t

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        return await asyncio.to_thread(self.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[Tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str, request_kiosk_id: Optional[str] = None) -> None:
        await asyncio.to_thread(self.delete_thread, thread_id, request_kiosk_id)

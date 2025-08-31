# projectwise/models/models.py
from __future__ import annotations

from datetime import datetime
from typing import List, Dict, Any, Optional
from sqlalchemy import (
    String,
    Text,
    DateTime,
    func,
    select,
    Index,
    ForeignKey,
    Column,
    Integer,
)
from sqlalchemy.orm import declarative_base, Mapped, mapped_column
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.exc import SQLAlchemyError

from projectwise.utils.helper import truncate_by_tokens
from projectwise.utils.logger import get_logger


logger = get_logger(__name__)
Base = declarative_base()


class ChatSession(Base):
    __tablename__ = "chat_sessions"
    id = Column(Integer, primary_key=True)
    user_id = Column(String(64), unique=True, nullable=False)


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id = Column(Integer, primary_key=True)
    user_id = Column(String(64), ForeignKey("chat_sessions.user_id"), nullable=False)
    role = Column(String(16), nullable=False)
    content = Column(Text, nullable=False)
    timestamp = Column(DateTime, server_default=func.now())


class WsMessage(Base):
    __tablename__ = "ws_messages"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    room_id: Mapped[str] = mapped_column(String(128), index=True)
    sender: Mapped[str] = mapped_column(String(128))
    type: Mapped[str] = mapped_column(String(16))  # 'message' | 'system'
    content: Mapped[str] = mapped_column(Text())
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


Index("ix_ws_messages_room_ts", WsMessage.room_id, WsMessage.ts)


class ModelDB:
    """SQLAlchemy Async Engine untuk non-blocking DB access"""

    def __init__(self, db_url: str):
        self.db_url = db_url
        self._initialized = False

        try:
            self._engine = create_async_engine(self.db_url, echo=False, future=True)
            self.Session = async_sessionmaker(
                bind=self._engine,
                class_=AsyncSession,
                expire_on_commit=False,
            )
            logger.info("ModelDB initialized with DB: %s", self.db_url)
        except SQLAlchemyError as e:
            logger.error("Gagal inisialisasi database: %s", e)
            raise

    async def init_models(self):
        """Init DB sekali dan buat tabel jika belum ada."""
        global _initialized
        if self._initialized:
            return
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self._initialized = True


    # * --------------------------------------------------
    # * chat database utils
    # * --------------------------------------------------
    async def save_chat_message(self, user_id: str, role: str, content: str) -> None:
        """Simpan pesan baru ke memory. Buat ChatSession jika belum ada."""
        if role not in ("user", "assistant", "system"):
            raise ValueError(f"Role tidak valid: {role}")

        async with self.Session() as s:  # type: ignore
            try:
                # Pastikan session ada
                result = await s.execute(select(ChatSession).filter_by(user_id=user_id))
                chat_session = result.scalars().first()
                if not chat_session:
                    s.add(ChatSession(user_id=user_id))
                    await s.commit()

                # Save chat ke db
                s.add(ChatMessage(user_id=user_id, role=role, content=content))
                await s.commit()
            except Exception as ex:
                await s.rollback()
                logger.error("Gagal simpan memory untuk user %s: %s", user_id, ex)
                raise

    async def query_chat_recent(
        self, user_id: str, limit: Optional[int] = 5
    ) -> List[Dict[str, Any]]:
        """Ambil chat history terbaru untuk user_id tertentu, urut dari lama ke baru.
        Format return: List[{"role": role, "content": content}, ...]
        """
        limit = limit

        async with self.Session() as s:
            try:
                result = await s.execute(
                    select(ChatMessage)
                    .filter_by(user_id=user_id)
                    .order_by(ChatMessage.id.desc())
                    .limit(limit)
                )
                raw = list(result.scalars().all())[::-1]  # Message object
            except Exception as ex:
                logger.error("Gagal ambil history untuk user %s: %s", user_id, ex)
                raise

        lines: List[Dict[str, Any]] = []
        for m in raw:
            snippet = truncate_by_tokens(getattr(m, "content"), 1500)
            lines.append({"role": getattr(m, "role"), "content": snippet})

        return lines


    # * --------------------------------------------------
    # * war room database utils
    # * --------------------------------------------------
    async def save_ws_message(
        self, room_id: str, sender: str, mtype: str, content: str
    ) -> None:
        async with self.Session() as s:  # type : AsyncSession
            s.add(
                WsMessage(room_id=room_id, sender=sender, type=mtype, content=content)
            )
            await s.commit()

    async def query_ws_recent(
        self, room_id: str, limit: int = 100
    ) -> List[Dict[str, Any]]:
        async with self.Session() as s:
            stmt = (
                select(WsMessage)
                .where(WsMessage.room_id == room_id)
                .order_by(WsMessage.ts.asc())
                .limit(limit)
            )
            rows = (await s.execute(stmt)).scalars().all()
            return [
                {
                    "room": room_id,
                    "type": r.type,
                    "from": r.sender,
                    "content": r.content,
                    "ts": r.ts.isoformat(),
                }
                for r in rows
            ]

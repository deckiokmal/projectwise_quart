# projectwise/services/memory/short_term_memory.py
from typing import Optional

from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.exc import SQLAlchemyError

from projectwise.utils.logger import get_logger
from projectwise.utils.helper import truncate_by_tokens

Base = declarative_base()
logger = get_logger(__name__)


class ChatSession(Base):
    __tablename__ = "chat_sessions"
    id = Column(Integer, primary_key=True)
    user_id = Column(String(64), unique=True, nullable=False)


class Message(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True)
    user_id = Column(String(64), ForeignKey("chat_sessions.user_id"), nullable=False)
    role = Column(String(16), nullable=False)
    content = Column(Text, nullable=False)
    timestamp = Column(DateTime, server_default=func.now())


class ShortTermMemory:
    """
    Menyimpan dan mengambil chat history jangka pendek.
    Menggunakan SQLAlchemy Async Engine untuk non-blocking DB access.
    """

    def __init__(self, db_url: str, echo: bool = False, max_history: int = 20):
        self.db_url = db_url
        self.max_history = max_history

        try:
            self.engine = create_async_engine(db_url, echo=echo, future=True)
            self.SessionLocal = sessionmaker(
                bind=self.engine,  # type: ignore
                class_=AsyncSession,
                expire_on_commit=False,
            )
            logger.info("ShortTermMemory initialized with DB: %s", db_url)
        except SQLAlchemyError as e:
            logger.error("Gagal inisialisasi database: %s", e)
            raise

    async def init_models(self):
        """Buat tabel jika belum ada."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def save(self, user_id: str, role: str, content: str) -> None:
        """Simpan pesan baru ke memory. Buat ChatSession jika belum ada."""
        if role not in ("user", "assistant", "system"):
            raise ValueError(f"Role tidak valid: {role}")

        async with self.SessionLocal() as session:  # type: ignore
            try:
                # Pastikan session ada
                result = await session.execute(
                    select(ChatSession).filter_by(user_id=user_id)
                )
                chat_session = result.scalars().first()
                if not chat_session:
                    session.add(ChatSession(user_id=user_id))
                    await session.commit()

                session.add(Message(user_id=user_id, role=role, content=content))
                await session.commit()
            except Exception as ex:
                await session.rollback()
                logger.error("Gagal simpan memory untuk user %s: %s", user_id, ex)
                raise

    async def get_history(self, user_id: str, limit: Optional[int] = None) -> str:
        """Ambil history pesan dan kembalikan sebagai markdown list terpotong token."""
        limit = limit or self.max_history

        async with self.SessionLocal() as session:  # type: ignore
            try:
                result = await session.execute(
                    select(Message)
                    .filter_by(user_id=user_id)
                    .order_by(Message.id.desc())
                    .limit(limit)
                )
                raw = list(result.scalars().all())[::-1]
            except Exception as ex:
                logger.error("Gagal ambil history untuk user %s: %s", user_id, ex)
                raise

        # Buat block markdown terpotong
        lines = []
        for m in raw:
            snippet = truncate_by_tokens(m.content, 150)
            lines.append(f"- **{m.role}**: {snippet}")
        return "\n".join(lines)

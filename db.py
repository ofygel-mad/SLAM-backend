# -*- coding: utf-8 -*-
"""БД: блоки (бригады) и работники. Postgres (Railway) или SQLite локально."""
import os
import datetime as dt
from sqlalchemy import (create_engine, Integer, String, Text, DateTime, ForeignKey)
from sqlalchemy.orm import (DeclarativeBase, Mapped, mapped_column, relationship,
                            sessionmaker)

# Railway даёт DATABASE_URL для Postgres; локально — SQLite-файл.
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./slam.db")
# SQLAlchemy требует postgresql:// (а не postgres://)
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


class Block(Base):
    """Бригада. Название произвольное (ставит бригадир). Хранит постоянные данные."""
    __tablename__ = "blocks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    company: Mapped[str] = mapped_column(String(300), default="")     # подрядная организация (сохраняется)
    object_key: Mapped[str] = mapped_column(String(20), default="sulphide_1")  # sulphide_1|sulphide_2
    task: Mapped[str] = mapped_column(String(20), default="montazh")  # montazh|demontazh
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)

    workers: Mapped[list["Worker"]] = relationship(
        back_populates="block", cascade="all, delete-orphan", order_by="Worker.order_index")

    def to_dict(self):
        return {
            "id": self.id, "name": self.name, "company": self.company,
            "object_key": self.object_key, "task": self.task,
            "workers": [w.to_dict() for w in self.workers],
        }


class Worker(Base):
    """Работник бригады. ФИО сохраняется."""
    __tablename__ = "workers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    block_id: Mapped[int] = mapped_column(ForeignKey("blocks.id", ondelete="CASCADE"))
    full_name: Mapped[str] = mapped_column(String(300))
    order_index: Mapped[int] = mapped_column(Integer, default=0)

    block: Mapped[Block] = relationship(back_populates="workers")

    def to_dict(self):
        return {"id": self.id, "full_name": self.full_name, "order_index": self.order_index}


def init_db():
    Base.metadata.create_all(engine)


def get_session():
    return SessionLocal()

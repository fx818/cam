from typing import Any

from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)

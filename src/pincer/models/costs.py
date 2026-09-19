"""LLM and image-generation spend."""

from sqlalchemy import REAL, Column, Index, Integer, Text
from sqlmodel import Field, SQLModel


class CostLog(SQLModel, table=True):
    __tablename__ = "cost_logs"
    __table_args__ = (
        Index("idx_cost_timestamp", "timestamp"),
        {"sqlite_autoincrement": True},
    )

    id: int | None = Field(default=None, sa_column=Column(Integer(), primary_key=True, autoincrement=True))
    timestamp: float = Field(sa_column=Column(REAL(), nullable=False))
    provider: str = Field(sa_column=Column(Text(), nullable=False))
    model: str = Field(sa_column=Column(Text(), nullable=False))
    input_tokens: int = Field(sa_column=Column(Integer(), nullable=False))
    output_tokens: int = Field(sa_column=Column(Integer(), nullable=False))
    cost_usd: float = Field(sa_column=Column(REAL(), nullable=False))
    session_id: str | None = Field(default=None, sa_column=Column(Text()))


class ImageCostLog(SQLModel, table=True):
    __tablename__ = "image_cost_logs"
    __table_args__ = (
        Index("idx_image_cost_timestamp", "timestamp"),
        {"sqlite_autoincrement": True},
    )

    id: int | None = Field(default=None, sa_column=Column(Integer(), primary_key=True, autoincrement=True))
    timestamp: float = Field(sa_column=Column(REAL(), nullable=False))
    provider: str = Field(sa_column=Column(Text(), nullable=False))
    model: str = Field(sa_column=Column(Text(), nullable=False))
    cost_usd: float = Field(sa_column=Column(REAL(), nullable=False))

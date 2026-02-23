"""表情包数据库模型定义。

定义 emoji 表结构，由插件自身管理（不在 core 模型中）。
"""

from sqlalchemy import Boolean, Float, Index, Integer, Text  # noqa: F401
from sqlalchemy.orm import Mapped, mapped_column

from src.core.models.sql_alchemy import Base, get_string_field


class Emoji(Base):
    """表情包信息模型。"""

    __tablename__ = "emoji"

    # 主键
    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    # 文件信息
    full_path: Mapped[str] = mapped_column(
        get_string_field(500),
        nullable=False,
        unique=True,
        index=True,
        comment="表情包文件完整路径",
    )
    format: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="图片格式：jpeg / png / gif / webp",
    )

    # 唯一标识
    emoji_hash: Mapped[str] = mapped_column(
        get_string_field(64),
        nullable=False,
        index=True,
        comment="文件内容 MD5 哈希，用于去重",
    )

    # VLM 生成的描述
    description: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="",
        comment="VLM 生成的描述",
    )

    # 状态标志
    is_registered: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="是否已注册",
    )
    is_banned: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="是否被禁用",
    )

    # 时间和统计
    register_time: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
        comment="注册时间（Unix 时间戳）",
    )
    usage_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="累计使用次数",
    )
    last_used_time: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
        comment="最后使用时间（Unix 时间戳）",
    )

    __table_args__ = (
        Index("idx_emoji_full_path", "full_path"),
        Index("idx_emoji_hash", "emoji_hash"),
    )

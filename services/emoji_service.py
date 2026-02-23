"""表情包数据库操作服务。

封装 Emoji 表的所有数据库操作，对外提供统一的服务接口。
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import TYPE_CHECKING

from src.app.plugin_system.api.log_api import get_logger
from src.core.components.base.service import BaseService
from src.kernel.db import CRUDBase, QueryBuilder, get_db_session

from ..models import Emoji

if TYPE_CHECKING:
    from src.core.components.base.plugin import BasePlugin

logger = get_logger("emoji_sticker.service")


class EmojiService(BaseService):
    """表情包数据库操作与管理服务。

    提供表情包的增删改查、使用统计、容量管理等功能。
    其他插件可通过 ServiceManager 获取此服务实例。
    """

    service_name = "emoji_service"
    service_description = "表情包数据库操作与管理服务"
    version = "1.0.0"

    def __init__(self, plugin: "BasePlugin") -> None:
        """初始化服务。

        Args:
            plugin: 所属插件实例
        """
        super().__init__(plugin)
        self._crud = CRUDBase(Emoji)

    async def get_active_emojis(self) -> list[Emoji]:
        """查询所有已注册且未禁用的表情包。

        Returns:
            活跃表情包列表
        """
        try:
            results: list[Emoji] = (
                await QueryBuilder(Emoji)
                .filter(is_registered=True, is_banned=False)
                .all()
            )
            return results
        except Exception as e:
            logger.error(f"查询活跃表情包失败: {e}")
            return []

    async def get_emoji_by_hash(self, emoji_hash: str) -> Emoji | None:
        """按 emoji_hash 查询单条记录。

        Args:
            emoji_hash: 表情包文件的 MD5 哈希

        Returns:
            Emoji 记录或 None
        """
        try:
            return await self._crud.get_by(emoji_hash=emoji_hash)
        except Exception as e:
            logger.error(f"查询表情包 {emoji_hash[:8]}... 失败: {e}")
            return None

    async def check_exists(self, emoji_hash: str) -> bool:
        """检查 emoji_hash 是否已存在。

        Args:
            emoji_hash: 表情包文件的 MD5 哈希

        Returns:
            是否存在
        """
        try:
            return await self._crud.exists(emoji_hash=emoji_hash)
        except Exception as e:
            logger.error(f"检查表情包存在性失败: {e}")
            return False

    async def register_emoji(
        self,
        emoji_hash: str,
        full_path: str,
        description: str,
        fmt: str,
    ) -> Emoji | None:
        """注册新的表情包到数据库。

        Args:
            emoji_hash: 文件内容 MD5 哈希
            full_path: 注册后的文件完整路径
            description: VLM 生成的精炼描述
            fmt: 图片格式（jpeg/png/gif/webp）

        Returns:
            创建的 Emoji 记录，失败返回 None
        """
        try:
            emoji = await self._crud.create({
                "emoji_hash": emoji_hash,
                "full_path": full_path,
                "description": description,
                "format": fmt,
                "usage_count": 0,
                "last_used_time": None,
                "is_registered": True,
                "is_banned": False,
                "register_time": time.time(),
            })
            logger.info(f"表情包注册成功: {emoji_hash[:8]}... - {description[:30]}")
            return emoji
        except Exception as e:
            logger.error(f"注册表情包失败 ({emoji_hash[:8]}...): {e}")
            return None

    async def record_usage(self, emoji_hash: str) -> None:
        """记录表情包使用，更新次数和时间。

        Args:
            emoji_hash: 表情包文件的 MD5 哈希
        """
        try:
            async with get_db_session() as session:
                from sqlalchemy import select

                stmt = select(Emoji).where(Emoji.emoji_hash == emoji_hash)
                result = await session.execute(stmt)
                emoji = result.scalar_one_or_none()
                if emoji:
                    emoji.usage_count += 1
                    emoji.last_used_time = time.time()
                    await session.commit()
                else:
                    logger.warning(f"记录使用失败: 未找到表情包 {emoji_hash[:8]}...")
        except Exception as e:
            logger.error(f"记录表情包使用失败 ({emoji_hash[:8]}...): {e}")

    async def get_registered_count(self) -> int:
        """获取已注册且未禁用的表情包总数。

        Returns:
            表情包数量
        """
        try:
            return await self._crud.count(is_registered=True, is_banned=False)
        except Exception as e:
            logger.error(f"统计表情包数量失败: {e}")
            return 0

    async def get_lru_emojis(self, count: int) -> list[Emoji]:
        """获取最久未使用的表情包（LRU 淘汰候选）。

        Args:
            count: 需要获取的数量

        Returns:
            按 last_used_time 升序排列的表情包列表
        """
        try:
            results: list[Emoji] = (
                await QueryBuilder(Emoji)
                .filter(is_registered=True, is_banned=False)
                .order_by("last_used_time")
                .limit(count)
                .all()
            )
            return results
        except Exception as e:
            logger.error(f"查询 LRU 表情包失败: {e}")
            return []

    async def delete_emoji(self, emoji_hash: str) -> bool:
        """删除表情包（数据库记录 + 文件系统）。

        Args:
            emoji_hash: 表情包文件的 MD5 哈希

        Returns:
            是否删除成功
        """
        try:
            emoji = await self.get_emoji_by_hash(emoji_hash)
            if not emoji:
                logger.warning(f"删除失败: 未找到表情包 {emoji_hash[:8]}...")
                return False

            # 删除文件
            file_path = emoji.full_path
            if file_path and os.path.exists(file_path):
                try:
                    await asyncio.to_thread(os.remove, file_path)
                except OSError as e:
                    logger.warning(f"删除表情包文件失败 ({file_path}): {e}")

            # 删除数据库记录
            deleted = await self._crud.delete(emoji.id)
            if deleted:
                logger.info(f"表情包已删除: {emoji_hash[:8]}...")
            return deleted
        except Exception as e:
            logger.error(f"删除表情包失败 ({emoji_hash[:8]}...): {e}")
            return False

    async def ban_emoji(self, emoji_hash: str) -> bool:
        """禁用表情包。

        Args:
            emoji_hash: 表情包文件的 MD5 哈希

        Returns:
            是否禁用成功
        """
        try:
            emoji = await self.get_emoji_by_hash(emoji_hash)
            if not emoji:
                logger.warning(f"禁用失败: 未找到表情包 {emoji_hash[:8]}...")
                return False

            result = await self._crud.update(emoji.id, {"is_banned": True})
            if result:
                logger.info(f"表情包已禁用: {emoji_hash[:8]}...")
                return True
            return False
        except Exception as e:
            logger.error(f"禁用表情包失败 ({emoji_hash[:8]}...): {e}")
            return False

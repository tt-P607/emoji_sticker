"""偷表情包事件处理器。

订阅 ON_MESSAGE_RECEIVED 事件，当聊天中收到表情包时：
1. 检查是否已注册（去重）
2. 优先复用框架 MediaManager 已识别的描述（避免重复 VLM 调用）
3. 若框架无缓存则调用插件自身的 VLM 描述生成
4. 保存文件并注册到插件表情包库
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
from typing import Any

from src.app.plugin_system.api.log_api import get_logger
from src.core.components.base.event_handler import BaseEventHandler
from src.core.components.types import EventType
from src.kernel.event import EventDecision

from ..config import EmojiStickerConfig
from ..services.emoji_service import EmojiService

logger = get_logger("emoji_sticker.steal")


class StealEmojiHandler(BaseEventHandler):
    """监听聊天消息，自动收集收到的表情包。

    通过 media_api 获取框架已识别的描述，避免重复 VLM 调用。
    不拦截消息，不影响正常聊天流程。
    """

    handler_name = "steal_emoji"
    handler_description = "自动收集聊天中收到的表情包"
    weight = 0
    intercept_message = False
    init_subscribe: list[EventType | str] = [EventType.ON_MESSAGE_RECEIVED]

    async def execute(
        self,
        event_name: str,
        params: dict[str, Any],
    ) -> tuple[EventDecision, dict[str, Any]]:
        """处理消息事件，检测并收集表情包。

        Args:
            event_name: 触发本处理器的事件名称
            params: 事件参数，包含 message / envelope / adapter_signature

        Returns:
            tuple[EventDecision, dict[str, Any]]: 决策与参数
        """
        if not params:
            return EventDecision.PASS, params

        # 检查功能是否启用
        config = getattr(self.plugin, "config", None)
        if not isinstance(config, EmojiStickerConfig):
            return EventDecision.PASS, params
        if not config.general.enabled or not config.steal.enabled:
            return EventDecision.PASS, params

        service = getattr(self.plugin, "emoji_service", None)
        if not isinstance(service, EmojiService):
            return EventDecision.PASS, params

        message = params.get("message")
        if not message:
            return EventDecision.PASS, params

        # 从 message.extra 获取媒体列表
        media_list: list[dict[str, str]] = getattr(message, "extra", {}).get("media", [])
        if not media_list:
            return EventDecision.PASS, params

        # 过滤出表情包类型
        emoji_media = [m for m in media_list if m.get("type") == "emoji"]
        if not emoji_media:
            return EventDecision.PASS, params

        # 容量检查
        current_count = await service.get_registered_count()
        max_count = config.general.max_registered
        if current_count >= max_count and not config.general.do_replace:
            return EventDecision.PASS, params

        for media_item in emoji_media:
            try:
                await self._steal_one(media_item, config, service)
            except Exception as e:
                logger.debug(f"收集表情包失败: {e}")

        return EventDecision.SUCCESS, params

    async def _steal_one(
        self,
        media_item: dict[str, str],
        config: EmojiStickerConfig,
        service: EmojiService,
    ) -> None:
        """尝试收集单个表情包。

        Args:
            media_item: 媒体数据字典 {"type": "emoji", "data": "base64|..."}
            config: 插件配置
            service: 表情包服务
        """
        raw_data = media_item.get("data", "")
        if not raw_data:
            return

        # 提取纯净 base64
        clean_b64 = _extract_clean_base64(raw_data)
        if not clean_b64:
            return

        # 解码为二进制
        try:
            binary = base64.b64decode(clean_b64)
        except Exception:
            return

        # 计算 MD5 哈希（与插件扫描注册保持一致）
        file_hash = hashlib.md5(binary).hexdigest()

        # 去重检查
        if await service.check_exists(file_hash):
            return

        # 容量 + LRU 淘汰检查
        from ..scanner import enforce_capacity
        await enforce_capacity(config, service)

        # 尝试获取框架已缓存的描述
        description = ""
        if config.steal.use_framework_description:
            description = await self._get_framework_description(clean_b64)

        # 框架无缓存 → 用插件自己的 VLM 描述
        if not description:
            description = await self._describe_with_plugin_vlm(clean_b64, config)

        if not description:
            logger.debug(f"表情包描述生成失败，跳过: {file_hash[:8]}...")
            return

        # 保存文件到注册目录
        registered_dir = config.general.emoji_registered_dir
        await asyncio.to_thread(os.makedirs, registered_dir, exist_ok=True)

        # 推断扩展名（简单判断 PNG 签名）
        ext = ".png" if binary[:4] == b"\x89PNG" else ".jpg"
        if binary[:4] == b"GIF8":
            ext = ".gif"
        if binary[:4] == b"RIFF":
            ext = ".webp"

        dest_path = os.path.join(registered_dir, f"{file_hash}{ext}")
        await asyncio.to_thread(_write_binary, dest_path, binary)

        # 注册到数据库
        fmt = ext.lstrip(".")
        if fmt == "jpg":
            fmt = "jpeg"

        emoji = await service.register_emoji(
            emoji_hash=file_hash,
            full_path=dest_path,
            description=description,
            fmt=fmt,
        )
        if emoji:
            logger.info(f"🎯 偷到表情包: {file_hash[:8]}... - {description[:30]}")

    @staticmethod
    async def _get_framework_description(clean_b64: str) -> str:
        """通过 media_api 查询框架已缓存的表情包描述。

        Args:
            clean_b64: 纯净 base64 字符串

        Returns:
            描述文本，无缓存返回空字符串
        """
        try:
            from src.app.plugin_system.api.media_api import get_media_info

            # MediaManager 使用 SHA256(base64 string) 作为哈希
            media_hash = hashlib.sha256(clean_b64.encode()).hexdigest()
            info = await get_media_info(media_hash)
            if info and info.get("description"):
                return info["description"]
        except Exception as e:
            logger.debug(f"查询框架缓存失败: {e}")
        return ""

    @staticmethod
    async def _describe_with_plugin_vlm(
        clean_b64: str,
        config: EmojiStickerConfig,
    ) -> str:
        """使用插件自身的 VLM prompt 生成描述。

        Args:
            clean_b64: 纯净 base64 字符串
            config: 插件配置

        Returns:
            描述文本，失败返回空字符串
        """
        try:
            from ..scanner import describe_emoji

            result = await describe_emoji(
                image_base64_list=[clean_b64],
                is_gif=False,
                config=config,
            )
            if result and result.get("description"):
                # 内容审核
                if config.scan.content_filtration and not result.get("is_compliant", True):
                    return ""
                return result["description"]
        except Exception as e:
            logger.debug(f"插件 VLM 描述失败: {e}")
        return ""


def _extract_clean_base64(data: str) -> str:
    """提取纯净 base64 数据。

    Args:
        data: 可能带前缀的 base64 字符串

    Returns:
        纯净 base64 字符串
    """
    if data.startswith("data:"):
        if "base64," in data:
            data = data.split("base64,", 1)[1]
    elif data.startswith("base64|"):
        data = data[7:]
    return data.replace("\n", "").replace("\r", "").replace(" ", "")


def _write_binary(path: str, data: bytes) -> None:
    """写入二进制文件。

    Args:
        path: 文件路径
        data: 二进制数据
    """
    with open(path, "wb") as f:
        f.write(data)

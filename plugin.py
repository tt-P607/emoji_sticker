"""EmojiSticker 插件入口。

注册插件、加载配置、初始化 Scheduler 扫描任务。
"""

from __future__ import annotations

from src.app.plugin_system.api.log_api import get_logger
from src.core.components.base import BasePlugin
from src.core.components.loader import register_plugin
from src.kernel.concurrency import get_task_manager

from .actions.send_emoji import SendEmojiAction
from .config import EmojiStickerConfig
from .handlers.steal_emoji import StealEmojiHandler
from .services.emoji_service import EmojiService

logger = get_logger("emoji_sticker")


@register_plugin
class EmojiStickerPlugin(BasePlugin):
    """通用表情包管理与发送插件。

    支持定时扫描注册、VLM 描述生成、智能选择发送、LRU 容量控制。
    """

    plugin_name = "emoji_sticker"
    plugin_version = "1.0.0"
    plugin_author = "MoFox Studio"
    plugin_description = "通用表情包管理与发送插件，支持智能选择和自动注册"
    configs = [EmojiStickerConfig]

    def __init__(self, config: EmojiStickerConfig | None = None) -> None:
        """初始化插件。

        Args:
            config: 插件配置实例
        """
        super().__init__(config)
        self.emoji_service: EmojiService | None = None

    async def on_plugin_loaded(self) -> None:
        """插件加载后初始化服务实例，并延迟注册 scheduler 扫描任务。"""
        # 数据库表自动创建 & 结构对齐
        try:
            from src.core.utils.schema_sync import enforce_database_schema_consistency

            from .models import Emoji

            stats = await enforce_database_schema_consistency(Emoji.metadata)
            logger.info(
                f"数据库 Schema 同步完成: "
                f"tables={stats.tables_checked}, "
                f"add={stats.columns_added}, drop={stats.columns_removed}"
            )
        except Exception as e:
            logger.error(f"数据库 Schema 同步失败: {e}", exc_info=True)

        # 初始化 EmojiService 实例
        self.emoji_service = EmojiService(self)
        logger.info("EmojiService 已初始化")

        # 确保表情包目录存在（首次运行时自动创建）
        config = self.config
        if isinstance(config, EmojiStickerConfig) and config.general.enabled:
            import asyncio
            import os

            await asyncio.to_thread(
                os.makedirs, config.general.emoji_dir, exist_ok=True
            )
            await asyncio.to_thread(
                os.makedirs, config.general.emoji_registered_dir, exist_ok=True
            )
            logger.debug(
                f"表情包目录已确认: {config.general.emoji_dir}, "
                f"{config.general.emoji_registered_dir}"
            )

        # 延迟注册调度器任务：等待调度器启动
        async def _delayed_scheduler_register() -> None:
            """延迟注册调度器任务，等待调度器启动。"""
            import asyncio

            for _ in range(30):
                await asyncio.sleep(1.0)
                try:
                    from src.kernel.scheduler import get_unified_scheduler

                    scheduler = get_unified_scheduler()
                    if scheduler._running:
                        await self._register_scheduler_tasks()
                        return
                except ImportError:
                    logger.warning("Scheduler 不可用，放弃注册扫描任务")
                    return
            logger.warning("等待调度器启动超时(30s)，放弃注册扫描任务")

        get_task_manager().create_task(
            _delayed_scheduler_register(),
            name="emoji_sticker_scheduler_init",
            daemon=True,
        )

        logger.info("EmojiSticker 插件已加载")

    async def _register_scheduler_tasks(self) -> None:
        """注册定时扫描任务到 scheduler。"""
        config = self.config
        if not isinstance(config, EmojiStickerConfig):
            return

        if not config.general.enabled:
            logger.info("插件已禁用，跳过扫描任务注册")
            return

        try:
            from src.kernel.scheduler import TriggerType, get_unified_scheduler

            scheduler = get_unified_scheduler()
        except ImportError:
            logger.warning("Scheduler 不可用，跳过扫描任务注册")
            return

        # 构建扫描回调
        async def _scan_callback() -> None:
            """scheduler 调度的扫描回调。"""
            from .scanner import scan_and_register

            if self.emoji_service is not None:
                await scan_and_register(config, self.emoji_service)

        # 注册周期性扫描任务
        delay_seconds = config.scan.interval_minutes * 60
        await scheduler.create_schedule(
            callback=_scan_callback,
            trigger_type=TriggerType.TIME,
            trigger_config={"delay_seconds": delay_seconds},
            is_recurring=True,
            task_name="emoji_sticker_scan",
            force_overwrite=True,
        )

        logger.info(
            f"扫描任务已注册，间隔 {config.scan.interval_minutes} 分钟"
        )

        # 启动时立即执行一次首次扫描
        logger.info("执行启动首次扫描...")
        try:
            await _scan_callback()
        except Exception as e:
            logger.warning(f"启动首次扫描失败（不影响后续定时扫描）: {e}")

    def get_components(self) -> list[type]:
        """获取插件内所有组件类。

        Returns:
            组件类列表
        """
        return [
            SendEmojiAction,
            EmojiService,
            StealEmojiHandler,
        ]

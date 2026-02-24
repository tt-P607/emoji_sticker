"""表情包插件配置定义。

定义插件所有可配置参数，基于 Pydantic + TOML 热重载。
通过 @config_section 划分为语义清晰的 Section。
"""

from __future__ import annotations

from typing import ClassVar

from src.core.components.base.config import BaseConfig, Field, SectionBase, config_section


class EmojiStickerConfig(BaseConfig):
    """表情包插件配置。"""

    config_name: ClassVar[str] = "config"
    config_description: ClassVar[str] = "表情包插件配置"

    @config_section("general")
    class GeneralSection(SectionBase):
        """基础配置。"""

        enabled: bool = Field(
            default=True,
            description="是否启用插件",
        )
        emoji_dir: str = Field(
            default="data/emoji",
            description="待注册表情包目录（从此目录扫描新表情）",
        )
        emoji_registered_dir: str = Field(
            default="data/emoji_registed",
            description="已注册表情包存储目录（注册后文件移动至此）",
        )
        max_registered: int = Field(
            default=200,
            description="最大注册数量",
        )
        do_replace: bool = Field(
            default=True,
            description="达到上限时是否自动替换（LRU 淘汰最久未用的）",
        )

    @config_section("scan")
    class ScanSection(SectionBase):
        """扫描配置。"""

        interval_minutes: int = Field(
            default=5,
            description="扫描间隔（分钟）",
        )
        content_filtration: bool = Field(
            default=False,
            description="是否启用 VLM 内容审核（过滤不合规图片）",
        )
        filtration_prompt: str = Field(
            default=(
                "请判断这张图片是否适合作为聊天表情包使用。"
                "合规标准：无暴力、无色情、无政治敏感内容、无恶意攻击性内容。"
            ),
            description="内容审核辅助提示词",
        )

    @config_section("selection")
    class SelectionSection(SectionBase):
        """选择配置。"""

        max_candidates: int = Field(
            default=20,
            description="传给模型进行选择的最大候选表情包数量",
        )
        model_task: str = Field(
            default="utils",
            description="用于选择的模型任务名（对应 model.toml 中的 task 配置）",
        )

    @config_section("steal")
    class StealSection(SectionBase):
        """偷表情包配置。"""

        enabled: bool = Field(
            default=False,
            description="是否启用偷表情包功能（自动收集聊天中收到的表情包）",
        )
        use_framework_description: bool = Field(
            default=True,
            description="优先使用框架 MediaManager 已识别的描述（避免重复 VLM 调用）",
        )

    @config_section("debug")
    class DebugSection(SectionBase):
        """调试配置。"""

        show_selection_prompt: bool = Field(
            default=False,
            description="是否在日志中显示完整的选择 prompt（调试用）",
        )

    general: GeneralSection = Field(default_factory=GeneralSection)
    scan: ScanSection = Field(default_factory=ScanSection)
    selection: SelectionSection = Field(default_factory=SelectionSection)
    steal: StealSection = Field(default_factory=StealSection)
    debug: DebugSection = Field(default_factory=DebugSection)

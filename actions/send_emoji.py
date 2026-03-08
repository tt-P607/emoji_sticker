"""表情包发送动作。

通过 LLM Tool Calling 接收意图描述，智能选择并发送最匹配的表情包。
"""

from __future__ import annotations

import asyncio
import base64
import os
import random
from typing import Annotated, Any

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.send_api import send_emoji
from src.core.components.base.action import BaseAction
from src.core.components.types import ChatType
from src.core.config import get_core_config, get_model_config
from src.kernel.llm import ROLE, LLMPayload, LLMRequest, Text

from ..models import Emoji
from ..prompts import build_selection_prompt
from ..services.emoji_service import EmojiService

logger = get_logger("emoji_sticker.action")


class SendEmojiAction(BaseAction):
    """根据意图描述，智能选择并发送一个表情包。

    使用要求：
    - **触发时机**：想用表情包配合文字、或单独发一个表情包时；情绪渲染（开心/无语/害羞/调皮）用表情包效果最佳。
    - **合理频率**：不用每条消息都配表情包，自然点缀即可，避免刷屏。
    - **与文字搭配**：可以先发文字再配一个表情包，也可单独只发表情包。
    """

    action_name = "send_emoji"
    action_description = "根据意图描述，智能选择并发送一个表情包"
    primary_action = False
    chatter_allow: list[str] = []  # 空列表 = 所有 Chatter 可用
    chat_type: ChatType = ChatType.ALL

    async def execute(
        self,
        intent: Annotated[str, "你想通过表情包表达的意图或情感描述，如'开心地打招呼'、'表示无语'"],
    ) -> tuple[bool, str]:
        """执行表情包选择与发送。

        Args:
            intent: 意图描述文本

        Returns:
            (是否成功, 结果详情)
        """
        if not intent or not intent.strip():
            return False, "意图描述为空"

        intent = intent.strip()

        # 获取配置和服务
        config = getattr(self.plugin, "config", None)
        if config is None:
            logger.error("无法获取插件配置")
            return False, "插件配置不可用"

        service = getattr(self.plugin, "emoji_service", None)
        if not isinstance(service, EmojiService):
            logger.error("无法获取 EmojiService 实例")
            return False, "表情包服务不可用"

        # 查询所有活跃表情包
        active_emojis = await service.get_active_emojis()
        if not active_emojis:
            return False, "无可用表情包"

        logger.info(f"📋 表情包选择: 活跃 {len(active_emojis)} 个, 意图: '{intent}'")

        # 预筛选候选
        max_candidates = config.selection.max_candidates
        candidates = _pre_filter_candidates(active_emojis, intent, max_candidates)

        if not candidates:
            return False, "无匹配的候选表情包"

        logger.debug(
            f"预筛选: {len(candidates)} 个候选"
        )

        # 构建候选列表
        candidate_list: list[dict[str, str | int]] = []
        for i, emoji_item in enumerate(candidates, start=1):
            candidate_list.append({
                "no": i,
                "description": emoji_item.description,
                "usage_count": emoji_item.usage_count,
            })

        # 获取聊天上下文
        chat_context = self._get_recent_chat_content(max_messages=20)

        # 获取人设信息
        core_cfg = get_core_config()
        persona_nickname = core_cfg.personality.nickname
        persona_personality = core_cfg.personality.personality_core

        # 构建选择 prompt
        selection_prompt = build_selection_prompt(
            intent=intent,
            chat_context=chat_context,
            persona_nickname=persona_nickname,
            persona_personality=persona_personality,
            candidates=candidate_list,
        )

        if config.debug.show_selection_prompt:
            logger.debug(f"选择 Prompt:\n{selection_prompt}")

        # 调用 LLM 选择
        try:
            model_task = config.selection.model_task
            model_set = get_model_config().get_task(model_task)

            llm_request = LLMRequest(
                model_set=model_set,
                request_name="emoji_selection",
            )
            llm_request.add_payload(LLMPayload(ROLE.USER, Text(selection_prompt)))

            # LLM 选择前发送心跳，防止 WatchDog 警告
            try:
                from src.kernel.concurrency import get_watchdog

                get_watchdog().feed_dog(stream_id=self.chat_stream.stream_id)
            except Exception:
                pass  # 心跳失败不影响核心逻辑

            llm_response = await llm_request.send(stream=False)
            response_text = (await llm_response).strip()
            logger.debug(f"LLM 选择响应: '{response_text}'")

            # LLM 选择后再次发送心跳
            try:
                get_watchdog().feed_dog(stream_id=self.chat_stream.stream_id)
            except Exception:
                pass
        except Exception as e:
            logger.error(f"LLM 选择调用失败: {e}")
            return False, "选择模型调用失败"

        # 解析编号
        selected_idx = _parse_selection_number(response_text, len(candidates))

        # LLM 返回 0 或无法解析时，回退到随机选择（因为 prompt 已要求必选）
        if selected_idx is None or selected_idx == 0:
            logger.warning(
                f"LLM 选择异常 (返回: '{response_text}'), "
                f"候选数: {len(candidates)}, 回退到随机选择"
            )
            selected_idx = random.randint(1, len(candidates))

        # 获取选中的表情包
        selected_emoji = candidates[selected_idx - 1]

        # 读取图片数据并发送
        file_path = selected_emoji.full_path
        if not file_path or not os.path.exists(file_path):
            logger.warning(f"表情包文件不存在: {file_path}")
            return False, "表情包文件丢失"

        try:
            image_data = await asyncio.to_thread(_read_file_base64, file_path)
            if not image_data:
                return False, "读取表情包文件失败"

            stream_id = self.chat_stream.stream_id
            # 历史记录只保留精炼描述，截取第一句或前50字
            short_desc = selected_emoji.description.split("\n")[0][:50]
            success = await send_emoji(
                emoji_data=image_data,
                stream_id=stream_id,
                processed_plain_text=f"[表情包: {short_desc}]",
            )

            if success:
                # 记录使用
                await service.record_usage(selected_emoji.emoji_hash)
                desc_preview = selected_emoji.description[:30]
                logger.info(f"✅ 选中并发送: #{selected_idx} - {desc_preview}")
                return True, f"已发送表情包：{desc_preview}"
            else:
                return False, "表情包发送失败"
        except Exception as e:
            logger.error(f"发送表情包失败: {e}")
            return False, f"发送失败: {e}"


def _pre_filter_candidates(
    emojis: list[Emoji],
    intent: str,
    max_candidates: int,
) -> list[Emoji]:
    """预筛选候选表情包。

    按 description 与 intent 的关键词文本包含匹配度评分，
    优先选匹配度高的，不足则随机补足至 max_candidates。

    Args:
        emojis: 全部活跃表情包列表
        intent: 意图描述
        max_candidates: 最大候选数

    Returns:
        筛选后的候选列表
    """
    if len(emojis) <= max_candidates:
        return emojis

    # 提取意图关键词（按字符拆分为若干搜索词）
    keywords = _extract_keywords(intent)

    # 对每个表情包评分
    scored: list[tuple[Emoji, int]] = []
    for emoji_item in emojis:
        desc = emoji_item.description.lower()
        score = sum(1 for kw in keywords if kw in desc)
        scored.append((emoji_item, score))

    # 按评分降序排列
    scored.sort(key=lambda x: x[1], reverse=True)

    # 取有匹配的候选
    matched = [item for item, s in scored if s > 0]
    unmatched = [item for item, s in scored if s == 0]

    if len(matched) >= max_candidates:
        return matched[:max_candidates]

    # 匹配不足，随机补足
    need = max_candidates - len(matched)
    if len(unmatched) <= need:
        return matched + unmatched
    else:
        return matched + random.sample(unmatched, need)


def _extract_keywords(text: str) -> list[str]:
    """从文本中提取关键词用于匹配。

    简单分词：按常见分隔符拆分，过滤过短的词。

    Args:
        text: 原始文本

    Returns:
        关键词列表（小写）
    """
    import re

    # 按标点、空格、符号分割
    tokens = re.split(r"[\s,，。！？!?、；;：:""''\"'\(\)（）\[\]【】{}]+", text)
    # 过滤空串和过短的词
    keywords = [t.lower() for t in tokens if len(t) >= 2]

    # 如果没有提取到关键词，用整个文本
    if not keywords:
        keywords = [text.lower()]

    return keywords


def _parse_selection_number(text: str, max_idx: int) -> int | None:
    """解析 LLM 返回的编号。

    支持纯数字、带文字的数字提取。

    Args:
        text: LLM 原始响应文本
        max_idx: 最大合法编号

    Returns:
        选择的编号（0 表示不选），None 表示解析失败
    """
    import re

    text = text.strip()

    # 尝试直接解析为数字
    try:
        num = int(text)
        if 0 <= num <= max_idx:
            return num
        return None
    except ValueError:
        pass

    # 从文本中提取数字
    numbers = re.findall(r"\d+", text)
    if numbers:
        num = int(numbers[0])
        if 0 <= num <= max_idx:
            return num

    return None


def _read_file_base64(file_path: str) -> str:
    """读取文件并返回 Base64 编码字符串。

    Args:
        file_path: 文件路径

    Returns:
        Base64 字符串，失败返回空字符串
    """
    try:
        with open(file_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    except Exception as e:
        logger.error(f"读取文件失败 ({file_path}): {e}")
        return ""

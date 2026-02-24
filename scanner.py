"""表情包扫描与注册逻辑。

提供定时扫描目录、VLM 描述生成、容量控制等功能。
由 scheduler 周期性调用 scan_and_register()。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import os
import shutil
from typing import Any

import json_repair
from PIL import Image as PILImage

from src.app.plugin_system.api.log_api import get_logger
from src.core.config import get_model_config
from src.kernel.llm import LLMRequest, LLMPayload, ROLE, Text, Image

from .config import EmojiStickerConfig
from .prompts import VLM_DESCRIBE_GIF_HINT, build_vlm_describe_prompt
from .services.emoji_service import EmojiService

logger = get_logger("emoji_sticker.scanner")

# 支持的图片扩展名
_SUPPORTED_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp"})


async def scan_and_register(config: EmojiStickerConfig, service: EmojiService) -> None:
    """扫描目录并注册新的表情包。

    被 scheduler 周期调用的主函数。

    Args:
        config: 插件配置
        service: 表情包服务实例
    """
    if not config.general.enabled:
        return

    emoji_dir = config.general.emoji_dir
    registered_dir = config.general.emoji_registered_dir

    # 确保目录存在
    await asyncio.to_thread(_ensure_directories, emoji_dir, registered_dir)

    # === 一致性检查：统计数据库状态，清理孤立记录 ===
    db_total = await service.get_registered_count()
    registered_files = await asyncio.to_thread(list_image_files, registered_dir)
    file_count = len(registered_files)

    logger.info(
        f"📊 表情包库状态: 数据库 {db_total} 条, 已注册目录 {file_count} 个文件"
    )

    if db_total > 0:
        all_emojis = await service.get_active_emojis()
        orphan_count = 0
        valid_count = 0

        for emoji_record in all_emojis:
            file_exists = await asyncio.to_thread(
                os.path.exists, emoji_record.full_path
            )
            if file_exists:
                valid_count += 1
            else:
                logger.debug(
                    f"孤立记录: {emoji_record.emoji_hash[:8]}... "
                    f"文件不存在: {emoji_record.full_path}"
                )
                await service.delete_emoji(emoji_record.emoji_hash)
                orphan_count += 1

        if orphan_count > 0:
            logger.info(
                f"🧹 一致性检查: {valid_count} 条有效, "
                f"{orphan_count} 条孤立记录已清理"
            )
        else:
            logger.info(
                f"✅ 一致性检查通过: {valid_count} 条记录全部有效"
            )

    # === 反向检查：文件存在但数据库无记录 → 删除孤儿文件 ===
    if db_total > 0 or file_count > 0:
        all_emojis_for_reverse = await service.get_active_emojis()
        db_paths: set[str] = set()
        for emoji_record in all_emojis_for_reverse:
            if emoji_record.full_path:
                db_paths.add(os.path.abspath(emoji_record.full_path))

        orphan_file_count = 0
        for file_path in registered_files:
            abs_path = os.path.abspath(file_path)
            if abs_path not in db_paths:
                logger.debug(
                    f"孤儿文件: {os.path.basename(file_path)} "
                    f"（目录中存在但数据库无记录）"
                )
                try:
                    await asyncio.to_thread(os.remove, file_path)
                    orphan_file_count += 1
                except OSError as e:
                    logger.warning(f"删除孤儿文件失败 ({file_path}): {e}")

        if orphan_file_count > 0:
            logger.info(
                f"🧹 反向检查: 清理了 {orphan_file_count} 个孤儿文件"
            )
    # === 反向检查结束 ===

    # === 一致性检查结束 ===

    # 列举待注册图片
    image_files = await asyncio.to_thread(list_image_files, emoji_dir)
    if not image_files:
        logger.debug("扫描完成，无新表情包文件")
        return

    # 单次扫描最多注册 20 个，避免大量文件一次性涌入导致阻塞
    max_per_scan = 20

    logger.info(
        f"发现 {len(image_files)} 个待注册的表情包文件"
        + (f"，本次最多处理 {max_per_scan} 个" if len(image_files) > max_per_scan else "")
    )

    registered_count = 0
    skipped_count = 0
    failed_count = 0

    for file_path in image_files:
        # 达到单次注册上限后停止，剩余文件下次扫描处理
        if registered_count >= max_per_scan:
            remaining = len(image_files) - (registered_count + skipped_count + failed_count)
            if remaining > 0:
                logger.info(f"已达单次注册上限 ({max_per_scan})，剩余 {remaining} 个留待下次扫描")
            break
        try:
            # 计算文件哈希
            file_hash = await asyncio.to_thread(compute_file_hash, file_path)
            if not file_hash:
                failed_count += 1
                continue

            # 检查是否已存在
            exists = await service.check_exists(file_hash)
            if exists:
                # 已存在，删除源文件
                try:
                    await asyncio.to_thread(os.remove, file_path)
                except OSError:
                    pass
                skipped_count += 1
                continue

            # 准备 VLM 描述所需的图片数据
            file_ext = os.path.splitext(file_path)[1].lower()
            is_gif = file_ext == ".gif"

            if is_gif:
                # GIF: 提取关键帧
                keyframes = await asyncio.to_thread(extract_gif_keyframes, file_path)
                if not keyframes:
                    logger.warning(f"GIF 关键帧提取失败: {os.path.basename(file_path)}")
                    failed_count += 1
                    continue
                image_base64_list = keyframes
            else:
                # 静态图片: 读取为 base64
                raw_base64 = await asyncio.to_thread(_read_file_base64, file_path)
                if not raw_base64:
                    failed_count += 1
                    continue
                image_base64_list = [raw_base64]

            # 推断格式
            fmt = _get_format_from_ext(file_ext)

            # 调用 VLM 生成描述
            vlm_result = await describe_emoji(
                image_base64_list=image_base64_list,
                is_gif=is_gif,
                config=config,
            )

            if vlm_result is None:
                logger.warning(f"VLM 描述返回为空: {os.path.basename(file_path)}")
                failed_count += 1
                continue

            # 内容审核
            if config.scan.content_filtration and not vlm_result.get("is_compliant", True):
                logger.info(f"表情包内容审核不通过，已跳过: {os.path.basename(file_path)}")
                try:
                    await asyncio.to_thread(os.remove, file_path)
                except OSError:
                    pass
                skipped_count += 1
                continue

            description = vlm_result.get("description", "")

            # 移动文件到注册目录
            dest_path = await asyncio.to_thread(
                _move_file_to_registered, file_path, registered_dir, file_hash, file_ext
            )
            if not dest_path:
                failed_count += 1
                continue

            # 注册到数据库
            emoji = await service.register_emoji(
                emoji_hash=file_hash,
                full_path=dest_path,
                description=description,
                fmt=fmt,
            )
            if emoji:
                registered_count += 1
            else:
                failed_count += 1

        except Exception as e:
            logger.error(f"处理表情包文件失败 ({os.path.basename(file_path)}): {e}")
            failed_count += 1

    logger.info(
        f"扫描注册完成: 成功 {registered_count}, 跳过 {skipped_count}, 失败 {failed_count}"
    )

    # 容量控制
    await enforce_capacity(config, service)


async def enforce_capacity(config: EmojiStickerConfig, service: EmojiService) -> None:
    """容量控制，按 LRU 淘汰超出上限的表情包。

    Args:
        config: 插件配置
        service: 表情包服务实例
    """
    if not config.general.do_replace:
        return

    current_count = await service.get_registered_count()
    max_count = config.general.max_registered

    if current_count <= max_count:
        return

    excess = current_count - max_count
    logger.info(f"表情包数量 ({current_count}) 超过上限 ({max_count})，准备淘汰 {excess} 个")

    lru_emojis = await service.get_lru_emojis(excess)
    deleted_count = 0

    for emoji in lru_emojis:
        success = await service.delete_emoji(emoji.emoji_hash)
        if success:
            deleted_count += 1

    logger.info(f"LRU 淘汰完成: 已删除 {deleted_count} 个表情包")


async def describe_emoji(
    image_base64_list: list[str],
    is_gif: bool,
    config: EmojiStickerConfig,
) -> dict[str, Any] | None:
    """调用 VLM 分析表情包图片，生成描述和合规性判断。

    Args:
        image_base64_list: 图片 base64 列表（静态图为单元素，GIF 为关键帧列表）
        is_gif: 是否为 GIF 动图
        config: 插件配置

    Returns:
        包含 description 和 is_compliant 的字典，失败返回 None
    """
    try:
        # 获取 VLM 模型配置
        model_set = get_model_config().get_task("vlm")

        # 构建描述 prompt
        prompt_text = build_vlm_describe_prompt(
            filtration_enabled=config.scan.content_filtration,
            filtration_prompt=config.scan.filtration_prompt,
        )

        if is_gif:
            prompt_text = VLM_DESCRIBE_GIF_HINT + "\n\n" + prompt_text

        # 构建 payload 内容列表：文本 + 图片
        content_parts: list[Text | Image] = [Text(prompt_text)]
        for img_b64 in image_base64_list:
            # 确保 base64 有正确的 data URL 前缀
            if not img_b64.startswith("data:"):
                img_b64 = f"data:image/jpeg;base64,{img_b64}"
            content_parts.append(Image(img_b64))

        # 创建 LLM 请求
        llm_request = LLMRequest(
            model_set=model_set,
            request_name="emoji_vlm_describe",
        )
        llm_request.add_payload(LLMPayload(ROLE.USER, content_parts))

        # 发送请求
        llm_response = await llm_request.send(stream=False)
        response_text = (await llm_response).strip()

        # 解析 JSON 响应
        parsed = _parse_json_response(response_text)
        if parsed and "description" in parsed:
            return parsed

        logger.warning(f"VLM 返回的 JSON 解析失败: {response_text[:200]}")
        return None

    except Exception as e:
        logger.error(f"VLM 描述生成失败: {e}")
        return None


def extract_gif_keyframes(gif_path: str, max_frames: int = 3) -> list[str]:
    """从 GIF 提取关键帧并转换为 JPEG Base64。

    使用 Pillow 均匀间隔选取关键帧，转换为 JPEG 格式的 Base64 字符串。
    仅用于 VLM 描述阶段，发送时仍使用完整 GIF。

    Args:
        gif_path: GIF 文件路径
        max_frames: 最大提取帧数，默认 3

    Returns:
        JPEG Base64 字符串列表（不含 data URL 前缀）
    """
    try:
        img = PILImage.open(gif_path)
        if not hasattr(img, "n_frames") or img.n_frames <= 1:
            # 单帧 GIF，当作普通图片处理
            buffer = io.BytesIO()
            img.convert("RGB").save(buffer, format="JPEG", quality=85)
            return [base64.b64encode(buffer.getvalue()).decode("utf-8")]

        total_frames = img.n_frames
        # 均匀间隔选取帧
        if total_frames <= max_frames:
            frame_indices = list(range(total_frames))
        else:
            step = total_frames / max_frames
            frame_indices = [int(i * step) for i in range(max_frames)]

        keyframes: list[str] = []
        for idx in frame_indices:
            try:
                img.seek(idx)
                frame = img.convert("RGB")
                buffer = io.BytesIO()
                frame.save(buffer, format="JPEG", quality=85)
                b64_str = base64.b64encode(buffer.getvalue()).decode("utf-8")
                keyframes.append(b64_str)
            except EOFError:
                break
            except Exception as e:
                logger.warning(f"提取 GIF 第 {idx} 帧失败: {e}")
                continue

        return keyframes

    except Exception as e:
        logger.error(f"GIF 关键帧提取失败 ({gif_path}): {e}")
        return []


def compute_file_hash(file_path: str) -> str:
    """计算文件内容的 MD5 哈希。

    Args:
        file_path: 文件路径

    Returns:
        MD5 哈希的十六进制字符串，失败返回空字符串
    """
    try:
        md5 = hashlib.md5()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                md5.update(chunk)
        return md5.hexdigest()
    except Exception as e:
        logger.error(f"计算文件哈希失败 ({file_path}): {e}")
        return ""


def list_image_files(directory: str) -> list[str]:
    """列出目录下所有支持的图片文件。

    Args:
        directory: 目录路径

    Returns:
        图片文件的完整路径列表
    """
    if not os.path.isdir(directory):
        return []

    result: list[str] = []
    try:
        for filename in os.listdir(directory):
            ext = os.path.splitext(filename)[1].lower()
            if ext in _SUPPORTED_EXTENSIONS:
                result.append(os.path.join(directory, filename))
    except OSError as e:
        logger.error(f"列举图片文件失败 ({directory}): {e}")

    return result


def _ensure_directories(*dirs: str) -> None:
    """确保目录存在。"""
    for d in dirs:
        os.makedirs(d, exist_ok=True)


def _read_file_base64(file_path: str) -> str:
    """读取文件并返回 Base64 编码字符串（不含前缀）。

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


def _move_file_to_registered(
    src_path: str,
    registered_dir: str,
    file_hash: str,
    ext: str,
) -> str:
    """移动文件到注册目录。

    使用哈希值作为文件名，避免冲突。

    Args:
        src_path: 源文件路径
        registered_dir: 注册目录路径
        file_hash: 文件哈希值
        ext: 文件扩展名（含点号）

    Returns:
        目标文件完整路径，失败返回空字符串
    """
    try:
        dest_path = os.path.join(registered_dir, f"{file_hash}{ext}")
        shutil.move(src_path, dest_path)
        return dest_path
    except Exception as e:
        logger.error(f"移动文件失败 ({src_path} -> {registered_dir}): {e}")
        return ""


def _get_format_from_ext(ext: str) -> str:
    """从扩展名推断图片格式。

    Args:
        ext: 文件扩展名（含点号，如 '.jpg'）

    Returns:
        格式字符串（如 'jpeg'）
    """
    mapping = {
        ".jpg": "jpeg",
        ".jpeg": "jpeg",
        ".png": "png",
        ".gif": "gif",
        ".webp": "webp",
    }
    return mapping.get(ext.lower(), "jpeg")


def _parse_json_response(text: str) -> dict[str, Any] | None:
    """解析 LLM 返回的 JSON 文本。

    支持从 markdown 代码块中提取 JSON。

    Args:
        text: 原始响应文本

    Returns:
        解析后的字典，失败返回 None
    """
    try:
        # 尝试直接解析
        result = json_repair.loads(text)
        if isinstance(result, dict):
            return result

        # 尝试从代码块中提取
        import re

        json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
        if json_match:
            result = json_repair.loads(json_match.group(1))
            if isinstance(result, dict):
                return result

        return None
    except Exception:
        return None

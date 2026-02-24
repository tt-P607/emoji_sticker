"""表情包 Prompt 模板。

包含 VLM 描述 prompt 和 LLM 选择 prompt 的模板与构建函数。
"""

# =============================================================================
# VLM 描述 Prompt
# =============================================================================

VLM_DESCRIBE_PROMPT = """\
这是一个表情包。请分析图片内容，以 JSON 格式返回结果。

description 要求：
- 概括表情包传达的核心情绪、氛围和画面主要特征。
- 如果能识别出角色来源（如具体作品、角色名），请写入描述。
- 准确复述图中所有文字，格式为：文字：'逐字抄录'，放在描述末尾。
- 描述部分不超过 40 字（图中文字复述不计入字数限制）。
- 无文字则省略文字部分。

is_compliant：适合日常聊天为 true，含暴力/色情/政治敏感内容为 false。

{filtration_hint}

只返回 JSON：
```json
{{
    "description": "描述内容，文字：'图中文字'",
    "is_compliant": true
}}
```\
"""

VLM_DESCRIBE_GIF_HINT = "注意：这是一个 GIF 动图表情包的关键帧截图，请综合所有帧的内容进行描述。"

# =============================================================================
# LLM 选择 Prompt
# =============================================================================

SELECTION_PROMPT_TEMPLATE = """\
你现在扮演"{persona_nickname}"，你的核心性格是：{persona_personality}。

## 当前对话上下文
{chat_context}

## 意图描述
对方或你自己想要表达的意图/情感：{intent}

## 候选表情包列表
以下是可供选择的表情包，每行格式为：编号. 描述（使用次数）

{candidate_list}

## 选择标准
你**必须**从候选列表中选出一个最合适的表情包，按以下优先级判断：
1. **语境贴合**：结合对话上下文，选择最符合当前聊天氛围和情感基调的表情包
2. **意图匹配**：表情包传达的情感应与意图描述尽量一致
3. **视觉表达力**：优先选择能直观传达情感、表达力强的表情包
4. **避免重复**：在同等匹配度下，优先选择使用次数较少的表情包
5. **人设一致**：表情包风格应符合你的性格特征

即使没有完美匹配，也要选择一个最接近或最中性、最通用的表情包。

## 输出要求
- 只回答一个数字编号（如 "3"）
- 不要输出任何其他内容\
"""


def build_vlm_describe_prompt(
    filtration_enabled: bool = True,
    filtration_prompt: str = "",
) -> str:
    """构建 VLM 描述 prompt。

    Args:
        filtration_enabled: 是否启用内容审核提示
        filtration_prompt: 自定义审核提示词

    Returns:
        完整的 VLM 描述 prompt 字符串
    """
    filtration_hint = ""
    if filtration_enabled and filtration_prompt:
        filtration_hint = f"额外审核标准：{filtration_prompt}"

    return VLM_DESCRIBE_PROMPT.format(filtration_hint=filtration_hint)


def build_selection_prompt(
    intent: str,
    chat_context: str,
    persona_nickname: str,
    persona_personality: str,
    candidates: list[dict[str, str | int]],
) -> str:
    """构建表情包选择 prompt。

    Args:
        intent: 意图/情感描述
        chat_context: 最近对话上下文文本
        persona_nickname: bot 昵称
        persona_personality: bot 核心性格描述
        candidates: 候选列表，每个元素包含 {"no": 编号, "description": 描述, "usage_count": 次数}

    Returns:
        完整的选择 prompt 字符串
    """
    # 构建候选列表文本
    candidate_lines: list[str] = []
    for c in candidates:
        candidate_lines.append(
            f"{c['no']}. {c['description']}（使用 {c['usage_count']} 次）"
        )
    candidate_list = "\n".join(candidate_lines)

    return SELECTION_PROMPT_TEMPLATE.format(
        persona_nickname=persona_nickname,
        persona_personality=persona_personality,
        chat_context=chat_context if chat_context else "（无最近对话记录）",
        intent=intent,
        candidate_list=candidate_list,
    )

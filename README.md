# emoji_sticker

Neo-MoFox 通用表情包管理与发送插件。自动扫描注册表情包文件，通过 VLM 生成语义描述，由 LLM 根据对话语境智能选择并发送。

## 功能特性

- **自动扫描注册** — 定时扫描指定目录，发现新表情包后调用 VLM 生成描述、移动至已注册目录、写入数据库
- **VLM 内容描述** — 每张表情包自动生成结构化描述（画面、文字、情绪、适用场景），供选择阶段使用
- **智能选择发送** — LLM Action，根据对话上下文从候选库中选出最贴合语境的表情包
- **LRU 容量控制** — 达到注册上限时自动淘汰最久未使用的表情包，腾出空间
- **内容审核（可选）** — 可启用 VLM 审核，自动过滤不合规图片
- **自动建表** — 首次启动自动创建/对齐数据库表结构，无需手动迁移

## 目录结构

```
emoji_sticker/
├── plugin.py              # 插件入口，初始化与调度器注册
├── manifest.json          # 插件清单
├── config.py              # 配置定义（Pydantic + TOML）
├── models.py              # 数据库模型（SQLAlchemy）
├── scanner.py             # 扫描器，文件发现 + VLM 描述生成
├── prompts.py             # VLM / LLM 提示词模板
├── __init__.py
├── actions/
│   └── send_emoji.py      # Action 组件，LLM 调用入口
└── services/
    ├── __init__.py
    └── emoji_service.py   # Service 组件，数据库 CRUD 封装
```

## 配置

配置文件位于 `config/plugins/emoji_sticker/config.toml`：

```toml
[general]
enabled = true
emoji_dir = "data/emoji"                  # 待注册目录（放新表情包到这里）
emoji_registered_dir = "data/emoji_registed"  # 已注册目录（自动移动）
max_registered = 200                      # 最大注册数量
do_replace = true                         # 满时 LRU 淘汰

[scan]
interval_minutes = 5                      # 扫描间隔
content_filtration = false                # VLM 内容审核开关
filtration_prompt = "请判断这张图片是否适合作为聊天表情包使用。..."

[selection]
max_candidates = 20                       # 候选数量上限
model_task = "utils"                      # 对应 model.toml 中的任务名

[debug]
show_selection_prompt = false             # 日志打印完整选择 prompt
```

## 使用方式

1. 将表情包图片（jpg/png/gif/webp）放入 `data/emoji/` 目录
2. 等待定时扫描（默认 5 分钟）或重启 Bot 触发首次扫描
3. 扫描器自动完成：VLM 描述生成 → 文件移动 → 数据库注册
4. 在对话中由 Chatter 通过 Tool Calling 自动触发 `send_emoji` Action

## 工作流程

```
用户消息 → Chatter 判断需要发表情 → 调用 send_emoji Action
                                          ↓
                               从数据库检索候选表情包
                                          ↓
                               LLM 根据上下文选择最佳
                                          ↓
                               读取文件 → base64 编码 → send_api 发送
```

## 依赖

- Neo-MoFox 核心框架 >= 1.0.0
- VLM 模型配置（`model.toml` 中需配置 `vlm` 任务，用于描述生成）
- LLM 模型配置（`model.toml` 中需配置 `utils` 任务，用于选择）

## 支持格式

JPEG、PNG、GIF、WebP

## 许可证

与 Neo-MoFox 主项目保持一致。

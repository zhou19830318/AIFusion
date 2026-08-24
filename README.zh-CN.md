# Fusion360-Add-Ins — Autodesk Fusion 360 AI 副驾驶

**[English](README.md) · **简体中文**

Fusion360-Add-Ins（AI Fusion）是 **Fusion 360 原生插件**，在 Fusion 内部嵌入一个停靠式 AI 聊天面板。用自然语言描述你要设计的内容——AI 会直接驱动 Fusion API 为你建模：草图、实体建模、特征编辑、并通过视口截图自动校验结果。

[点击这里访问我的博客](https://blog.csdn.net/acldy833/article/details/163825853?sharetype=blogdetail&sharerId=163825853&sharerefer=PC&sharesource=acldy833&spm=1011.2480.3001.8118)

---


## 功能特性

- **嵌入式聊天面板** — AI 副驾驶直接存在于 Fusion 内部，无需浏览器标签页
- **6 家 AI 提供商** — DeepSeek、Kimi（月之暗面）、智谱 GLM、OpenAI、Anthropic Claude、Google Gemini，全部使用最新旗舰模型（2026-08）
- **自动识别提供商** — 只配置了一个 API Key 时自动选中
- **视觉校验闭环** — 建模完成后自动抓取 Fusion 视口（`saveAsImageFile`）回传给支持视觉的模型检查设计，最多 3 轮修复
- **附件上传** — 可附加图片或 PDF（单张 ≤8MB，最多 4 个）作为设计参考；纯文本模型自动降级提示，不报错
- **按模型能力自适应** — 图片输入按模型门控（DeepSeek 官方 API、GLM-5.3 仅支持文本，服务端自动剥离图片而非报 400）
- **已验证的 API 知识库** — 系统提示中内置 Fusion API 精确签名，避免模型乱猜
- **模型别名** — 输入 `sonnet5`、`fable`、`gpt5.6`、`gemini-3.7` 会自动映射到真实模型 ID
- **依赖自动安装** — 缺少 `flask`/`requests` 时首次启动自动安装，并把真实 Python 的 site-packages 注入 Fusion 解释器路径
- **进度反馈** — 界面显示耗时秒数、轮次进度与忙碌提示，长任务不再看起来像卡死
- **全部本地进程内执行** — Fusion API 调用都在本机；只有聊天协议与（可选的）参考图片会离开本机

## 支持的 AI 提供商（2026-08 最新旗舰）

| 提供商 | API 格式 | 模型 | 图片输入 |
|--------|---------|------|:---:|
| **DeepSeek** | OpenAI 兼容 | deepseek-v4-pro、deepseek-v4-flash | ❌（官方 API 仅文本） |
| **Kimi（月之暗面）** | OpenAI 兼容 | kimi-k3 | ✅ |
| **智谱 GLM** | OpenAI 兼容 | glm-5.3 | ❌ |
| **OpenAI** | OpenAI 兼容 | gpt-5.6-sol、gpt-5.6-terra、gpt-5.6-luna | ✅ |
| **Anthropic Claude** | 原生 Messages API | claude-sonnet-5、claude-fable-5 | ✅ |
| **Google Gemini** | OpenAI 兼容 | gemini-3.7-flash | ✅ |

> **注意：** DeepSeek 与 GLM 的官方 API **仅接受文本**。图片门控已自动化——在纯文本模型上发送图片时，服务端会将其转换为提示文本，而不是报 400 错误。

## 系统要求

- **Windows** 10/11（manifest 支持 macOS，尚未实测）
- **Autodesk Fusion 360**（任意支持插件的近期版本）
- **Python** — Fusion 360 自带 Python，无需另行安装
- **网络连接** — 用于调用 AI API

## 快速安装

### 方式一：一键脚本安装（推荐，小白友好）

1. 从 GitHub 下载 ZIP 并解压到任意位置（如桌面）
2. **双击 `install.bat`**
3. 按提示**粘贴你的 DeepSeek API Key**（直接回车 = 跳过，稍后在 Fusion 面板设置里再填）
4. 完全退出并重启 Fusion 360 → 按 `Shift+S` → **Add-Ins** → **Fusion360-Add-Ins** → **Run** → 勾选 **Run on Startup**

脚本会自动完成：

- 自动定位 Fusion 的 `AddIns` 目录并把插件复制过去（幂等——已安装过则自动跳过复制）
- 检测系统 Python 并自动安装 `flask`/`requests`（插件首次启动时也会自动安装兜底）
- 为你生成 `local_server/config.json`——无需手动编辑 JSON，也绝不改动其它文件的配置

### 方式二：手动安装

#### 1. 下载

```powershell
# 从 GitHub 克隆
git clone https://github.com/zhou19830318/Fusion360-Add-Ins.git "$env:APPDATA\Autodesk\Autodesk Fusion 360\API\AddIns\Fusion360-Add-Ins"
```

或下载 ZIP 解压到：
```
%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\Fusion360-Add-Ins\
```

### 2. 安装依赖

**以管理员身份运行：**

```batch
cd "%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\Fusion360-Add-Ins"
install_deps.bat
```

该脚本会：
1. 查找带 pip 的真实 Python
2. 缺失 pip 时先引导（ensurepip）
3. 安装 `flask` 与 `requests`

> **提示：** 插件首次启动也会自动安装依赖。提前运行 `install_deps.bat` 只是省去首启等待。

### 3. 配置 API Key

编辑 `local_server\config.json`，至少填入一个提供商的 key：

```json
{
  "provider": "deepseek",
  "model": "deepseek-v4-pro",
  "deepseek_api_key": "sk-你的-deepseek-key",
  "openai_api_key": "",
  "anthropic_api_key": "",
  "google_api_key": ""
}
```

**各厂商 Key 申请地址：**
- DeepSeek：[platform.deepseek.com](https://platform.deepseek.com)
- 智谱 GLM：[open.bigmodel.cn](https://open.bigmodel.cn)
- OpenAI：[platform.openai.com/api-keys](https://platform.openai.com/api-keys)
- Anthropic：[console.anthropic.com](https://console.anthropic.com)
- Google Gemini：[aistudio.google.com](https://aistudio.google.com)（有免费额度）
- Kimi：[platform.moonshot.cn](https://platform.moonshot.cn)

> **安全提醒：** `local_server/config.json` 含 API Key，已通过 `.gitignore` **排除在版本控制之外**，切勿推送到公开仓库。无 key 的模板请使用 `local_server/config.example.json`。

### 4. 在 Fusion 360 中启用

1. 完全退出并重启 Fusion 360
2. 按 `Shift+S` → **Add-Ins** 标签页 → 找到 **Fusion360-Add-Ins**
3. 点击 **Run**，勾选 **Run on Startup**
4. 面板停靠在右侧

## 项目结构

```
Fusion360-Add-Ins/
├── AIFusion.py                  # 插件入口（由 Fusion 加载）
├── AIFusion.manifest            # Fusion 插件清单
├── install_deps.bat             # 依赖安装脚本（运行一次）
├── install.bat                  # 一键部署脚本（双击即可）
├── tools/
│   └── setup_config.ps1         # 引导式 API Key 配置（由 install.bat 调用）
├── .gitignore                   # 排除 config.json（API Key）、日志、缓存
├── resources/
│   └── palette.html             # 加载/回退页面
├── bridge/
│   ├── palette.pyc              # 消息桥（HTML ↔ Python）
│   └── selection_watcher.pyc    # 选择变化监听
├── handlers/
│   ├── __init__.pyc             # 工具路由（分发）
│   ├── read.pyc                 # 查询模型状态（含视口截图）
│   ├── create.pyc               # 创建简单特征
│   ├── execute.pyc              # 执行 Python 脚本
│   ├── update.pyc               # 修改特征
│   ├── delete.pyc               # 删除特征
│   └── api_documentation.pyc    # API 文档检索
└── local_server/
    ├── server.py                # Flask 后端（多提供商路由、视觉门控）
    ├── chat_ui.html             # 聊天界面（在 Fusion 面板中加载）
    ├── config.json              # 仅本地 — 提供商与 API Key（git 忽略）
    ├── config.example.json      # 无 key 配置模板
    ├── requirements.txt         # Python 依赖
    └── __init__.py              # 包初始化
```

## 工作原理

1. Fusion 加载 `AIFusion.py` → 检查依赖 → 在 8765 端口启动本地 Flask 服务
2. 停靠面板（Qt WebView）加载 `chat_ui.html`
3. 用户输入请求 → `POST /api/chat` → 服务器路由到所选的提供商/模型
4. AI 返回工具调用（如带 Python 脚本的 `execute`，或 `read` 的 `queryType=screenshot`）
5. JS 桥通过 `adsk.fusionSendData` 把工具调用发给 Fusion
6. `handlers/*.pyc` 在进程内执行 Fusion API 调用
7. 结果回流给 AI 进入下一轮（最多 10 轮）

## 稳定性与可控性增强（1.1）

- **真实成功判定**：工具返回中的 `payload.success:false` 不再被 UI 当作成功；执行失败会以失败状态回流给模型，避免“有 stdout 就认为建模完成”。
- **有限状态闭环**：同一用户请求最多 10 轮；相同的工具名和参数不会重复执行；`execute` 脚本必须定义 `run(_context)`。
- **服务端防护**：聊天 payload、消息数量/大小、工具数量和请求体大小均有限制；供应商请求使用连接/读取超时，并对 408/429/5xx 做有限指数退避。
- **配置抗崩溃**：`config.json` 使用临时文件 + 原子替换写入，进程中断不会留下半截 JSON。
- **同厂商模型切换不丢 Key**：设置接口返回的 `***末四位` 只是脱敏显示值；切换同一 AI 厂商的模型并保存时，服务端会识别该标记并保留本地真实 API Key，无需重新输入。
- **大型 CAD 读取结果压缩**：bodies、timeline 等查询的完整结果仍显示在面板并写入本地历史，但回传模型前会压缩为有限 observation，避免实体列表超过上下文限制；`read(document)` 缺少 `operation` 时会在本地拦截并给出可修复提示。
- **文本标注 API 约束**：系统提示明确要求 `SketchTexts.createInput(text, position)` 必须传入 `adsk.core.Point3D`，避免端口标签建模再次因缺少 position 参数失败。
- **可观测性**：新增 `GET /api/health`；API 错误包含 code、是否可重试和 request id，但不返回 API key。
- **项目历史与会话恢复**：对话、AI 回复、工具调用和工具结果保存到本机 SQLite；重启 Palette 后可从“历史”面板恢复项目和会话，切换模型不会清空上下文。
- **事件 + 快照**：每次变更追加不可变事件，同时更新可快速恢复的会话快照；数据库启用 WAL 和 FULL synchronous，避免单个 JSON 状态文件被覆盖或中断写坏。
- **隐私保护**：历史库不保存 API Key、Authorization、图片/PDF data URL 或截图 base64；附件只保留可恢复提示。
- **验证原则**：截图只用于视觉辅助检查，实体数量、包围盒、体积、健康状态等 Fusion 观测才是几何成功的依据。

本次改造刻意没有伪造或反编译 `bridge/handlers/*.pyc`。这些文件是当前真正执行 Fusion API 的黑盒边界；要进一步实现事务/撤销、稳定拓扑引用和逐特征验证，需要在目标 Fusion 版本中提供可读源码或进行实机 smoke test。

## 视觉校验（多模态自检）

对支持视觉的模型，设计流程可以截图自检收尾：

```
execute(建模) → read(screenshot) → Fusion 抓取视口 PNG(base64)
  → 以多模态 user 消息注入图片(image_url / image block)
  → 模型判定: 符合 → 输出总结; 不符合 → 用 execute 修复…(最多 3 轮)
```

- 截图功能已内置于 `read` 工具（`queryType: "screenshot"`，返回 `{type:"image", mimeType:"image/png", data:…}`）
- 通过面板设置中的"设计完成后自动截图视觉校验"开关启用（localStorage 持久化）
- 纯文本模型（DeepSeek 官方 API、GLM-5.3）自动跳过图片注入

## 附件上传（参考图片 / PDF）

点击输入框旁的 **📎 附件** 上传参考资料：

- **类型：** PNG、JPG、WebP、GIF 图片；PDF
- **限制：** 单张 ≤ 8MB，最多 4 个
- 附件经 base64 编码后以多模态 user 消息注入
- 纯文本模型上附件会被替换为文字提示（并提示切换视觉模型）
- Claude 通过 Anthropic `document` 块接收 PDF；其它视觉提供商走 data URL

## 切换提供商

1. 点击面板头部 ⚙ 徽章 → 打开设置
2. 选择提供商（DeepSeek、Kimi、智谱 GLM、OpenAI、Claude、Gemini）
3. 选择模型
4. 粘贴该提供商的 API Key，点击 **Save**

只配置了一个 key 时启动自动选中。模型下拉框与设置中的视觉能力徽标由 `/api/models` 动态提供。

## HTTP API

| 端点 | 用途 |
|------|------|
| `GET /chat` | 提供聊天 UI（面板） |
| `POST /api/chat` | 对话补全（含工具调用）；多提供商路由；视觉门控与图片剥离 |
| `GET /api/models` | 提供商、模型列表、视觉能力（`vision_all` / `vision_models`）、配置状态 |
| `POST/GET /api/config` | 读写本地配置 |
| `GET/POST /api/projects` | 查询/创建本地项目 |
| `GET/POST /api/projects/<id>/sessions` | 查询/创建项目会话 |
| `GET /api/sessions/<id>` | 读取会话快照和事件历史 |
| `POST /api/sessions/<id>/events` | 追加会话事件并更新快照 |

## 常见问题

### 插件显示 "No API key set"

打开设置（⚙），选择提供商，粘贴 API Key，点 Save。Key 存储在本地 `local_server\config.json`。

### 项目历史在哪里保存？

历史默认保存在 AIFusion 工程目录下，便于随工程一起备份和迁移：

- 工程目录：`.aifusion/history.sqlite3`
- 可通过环境变量 `AIFUSION_DATA_DIR` 指定其他目录

插件启动时会自动扫描工程目录下的 `.aifusion/history.sqlite3`：已有历史则自动打开并显示最近项目/会话；没有历史则自动创建项目和主会话。点击面板顶部“历史”仍可切换项目、创建新会话或恢复其他会话。历史库只保存对话和工具结果摘要，不保存 API Key 或图片原始数据。

如果升级插件后历史页面提示“本地服务未加载最新接口”，请完全停止并重新启动 AIFusion 插件；新版服务已使用可关闭的 HTTP server，后续停止插件时会正确释放 8765 端口，避免旧服务实例继续响应旧页面。

如果日志出现 `Server NOT ready after 20 s`，新版启动器会自动尝试 `8765` 到 `8790` 端口，并让 Palette 连接实际成功的端口。该机制用于兼容旧版本遗留的 Flask 线程占用 8765 的情况。

### 报 `ModuleNotFoundError: No module named 'flask'`

插件会自动安装依赖并把真实 Python 的 site-packages 注入 Fusion 解释器路径。若失败，以管理员运行 `install_deps.bat` 后重启插件。

### 报 "Cannot connect to …"

检查网络、API Key 是否正确（无多余空格）、提供商订阅是否有效，或换一个提供商。

### 报 `DeepSeek returned 400: unknown variant 'image_url'`

DeepSeek 官方 API 仅支持文本。当前版本已自动剥离图片（旧版本才会报这个错）。要用图片附件请切换到视觉模型（Kimi K3 / GPT-5.6 / Claude / Gemini）。

### 聊天看起来卡在 "Thinking…"

状态栏现在会显示已耗时秒数与轮次进度——长工具链任务（尤其推理型模型）可能耗时数分钟。重启插件后观察计时；若秒数完全不动再反馈，附上 `aifusion_debug.log` 片段。

### 模型画错或崩溃

系统提示词内置了已验证的 Fusion API 签名。遇到错误时：
1. 查看 `aifusion_debug.log` 中的确切 API 报错
2. 系统提示词的修复流程覆盖常见失败模式（body 引用过期、属性名错误、healthState ≠ 0 等）
3. 带着日志片段到 GitHub 提 issue

## 隐私与安全

- **API Key：** 存储在 `local_server\config.json`，该文件**已被 git 忽略、绝不推送**；仓库内模板为 `config.example.json`
- **Fusion 文件：** 永不上传，所有 Fusion API 操作都在进程内完成
- **聊天数据与附件：** 只有对话文本、工具调用结果和你附加的参考图片会发给你选择的 AI 提供商
- **网络：** 仅 `localhost:8765` → 所选 AI 提供商的 API 端点

## 卸载

1. 退出 Fusion 360
2. 删除目录：
   ```
   %APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\Fusion360-Add-Ins
   ```
3. 重启 Fusion 360

## 贡献

欢迎贡献。`bridge/` 与 `handlers/` 中的 `.pyc` 实现了 Fusion API 桥接；`local_server/`（server.py、chat_ui.html）与 `AIFusion.py` 是开放、可自由编辑的主要部分。

## 许可证

本项目基于 MIT License 许可，详见 `LICENSE`。

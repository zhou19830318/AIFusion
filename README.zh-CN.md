# AIFusion — Autodesk Fusion 360 AI 副驾驶

**[English](README.md) · [简体中文](README.zh-CN.md)**

AIFusion 是 **Fusion 360 原生插件**，在 Fusion 内部嵌入一个停靠式 AI 聊天面板。用自然语言描述你要设计的内容——AI 会直接驱动 Fusion API 为你建模：草图、实体建模、特征编辑、并通过视口截图自动校验结果。

[点击这里访问我的博客](https://blog.csdn.net/acldy833/article/details/163825853?sharetype=blogdetail&sharerId=163825853&sharerefer=PC&sharesource=acldy833&spm=1011.2480.3001.8118)

---

## 功能特性

- **嵌入式聊天面板** — AI 副驾驶直接存在于 Fusion 内部，无需浏览器标签页
- **6 家 AI 提供商** — DeepSeek、Kimi（月之暗面）、智谱 GLM、OpenAI、Anthropic Claude、Google Gemini，全部使用最新旗舰模型（2026-09）
- **自动识别提供商** — 只配置了一个 API Key 时自动选中
- **智能体工具循环** — 模型自主规划：先 `read` 查询模型状态，再用 `execute` 执行 Python 脚本驱动 Fusion API，每次请求最多 10 轮
- **视觉校验闭环** — 建模完成后自动抓取 Fusion 视口回传给支持视觉的模型检查设计，最多 3 轮修复；仅保留最新一张校验截图，图片在浏览器端自动降采样
- **附件上传** — 可附加图片或 PDF（单张 ≤8MB，最多 4 个）作为设计参考；超大图片在浏览器端自动压缩后再上传
- **按模型能力自适应** — 图片输入按模型门控（GLM-5.3 仅支持文本，服务端自动替换为文字提示而非报 400）
- **提示词内置已验证 API 知识** — 实战积累的 Fusion API 关键事实（cm→mm 的 `MM=0.1` 常量、`SketchTexts.createInput(text, position)` 签名、`root.bRepBodies` 访问方式……）始终合并进系统提示词，即使客户端自带 system 消息也不会丢失
- **模型别名** — 输入 `sonnet5`、`fable`、`gpt5.6`、`gemini-3.7` 自动映射到真实模型 ID
- **采样控制** — 默认 `temperature: 0.2`，保证 CAD 脚本生成的一致性与可复现性；可按请求或在 `config.json` 中覆盖（设为 `"none"` 跟随提供商默认，适配推理型模型）
- **诚实的截断报告** — Anthropic 的 `max_tokens` 停止原因会如实映射为 OpenAI 风格的 `finish_reason: "length"`，半截脚本可被检测，不会被当作完整代码静默执行
- **截断自动恢复** — 推理型模型把同一个输出上限同时用于"推理"和"正文"（DeepSeek 思考模式默认 64K）。推理吃满额度时厂商会返回 `HTTP 200` + 空消息；面板会先展示已产生的推理，再用降档的 `reasoning_effort`（先 `low`、再 `none`）重试，都失败才给出可操作的错误
- **厂商调用日志** — 每次尝试与响应（`finish_reason`、token 用量、`TRUNCATED` / `EMPTY_MESSAGE` 标记、耗时）都会写入 `aifusion_debug.log`，失败轮次可事后复盘；单次超时可通过 `provider_read_timeout` 配置（默认 420 秒），且超时不再重试 3 次
- **项目历史与会话恢复** — 对话、工具调用与结果持久化到本地 SQLite 数据库（WAL + FULL synchronous）；重启面板即可恢复任意会话，切换模型不丢上下文
- **本地服务端加固** — 多层负载上限（消息数/字符数/请求体/单图/脚本长度）、提供商请求指数退避重试、CORS 仅限本机回环来源、按请求 `base_url` 白名单、配置原子写入、API Key 在接口响应中始终脱敏
- **精致 UI** — 工具调用可折叠卡片（状态/耗时/摘要/一键复制）、markdown 渲染（标题、列表、代码）、智能吸底滚动（可跳回底部）、实时轮次与耗时指示、一键示例任务、项目/会话历史面板、一键清空
- **依赖自动安装** — 缺少 `flask`/`requests` 时首次启动自动安装，并把真实 Python 的 site-packages 注入 Fusion 解释器路径

## 支持的 AI 提供商（2026-09 最新旗舰）

| 提供商 | API 格式 | 模型 | 图片输入 |
|--------|---------|------|:---:|
| **DeepSeek** | OpenAI 兼容 | deepseek-flash、deepseek-v4-pro | ✅（deepseek-flash 原生多模态） |
| **Kimi（月之暗面）** | OpenAI 兼容 | kimi-k3 | ✅ |
| **智谱 GLM** | OpenAI 兼容 | glm-5.3-flash、glm-5.3 | ✅（glm-5.3-flash 原生多模态） |
| **OpenAI** | OpenAI 兼容 | gpt-5.6-sol、gpt-5.6-terra、gpt-5.6-luna | ✅ |
| **Anthropic Claude** | 原生 Messages API | claude-sonnet-5、claude-fable-5 | ✅ |
| **Google Gemini** | OpenAI 兼容 | gemini-3.7-flash | ✅ |

> **注意：** `glm-5.3`（文本旗舰）与 `deepseek-v4-pro` 仍为纯文本模型（`glm-5.3-flash` 自 2026-08、`deepseek-flash` 自 2026-09 起原生支持多模态）。图片门控已自动化——向纯文本模型发送图片时，服务端会将其转换为提示文本，而不是报 400 错误。

**模型别名**（服务端解析）：`sonnet5` / `sonnet-5` → `claude-sonnet-5`，`fable` → `claude-fable-5`，`gpt5.6` / `gpt-5.6` → `gpt-5.6-sol`，`gemini3.7` / `gemini-3.7` → `gemini-3.7-flash`，`glm5.3-flash` / `glm5.3` → 对应规范名，`deepseek-v4-flash` → `deepseek-flash`（旧模型名已下线）。

## 系统要求

- **Windows** 10/11（manifest 兼容 macOS，尚未实测）
- **Autodesk Fusion 360**（任意支持插件的近期版本）
- **Python** — Fusion 360 自带 Python，无需另行安装
- **网络连接** — 用于调用 AI API

## 快速安装

### 方式一：一键脚本安装（推荐）

1. 从 GitHub 下载 ZIP 并解压到任意位置（如桌面）
2. **双击 `install.bat`**
3. 按提示**粘贴你的 DeepSeek API Key**（直接回车 = 跳过，稍后在 Fusion 面板设置里再填）
4. 完全退出并重启 Fusion 360 → 按 `Shift+S` → **Add-Ins** → **AIFusion** → **Run** → 勾选 **Run on Startup**

脚本会自动完成：

- 自动定位 Fusion 的 `AddIns` 目录并把插件复制过去（幂等——已安装过则自动跳过复制）
- 检测系统 Python 并自动安装 `flask`/`requests`（插件首次启动时也会自动安装兜底）
- 通过引导式 `tools/setup_config.ps1` 为你生成 `local_server/config.json`——无需手动编辑 JSON

### 方式二：手动安装

#### 1. 下载

```powershell
# 从 GitHub 克隆
git clone https://github.com/zhou19830318/AIFusion.git "$env:APPDATA\Autodesk\Autodesk Fusion 360\API\AddIns\AIFusion"
```

或下载 ZIP 解压到：

```
%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\AIFusion\
```

#### 2. 安装依赖

```batch
cd "%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\AIFusion"
install_deps.bat
```

该脚本（若 pip 遇到权限问题请以管理员运行）会：

1. 查找真实 Python（优先使用 Fusion 自带 Python，位于 `%LOCALAPPDATA%\Autodesk\webdeploy\production`）
2. 缺失 pip 时先引导安装（ensurepip → get-pip.py 兜底）
3. 安装 `flask` 与 `requests`

> **提示：** 插件首次启动也会自动安装依赖。提前运行 `install_deps.bat` 只是省去首启等待。

#### 3. 配置 API Key

推荐直接在 Fusion 面板里打开设置（⚙）填写；也可以编辑 `local_server\config.json`：

```json
{
  "provider": "deepseek",
  "model": "deepseek-flash",
  "deepseek_api_key": "sk-你的-deepseek-key"
}
```

完整 Key 列表：`deepseek_api_key`、`kimi_api_key`、`zhipu_api_key`、`openai_api_key`、`anthropic_api_key`、`google_api_key`（每家提供商还有对应的 `<provider>_base_url` 覆盖项）。完整模板见 `local_server/config.example.json`。提供商 Key 也可从环境变量读取（如 `DEEPSEEK_API_KEY`）作为兜底。

**各厂商 Key 申请地址：**

- DeepSeek：[platform.deepseek.com](https://platform.deepseek.com)
- 智谱 GLM：[open.bigmodel.cn](https://open.bigmodel.cn)
- OpenAI：[platform.openai.com/api-keys](https://platform.openai.com/api-keys)
- Anthropic：[console.anthropic.com](https://console.anthropic.com)
- Google Gemini：[aistudio.google.com](https://aistudio.google.com)（有免费额度）
- Kimi：[platform.moonshot.cn](https://platform.moonshot.cn)

> **安全提醒：** `local_server/config.json` 含 API Key，已通过 `.gitignore` **排除在版本控制之外**，切勿推送到公开仓库。

#### 4. 在 Fusion 360 中启用

1. 完全退出并重启 Fusion 360
2. 按 `Shift+S` → **Add-Ins** 标签页 → 找到 **AIFusion**
3. 点击 **Run**，勾选 **Run on Startup**
4. 面板停靠在右侧；Solid 选项卡上的工具栏按钮可随时重新打开

## 使用副驾驶

- **自然语言对话。** AI 会先用 `read` 分析当前设计，规划后用 `execute` 执行 Python 脚本建模，并通过包围盒/体积/截图校验结果。
- **示例任务**（空状态可点击）：*"建模：一个 80×60×30 mm 的外壳，正面留 USB 和 BOOT 开孔"*、*"修改：把所有圆角半径改成 3mm，并加 2mm 壁厚"*、*"查询：列出当前设计所有实体的尺寸和体积"*。
- **附件：** 点击回形针按钮附加参考图片或 PDF（单张 ≤8MB，最多 4 个）。
- **视觉校验：** 在设置中开启"设计完成后自动截图视觉校验"，视觉模型会对自己的建模结果截图自检（最多 3 轮修复）。
- **历史：** ⏱ 按钮打开历史面板——切换项目、新建会话、恢复历史对话。历史保存在本地 `.aifusion/history.sqlite3`（可用环境变量 `AIFUSION_DATA_DIR` 指定其他目录）。
- **清空：** ✕ 按钮会在二次确认后**永久删除**当前会话已存储的历史（事件 + 快照），因此重开面板不会恢复对话。其他会话不受影响——若想保留旧对话以便随时恢复，请改用*新建会话*。

## 项目结构

```
AIFusion/
├── AIFusion.py                  # 插件入口（面板、工具栏、服务生命周期）
├── AIFusion.manifest            # Fusion 插件清单（v1.0.0，runOnStartup）
├── install.bat                  # 一键部署脚本（双击即可）
├── install_deps.bat             # 依赖安装脚本（flask + requests）
├── tools/
│   └── setup_config.ps1         # 引导式 API Key 配置（由 install.bat 调用）
├── resources/
│   └── palette.html             # 服务启动期间显示的加载/回退页面
├── bridge/                      # 消息桥（HTML ↔ Python），以 .pyc 形式发布
│   ├── palette.pyc
│   └── selection_watcher.pyc
├── handlers/                    # Fusion API 工具处理器，以 .pyc 形式发布
│   ├── __init__.pyc             # 工具路由（分发）
│   ├── read.pyc                 # 查询模型状态（含视口截图）
│   ├── create.pyc               # 创建简单特征
│   ├── execute.pyc              # 执行 Python 脚本
│   ├── update.pyc               # 修改特征
│   ├── delete.pyc               # 删除特征
│   └── api_documentation.pyc    # API 文档检索
├── local_server/
│   ├── server.py                # Flask 后端（多提供商路由、Anthropic 适配器、
│   │                            #   负载防护、重试、CORS 加固、配置管理）
│   ├── history_store.py         # SQLite 历史（事件 + 快照、WAL、隐私脱敏）
│   ├── chat_ui.html             # 聊天界面（在 Fusion 面板中加载）
│   ├── config.json              # 仅本地 — 提供商与 API Key（git 忽略）
│   ├── config.example.json      # 无 Key 配置模板
│   ├── requirements.txt         # Python 依赖（flask、requests）
│   └── __init__.py              # 包初始化
├── tests/
│   └── test_server.py           # 服务端单元测试（从仓库根目录运行）
├── specs/                       # 设计文档与已知约束
└── README.md / README.zh-CN.md  # 本文档（英文/中文）
```

> **说明：** `bridge/` 与 `handlers/` 仅有编译后的 `.pyc`（CPython 3.14），没有源码。它们是真正驱动 Fusion API 的黑盒边界；插件需要兼容的解释器才能导入。

## 工作原理

```
Fusion 360 插件 (AIFusion.py)
  └─ 启动本地 Flask 服务 (127.0.0.1:8765，守护线程)
       └─ 提供聊天面板 chat_ui.html + REST API
            └─ 把对话转发给 6 家 LLM 提供商
Fusion 桥 (bridge/*.pyc)
  └─ 通过 adsk.fusionSendData 在面板与 Fusion 之间路由工具调用
```

1. Fusion 加载 `AIFusion.py` → 检查依赖 → 健康检查并在端口 **8765** 启动本地 Flask 服务（若旧实例占用端口，自动扫描 **8765–8780**，面板跟随实际端口；可用环境变量 `AIFUSION_PORT` 覆盖）
2. 停靠面板（Qt WebView）加载 `chat_ui.html`
3. 用户输入请求 → `POST /api/chat` → 服务端注入 CAD 系统提示词并路由到所选提供商/模型
4. AI 返回工具调用（如带 Python 脚本的 `execute`，或 `read` 的 `queryType=screenshot`）
5. JS 桥通过 `adsk.fusionSendData` 把工具调用发给 Fusion
6. `handlers/*.pyc` 在进程内执行 Fusion API 调用
7. 结果回流给 AI 进入下一轮（每次用户请求最多 10 轮）

暴露给每家提供商的两个 Fusion 工具：

- **`read`** — 查询设计：`bodies`、`faces`、`edges`、`features`、`sketches`、`sketchProfiles`、`parameters` / `userParameters` / `modelParameters`、`materialLibraries` / `materialAppearance`、`selection` / `selectionSets`、`timeline` / `timelineStatus`、`document`、`projects`、测量（`volume`、`area`、`length`、`centroid`）、`similar`、`edgesByType`、`apiDocumentation`、`screenshot`
- **`execute`** — 对 Fusion API 执行 Python 脚本（脚本必须定义 `run(_context)`）

## 服务端加固与限制

| 防护项 | 数值 / 行为 |
|--------|------------|
| 请求体大小 | ≤ 32 MB |
| 每次请求消息数 | ≤ 80 |
| 文本总字符数 | ≤ 120,000（base64 图片按每张名义 512 字符计） |
| 单张图片（data URL） | ≤ 6,000,000 字符（≈ 4.5 MB 二进制，低于各家单图上限） |
| `execute` 脚本参数 | ≤ 120,000 字符（拒绝会挤占系统提示词的失控脚本） |
| tools 数组 | ≤ 32 项 |
| `tool` 消息 | 必须携带非空 `tool_call_id`（提前校验，返回明确的 400） |
| 提供商重试 | 408/429/5xx 最多重试 2 次并退避；超时连接 10 秒 / 读取 180 秒 |
| `max_tokens` | 8192；Anthropic 的 `max_tokens` 停止原因如实映射为 `finish_reason: "length"` |
| Temperature | 默认 0.2；优先级：请求参数 > `config.json`；`"none"`/留空则不发送该参数（适配推理型模型） |
| CORS | 仅允许本机回环来源（127.0.0.1 / localhost，任意端口） |
| 按请求 `base_url` | 限制为该提供商的默认/已配置主机或回环地址（阻断通过恶意 `base_url` 外泄 Key 的路径） |
| 配置写入 | 原子写入（`mkstemp` + `os.replace`）；GET 时 Key 脱敏为 `***末四位`，脱敏值保存时被忽略、真实 Key 保留 |
| 错误响应 | 规范化 JSON（`code`、`retryable`、`request_id`）——绝不包含原始 Key |

## 视觉校验（多模态自检）

对支持视觉的模型，设计流程可以截图自检收尾：

```
execute(建模) → read(screenshot) → Fusion 抓取视口 PNG(base64)
  → 客户端降采样到 ≤1024px JPEG，以多模态 user 消息注入
  → 模型判定: 符合 → 输出总结; 不符合 → 用 execute 修复…(最多 3 轮)
```

- 截图功能内置于 `read` 工具（`queryType: "screenshot"`）
- 通过面板设置中的"设计完成后自动截图视觉校验"开关启用（localStorage 持久化）
- 视觉模型（deepseek-flash、Kimi K3、GPT-5.6、Claude、Gemini）会对自己的建模结果截图自检；纯文本模型自动跳过图片注入
- 对话中只保留最新一张校验截图——新截图替换旧截图，不会累积

## 附件上传（参考图片 / PDF）

点击输入框旁的**回形针**按钮上传参考资料：

- **类型：** PNG、JPG、WebP、GIF 图片；PDF
- **限制：** 单张 ≤ 8MB，最多 4 个
- 超过 512KB 的图片在浏览器端自动降采样（≤ 2048px，JPEG）后再上传
- 附件经 base64 编码后以多模态 user 消息注入
- 纯文本模型上附件会被替换为文字提示（并提示切换视觉模型）
- Claude 通过 Anthropic `document` 块接收 PDF；其它视觉提供商走 data URL

## HTTP API

| 端点 | 用途 |
|------|------|
| `GET /chat` | 提供聊天 UI（面板） |
| `POST /api/chat` | 对话补全（含工具调用）；多提供商路由；视觉门控与图片剥离 |
| `GET /api/models` | 提供商、模型列表、别名、视觉能力（`vision_all` / `vision_models`）、配置状态 |
| `GET /api/config` | 读取配置（API Key 脱敏为 `***末四位`） |
| `POST /api/config` | 更新配置（脱敏值被忽略，本地真实 Key 保留） |
| `GET /api/health` | 插件启动时的存活探测 |
| `GET /api/projects` | 列出本地项目 |
| `POST /api/projects` | 创建项目 |
| `GET/POST /api/projects/<id>/sessions` | 列出 / 创建项目会话 |
| `GET /api/sessions/<id>` | 读取会话快照和事件历史 |
| `POST /api/sessions/<id>/events` | 追加会话事件并更新快照 |
| `DELETE /api/sessions/<id>/history` | 永久删除单个会话的事件与快照（面板清空按钮的落库接口） |
| `POST /api/sessions/<id>/archive` | 归档会话 |

## 项目历史与会话

- 对话、AI 回复、工具调用与（摘要后的）工具结果保存在本地 **SQLite** 数据库，启用 **WAL** 日志与 **FULL** synchronous 模式：每次变更追加不可变事件，同时更新可快速恢复的会话快照
- 默认位置：插件目录下 `.aifusion/history.sqlite3`（便于随工程一起备份迁移）；可用 `AIFUSION_DATA_DIR` 覆盖
- 启动时面板自动打开最近的项目/会话；历史面板支持切换项目、新建会话、恢复任意会话——切换模型不会清空上下文
- **隐私：** 历史库绝不保存 API Key、`Authorization` 头、图片/PDF data URL 或截图 base64——附件只保留可恢复提示
- **清空：** ✕ 按钮调用 `DELETE /api/sessions/<id>/history`，删除该会话的事件与快照但保留会话本身。只清面板永远不够——面板启动时恢复的正是这份快照，所以纯客户端的清空会在下次启动时把对话原样复活

## 开发与测试

```powershell
# 在仓库根目录运行
python -m unittest discover -s tests
```

测试套件（74 个）覆盖：负载校验、提供商路由、Anthropic 适配器（消息合并、finish_reason 映射）、temperature 与 `reasoning_effort` 解析、配置脱敏/保存、历史 API（项目/会话/事件/归档/重水化、data URL 脱敏、清空历史）、视觉能力剥离、长会话截断、厂商调用日志与截断响应处理（服务端契约 + 面板静态契约）。必须从**仓库根目录**运行（不能在 `local_server/` 内运行）。`package.json` 另提供 `npm run build:local` 做快速 `py_compile` 语法检查。

## 常见问题

### 插件显示 "No API key set"

打开设置（⚙），选择提供商，粘贴 API Key，点 Save。Key 存储在本地 `local_server\config.json`。

### 报 `ModuleNotFoundError: No module named 'flask'`

插件会自动安装依赖并把真实 Python 的 site-packages 注入 Fusion 解释器路径。若失败，以管理员运行 `install_deps.bat` 后重启插件。

### 报 "Cannot connect to …"

检查网络、API Key 是否正确（无多余空格）、提供商订阅是否有效，或换一个提供商。

### 报 `DeepSeek returned 400: unknown variant 'image_url'`

只有旧的纯文本 `deepseek-v4-flash`（已下线；该名称现已路由到多模态的 `deepseek-flash`）或 `deepseek-v4-pro` 才可能报这个错。当前版本会自动检测纯文本模型并剥离图片。要用图片附件和截图校验闭环，请使用 `deepseek-flash`。

### 聊天看起来卡在 "Thinking…"

状态栏会显示已耗时秒数与轮次进度——长工具链任务（尤其推理型模型）可能耗时数分钟。若秒数完全不动，重启插件并反馈 `aifusion_debug.log` 片段。

### 报 `Error: Empty response` / `模型输出被截断`

推理型模型把**同一个输出上限**同时用于"推理"和"正文"（DeepSeek 思考模式默认 64K tokens）。如果推理把额度全部吃掉，厂商依然返回 `HTTP 200`，但消息是 `content: ""`、没有工具调用、`finish_reason: "length"`——这一轮什么都没产出，模型压根没来得及写正文或调用工具。

面板现在会识别这种情况：先把它已经产生的**推理**显示出来，再用降档的 `reasoning_effort` 重试两次（先 `low`，再 `none`），都失败才给出可操作的错误提示，而不是只抛一句无信息的 "Empty response"。

每次厂商调用也会写进 `aifusion_debug.log`：

```
[2026-09-13 16:23:41] [provider] deepseek-flash openai attempt 1 -> status=200 finish_reason=length content_chars=0 tool_calls=0 reasoning_chars=60866 prompt_tokens=42807 completion_tokens=16184 reasoning_tokens=15886 TRUNCATED EMPTY_MESSAGE elapsed=142.7s
```

`local_server\config.json` 里两个相关设置：

- `provider_read_timeout`（默认 `420`，范围 30–3600）—— 单次厂商调用的等待上限。现在超时**不再重试 3 次**：旧行为会把一次"生成较慢"放大成约 9 分钟的假死。
- 换成非思考模型，或清空会话后重开一个更聚焦的任务，可以直接避开"共享输出上限"这个问题。

### 面板提示"本地服务未加载最新接口"

**请完全退出并重新启动 Fusion 360。** 仅重启插件是无效的：Flask 服务运行在 Fusion 自身的 Python 进程内，`local_server.*` 会在整个 Fusion 会话期间留在 `sys.modules` 里，重新 import 拿到的还是最早加载的那份模块。现在启动器会在 import 前主动清除这些缓存模块（`AIFusion.py` 的 `_reload_local_modules()`），因此重启插件已能加载到新代码——但**在你修改代码之前就已启动的那个 Fusion 会话**仍然持有旧版本。若日志出现 `Server NOT ready after …`，启动器会自动尝试 8765–8780 端口，以绕开占用默认端口的旧实例。

### 模型画错或崩溃

系统提示词会合并已验证的 Fusion API 指引。遇到错误时：

1. 查看 `aifusion_debug.log` 中的确切 API 报错（日志约 500 KB 轮转）。厂商调用现在也会入日志：`[provider] chat rid=… provider=… model=… finish_reason=… prompt_tokens=… completion_tokens=… reasoning_tokens=…`，空回复还会带上 `TRUNCATED` / `EMPTY_MESSAGE` 标记
2. 提示词中的修复流程覆盖常见失败模式（body 引用过期、属性名错误、`healthState ≠ 0` 等）
3. 带着日志片段到 GitHub 提 issue

## 隐私与安全

- **API Key：** 存储在 `local_server\config.json`，该文件**已被 git 忽略、绝不推送**；仓库内模板为 `config.example.json`；所有接口响应中 Key 均已脱敏
- **Fusion 文件：** 永不上传，所有 Fusion API 操作都在进程内完成
- **聊天数据与附件：** 只有对话文本、工具调用结果和你附加的参考图片会发给你选择的 AI 提供商
- **网络：** 仅 `127.0.0.1:<端口>` → 所选 AI 提供商的 API 端点；本地 API 拒绝非回环浏览器来源和攻击者控制的 `base_url`
- **历史：** 仅保存在本地；敏感内容（Key、原始图片数据）绝不写入数据库

## 卸载

1. 退出 Fusion 360
2. 删除目录：

   ```
   %APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\AIFusion
   ```

3. 重启 Fusion 360

## 贡献

欢迎贡献。`bridge/` 与 `handlers/` 中的 `.pyc` 实现了 Fusion API 桥接，目前没有可读源码；`local_server/`（server.py、chat_ui.html、history_store.py）、`AIFusion.py` 与测试套件是开放、可自由编辑的主要部分。设计文档与已知约束见 `specs/`。

## 许可证

本项目基于 MIT License 许可，详见 `LICENSE`。

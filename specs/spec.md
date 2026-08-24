# AIFusion 稳定性与可控性优化规范

## 项目概述
AIFusion 是运行在 Autodesk Fusion 360 内的 AI 辅助绘图插件：Fusion 插件入口负责生命周期与 Palette，localhost Flask 服务负责多模型适配，Web UI 负责对话和工具调用，`.pyc` bridge/handlers 在 Fusion 进程内执行 CAD 操作。

## 目标
1. 将“模型返回 HTTP 200”与“CAD 操作成功”严格区分。
2. 让自然语言请求经过可追踪的 CAD brief、参数、执行、验证和修复闭环。
3. 减少 API 输入异常、供应商错误、超时、重复工具调用和过大上下文导致的不稳定。
4. 在不破坏现有 Fusion bridge 协议的前提下，优先改造可编辑源码；对仅有 `.pyc` 的部分记录边界。
5. 让本地服务可在无 Fusion 环境下做单元/集成测试。

## 设计方向
- **参数化优先**：借鉴 OpenSCAD/CADAM/text-to-CAD 的脚本化与可复现思路，要求尺寸、单位、特征和操作模式结构化。
- **执行反馈闭环**：借鉴 CAD-Assistant 的 planner → CAD tool → observation → next action 循环，反馈必须包含 success、错误、stdout/stderr 和几何验证摘要。
- **拓扑与引用可解释**：借鉴 FutureCAD 的当前 B-Rep 原语 grounding 思路，优先使用稳定的实体 token/查询和执行前 read，而不是依赖模型记忆的瞬态引用。
- **安全可控**：默认本地绑定、严格 payload 校验、有限轮次、有限重试、敏感信息不回显。

## 技术栈决策
- Python 3.x；Fusion 端保持 Autodesk Fusion API。
- Flask + requests 保持现有依赖，新增逻辑尽量使用标准库。
- 前端保持原生 HTML/CSS/JavaScript，兼容 Fusion Palette WebView。
- 不引入数据库；配置继续使用本地 JSON，但采用原子写入和敏感字段脱敏。

## 架构规则
1. API 层只做输入校验、路由和统一错误格式；供应商适配层只负责协议转换。
2. 所有模型调用必须使用显式 connect/read 超时，不能使用单一无限等待。
3. CAD 工具结果必须保留真实 `success`；工具异常不能被包装成“成功响应”。
4. 工具调用参数必须在本地校验，未知工具、非法 JSON、超长脚本和未知枚举立即失败。
5. 失败修复必须基于最近一次 observation，不能无限循环或无条件重试。
6. 日志不得写入 API key、完整 Authorization header 或完整附件内容。

## 功能清单
| Feature | Spec | Status |
|---|---|---|
| Runtime hardening | [specs/runtime-hardening/document.md](specs/runtime-hardening/document.md) | done |
| CAD execution contract | [specs/cad-execution-contract/document.md](specs/cad-execution-contract/document.md) | in-progress |
| Verification and testability | [specs/verification-testability/document.md](specs/verification-testability/document.md) | in-progress |
| Project history and session continuity | [specs/project-history/document.md](specs/project-history/document.md) | done |

## 研究依据
- Autodesk Fusion API overview：支持 scripts、add-ins、applications，用于自动化、扩展和外部集成。
- CAD-Assistant project/paper：planner 生成 Python action，在 CAD 中执行，把执行结果和视觉/参数工具结果回馈给 planner。
- text-to-CAD topic / AgentSCAD：自然语言生成后进行 validated artifact、自动几何修复和制造验证。
- FutureCAD（arXiv:2603.11831）：可执行特征序列、参数 theta、当前 B-Rep 原语引用、执行有效性、invalidity ratio 和几何距离评测。
- Event Sourcing（Microsoft/Azure、AWS）：追加不可变事件日志 + 定期快照，用于审计和高效恢复。
- VS Code Chat Sessions / Checkpoints：重载窗口后恢复会话历史，checkpoint 同时关联工作区和对话状态。
- SQLite WAL：本地持久化采用事务、WAL 和 FULL synchronous，适合单机插件的并发读取与可靠写入。

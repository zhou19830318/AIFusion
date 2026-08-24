# CAD execution contract

## Overview
定义 AI 工具调用与 Fusion bridge 之间的可验证契约，解决“HTTP/工具层成功但实际建模失败”的问题。

## Goals
- 统一工具结果为 `ok + payload + error`。
- 每次 execute 必须返回执行成功、stdout/stderr、异常和可选 geometry observation。
- 支持计划、执行、验证、修复的有限状态机。

## Scope / non-goals
范围：工具描述、前端结果判定、系统提示词约束和未来 `.pyc` handler 重建接口。非目标：本阶段不反编译并重写全部 `.pyc` handler。

## User flows / UX / design notes
在修改模型前 AI 先 read；涉及尺寸的请求先给 CAD brief；execute 后必须 read/验证；失败时 UI 显示失败而非绿色成功。

## Functional requirements
1. 工具成功条件是 `result.ok === true` 且 payload.success 不为 false。
2. execute 脚本必须包含 `def run(_context)`，本地限制长度并拒绝明显的进程/文件破坏操作。
3. 失败结果包含稳定 error code（validation、fusion_runtime、no_target、timeout、unknown）。
4. 一次用户请求最多 10 个 planner rounds、3 个视觉验证轮次；重复相同 tool+args 不得无限执行。
5. 视觉校验是辅助证据，不能替代 Fusion API 的实体/体积/健康状态验证。
6. 大型 bodies/timeline/read 结果必须在回传模型前压缩为有限 observation；完整结果只用于 Palette 展示和本地历史。
7. `read(queryType=document)` 必须提供 `operation`，禁止发送不完整调用。

## Data model / schema
`ToolResult = {ok: boolean, tool: string, request_id: string, payload?: object, error?: {code,message,details?}, observation?: {body_count?,bbox?,volume?,health?}}`。

## API contracts
前端 bridge 返回的 JSON 必须符合 ToolResult；现有旧格式可由适配器映射，但不得把 `payload.success:false` 映射为 ok。

## Edge cases / failure modes
目标实体为空、过期 token、布尔操作无交集、截面/轮廓为空、脚本部分成功、stdout 有输出但后续特征失败、相同修复脚本重复执行。

## Acceptance criteria
日志中的 `success:false` 在 UI 里显示红色失败并进入修复上下文；空结果、异常、Fusion runtime error 都能被模型区分；相同 tool call 不会在同一轮无限重复。

## Test plan / test cases
用模拟结果验证 success false、异常、截图、旧格式兼容；用重复 tool call 验证去重/限流；对非法 execute script 验证本地拒绝。

## Implementation notes
先在 chat_ui.js 实现结果归一化，再逐步将 `.pyc` handlers 替换为可读源码；保留现有 `featureType/object` bridge 适配。

## Status / open questions
Status: in-progress。已完成前端结果归一化、重复调用抑制、execute 入口校验、read(document) 参数校验、大型工具结果上下文压缩，以及 `SketchTexts.createInput(text, position)` 的系统提示约束；Fusion `.pyc` handler 仍需在真实 Fusion 环境中重建/验证。Open question：Fusion API 的 undo/事务能力需在真实 Fusion 版本中确认，不能仅凭 Revit transaction 资料假设。

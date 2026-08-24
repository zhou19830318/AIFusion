# Project history and session continuity

## Overview

为 AIFusion 增加工程目录内可持续的项目/会话历史，使 Fusion 重启、Palette 刷新或模型设置变化后能够恢复对话上下文和操作记录。

## Goals

- 持久保存用户消息、AI 回复、工具调用和工具结果。
- 用快照快速恢复，用追加事件保留审计轨迹。
- 支持多个项目、多个会话和显式恢复。
- API Key、Authorization、图片/PDF base64 不落盘。

## Scope / non-goals

范围：工程目录 `.aifusion/history.sqlite3` 本地 SQLite 历史库、Flask API、Palette 历史面板和恢复逻辑。

非目标：替代 Fusion 云端版本历史；不在没有 Fusion SDK 时伪造 CAD 几何回滚；不默认保存完整附件文件。

## User flows / UX / design notes

插件启动时扫描工程目录历史库；已有历史自动打开最近项目/会话，没有历史自动创建本地项目和主会话；用户可从“历史”面板切换项目/会话、恢复会话、新建会话。设置切换不再清空当前会话。

## Functional requirements

1. 每次用户消息、AI 回复和工具结果产生一个不可变事件。
2. 每个事件同时更新 session snapshot，snapshot 至少包含可重放的 conversation。
3. 重启后从最新 snapshot 恢复，不依赖浏览器 localStorage。
4. 图片只保留附件存在/被省略的提示，不保存 data URL；截图 base64 不保存。
5. SQLite 开启 WAL、foreign keys 和 FULL synchronous；事件和快照在同一事务写入。
6. 历史状态限制 8 MB，避免上下文无限膨胀。

## Data model / schema

- `projects(id, name, created_at, updated_at, archived)`
- `sessions(id, project_id, title, created_at, updated_at, status)`
- `events(id, session_id, event_id, event_type, payload_json, created_at)`
- `snapshots(session_id, event_id, state_json, created_at)`

## API contracts

- `GET/POST /api/projects`
- `GET/POST /api/projects/<project_id>/sessions`
- `GET /api/sessions/<session_id>`
- `POST /api/sessions/<session_id>/events`，body `{event_type,payload,state}`
- `POST /api/sessions/<session_id>/archive`

## Edge cases / failure modes

- 数据库目录不可写、数据库锁、历史损坏、状态超过 8 MB、session/project 不存在。
- 旧版本没有历史库时自动建表。
- 多个 Palette/线程同时写入时由 SQLite 事务和进程内锁串行化。

## Acceptance criteria

- 刷新/重启 UI 后可恢复最近会话的用户消息和 AI 文本。
- 工具失败结果可从历史中识别，不被转换为成功。
- 项目切换和新建会话不会覆盖旧会话。
- API Key、data URL 和长 base64 不出现在 SQLite 内容。

## Test plan / test cases

- 临时 SQLite：建项目、建会话、追加事件、读取 snapshot、读取事件顺序。
- Flask API：项目/会话 CRUD、事件保存、404、8 MB 限制。
- 前端语法检查；在真实 Fusion 中验证 Palette 刷新后恢复。

## Implementation notes

采用 event sourcing + snapshot；这是本地单用户插件场景下比单个 JSON 文件更适合的持久化方式，避免配置式 JSON 被并发或中断写坏。

## Status / open questions

Status: done。历史默认位于工程目录；后续可增加“将历史操作映射到 Fusion 云端版本号”和附件文件持久化，但需要明确隐私和容量策略。

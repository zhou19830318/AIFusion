# Runtime hardening

## Overview
强化本地 Flask 服务和插件启动路径，避免坏请求、配置损坏、供应商临时错误和并发请求造成不可控状态。

## Goals
- 对 `/api/chat`、`/api/config` 做可预测的 4xx/5xx 响应。
- 使用 connect/read timeout、有限重试和 Retry-After 友好错误。
- 配置原子写入、读取失败可诊断且不泄露 key。
- 保护本地服务免受非本地 Origin 和过大 JSON 请求影响。

## Scope / non-goals
范围：`local_server/server.py`、必要的配置文档和测试。非目标：重写供应商 API、改变 Fusion bridge 消息格式、实现云端账户体系。

## User flows / UX / design notes
用户发起请求时，服务在供应商超时、429、5xx、非法响应 JSON 时给出统一可读错误；设置保存失败不应破坏旧配置。

## Functional requirements
1. 只接受 object JSON；messages 必须是有限长度 list，message role/content 结构必须可序列化。
2. 脚本、消息、附件和 tools 有大小上限。
3. HTTP 调用默认连接超时 10 秒、读取超时 180 秒；429/408/5xx 最多重试 2 次，指数退避。
4. API 响应不是 JSON 时返回供应商错误摘要，不抛出 HTML/堆栈给客户端。
5. 配置更新只接受字符串字段并原子替换文件。
6. `/api/health` 返回本地服务状态、版本和配置是否完整，不返回密钥。
7. `/api/config` 返回的 `***last4` 脱敏 API Key 仅用于显示；再次保存模型/供应商设置时不得覆盖本地真实 Key。

## Data model / schema
统一错误：`{"error": {"code": string, "message": string, "retryable": boolean, "request_id": string}}`。

## API contracts
- `GET /api/health` → 200 `{ok, service, version, configured_count}`。
- `POST /api/chat` → 成功返回统一 OpenAI-compatible completion；失败使用统一错误对象。
- `POST /api/config` → 成功 `{status:"ok", updated:[...]}`；失败保留原配置。

## Edge cases / failure modes
配置 JSON 损坏、文件无权限、供应商返回空 body/非 JSON/429/超时、并发设置保存、用户切换模型后提交脱敏 Key、Origin 伪造、请求体过大、模型/提供商不存在。

## Acceptance criteria
无 key、未知 provider、坏 JSON、超大请求分别得到稳定的 4xx；429/5xx 在有限重试后返回 retryable=true；配置写入失败时旧文件内容不被截断；已配置供应商切换模型后真实 API Key 保持不变；health endpoint 可在无 Fusion 环境下运行。

## Test plan / test cases
Flask test client：health、models、config GET/POST、chat validation；mock requests：200、429、500、timeout、invalid JSON；临时目录：原子保存、损坏配置回退。

## Implementation notes
抽取 `_json_error`、`_validate_chat_payload`、`_request_json_with_retry` 等纯函数；不在测试中导入 `adsk`。

## Status / open questions
Status: done。Open question：未来是否需要本地 token 防止同机其他进程调用；当前保持兼容并先限制 Origin/loopback。

# Verification and testability

## Overview
建立不依赖 Fusion 的服务测试，以及可观察的 CAD 验证结果，支持回归和失败分析。

## Goals
- 本地可运行单元测试覆盖配置、路由、供应商错误和多模态降级。
- 每次请求有 request id，日志可关联但不泄露密钥。
- 文档记录真实限制：bridge/handlers 当前只有 pyc，Fusion 实机验证仍需人工/CI 环境。

## Scope / non-goals
范围：测试夹具、健康检查、日志规范、回归样例。非目标：在没有 Fusion SDK 的环境伪造几何正确性。

## User flows / UX / design notes
失败提示应包含下一步建议：查看当前选择、重新 read、降低操作粒度、切换模型或检查 API key。

## Functional requirements
1. 测试不需要 `adsk`，可直接导入 server。
2. 记录 request id、provider、model、status、elapsed_ms；不记录 Authorization/key/完整 base64。
3. 测试覆盖视觉模型与纯文本模型的图片剥离。

## Data model / schema
日志事件：`{ts, request_id, event, provider?, model?, status?, elapsed_ms?, error_code?}`。

## API contracts
测试使用 Flask test client；供应商请求通过可注入的 requests session/函数 mock。

## Edge cases / failure modes
测试环境无 config.json、环境变量存在、配置 JSON 损坏、供应商响应非 JSON。

## Acceptance criteria
项目在普通 Python 环境下可以运行测试；失败输出能定位到 endpoint 和 request id。

## Test plan / test cases
至少覆盖：config merge、api key redaction、provider auto-detect、vision strip、anthropic conversion、chat validation、retry classification。

## Implementation notes
使用 `unittest` 标准库，避免增加测试依赖；在真实 Fusion 中另行执行 smoke test。

## Status / open questions
Status: in-progress。已添加 Flask test client、重试和原子配置保存回归测试；真实 Fusion bridge smoke test 仍需 Fusion 环境。Open question：是否为 Fusion bridge 增加可读源码版本及 API 文档快照。

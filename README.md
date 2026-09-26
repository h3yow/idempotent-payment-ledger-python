# Idempotent Payment Ledger (Local)

本地可运行的幂等支付受理与对账服务（FastAPI）。用本地账本与本地模拟的
渠道流水完整复现支付结果：重复提交、并发重试、乱序回调与对账差异下，
账务结论一致且可解释。无网络、无凭据、无外部服务依赖。

## 启动与验证

```bash
uv sync                 # 安装依赖
uv run python main.py   # 启动服务（127.0.0.1，随机端口）
uv run pytest -q        # 全部测试（本地、可重复）
uv run pytest -q --log-cli-level=INFO   # 查看幂等键/状态/判定依据日志
```

既有接口保持可用：`GET /` 返回 `{"Hello": "World"}`，
`GET /payments/health` 返回 `{"status": "ok"}`。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/payments` | 幂等受理 `{idempotency_key, amount, currency}` |
| POST | `/payments/{id}/confirm?succeed=true|false` | 确认并走本地渠道处理 |
| POST | `/payments/{id}/expire` | 超时过期 |
| POST | `/callbacks` | 渠道回调（可重复/乱序/迟到） |
| GET | `/payments/{id}` / `/payments` | 查询 |
| GET | `/ledger/balance` | 账本净额（恒为 0） |
| GET | `/reconcile` | 对账并输出差异分类 |

## 幂等语义

- 首次提交：幂等键被“声明”（in_progress），记录请求指纹（payload 的
  规范化 SHA-256）。
- 相同键 + 相同内容：返回首次存储的响应（`replayed: true`），只产生一次
  资金变动。
- 相同键 + 不同内容：拒绝，`IDEMPOTENCY_CONFLICT`（HTTP 409），与其他
  失败原因（如 `PAYMENT_NOT_FOUND` 404、`INVALID_STATE_TRANSITION` 409）
  通过 `error.code` 区分。
- 并发同键：处理中窗口内的竞争者得到 `IDEMPOTENCY_IN_PROGRESS`（409），
  不会重复入账；处理完成后的重试走重放路径。
- 可重试失败会释放声明，调用方可安全重试。

## 状态流转

```
CREATED ──▶ PROCESSING ──▶ SUCCEEDED (终态)
   │             ├──▶ FAILED (终态)
   └──▶ EXPIRED ◀┘        (终态)
```

- 只允许沿确定边前进；终态无出边。
- 非法跃迁、重复确认、重复过期：拒绝 `INVALID_STATE_TRANSITION`。
- 回调处理：按 `event_id` 去重；终态吸收迟到/乱序事件（`ALREADY_TERMINAL`
  无操作）；金额/币种不符的回调不落账（`AMOUNT_MISMATCH`）。
- 复式记账：每笔成功支付落地一对方向相反、金额相等的分录
  （DEBIT `channel:clearing` / CREDIT `merchant:receivable`），先落账后
  置状态，任何路径都不会留下半完成记录；`/ledger/balance` 恒为 0。

## 对账差异分类

`GET /reconcile` 逐笔独立比对本地账本与渠道流水，单条差异不阻断其余条目，
输出按 payment_id 排序、可复现：

| 分类 | 含义 |
| --- | --- |
| `LOCAL_ONLY` | 本地有意图、渠道无记录（缺失） |
| `CHANNEL_ONLY` | 渠道有流水、本地无意图（单边） |
| `AMOUNT_MISMATCH` | 双方金额不一致 |
| `STATUS_MISMATCH` | 双方结果不一致（如本地 FAILED、渠道成功） |
| `DUPLICATE` | 任一侧出现重复记录 |

## 测试覆盖

- `tests/test_idempotency.py`：重放、内容冲突、并发同键（屏障强制竞争，
  1 胜 15 冲突）、并发确认只入账一次。
- `tests/test_statemachine.py`：非法跃迁、重复确认、超时重试收敛、
  失败路径无半完成账务。
- `tests/test_callbacks.py`：重复、乱序、迟到回调与金额不符回调。
- `tests/test_reconcile.py`：五类差异分类与结论可复现。
- `tests/test_api.py`：既有接口可用、HTTP 层错误码可区分、全链路账本守恒。

测试日志打印幂等键、状态与判定依据（`--log-cli-level=INFO`）。

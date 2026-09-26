# 幂等支付账本（本地可运行）

基于 FastAPI 的本地幂等支付受理与对账服务：用本地账本与本地模拟渠道流水完整复现支付结果，
在重复提交、并发重试、乱序回调与对账差异下保证账务结论一致且可解释。
全程无网络、无凭据、无外部服务依赖。

## 运行与验证

```bash
uv sync                 # 安装依赖
uv run python main.py   # 启动服务（保持既有启动方式，监听 127.0.0.1 随机端口）
uv run pytest           # 运行全部测试（日志打印幂等键、状态与判定依据）
```

既有接口 `GET /` 保持可用；交互式文档见 `/docs`。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/payments` | 幂等受理支付（`idempotency_key` + `amount` + `currency`） |
| GET | `/payments/{id}` | 查询支付意图状态 |
| POST | `/payments/{id}/confirm` | 确认支付（CREATED → CONFIRMED） |
| POST | `/payments/{id}/expire` | 超时关闭（CREATED → EXPIRED） |
| POST | `/channel/emit` | 模拟渠道结算并产生回调事件 |
| POST | `/callbacks` | 接收渠道回调（重复/乱序/迟到均安全） |
| GET | `/ledger/entries` | 查看账本全部分录 |
| GET | `/reconcile` | 本地账本 vs 渠道流水对账 |

## 幂等语义

- 受理以 `idempotency_key` 为去重键，请求内容（金额、币种）计算 SHA-256 指纹。
- **相同键 + 相同内容**：返回首次受理的原始结果（`replayed=true`），不重复执行、只产生一次资金变动。
- **相同键 + 不同内容**：拒绝并返回 `409 {"error": "IDEMPOTENCY_CONFLICT"}`，
  与参数校验失败（`VALIDATION_ERROR`，422）、非法状态跃迁（`INVALID_STATE_TRANSITION`，409）、
  支付不存在（`PAYMENT_NOT_FOUND`，404）等失败原因明确区分。
- **并发同键提交**：首个到达者获得执行权，其余线程阻塞等待其完成后直接复用结果；
  执行失败会释放键供安全重试。任何情况下不会重复入账或部分入账。

## 状态流转规则

```
CREATED --confirm--> CONFIRMED --渠道成功--> SUCCEEDED   （落复式分录）
                     CONFIRMED --渠道失败--> FAILED      （不落分录）
CREATED --expire---> EXPIRED                             （不落分录）
```

- `SUCCEEDED` / `FAILED` / `EXPIRED` 为终态，结果 sticky，不可被任何回调推翻。
- 非法跃迁（如未确认就收到渠道结果、终态后再确认、确认后再超时）一律拒绝。
- 重复确认、重复超时、重复同结果回调**安全收敛**为无副作用的幂等空操作。
- 与终态矛盾的迟到回调被拒绝并记录原因，不改变既有结论。
- 资金仅在 `CONFIRMED → SUCCEEDED` 时变动一次：借贷等额反向两条分录
  （`channel_clearing` 借 / `merchant_revenue` 贷），整组原子落账，
  借贷不平或金额非正即整组拒绝，任何路径下账本总额守恒。

## 回调处理

- 按 `event_id` 去重：重复投递直接忽略（`duplicate_ignored`）。
- 乱序、迟到回调逐条独立处理，单条失败（`rejected` 并附原因）不阻断其余事件。
- 终态支付收到矛盾回调时拒绝且不改结果。

## 对账差异分类

`GET /reconcile` 比对本地账本（清算户净额）与渠道结算流水，差异分为四类：

| 类型 | 含义 |
| --- | --- |
| `MISSING_IN_CHANNEL` | 账本已入账，渠道无结算记录（单边：本地多） |
| `MISSING_IN_LEDGER` | 渠道已结算，账本无对应分录（单边：渠道多） |
| `AMOUNT_MISMATCH` | 双方都有记录但金额不符 |
| `DUPLICATE_IN_CHANNEL` | 渠道对同一支付重复结算 |

每条支付独立比对，单条差异不阻断其余条目；输出按键排序，结论可复现。

## 本地验证方法

```bash
uv run pytest -q          # 全部用例本地可重复运行，无需网络/凭据/外部服务
```

测试覆盖：并发同键提交（16 线程仅一次入账）、同键不同内容拒绝、非法状态跃迁、
重复/乱序/迟到回调、四类对账差异与结论可复现性。每条断言的日志均打印
幂等键、当前状态与判定依据（`log_cli` 默认开启）。

## 代码结构

```
app/
  models.py       类型定义：状态、分录、渠道事件、对账差异
  errors.py       可区分的失败原因（各自携带 error code）
  ledger.py       复式账本：借贷平衡校验 + 原子落账
  idempotency.py  幂等键存储：指纹比对 + 并发认领/等待
  payments.py     支付意图状态机 + 幂等受理
  channel.py      本地模拟渠道（重复/乱序/迟到回调）+ 幂等回调处理器
  reconcile.py    对账：差异分类、独立处理、确定性输出
main.py           FastAPI 路由（保留既有启动方式与 GET /）
tests/            全部本地测试
```

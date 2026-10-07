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
| POST | `/payments/{id}/refunds` | 幂等受理退款（部分/全额） |
| GET | `/payments/{id}/refunds` | 列出该支付的全部退款 |
| GET | `/refunds/{id}` | 查询退款状态 |
| POST | `/refunds/{id}/expire` | 退款超时关闭（CREATED → EXPIRED） |
| POST | `/channel/emit` | 模拟渠道结算支付并产生回调事件 |
| POST | `/channel/emit_refund` | 模拟渠道结算退款并产生退款回调事件 |
| POST | `/callbacks` | 接收渠道回调（支付/退款；重复/乱序/迟到均安全） |
| GET | `/ledger/entries` | 查看账本全部分录（退款分录带 `refund_id`） |
| GET | `/reconcile` | 本地账本 vs 渠道流水对账（含退款） |

新增退款接口与字段均为**叠加式扩展**：既有接口、请求/响应字段、启动方式
（`uv run python main.py`）与 `GET /` 保持不变。

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

## 退款幂等语义

- 退款挂在具体支付下，**只有 `SUCCEEDED` 的支付可退**，支持部分退款与全额退款；
  对非成功支付发起退款返回 `409 REFUND_NOT_ALLOWED`。
- 退款使用**独立的幂等键空间**（与支付受理互不影响），指纹内容为
  `{payment_id, amount, currency}`。
- **相同键 + 相同内容**：重复提交只产生一笔退款，返回首次结果（`replayed=true`），
  包括跨线程的并发重试（首个到达者执行，其余阻塞后复用同一结果）。
- **相同键 + 不同内容**：返回 `409 IDEMPOTENCY_CONFLICT`，与其它失败原因区分：
  - `VALIDATION_ERROR`（422）：金额非正、币种为空、退款币种与支付币种不符；
  - `REFUND_AMOUNT_EXCEEDED`（422）：累计退款将超过原受理金额；
  - `REFUND_NOT_ALLOWED`（409）：支付未成功不可退；
  - `REFUND_NOT_FOUND`（404）：退款不存在。
- **累计退款不得超退**：受理时在同一把服务锁内完成“占用额检查 + 落库”，
  占用额 = 全部 `CREATED`（已受理预留）+ `SUCCEEDED`（已退）退款金额之和；
  一旦 `占用 + 本次 > 原金额` 立即拒绝，不会悄悄放行。`FAILED`/`EXPIRED`
  的退款释放占用额，可用新幂等键安全重试。因此并发多笔退款下累计金额、
  分录条数都不会超退、重复或只入一部分。

## 退款状态流转规则

```
CREATED --渠道退款成功--> SUCCEEDED   （落复式分录）
CREATED --渠道退款失败--> FAILED      （不落分录，释放占用额）
CREATED --超时----------> EXPIRED     （不落分录，释放占用额）
```

- `SUCCEEDED` / `FAILED` / `EXPIRED` 为终态，结果 sticky；重复同结果回调
  安全收敛为无副作用空操作，矛盾结果（如成功后来失败）拒绝且不改写结论。
- 成功退款只落一次**反向冲销**分录：`merchant_revenue` 借 / `channel_clearing` 贷，
  与支付分录等额反向。部分退款可多笔，每笔独立成组、原子落账；
  全额退完后清算户净额归零，账本借贷总额始终守恒，任何成功/失败路径都不会
  留下半完成账务记录。退款分录带 `refund_id`，与支付分录在对账时分别核算，
  互不抵消。

## 回调处理

- 按 `event_id` 去重：重复投递直接忽略（`duplicate_ignored`）。
- 乱序、迟到回调逐条独立处理，单条失败（`rejected` 并附 `reason_code`/原因）不阻断其余事件。
- 支付回调为 `SUCCEEDED`/`FAILED`；退款回调为 `REFUND_SUCCEEDED`/`REFUND_FAILED`，
  携带 `refund_id`（必填）与渠道声明的 `amount`、`currency`。
- 退款回调金额与受理金额不符：拒绝并返回 `CALLBACK_AMOUNT_MISMATCH`；
  币种不符：`CALLBACK_CURRENCY_MISMATCH`；二者与状态矛盾
  （`INVALID_STATE_TRANSITION`）、未知退款（`REFUND_NOT_FOUND`）稳定可区分，
  均不改状态、不落分录，之后内容正确的回调仍可正常推进。
- 终态支付/退款收到矛盾回调时拒绝且不改结果。

## 对账差异分类

`GET /reconcile` 比对本地账本与渠道流水：支付按清算户支付分录净额比对，
退款按 `refund_id` 逐条比对本地退款结论与渠道退款流水，差异分为五类：

| 类型 | 含义 |
| --- | --- |
| `MISSING_IN_CHANNEL` | 本地已入账（支付成功或退款成功），渠道无记录（单边：本地多） |
| `MISSING_IN_LEDGER` | 渠道有记录（已结算/已退款），本地无对应入账（单边：渠道多） |
| `AMOUNT_MISMATCH` | 两边都有记录但金额或币种不符 |
| `DUPLICATE_IN_CHANNEL` | 渠道对同一支付/退款重复结算 |
| `CONTRADICTORY_RESULT` | **两边都有记录但结论相反**（如本地退款 FAILED/EXPIRED，渠道却退款成功） |

每条支付/退款独立比对，单条差异不阻断其余条目；输出按 id 排序，结论可复现。
支付与退款分录分开核算，全额退款不会把支付本身误判成金额不符。

## 兼容范围

- 未改动启动方式（`uv run python main.py`）、`GET /`、既有支付接口路径与既有字段。
- 新能力均为新增：退款相关 4 个接口、`/channel/emit_refund`、回调的
  `REFUND_*` 事件类型与可选 `refund_id`/`currency` 字段、`LedgerEntry.refund_id`、
  `ReconDiff.refund_id` 与 `CONTRADICTORY_RESULT` 差异类型；不传新字段时行为与旧版一致。

## 本地验证方法

```bash
uv run pytest -q          # 全部用例本地可重复运行，无需网络/凭据/外部服务
```

测试覆盖：并发同键提交（16 线程仅一次入账）、同键不同内容拒绝、非法状态跃迁、
重复/乱序/迟到回调、四类支付对账差异与结论可复现性；以及退款场景——
退款幂等重放与内容冲突、部分/全额累计与超退拒绝、退款失败/超时释放额度、
20 路并发退款恰好 10 笔成功且累计不超退、退款回调的重复/乱序/迟到/
金额不符/币种不符、退款对账的单边/金额或币种不符/重复/**结论相反**差异。
每条断言的日志均打印幂等键、当前状态与判定依据（`log_cli` 默认开启）。

## 代码结构

```
app/
  models.py       类型定义：状态、分录、退款、渠道事件/流水、对账差异
  errors.py       可区分的失败原因（各自携带 error code）
  ledger.py       复式账本：借贷平衡校验 + 原子落账
  idempotency.py  幂等键存储：指纹比对 + 并发认领/等待
  payments.py     支付意图状态机 + 幂等受理
  refunds.py      退款受理（超退控制）+ 退款状态机 + 反向复式分录
  channel.py      本地模拟渠道（支付/退款，重复/乱序/迟到回调）+ 幂等回调处理器
  reconcile.py    对账：支付+退款差异分类（含结论相反）、独立处理、确定性输出
main.py           FastAPI 路由（保留既有启动方式与 GET /，叠加退款接口）
tests/            全部本地测试（含退款受理/并发/状态机/回调/对账/HTTP 用例）
```

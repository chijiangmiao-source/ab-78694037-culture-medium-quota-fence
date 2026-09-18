# Medium Reservation Fence Service

纯后端服务：自动配液设备在泵送培养基前先**预留额度**，泵送完成后再**确认**；
预留带栅栏令牌（fence token）与租期，超时自动释放，确认严格幂等。

- 框架：FastAPI + PostgreSQL（唯一持久化，所有期限判定只取数据库时钟）
- 金额单位：整数毫升（INTEGER）
- 批次四量：`total_ml`（总额，建立后不可变）、`available_ml`（可用）、
  `reserved_ml`（有效预留）、`confirmed_ml`（已确认）
- 恒等式（由数据库 CHECK 约束强制）：
  `total_ml = available_ml + reserved_ml + confirmed_ml`

## 核心规则

1. **预留** `POST /batches/{id}/reservations`：`amount_ml` 必须为正整数；
   `lease_seconds` 取值 5–300。成功后从 `available` 扣到 `reserved`，
   并取得该批次内**严格递增且永不复用**的栅栏令牌。
2. **确认** `POST /batches/{id}/reservations/{token}/confirm`：
   仅当数据库当前时刻**严格早于** `expires_at` 才成功（恰好相等即过期），
   金额从 `reserved` 转到 `confirmed`。同一令牌重复提交返回**原确认结果**
   （相同的 `confirmed_at`），绝不重复扣减。
3. **取消** `.../cancel`：仅未到期的 held 预留可取消，金额一次性退回
   `available`；取消与过期均为**终态**，之后确认永久拒绝。
4. 每次状态变更（以及余额查询）都在**同一事务**中：先
   `SELECT ... FOR UPDATE` 锁定批次 → 读取数据库时钟
   （`clock_timestamp()`）→ 结算到期预留（`expires_at <= now`）→
   再执行操作。行锁串行化所有变更，并发下不会超量、不会重复扣减。

## 运行（Docker Compose）

```bash
docker compose up --build            # db + api，宿主默认端口 8080
API_PORT=9090 docker compose up      # 覆盖宿主端口
```

一次性验收服务（对运行中的 API 执行黑盒 HTTP 用例后退出）：

```bash
docker compose run --rm --build verify
# 或： docker compose up --build verify
```

健康检查：`GET http://localhost:${API_PORT:-8080}/health`

## 本地测试（真实 PostgreSQL）

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
# 需要真实 postgres 工具链（initdb/pg_ctl），测试会自建一次性实例：
PGBIN=/usr/lib/postgresql/16/bin .venv/bin/pytest tests -v
```

## API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/batches` | 建立批次 `{total_ml}`，总额此后不可变 |
| GET | `/batches` / `/batches/{id}` | 列表 / 余额（查询时也会结算到期预留，恒守恒） |
| POST | `/batches/{id}/reservations` | 预留 `{amount_ml, lease_seconds}` |
| GET | `/batches/{id}/reservations/{token}` | 查询预留 |
| POST | `/batches/{id}/reservations/{token}/confirm` | 确认（幂等） |
| POST | `/batches/{id}/reservations/{token}/cancel` | 取消（幂等） |

错误统一结构：

```json
{ "error": { "code": "insufficient_balance",
             "message": "...", "details": { } } }
```

错误码：`validation_error`、`batch_not_found`、`reservation_not_found`、
`insufficient_balance`、`reservation_expired`、`reservation_cancelled`、
`reservation_already_confirmed`、`internal_error`。

设备网关最终只能观察到四种确定结果之一：**一次有效确认**、**明确过期**、
**已取消**或**额度不足**；任意时刻查询余额都满足守恒。

# Action 执行实例 Specification

## 1. 目标

让 action 的执行过程在 web 上可见，并把「立即执行」统一成实例 + actor 调度：

1. **Phase 1**：web 新增执行状态区，能看到预约排队、执行中进度、最近完成结果。**不修改 `daemon/actor.py`**，执行中进度来自 actor 的 `GET /current`，其余状态来自实例文件。
2. **Phase 2**：`POST /action/<id>/run` 不再在请求线程里同步执行，而是创建 `scheduled_at = now` 的实例，由 actor 执行；同时删除 `action/service.py` 里的同步执行代码。
3. **Phase 3**：`actions.json` 在 `steps` 同级新增可选 `cleanup` 组（成功 / 失败 / 中止都执行），action 支持执行中中止，recorder 支持单次录制中止。
4. 保持现有文件契约：实例仍落在 `action_instances/*.json`，不引入数据库、消息队列或新依赖。

设计原则：

- **实例文件是唯一持久状态**，`/current` 只是执行中实例的实时补充。
- **web 请求不执行 command**（Phase 2 起），创建实例即返回。
- **actor 保持单线程、单实例执行**，不新增并发模型。
- **能不改 actor 就不改**：Phase 1 零改动；Phase 2 只做一行 `kind` 透传；Phase 3 为支持中止必须扩展 actor 状态机与 HTTP 接口。

## 2. 范围

### 2.1 包含

1. 执行状态区：排队中、执行中、最近完成实例的列表、进度与失败原因。
2. `action.port` 指向的 actor `GET /current` 客户端查询与降级。
3. 实例视图字段：状态文案/样式、步骤进度、本地时间、失败摘要。
4. 立即执行改为创建实例（`scheduled_at = now`、`kind = "immediate"`）。
5. 单实例结果轮询端点，终态后停止轮询。
6. 删除 `action/service.py` 的同步执行函数与旧的 pending 列表函数。
7. 测试与验收标准。
8. `cleanup` 组（finally 语义）：与 `steps` 同级，成功 / 失败 / 中止都执行一次（Phase 3）。
9. 执行中实例中止：`running → cancelling → aborted`，含 actor `POST /current/abort` 与 web 中止按钮（Phase 3，见第 7 节）。

### 2.2 不包含

- 不引入 vars / `${}` / object 参数；`cleanup` 只使用动作定义中的值，不支持表单覆盖。
- 不修改 actor 的单线程、单实例调度模型；执行语义仅按 Phase 2（`kind` 透传）与 Phase 3（cleanup / 中止）扩展。
- 不新增数据库模型、队列、WebSocket、SSE。
- 不做历史分页、筛选、搜索；只保留最近完成的固定条数。
- 不支持暂停、续跑、回滚已执行步骤；中止后不可恢复。
- 不实现 `lidar-refresh` 的跨模块自动刷新（见第 8 节）。

## 3. 总体架构

```text
Phase 1
  GET /action/                 首屏：action 卡片 + 执行状态表
  GET /action/instances        状态表片段（有进行中实例时每 2s 自刷新）
        │
        ├── 读 action_instances/*.json：pending / running / 最近完成
        └── GET http://127.0.0.1:{action.port}/current
              └── 只用于当前执行中实例的 next_index / not_before / run

Phase 2
  POST /action/<id>/run        校验 + 创建实例（scheduled_at = now）后立即返回
        │
        ▼
  actor 每 tick 选中到点实例 → 逐步执行（原逻辑）
        │
  GET /action/instances/<id>/result   结果区每 1s 轮询，终态后不再带轮询属性
```

职责边界：

| 组件 | 负责 | 不负责 |
| --- | --- | --- |
| action web | 创建实例、合并文件与 `/current`、渲染与轮询 | 不执行 command（Phase 2 起） |
| action service | 模板校验、实例读写、视图字段、`/current` 客户端 | 不调度、不推进步骤 |
| actor | 到点选择、推进步骤、终态落盘、`/current` 快照 | 不渲染页面、不关心展示字段 |
| instance 文件 | 排队、运行中占位、终态结果 | 不提供运行中步骤进度（Phase 1） |

## 4. 数据契约

### 4.1 实例文件

沿用现有字段，新增一个字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `kind` | string | **新增**，`"immediate"` / `"scheduled"`；旧文件缺失时按 `"scheduled"` 处理 |
| `status` | string | `pending` / `running` / `cancelling` / `succeeded` / `failed` / `expired` / `superseded` / `cancelled` / `aborted` |
| `scheduled_at` | string | UTC ISO 8601，立即执行为创建时刻 |
| `created_at` / `started_at` / `finished_at` | string/null | UTC ISO 8601 |
| `steps` | array | 已解析的步骤快照（含表单覆盖） |
| `cleanup` | array | **新增（Phase 3）**，已解析的 cleanup 步骤快照；旧文件缺失时按空数组处理 |
| `run` | object/null | 执行结果；**actor 只在 `_finish` 时写入**，运行中为 `null`；终态时含 `steps` 与 `cleanup` 两个结果数组 |
| `error` | object/null | 实例级错误（expired / superseded / actor_restarted / cancelled / aborted / cleanup_failed） |

不新增 `wait_until`：执行中的等待时间以 `/current.not_before` 为准，避免 actor 额外落盘。

### 4.2 actor `/current`

`GET http://127.0.0.1:{action.port}/current`，与 [daemon/actor.py](../../daemon/actor.py) 的 `snapshot()` 一致：

空闲：

```json
{"running": false}
```

执行中：

```json
{
  "running": true,
  "instance_id": "record_600rpm-20261122101400000000",
  "action_id": "record_600rpm",
  "status": "running",
  "next_index": 2,
  "total_steps": 4,
  "not_before": "2026-11-22T10:15:05+00:00",
  "run": {
    "run_id": "record_600rpm-20261122T101400123456Z",
    "status": "failed",
    "started_at": "2026-11-22T10:14:00.123456+00:00",
    "finished_at": null,
    "steps": [
      {"index": 0, "command": "lidar.standby", "args": {"mode": 0},
       "status": "succeeded", "result": {}, "error": null, "duration_ms": 54}
    ],
    "error": null
  }
}
```

字段约定：

- `next_index` 是已完成步数，与页面进度 `done_steps` 一致。
- `not_before` 是 delay 等待结束时刻；`not_before > now` 时页面显示「等待至 …」。
- `run.steps` 只含已完成步骤，`run.status` 在运行中不可信，不用于页面判定。
- `/current` 只覆盖当前一个实例；actor 单线程，这已足够。
- **Phase 3 新增字段**：`phase`（`"steps"` / `"cleanup"`）、`cancel_requested`（bool）、`cleanup_total`、`cleanup_done`；actor 不支持时这些字段可缺失，页面按 steps 阶段降级。

### 4.3 合并规则

`list_instances()` 同时读取实例文件与 `/current`，按 `instance_id` 合并：

1. 文件状态为终态（`succeeded` / `failed` / `expired` / `superseded` / `cancelled`）→ **一律以文件为准**，忽略 `live`。
2. 文件状态为 `running` 且 `live.running == true` 且 `live.instance_id == instance.instance_id` → 用 `live.next_index` / `live.total_steps` / `live.not_before` / `live.run`。
3. 文件状态为 `running` 但 `/current` 不可达、空闲或实例不匹配 → 显示 `0 / total_steps`，进度标注「不可用」，不报错。
4. `pending` 不使用 `live`。
5. `/current` 的读取失败不得影响列表：整体降级为规则 3。

### 4.4 状态映射

| status | 文案 | 样式类 | 是否终态 |
| --- | --- | --- | --- |
| `pending` | 等待执行 | `status-offline` | 否 |
| `running` | 执行中 | `status-running` | 否 |
| `succeeded` | 成功 | `status-ok` | 是 |
| `failed` | 失败 | `status-abnormal` | 是 |
| `expired` | 已过期 | `status-abnormal` | 是 |
| `superseded` | 被覆盖 | `status-abnormal` | 是 |
| `cancelled` | 已取消 | `status-offline` | 是 |
| `cancelling` | 中止中 | `status-running` | 否 |
| `aborted` | 已中止 | `status-abnormal` | 是 |

`status-running` 为新增样式类，颜色使用现有 `--quota`；未知状态原样显示文案，样式按 `status-offline`。

## 5. Phase 1：web 看到预约执行

### 5.1 `action/service.py`

新增，不改现有执行与实例写入逻辑：

```python
def current_snapshot() -> dict[str, Any] | None:
    """读取 actor /current；任何异常或超时返回 None。"""

def instance_view(instance: dict[str, Any],
                  live: dict[str, Any] | None = None) -> dict[str, Any]:
    """模板视图，字段见 5.1.1。"""

def list_instances(limit: int = INSTANCE_HISTORY_LIMIT
                   ) -> list[dict[str, Any]]:
    """进行中实例 + 最近完成实例，合并 /current 后返回。"""
```

`current_snapshot()` 约束：

- 端口来自 `load_action_config()['http_port']`；`ActionConfigError` → `None`。
- URL 固定 `http://127.0.0.1:{port}/current`，超时 0.5 秒。
- 用标准库 `urllib.request.urlopen`；捕获 `OSError`（含 `URLError` / 超时）与 `ValueError`（含 JSON 解码）。
- 返回必须是 `dict`，否则按 `None` 处理。

`list_instances()` 约束：

- 通过 `iter_instances()` 读取全部实例；坏文件继续跳过（现有行为）。
- `pending` 超过 `expire_after_seconds` 未执行的实例不展示（与 `list_pending_instances` 一致，过期由 actor 标记）。
- 排序：进行中按 `scheduled_at` 升序（先到先执行），完成按 `created_at` 降序；完成项最多 `INSTANCE_HISTORY_LIMIT = 20` 条。
- 一次性调用 `current_snapshot()`，按 `instance_id` 匹配，不逐实例发请求。

#### 5.1.1 `instance_view` 字段

| 字段 | 说明 |
| --- | --- |
| `status_label` / `status_class` | 4.4 的映射 |
| `terminal` | 是否终态 |
| `done_steps` / `total_steps` | 进度；running 时优先取 `live.next_index` |
| `pending_command` | running 且未完成时为下一步 command，否则 `None` |
| `waiting` / `wait_until_local` | running 且 `live.not_before > now` 时为真，并给 UTC+8 文案 |
| `scheduled_local` / `created_local` / `started_local` / `finished_local` | UTC+8 展示；解析失败为 `None` |
| `run` | 终态取文件 `run`，running 取 `live.run`，否则 `{}` |
| `failure_text` | 实例 `error` → 失败步骤 `error` → `run.error`，取第一个可用项 |

### 5.2 URL 与视图

| 方法 | 路径 | 行为 |
| --- | --- | --- |
| GET | `/action/` | 首屏：cards + 状态表 |
| GET | `/action/instances` | 状态表片段 |
| POST | `/action/instances/<instance_id>/cancel` | 取消 pending，返回状态表片段 |
| POST | `/action/<action_id>/run` | Phase 1 保持现状（同步执行） |
| POST | `/action/<action_id>/schedule` | 保持现状，触发事件改为 `instances-refresh` |

- 删除 `GET /action/pending` 及 `list_pending_instances` 的视图依赖，由 `/action/instances` 取代。
- `index` 上下文增加 `instances` 与 `has_active`（`any(not item['terminal'])`）。
- `instances` 视图每次渲染都重新读取文件与 `/current`，不缓存。
- `cancel` 仅对 `status == "pending"` 生效（沿用 `cancel_instance`）。

### 5.3 模板与刷新

`action/templates/action/index.html`：

- 「预约队列」卡改为「执行状态」，外壳 `#instances-body` 监听 `instances-refresh from:body`（不直接轮询）。

`action/templates/action/_instances.html`（新建，替换 `_pending.html`）：

- 根元素在 `has_active` 时带 `hx-get="/action/instances"`、`hx-trigger="every 2s"`、`hx-swap="outerHTML"`；无进行中实例时不带轮询属性，页面静默。
- 表头：动作 / 状态 / 时间 / 进度 / 详情 / 操作。
- 时间列：pending 显示「计划 …」，running 显示「开始 …」，完成显示开始或创建时间。
- 进度列：`done/total`；running 追加下一步 command 或「等待至 …」；`/current` 不可用时显示 `—`。
- 详情列：`failure_text`，空则不显示。
- 操作列：仅 pending 显示「取消」按钮，`hx-post` 到 cancel，`hx-target="#instances-body"`、`hx-swap="innerHTML"`。
- 不新增 JS；所有值由模板默认转义。

`static/base.css` 新增两行：

```css
.status-inline { font-weight: 600; white-space: nowrap; }
.status-running { color: var(--quota); }
```

### 5.4 降级

- actor 未启动：`/current` 返回 `None`，状态表仍显示 pending 与历史；running 行进度显示 `—`。
- `action.port` 配置缺失：`current_snapshot()` 返回 `None`，不抛 `ActionConfigError` 到视图。
- 单次 `/current` 响应慢：0.5 秒超时，状态表最多延迟一次刷新。
- 实例文件损坏：沿用 `iter_instances` 的跳过策略。

## 6. Phase 2：立即执行统一走实例 + actor

### 6.1 提交立即执行

`action/service.py` 新增并复用：

```python
def _find_action(action_id) -> tuple[dict | None, str | None]
def _prepare_steps(action, overrides) -> tuple[list | None, str | None]
def _create_instance(action, steps, scheduled_at, kind) -> dict
def submit_action(action_id, overrides=None) -> tuple[dict | None, str | None]
```

- `submit_action`：校验模板与参数，创建 `scheduled_at = now`、`kind = "immediate"` 的实例并落盘，返回实例。
- `schedule_action` 改用 `_find_action` / `_prepare_steps` / `_create_instance`，返回结构与现在一致（含 `scheduled_local`）。
- `_create_instance` 统一写入 `kind`、`wait_until` 不写、`run = None`、`status = "pending"`。
- 实例 ID 规则不变：`{action_id}-{UTC 微秒时间戳}`。

### 6.2 结果轮询

| 方法 | 路径 | 行为 |
| --- | --- | --- |
| POST | `/action/<action_id>/run` | 创建立即执行实例，返回结果片段 |
| GET | `/action/instances/<instance_id>/result` | 返回单实例结果片段 |

`_instance_result.html`：

- 根元素在非终态时带 `hx-get="/action/instances/<id>/result"`、`hx-trigger="every 1s"`、`hx-swap="outerHTML"`；终态时不带，轮询自然停止。
- 展示：状态、`run_id`、进度、失败原因、已完成步骤（command / status / duration_ms / error / result）。
- 实例不存在：HTTP 200，显示「实例不存在」，不带轮询属性，避免 htmx 因 404 不替换导致旧内容残留。

`views.run`：

- 调用 `submit_action`；成功时返回结果片段并触发 `instances-refresh`，让状态表立刻出现新行。
- 失败时返回错误片段（无轮询）。
- **不发送 `lidar-refresh`**（见第 8 节）。

### 6.3 actor 一行改动

`daemon/actor.py` 的 `_advance` 中，执行身份改为读取实例：

```python
execution = {
    'kind': instance.get('kind', 'scheduled'),
    ...
}
```

理由：`_build_step_context` 会把 `kind` 写进 recorder sidecar 的 `action.kind`；不读实例会导致立即执行被记成 scheduled。旧实例无 `kind`，默认 `scheduled`。

### 6.4 删除清单

`action/service.py` 删除：

- `execute_action`
- `execute_steps`
- `_run_steps`
- `_failure_run`
- `_finish_run`
- `list_pending_instances`

保留：`execute_step`、`new_run`、`_build_step_context`、`_resolve_steps`、`_apply_overrides`、实例读写与调度辅助函数。

### 6.5 已接受的语义

1. **actor 不在线**：立即执行实例停在 `pending`，直到 `expire_after_seconds`（默认 600 秒）过期并标记 `expired`，不会在 web 进程内执行。
2. **同 tick 多个到点**：只执行创建时间最新的实例，其余标记 `superseded`（现状逻辑不变）。连续点击多个「执行」时可能出现「被覆盖」，接受该行为。
3. 不引入 FIFO 队列；需要排队时再单独设计。

## 7. Phase 3：cleanup 与中止

让「执行中 action 可被中止」落地，并提供类似 try/finally 的 cleanup 组：正常路径失败或被中止后，仍顺次执行一组收尾指令（含 recorder 中止等），保证设备回到安全态。

### 7.1 `actions.json` 增加 `cleanup`

`cleanup` 与 `steps` 同级，可选，元素结构与 `steps` 完全一致（`command` / `args` / `delay`）：

```json
{
  "action_id": "record_10s_and_standby",
  "title": "录制 10 秒并回到待机",
  "steps": [
    {"command": "lidar.standby", "args": {"mode": 0}},
    {"command": "recorder.record", "args": {"duration": 10}}
  ],
  "cleanup": [
    {"command": "recorder.abort", "args": {}},
    {"command": "lidar.standby", "args": {"mode": 1}}
  ]
}
```

解析与实例化：

- `_normalize_action()` 复用 `_normalize_step()` 校验 `cleanup`，错误合并进 `errors`；`cleanup` 缺失按空数组处理。
- `cleanup` 不接受表单覆盖，只用定义值（避免用户在 web 上误关掉机安全指令）。
- `_create_instance()` 把已解析的 `cleanup` 快照写入实例文件，与 `steps` 同级；旧实例缺失 `cleanup` 时按空数组处理。
- 仍不引入 vars / `${}` / object 参数。

### 7.2 actor 状态机扩展

实例 `status` 新增两个值：`cancelling`（中止中，非终态）、`aborted`（已中止，终态）。

actor 执行分两个阶段：

1. **steps 阶段**：逐条执行 `steps`，与现状一致；某条失败即停止后续 steps。
2. **cleanup 阶段**：steps 全部成功、steps 失败、或收到中止后进入；逐条执行 `cleanup`，单条失败只记录、不阻断后续 cleanup。

`/current` 快照新增字段：

| 字段 | 说明 |
| --- | --- |
| `phase` | `"steps"` / `"cleanup"` |
| `cancel_requested` | bool，是否已收到中止 |
| `cleanup_total` | cleanup 总条数 |
| `cleanup_done` | 已完成 cleanup 条数 |

`run` 结果增加 `cleanup` 数组，结构与 `run.steps` 一致（`index` / `command` / `args` / `status` / `result` / `error` / `duration_ms`），与 `steps` 分开存放，不污染 `steps` 的索引。

终态规则：

| steps 结果 | cleanup 结果 | 收到中止 | 最终 status |
| --- | --- | --- | --- |
| 全成功 | 全成功 | 否 | `succeeded` |
| 全成功 | 有失败 | 否 | `failed`，`error.code = "cleanup_failed"`，cleanup 错误记入 `run` |
| 失败 | 已执行 | 否 | `failed`，保留 steps 原始错误，cleanup 错误附加在 `run` |
| 任意 | 已执行 | 是 | `aborted`；cleanup 仍执行完，cleanup 错误附加在 `run`，不覆盖中止结论 |

中止时的判定不看错误码：只要 `cancel_requested` 为真，正在执行的步骤无论返回什么错误，都视为中止导致，记录到 `run` 后进入 cleanup，最终落 `aborted`。不依赖 `recorder_aborted` 这类具体字符串，新增命令无需让 actor 认识它的错误码。

### 7.3 中止入口：`POST /current/abort`

actor 新增本地 HTTP 端点（仅回环）：

```
POST http://127.0.0.1:{action.port}/current/abort
{"instance_id": "<当前实例 ID>"}
```

行为：

1. 校验 `current` 非空且 `current.instance_id == 请求 instance_id`；不匹配 → `409`，`{ok: false, error: {...}}`。
2. 设置 `current.cancel_requested = true`，把实例文件 `status` 写为 `cancelling`。
3. **立即**执行通用中断：取当前步骤的 `command` → 查注册表定义里的可选字段 `abort` → 存在则调用该命令的 handler，打断阻塞中的步骤。这是必须的快路径：actor 主循环卡在阻塞步骤里，只有 HTTP 线程能打断它。
4. 当前步骤没有声明 `abort`（或不支持中断）→ 只保留 `cancel_requested` 标志，不做中断调用；步骤返回后跳过剩余 steps。
5. 返回 `202 {ok: true}`，不等待 cleanup 完成。
6. 后续 tick：阻塞步骤返回后，跳过剩余 steps，进入 cleanup；cleanup 完成后落盘 `aborted`。

并发安全：`current` 与实例落盘由同一把锁保护；**不在 command handler 执行期间持锁**，避免 HTTP 线程死锁。

#### 7.3.1 命令中断声明契约

任何命令都可以选择支持中止，只需在自己的 `COMMANDS` 定义里加一个可选字段：

```python
'recorder.record': {
    'abort': 'recorder.abort',   # 中断本命令时调用的命令名
    ...
}
```

约束：

1. `abort` 是**命令名字符串**，不是 handler；actor 通过注册表解析，避免直接 import 具体模块。
2. 中断命令的 handler 签名为 `handler(args, ctx)`，actor 以 `({}, {})` 调用；因此中断 handler **不得依赖 `ctx` 里的执行上下文**，也不得要求 `step_index` / `step_count`。
3. 中断 handler 必须幂等：没有可中止的目标时返回成功，不抛错。
4. 中断 handler 必须有内部超时，不能长时间阻塞调用它的 HTTP 线程。
5. `registry.get_registry()` 加载时校验：每处声明的 `abort` 命令必须存在，否则视为配置错误，启动即失败。
6. 未声明 `abort` 的命令按协作式取消处理：置标志、等返回、跳过剩余 steps。

新增一个「阻塞且支持 abort」的命令时，只需在自己模块的 `COMMANDS` 中声明 `abort` 并注册中断命令，`action` / actor 不再改动。

`action/service.py` 新增 `request_abort(instance_id)`：

- `pending` → 复用 `cancel_instance()`。
- `running` / `cancelling` → 转发 actor `POST /current/abort`，返回错误文案或 `None`。
- 终态 → 返回「实例已结束，无法中止」。

### 7.4 Web 中止按钮

复用现有 action 执行状态卡（Phase 1 已建），不新建 app：

| 方法 | 路径 | 行为 |
| --- | --- | --- |
| POST | `/action/instances/<instance_id>/abort` | 中止 running / cancelling 实例，返回状态表片段 |

**单入口**：中止按钮只放在 `_instances_live.html` 的状态表操作列，与现有「取消」共用同一列、同一套写法。`_instance_result.html` 不加按钮，保持不变。

理由：

1. 状态表覆盖全部实例（立即执行 + 预约执行），页面刷新后仍在；结果页是点击「执行」后的临时面板，刷新即消失。
2. 结果页每 1 秒轮询自身，从状态表发起的中止会在下一次轮询自然显示为「中止中 / 已中止」，无需第二个按钮。
3. 只维护一套 `hx-target` 与状态分支，避免两处显示规则漂移。

`_instances_live.html` 操作列行为：

| status | 操作列 |
| --- | --- |
| `pending` | 「取消」按钮（现状不变） |
| `running` | 「中止」按钮，`hx-post` 到 abort，`hx-target="#instances-live"`、`hx-swap="outerHTML"` |
| `cancelling` | 「中止中」，按钮禁用（`disabled`），不重复发请求 |
| `aborted` | 「已中止」，无按钮 |

沿用现有写法：`hx-disabled-elt="find button"`（发送期间禁用）、`hx-confirm`（确认后再发）。

`_instance_result.html` 仅需在状态分支里显示 `cancelling` / `aborted` 文案（`status_label` 已覆盖），不改结构、不加 `id`。

recorder web 卡片继续只读，不在 recorder 页面提供中止按钮；中止统一在 action 页发起。

### 7.5 已接受语义与限制

1. **进程存活期间** cleanup 保证执行一次；actor 崩溃或断电不保证 cleanup 执行，遗留 `running` / `cancelling` 实例由重启恢复标记为 `failed`（`actor_restarted`）。
2. 中止是幂等的：对 `cancelling` 重复发 abort 返回成功但不重复动作。
3. `recorder.abort` 只按 `record_id` 限定（可选）：不传则中止当前唯一录制；进程内单实例，不引入 `run_id` / `instance_id` 作用域。
4. `cancelling` 可能持续到当前阻塞步骤返回；UI 以「中止中」呈现，不提前显示「已中止」。
5. 对无中止能力的 command，中止只保证跳过后续 steps，当前步骤会跑到自然返回。

## 8. 延后项：`lidar-refresh`

现状：同步执行时 `views.run` 在成功后返回 `HX-Trigger: lidar-refresh`。Phase 2 移除同步执行后，该刷新会自然消失。

**本轮不实现替代方案**，原因：若在结果轮询端点补发 `lidar-refresh`，会把 action 与 lidar 的 UI 耦合散落在 `action/views.py`，职责不清。

候选方案（后续单独评估）：

| 方案 | 说明 |
| --- | --- |
| lidar 卡片自轮询 | lidar 卡片按固定间隔刷新自身，action 不再发事件 |
| 统一事件层 | 由实例终态变化统一通知各卡片，action 不直接引用 lidar |
| 保持手动刷新 | 接受 lidar 卡片只在用户点击「刷新」时更新 |

Phase 2 验收不包含 `lidar-refresh`；文档与页面不承诺自动刷新。

## 9. 预期文件变更

| 文件 | Phase | 变更 |
| --- | --- | --- |
| `action/service.py` | 1 | 新增 `current_snapshot` / `instance_view` / `list_instances` / 状态常量 |
| `action/views.py` | 1 | `index` 增加实例上下文；新增 `instances`；`cancel` 返回实例片段 |
| `action/urls.py` | 1 | 用 `instances` 替换 `pending` 路由 |
| `action/templates/action/index.html` | 1 | 预约队列卡改为执行状态卡 |
| `action/templates/action/_instances.html` | 1 | 新建，替换 `_pending.html` |
| `action/templates/action/_pending.html` | 1 | 删除 |
| `static/base.css` | 1 | 新增 `.status-inline` / `.status-running` |
| `action/service.py` | 2 | 新增 `submit_action` 与实例创建辅助；删除 6.4 清单 |
| `action/views.py` | 2 | `run` 改为创建实例；新增 `instance_result` |
| `action/urls.py` | 2 | 新增结果轮询路由 |
| `action/templates/action/_instance_result.html` | 2 | 新建，单实例结果与轮询 |
| `daemon/actor.py` | 2 | `_advance` 读取实例 `kind` |
| `action/service.py` | 3 | `_normalize_action` 解析 `cleanup`；`_create_instance` 写入 cleanup 快照；新增 `request_abort`；状态常量加 `cancelling` / `aborted` |
| `action/registry.py` | 3 | 加载时校验命令声明的 `abort` 存在；提供按命令名取中断定义 |
| `daemon/actor.py` | 3 | 状态机加 cleanup 阶段与 `cancel_requested`；新增 `POST /current/abort`（按当前 command 的 `abort` 声明通用中断）；`snapshot` 增加 phase / cleanup 字段 |
| `action/views.py` | 3 | 新增 `abort` 视图 |
| `action/urls.py` | 3 | 新增 abort 路由 |
| `action/templates/action/_instances_live.html` | 3 | 操作列：`running` 显示中止按钮、`cancelling` 显示中止中 |
| `action/templates/action/_instance_result.html` | 3 | 不改结构；状态文案自动显示 `cancelling` / `aborted` |
| `actions.json` | 3 | 示例 action 增加 `cleanup` 组 |
| `recorder/*` | 3 | 见 recorder_spec 5.8（`/abort` 与 `recorder.abort` command） |
| `specs/action/action_spec.md` | — | 本文件 |

不改：`device_config.json`、`lidar/*`、`artifact/*` 的接口与模板；`actions.json` 仅新增可选 `cleanup`，`recorder/*` 仅新增 `/abort` 与 `recorder.abort`（见各自 spec）。

## 10. 测试要求

全部使用临时实例目录、mock HTTP / fake handler，不依赖真机与真实 actor 进程。

### 10.1 Phase 1

1. `/current` 正常返回 running → 对应实例显示 `next_index / total_steps` 与 `not_before` 等待文案。
2. `/current` 超时、连接失败、JSON 非法、`action.port` 非法 → `current_snapshot()` 返回 `None`，页面不抛异常。
3. `/current` 指向的实例与文件不匹配 → 使用文件状态，进度显示 `—`。
4. 文件已终态但 `/current` 仍报 running → 以文件终态为准。
5. `list_instances`：pending 未过期的展示、过期的隐藏；完成项按 `created_at` 降序且不超过 20 条。
6. `GET /action/instances` 可独立渲染；有进行中实例时片段带轮询属性，无则不带。
7. `cancel` 只对 pending 生效，返回更新后的状态表片段。
8. 模板输出经过转义；不出现新增 JS。

### 10.2 Phase 2

1. `submit_action` 创建 `kind == "immediate"`、`scheduled_at == created_at` 的 pending 实例。
2. `action` 不存在、`actions.json` 非法、参数越界 → 返回错误且不产生实例文件。
3. `POST /run` 返回带轮询属性的结果片段；`GET /result` 在非终态保留轮询、终态移除轮询。
4. `/result` 对不存在实例返回 200 与错误文案，不返回 404。
5. `execute_action` / `execute_steps` / `_run_steps` 不再被引用（导入检查或调用检查）。
6. actor `_advance` 传给 `execute_step` 的 `execution.kind` 等于实例 `kind`；旧实例缺失时为 `scheduled`。
7. 立即执行成功后实例文件含完整 `run`，页面刷新后仍可在完成列表看到。

### 10.3 Phase 3

1. `cleanup` 解析：缺失 → 空数组；非法 command / args → 合并进 `errors`，不创建实例。
2. 成功路径：steps 全成功后 cleanup 仍执行一次，`run.cleanup` 完整；终态 `succeeded`。
3. steps 失败：剩余 steps 跳过，cleanup 仍执行；cleanup 单条失败不阻断后续 cleanup；终态 `failed` 保留原始错误。
4. 中止：`POST /current/abort` 后 `cancel_requested` 置位，跳过剩余 steps 进入 cleanup，终态 `aborted`；cleanup 错误附加在 `run`。
5. 幂等：对 `cancelling` 重复 abort 不产生第二次 cleanup。
6. `request_abort`：`pending` → `cancelled`；`running` / `cancelling` → 转发 actor；终态 → 返回错误文案。
7. 中止不匹配的 `instance_id` → 409，不影响当前实例。
8. cleanup 阶段 `execute_step` 的 `step_index` / `step_count` 取自 cleanup，不与 steps 混用。
9. 中止按钮只出现在状态表；`_instance_result.html` 不含按钮，但能在一次轮询内显示「中止中」→「已中止」。
10. 通用中断：当前步骤声明 `abort` → 快路径调用该中断命令；未声明 → 只置标志、不调用、不报错。
11. `abort` 声明指向不存在的命令 → `get_registry()` 加载失败。
12. `cancel_requested` 为真时，当前步骤返回任意错误码都进入 cleanup 且终态为 `aborted`，不需要 actor 认识具体错误码。

## 11. 验收标准

1. 预约一条含 `delay` 的多步 action，不修改 actor，页面能看到 pending → running 逐步推进（进度、下一步、等待时刻）→ 成功/失败。
2. 停掉 actor：状态表仍显示历史；running 行进度降级为 `—`，页面无 500。
3. Phase 2 后点击「执行」：请求立即返回，结果区显示等待中 → 执行中 → 终态；刷新页面后状态表仍能看到该实例与结果。
4. 立即执行实例 `kind = "immediate"`，`scheduled_at` 与 `created_at` 一致。
5. 删除清单中的函数不再存在，`views.py` 不再直接执行 command。
6. 已接受语义：actor 不在线时立即执行停在 pending 至过期；同 tick 多实例只跑最新、其余 superseded。
7. 本轮不产生 `lidar-refresh` 自动刷新；lidar 卡片行为与第 8 节一致。
8. 配置含 `cleanup` 的 action：成功 / 失败 / 中止三种情况下 cleanup 都执行一次，`run.cleanup` 在结果页可见。
9. 执行中点「中止」：实例 `running → cancelling → aborted`，录制被打断（sidecar `aborted`），设备回到 cleanup 定义的安全态；重复点击不重复执行。
10. actor 崩溃后重启：`running` / `cancelling` 实例标记为 `failed`，不假装 cleanup 已执行。

## 12. 已知限制与后续

- `/current` 只覆盖当前实例；actor 单线程，无法同时展示多个执行中实例。
- 每个打开的状态页每 2 秒产生一次本机 `/current` 请求；当前规模可忽略。
- 立即执行依赖 actor 在线；离线时最长等待 `expire_after_seconds` 才可见过期。
- `superseded` 是现有调度语义的直接结果，立即执行沿用，不做排队。
- 正在执行的实例可中止（`aborted`），pending 可取消（`cancelled`）；不支持暂停、续跑或回滚已执行步骤。
- 完成历史固定保留 20 条，不做分页与归档。
- actor 崩溃或断电期间不保证 cleanup 执行；遗留 `running` / `cancelling` 实例重启后统一标记 `failed`。
- `lidar-refresh` 的替代方案延后，见第 8 节。

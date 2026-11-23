# Recorder 录制服务 Specification

## 1. 目标

把激光雷达 UDP 抓包做成一个可被 action 调用的能力，并让每条 pcap 自带一份可追溯的 sidecar：

1. `daemon/recorder.py`：独立常驻进程，独占 dumpcap / tcpdump，负责单实例录制、状态机、sidecar 落盘。
2. `recorder` Django app：只读状态卡片 + `recorder.record` command handler，不自己抓包、不自己写 sidecar。
3. sidecar 记录：pcap 基础信息、雷达 SN / 转速、现场标定、所属 action 与步骤位置。

设计原则：

- **一条 pcap 对应一份 sidecar**，同目录、同 stem。
- **录制开始前完成所有校验**（配置、标定、目录/配额、雷达可达），失败不产生无元数据的 pcap。
- **action 只传上下文，不传标定值**；标定由 recorder 从唯一真源读取。
- **web 只读**，不提供绕过 action 的录制按钮。

## 2. 范围

### 2.1 包含

1. `device_config.json → recorder` 的读取：`win_interface` / `linux_interface` / `udp_port` / `port` / `device_ip` / `request_timeout_seconds`。
2. 输出目录复用 `artifact` 配置与 `artifact.service` 的路径解析、配额判定。
3. `calibration.json` 的严格校验与快照。
4. 抓包命令构造（Windows dumpcap / Linux tcpdump）、单实例互斥、进程退出处理。
5. sidecar 的 schema、写入时机、原子写、重启恢复。
6. recorder 守护进程的本地 HTTP API（status / record / abort）。
7. `recorder` Django app 的 URL、视图、模板、只读卡片。
8. `recorder.record` command 的 schema、handler、错误映射。
9. 单次录制中止：daemon `POST /abort` 与 `recorder.abort` command（见 5.8）。

### 2.2 不包含

- 不支持暂停、续录；中止后不可恢复，也不保留可续录的中间状态。
- 不提供 web 端手动录制入口，录制只能由 action 触发。
- 不做多网卡、多雷达并发录制；进程内单实例。
- 不做 pcap 解析、点云转换、上传、压缩、分片。
- 不做 BPF 过滤配置项；固定 `udp port {udp_port}`。
- 不引入数据库模型、消息队列、第三方依赖。
- 不修改 `device_config.json` 的既有键；`recorder.dumpcap_path` 为可选新增键。
- 不实现 action 的 vars / constants / `${}` 功能（已决定不做）。

## 3. 总体架构

```text
action step (web 同步执行 / actor 预约执行)
        │  ctx: run_id, action_id, step_index, step_count, action 快照
        ▼
recorder.commands.record  (Django 进程内)
        │  POST http://127.0.0.1:{recorder.port}/record
        ▼
daemon/recorder.py  (常驻，独占抓包工具)
        ├── 预检：recorder 配置 / artifact 目录 / calibration.json / lidar HTTP
        ├── 查 lidar：get_standby_mode() == 0，再取 SN 与 spin_rpm
        ├── 写 sidecar（status=recording）
        ├── spawn dumpcap / tcpdump，阻塞等待退出
        └── 更新 sidecar（succeeded / failed）

recorder Django app
        ├── GET /recorder/            只读卡片
        ├── GET /recorder/frag/card   htmx 片段
        └── GET /recorder/status      （可选）透传守护进程状态
```

职责边界：

| 组件 | 负责 | 不负责 |
| --- | --- | --- |
| action | 解析步骤、提供 ctx | 不读标定、不查雷达、不写 sidecar |
| recorder command | 校验 daemon 可达、转发请求、映射错误 | 不构造抓包命令、不落盘 |
| recorder daemon | 配置/标定/设备预检、抓包、sidecar | 不解析 action 语义，只存快照 |
| recorder web | 展示状态与配置 | 不触发录制、不修改文件 |

## 4. 配置与文件布局

### 4.1 `device_config.json → recorder`

现有键（已存在，spec 只消费）：

| 键 | 类型 | 说明 |
| --- | --- | --- |
| `win_interface` | string | Windows 抓包网卡名，如 `WLAN 2`，来自 `dumpcap -D` |
| `linux_interface` | string | Linux 抓包网卡名，如 `eth0` |
| `udp_port` | integer | 雷达 UDP 目的端口，1–65535，默认 2368 |
| `port` | integer | daemon HTTP 端口，1–65535，当前为 6 |
| `device_ip` | string | daemon 绑定地址，必须为回环地址（`127.0.0.1`） |
| `request_timeout_seconds` | number | 客户端查询超时，默认 1.5 |
| `dumpcap_path` | string | **可选**，仅 Windows；显式指定 dumpcap |

读取规则：

- 按 `sys.platform == 'win32'` 选 `win_interface`，否则选 `linux_interface`。
- 网卡名缺失、空白 → `config_invalid`，拒绝录制。
- 端口、超时非法（bool 不算 number/int）→ `config_invalid`。
- `device_ip` 不是回环地址 → daemon 启动失败，不允许对外监听。

### 4.2 输出目录（来自 artifact）

不重复实现路径逻辑，直接复用 `artifact.service`：

```python
section, err = artifact.service.load_artifact_config()
raw = artifact.service.read_local_folder(section)      # 按平台选 win/linux
folder = artifact.service.resolve_local_folder(raw)    # 展开变量/~，锚定 BASE_DIR
```

- 目录不存在、不可写、配额已满（`overview['recording_enabled'] == False`）→ `artifact_unavailable`。
- 录制中 artifact 页面必须禁止删除当前 pcap（见第 9.3 节）。
- 目录只由 daemon 写入；web 与 action 不写。

### 4.3 `calibration.json`

固定路径：`device_control_server/calibration.json`，与 `device_config.json` 同级。

```json
{
  "schema_version": 1,
  "alpha1_degrees": 1.5,
  "alpha2_degrees": -1.5,
  "arm_meter": 0.12,
  "calibrated_at": "2026-11-20T02:00:00Z",
  "note": "更换雷达后重标"
}
```

校验规则（`recorder/calibration.py`，纯库，不依赖 Django）：

| 键 | 规则 |
| --- | --- |
| `schema_version` | 必须等于 `1` |
| `alpha1_degrees` | number（排除 bool），`-180 ≤ x ≤ 180` |
| `alpha2_degrees` | number（排除 bool），`-180 ≤ x ≤ 180` |
| `arm_meter` | number（排除 bool），`0 < x ≤ 10` |
| `calibrated_at` | 可选，ISO 8601 字符串，不做时区推断 |
| 其他键 | 保留并写入 sidecar；daemon 日志记一条 warning |

- 文件缺失 / JSON 非法 / 必填键缺失 / 类型或范围非法 → `calibration_invalid`。
- 必填键缺失能覆盖大部分拼写错误（如 `alpha1_degree`），因此不禁止额外键，保证以后加字段时旧版本不会误拒。
- 返回 `{'values': <文件原样 dict>, 'mtime': <UTC ISO>, 'sha256': <64 位十六进制>}`。
- **严格失败**：录制前校验不通过即拒绝，不产生 pcap。

### 4.4 文件命名

`record_id` 使用 UTC 微秒时间戳：

```text
record-20261122T101500123456Z
record-20261122T101500123456Z.pcap
record-20261122T101500123456Z.record.json
```

- 同目录、同 stem，便于配对和人工查找。
- 生成前检查同名文件；已存在则追加 `-1`、`-2`，不覆盖。
- 时间戳统一 UTC；页面展示时再转本地时区。

## 5. `daemon/recorder.py`

### 5.1 启动与依赖

运行方式（在 `device_control_server` 目录下）：

```text
..\venv\Scripts\python.exe -m daemon.recorder
```

- 参照 `daemon/actor.py`：`sys.path` 插入项目根、设置 `DJANGO_SETTINGS_MODULE`、`django.setup()`，以便复用 `artifact.service`。
- 复用 `lidar.service`（纯库）查询 SN 与转速。
- 使用 `ThreadingHTTPServer`，保证录制期间 `GET /status` 仍可响应。
- 仅监听 `recorder.device_ip`，必须回环；不提供鉴权，依赖本机访问控制。
- 日志输出到 stdout，格式与 actor 一致；单次 tick 异常不得退出进程。

权限前提：

- Windows：安装 Npcap / Wireshark，`dumpcap` 可执行；必要时以管理员身份运行。
- Linux：`tcpdump` 存在，进程具备 root 或 `CAP_NET_RAW`；绑定端口 6 通常也需要 root。
- 权限不足按 `capture_tool_missing` / `capture_permission_denied` 明确报错，不做提权尝试。

### 5.2 状态机

```text
idle ──POST /record（预检通过）──▶ recording ──进程自然退出──▶ idle
  ▲                                    │
  │                                    ├── POST /abort ──▶ sidecar=aborted ──▶ idle
  └──────── 预检失败 / 进程启动失败 ◀──┘
```

`POST /abort`（5.8）只中止当前录制、不停止 daemon；与 daemon 收到 SIGTERM 走的 `failed/daemon_stopped`（5.7）不同。

进程内只允许一个 `recording`：

- 用 `threading.Lock` 或事件保护当前录制；第二个请求返回 `409 recorder_busy`。
- `current` 持有：`record_id`、pcap 路径、sidecar 路径、子进程、`started_at`、`requested_duration`、`action` 快照、`lidar` 快照、`calibration` 快照。
- 录制期间不接受任何修改配置的请求（v1 没有此类接口）。

### 5.3 预检顺序

严格按以下顺序，任何一步失败立即返回，**不 spawn、不写文件**：

1. 校验 `duration`：int（排除 bool），`1 ≤ duration ≤ 600`。
2. 读取 `recorder` 配置并校验网卡、`udp_port`、`port`。
3. 解析抓包工具：`dumpcap_path` → PATH → `C:\Program Files\Wireshark\dumpcap.exe`；Linux 用 `shutil.which('tcpdump')`。找不到 → `capture_tool_missing`。
4. 读取 artifact 配置、解析目录、调用 `artifact.service.get_card()`，要求 `recording_enabled == True`。
5. 读取并校验 `calibration.json`。
6. 查询雷达，顺序固定：
   - `lidar.service.get_standby_mode()` → 必须为 `0`（In Operation）；为 `1` → `lidar_standby`，立即终止预检；
   - `lidar.service.get_device_info()` → `SN`；
   - `lidar.service.get_spin_rate()` → `rpm`。
   - 网络/超时 → `lidar_unreachable`；响应非法 → `lidar_response_invalid`。
   - v1 不做配置兜底：SN 只能来自设备。
   - 待机时雷达停止发数据，抓到的 pcap 没有有效内容，因此必须在 spawn 前拦截。
7. 生成 `record_id` 与路径，确认不冲突。

顺序理由：先做本地可判定项，最后才访问设备；避免标定/目录已坏还去占设备。

### 5.4 抓包命令

与 `request-pandar/record_lidar_pcap.py` 保持一致：

```text
# Windows
dumpcap -i <interface> -f "udp port <udp_port>" -a duration:<duration> -w <pcap>

# Linux
timeout -s INT <duration> tcpdump -i <interface> -n -U -w <pcap> udp port <udp_port>
```

- 参数一律用列表传给 `subprocess.Popen`，不拼 shell 字符串。
- Windows 正常停止返回 `0`；Linux `0` 与 `124`（timeout 发送 SIGINT）均视为成功。
- 子进程 `stdout`/`stderr` 合并读取，边读边累积（上限 16 KB），解码后写入 sidecar 的 `process.output`，成功与失败都保留。
- 录制期间不读取 pcap、不移动文件、不改名。

### 5.5 sidecar schema

```json
{
  "schema_version": 1,
  "record_id": "record-20261122T101500123456Z",
  "status": "succeeded",
  "pcap_file": "record-20261122T101500123456Z.pcap",
  "requested_duration_seconds": 10,
  "started_at": "2026-11-22T10:15:00.123456Z",
  "finished_at": "2026-11-22T10:15:10.234567Z",
  "interface": "WLAN 2",
  "udp_port": 2368,
  "bpf": "udp port 2368",
  "pcap_size_bytes": 123456,
  "lidar": {
    "sn": "PANDAR-XXXX-001",
    "spin_rpm": 600
  },
  "calibration": {
    "values": {
      "schema_version": 1,
      "alpha1_degrees": 1.5,
      "alpha2_degrees": -1.5,
      "arm_meter": 0.12,
      "calibrated_at": "2026-11-20T02:00:00Z",
      "note": "更换雷达后重标"
    },
    "mtime": "2026-11-20T02:00:00Z",
    "sha256": "<64 位十六进制>"
  },
  "action": {
    "kind": "scheduled",
    "instance_id": "record_600rpm-20261122101400000000",
    "run_id": "record_600rpm-20261122T101400123456Z",
    "action_id": "record_600rpm",
    "title": "600 rpm 录制 10 秒",
    "scheduled_at": "2026-11-22T10:15:00Z",
    "started_at": "2026-11-22T10:15:00.000001Z",
    "step_index": 3,
    "step_count": 4,
    "steps": [
      {"command": "lidar.spin", "args": {"rpm": 600}, "delay": 5},
      {"command": "recorder.record", "args": {"duration": 10}, "delay": 0}
    ]
  },
  "process": {
    "command": ["C:\\Program Files\\Wireshark\\dumpcap.exe", "-i", "WLAN 2", "..."],
    "returncode": 0,
    "output": "Capturing on 'WLAN 2'; Packets captured: 1024; Packets received/dropped on interface 'WLAN 2': 1050/26 (97.5%)",
    "output_truncated": false
  },
  "error": null
}
```

字段约定：

- `status`：`recording` / `succeeded` / `failed` / `interrupted` / `aborted`。
- `process.output`：抓包工具合并后的原始输出，包含 dumpcap / tcpdump 打印的收包与丢包统计；成功与失败都写，超过 16 KB 截断并置 `output_truncated: true`。**不另外解析 `stats` 字段**，需要时从文本读取。
- `lidar`：只来自设备，不写 `source` 这类恒定字段。
- `calibration.values`：**整体抄写**校验后的 `calibration.json`，不逐字段挑选，字段增加无需改 recorder。
- `action.step_index`：**0 基**，与 `run.steps[].index`、页面 `step_<i>_*` 一致；人读为第 `step_index + 1` 步。
- `action.step_count`：解析后的总步数；必须满足 `0 ≤ step_index < step_count`。
- `action` 不写 `status` / `run` / `finished_at`：录制时这些值尚不完整，完整结果用 `instance_id` / `run_id` 回查实例文件。
- `action` 缺失或非法（例如 `step_index >= step_count`）→ 请求按 `invalid_action_context` 拒绝，不开始录制。
- `error`：`{"code": "...", "message": "...", "detail": {...}}` 或 `null`。

写入时机：

1. 预检通过、spawn 之前：写 `status: "recording"`，此时 `finished_at`、`pcap_size_bytes`、`process.returncode` 为 `null`。
2. 进程退出后：重写完整内容，状态改为成功/失败，补 `finished_at`、`pcap_size_bytes`、`process.returncode`、`process.output`。
3. 预检失败：不写 sidecar（没有 pcap 可对应）。
4. spawn 失败：写 `status: "failed"`，`pcap_file` 保留预期名，附错误码。

原子写：先写 `.tmp`，再 `os.replace`，与 `action.service.save_instance` 一致。

### 5.6 HTTP API

监听 `http://{recorder.device_ip}:{recorder.port}`，仅处理 `127.0.0.1`。

#### `GET /status`

```json
{
  "status": "idle",
  "record_id": null,
  "pcap_file": null,
  "started_at": null,
  "requested_duration_seconds": null,
  "elapsed_seconds": 0
}
```

录制中 `status` 为 `"recording"`，并返回当前 `record_id`、`pcap_file`、`started_at`、`elapsed_seconds`。

#### `POST /record`

请求：

```json
{
  "duration": 10,
  "action": {
    "kind": "immediate",
    "instance_id": null,
    "run_id": "record_600rpm-20261122T101400123456Z",
    "action_id": "record_600rpm",
    "title": "600 rpm 录制 10 秒",
    "scheduled_at": null,
    "started_at": "2026-11-22T10:15:00.000001Z",
    "step_index": 3,
    "step_count": 4,
    "steps": [ ... ]
  }
}
```

- 阻塞直到录制结束，再返回最终 sidecar；请求线程与抓包子进程分离，`GET /status` 不受影响。
- 请求体上限 256 KB；非 JSON / 缺 `duration` → 400。

成功响应（HTTP 200）：

```json
{"ok": true, "record": { <最终 sidecar 内容> }}
```

失败响应：

```json
{"ok": false, "error": {"code": "calibration_invalid", "message": "..."}}
```

| HTTP | code | 含义 |
| --- | --- | --- |
| 400 | `invalid_duration` / `invalid_action_context` | 参数问题 |
| 409 | `recorder_busy` | 已有录制在进行 |
| 422 | `config_invalid` / `calibration_invalid` / `artifact_unavailable` | 本地配置或目录问题 |
| 422 | `lidar_standby` | 雷达处于待机模式，无有效数据 |
| 422 | `lidar_unreachable` / `lidar_response_invalid` | 雷达查询失败 |
| 422 | `capture_tool_missing` / `capture_permission_denied` | 抓包工具问题 |
| 200 | `capture_failed`（放在 `record.status` / `record.error`） | 进程启动或运行失败，sidecar 仍保留 |

### 5.7 并发与重启恢复

- 同一时刻只有一个录制；忙时直接 409，不排队。
- 启动时扫描输出目录的 `*.record.json`：
  - `status == "recording"` 且没有对应活跃进程 → 改为 `"interrupted"`，`error.code = "daemon_restarted"`，`finished_at` 填当前时间。
  - 不删除、不截断已有 pcap。
- 不自动续录、不自动重试。
- daemon 收到 SIGINT / SIGTERM：
  - 若正在录制，向子进程发终止信号，等待退出，落盘为 `"failed"`，`error.code = "daemon_stopped"`。
  - 超时未退出则记录后直接退出，下次启动按 restart 规则处理。

### 5.8 单次录制中止（`POST /abort`）

新增本地 HTTP 端点，用于中止当前正在进行的单次录制（不暂停、不续录、不停止 daemon）：

```
POST http://{recorder.device_ip}:{recorder.port}/abort
{"record_id": "<可选，当前录制 ID>"}
```

行为：

1. **空闲**（无 `current`）→ `200 {ok: true, aborted: false}`，幂等 no-op，不报错、不产生 sidecar。
2. **`record_id` 不匹配**（请求体提供了 `record_id` 且与当前不一致）→ `409 {ok: false, error: {code: "record_mismatch", ...}}`，不误停其他录制。
3. **匹配或未提供 `record_id`** → 终止抓包子进程，把 sidecar 终态写为 `aborted`，保留 `pcap_file`、`pcap_size_bytes`、`process.output`、`process.returncode`，`error.code = "recorder_aborted"`；随后回到 `idle`。
4. 与进程退出分离：不设置 daemon 的全局停止标志，daemon 继续存活；SIGINT/SIGTERM 仍按 5.7 走 `failed/daemon_stopped`。
5. 终态判定优先级：`_record_locked` 在进程退出后先判断「是否收到过 abort」，再判断返回码 / pcap 大小，避免中止被「pcap 为空 = 失败」覆盖。
6. sidecar 一旦写入终态（`succeeded` / `failed` / `aborted` / `interrupted`）不可再改写；abort 到达时若录制已自然结束，返回 `200 {ok: true, aborted: false}`，不回溯改写。

`GET /status` 不受影响，录制中仍返回 `recording`。

action 通过 `recorder.abort` command 调用本端点（cleanup 组可用）；`recorder.record` handler 收到 `status == "aborted"` 的 sidecar 时，抛出 `CommandError('recorder_aborted', ...)`，使 actor 把该步骤记为失败并进入 cleanup（见 action_spec 第 7 节）。

## 6. `recorder` Django app

### 6.1 接入

- `config/settings.py`：`INSTALLED_APPS` 增加 `'recorder'`。
- `config/urls.py`：增加 `path('recorder/', include('recorder.urls'))`。
- `recorder/apps.py`：现有 `RecorderConfig` 保持不变。

### 6.2 URL 与视图

| 方法 | 路径 | 行为 |
| --- | --- | --- |
| GET | `/recorder/` | 首屏渲染只读卡片 |
| GET | `/recorder/frag/card` | htmx 刷新片段 |

`recorder/views.py`：

```python
def _card_context() -> dict: ...
```

上下文：

```python
{
    'status': 'ok' | 'offline' | 'config_error' | 'abnormal',
    'status_label': '正常' | '守护进程离线' | '配置错误' | '录制异常',
    'detail': str | None,
    'config': {
        'interface': str | None,
        'udp_port': int | None,
        'folder': str | None,
    },
    'artifact': {
        'ready': bool,
        'used_bytes': int,
        'quota_bytes': int,
        'blocked_reason': str | None,
    },
    'calibration': {
        'ok': bool,
        'values': dict | None,
        'error': str | None,
    },
    'daemon': {
        'reachable': bool,
        'recording': bool,
        'record_id': str | None,
        'pcap_file': str | None,
        'elapsed_seconds': float | None,
    },
}
```

判定规则：

- `recorder` 配置非法 → `config_error`，仍展示 artifact / calibration。
- daemon 不可达 → `offline`；不把“配置本身错误”误报成离线。
- daemon 返回录制中 → 展示当前 `record_id` / `pcap_file` / 已录秒数。
- `calibration` 与 `artifact` 各自独立降级，不因一项失败抹掉其他项。

### 6.3 模板

`recorder/templates/recorder/index.html` + `_card.html` + `_card_body.html`，沿用现有卡片结构：

- 状态横幅：`status_label` + `detail`。
- 设备与输出：网卡名、`udp_port`、输出目录。
- 配额：`Used / Quota`，复用 artifact 卡片文案。
- 标定：`alpha1_degrees`、`alpha2_degrees`、`arm_meter`、`calibrated_at`；非法时显示错误，不显示数值。
- 当前录制：`record_id`、`pcap_file`、已录秒数。
- 一个“刷新”按钮：`hx-get="{% url 'recorder:card' %}"`，目标 `#recorder-body`。
- v1 不做自动轮询，不新增 CSS；所有值由 Django 模板默认转义。

### 6.4 只读原则

- 不提供“开始录制 / 停止录制”按钮。
- 不提供删除 pcap 的入口（artifact 页面负责文件管理）。
- web 不读 sidecar 列表，不做历史列表；录制产物天然出现在 artifact 页面。

## 7. `recorder.record` command

文件：`recorder/commands.py`（Django app 内，纯函数，不接触 request）。

```python
COMMANDS = {
    'recorder.record': {
        'label': '录制雷达数据',
        'description': '按指定时长录制 UDP 数据并生成 pcap + sidecar',
        'params': [{
            'name': 'duration',
            'label': '时长（秒）',
            'type': 'integer',
            'required': True,
            'minimum': 1,
            'maximum': 600,
            'default': 10,
            'help': '录制期间该步骤阻塞，结束后才执行后续步骤',
        }],
        'handler': record,
    },
}
```

handler 行为：

1. `load_config()` 读取 `recorder` 段；非法 → `CommandError('config_error', ...)`。
2. 从 ctx 取 `step_index` / `step_count` / `action` 快照；缺失或非法 → `CommandError('invalid_args', ...)`。
3. `POST /record`，请求体 `{"duration": ..., "action": ...}`。
4. 客户端超时 = `duration + 30` 秒（`request_timeout_seconds` 只用于 `GET /status`）。
5. 按第 5.6 节的 code 映射 `CommandError`：

| daemon code | CommandError code |
| --- | --- |
| `recorder_busy` | `recorder_busy` |
| `calibration_invalid` | `calibration_invalid` |
| `artifact_unavailable` | `artifact_unavailable` |
| `config_invalid` | `config_error` |
| `lidar_standby` | `device_standby` |
| `lidar_unreachable` / `lidar_response_invalid` | `device_unreachable` |
| `capture_tool_missing` / `capture_permission_denied` | `capture_unavailable` |
| `capture_failed` / `daemon_stopped` | `capture_failed` |
| `recorder_aborted` | `recorder_aborted` |
| 连接失败 / 超时 | `recorder_unreachable` |

6. 成功返回：

```python
{
    'record_id': ...,
    'pcap_file': ...,
    'duration_seconds': 10,
    'elapsed_seconds': 10.1,
    'sidecar': 'record-....record.json',
}
```

7. `action/registry.py` 的 `COMMAND_MODULES` 增加 `'recorder.commands'`。

#### `recorder.abort`

（Phase 3 新增）调用 daemon `POST /abort`；参数可选 `record_id`。空闲时 no-op 返回成功，不抛错；`record_id` 不匹配抛 `CommandError('record_mismatch', ...)`。用于 action 的 cleanup 组与 actor 中止快路径（见 action_spec 第 7 节）。

## 8. 与 action 的契约

### 8.1 ctx 扩展

当前 `execute_step` 传给 handler 的 ctx 只有 `{'run_id', 'action_id'}`（`action/service.py` 约 405–406 行）。需扩展为：

```python
{
    'run_id': ...,
    'action_id': ...,
    'step_index': len(run['steps']),      # 0 基
    'step_count': len(steps),             # 本次 action 解析后的步骤数
    'action': {
        'kind': 'scheduled' | 'immediate',
        'instance_id': str | None,
        'run_id': ...,
        'action_id': ...,
        'title': ...,
        'scheduled_at': str | None,
        'started_at': ...,
        'step_index': ...,                # 与 ctx['step_index'] 一致
        'step_count': ...,
        'steps': [{'command', 'args', 'delay'}, ...],   # 已解析，含表单覆盖
    },
}
```

实现约束：

- `execute_step` 增加可选参数 `execution`（身份 + 已解析 steps），由同步执行与 actor 各自传入；不传时退化为现有 `{'run_id', 'action_id'}`，保持兼容。
- `step_index` 必须在 handler 执行前确定，不能用“handler 返回后的 `len(run['steps'])`”反推。
- `steps` 是**解析后**的值：表单把 rpm 从 600 改成 300，快照里必须是 300。
- 这是 action 侧唯一的执行语义改动；不引入 vars / `${}` / object 参数。

### 8.2 阻塞语义

- `recorder.record` 是同步阻塞步骤：`dumpcap` 退出后才返回，后续步骤（如停转台）在录制结束后才执行。
- 同步执行时，Django 请求线程会被占用 `duration` 秒；部署若用多 worker，需保证并发数 ≥ 1 个录制请求。
- actor 是单线程，录制期间不会推进其他实例；这是 v1 接受的限制。
- 预约实例在录制期间保持 `running`；收到中止后流转 `running → cancelling → aborted`（见 action_spec 第 7 节）。

### 8.3 actions.json 约定

推荐顺序：待机 → 转速 → 转台 → 录制；`delay` 用于等待转速/转台稳定，不是录制时长。

```json
{
  "action_id": "record_600rpm",
  "title": "600 rpm 录制 10 秒",
  "steps": [
    {"command": "lidar.standby", "args": {"mode": 0}},
    {"command": "lidar.spin", "args": {"rpm": 600}, "delay": 5},
    {"command": "turntable.set", "args": {"seconds_per_revolution": 5.0}, "delay": 5},
    {"command": "recorder.record", "args": {"duration": 10}}
  ]
}
```

## 9. 对外部模块的假设与所需改动

### 9.1 lidar

- 依赖 `lidar.service.get_standby_mode()`、`get_device_info()`、`get_spin_rate()`；均已存在。
- 待机模式必须为 `0`，否则拒绝录制。
- 不做 SN 配置兜底；雷达 HTTP 不可达即拒绝录制。
- 查询发生在 spawn 之前，最多三个超时周期（约 4.5 秒）。

### 9.2 calibration

- 新增 `calibration.json` 与 `recorder/calibration.py`。
- 部署时必须提供合法文件；缺失即拒绝录制。
- 修改文件后下一次录制自动生效，不需要重启 daemon，也不需要批量修改预约实例。

### 9.3 artifact

- 录制目录、配额判定直接复用现有 `artifact.service`。
- 新增删除保护：`artifact.service.delete_file()` 在删除前调用 `recorder.service.recording_pcap()`（超时 1.5s）：
  - 返回的文件名等于待删文件 → `正在录制，禁止删除`；
  - daemon 不可达 → 视为没有录制，允许删除（daemon 独占抓包，不可达时不可能正在录制）。
- sidecar（`*.record.json`）与 pcap 一样出现在 artifact 列表中，可下载；不特殊隐藏。

### 9.4 action

- `action/service.py`：ctx 扩展（第 8.1 节）。
- `action/registry.py`：`COMMAND_MODULES` 增加 `'recorder.commands'`。
- 不修改 `daemon/actor.py`；actor 只负责把实例身份传给 `execute_step`。
- 需要额外的转台 command 才能实现“设置转台快慢”，不属于本 spec。

### 9.5 部署

- daemon 需由 systemd / Windows 服务 / 计划任务托管，崩溃后自动拉起。
- Windows 需 Npcap；Linux 需 tcpdump 与抓包权限。
- `recorder.port = 6` 在 Linux 上需要 root（daemon 通常已经为抓包而具备）。
- 网卡名必须与 `dumpcap -D` / `ip link` 输出一致；配置错误在预检阶段返回 `config_invalid`。

## 10. 预期文件变更

| 文件 | 变更 |
| --- | --- |
| `daemon/recorder.py` | 新建：daemon、状态机、预检、抓包、sidecar、HTTP API |
| `recorder/calibration.py` | 新建：`calibration.json` 读取与严格校验 |
| `recorder/service.py` | 新建：配置读取 + daemon HTTP 客户端（status / record / recording_pcap） |
| `recorder/commands.py` | 新建：`recorder.record` 定义与 handler |
| `recorder/views.py` | 新建：只读卡片上下文与两个视图 |
| `recorder/urls.py` | 新建：`/recorder/`、`/recorder/frag/card` |
| `recorder/templates/recorder/index.html` | 新建 |
| `recorder/templates/recorder/_card.html` | 新建 |
| `recorder/templates/recorder/_card_body.html` | 新建 |
| `recorder/tests.py` | 重写：daemon / service / command / 视图测试 |
| `recorder/apps.py` | 保持 |
| `config/settings.py` | `INSTALLED_APPS` 增加 `recorder` |
| `config/urls.py` | 增加 `include('recorder.urls')` |
| `action/service.py` | ctx 扩展 |
| `action/registry.py` | 注册 `recorder.commands` |
| `artifact/service.py` | 删除保护 |
| `calibration.json` | 新建（部署数据） |
| `device_config.json` | 不改（已含 interface / udp_port / port / device_ip / timeout）；`dumpcap_path` 可选 |

不改：`lidar/*`、`artifact` 视图/模板、`deploy` 之外的配置。

## 11. 测试要求

全部使用 fake 进程、临时目录、mock HTTP，不真正抓包、不依赖真机。

### 11.1 daemon 预检

1. `duration` 为 0、601、bool、字符串 → `invalid_duration`，不 spawn、不写文件。
2. 缺网卡名 / `udp_port` 非法 / 非回环 `device_ip` → `config_invalid`。
3. 找不到 dumpcap / tcpdump → `capture_tool_missing`。
4. artifact 目录不存在、不可写、配额已满 → `artifact_unavailable`。
5. calibration 缺失 / JSON 非法 / 缺必填键 / alpha 越界 / `arm_meter=0` / bool 冒充 number → `calibration_invalid`。
6. 待机模式为 `1` → `lidar_standby`，且**未 spawn**。
7. lidar 不可达 / SN 非法 / SpinSpeed 非法 → 对应错误，且**未 spawn**。
8. 预检失败时输出目录不新增任何 pcap / sidecar。

### 11.2 daemon 抓包与 sidecar

1. Windows 命令为 dumpcap 参数列表；Linux 命令为 `timeout -s INT ... tcpdump ...`。
2. 成功退出（Windows 0 / Linux 0 或 124）→ sidecar `succeeded`，含 `returncode`、`pcap_size_bytes`、`finished_at`。
3. 非零退出 → sidecar `failed`，`error.code = "capture_failed"`，`process.output` 保留工具输出。
4. 录制中 `GET /status` 返回 `recording` 与 `elapsed_seconds`，且可被并发访问。
5. 第二个 `POST /record` → `409 recorder_busy`。
6. sidecar `action` 块原样保存请求内容；`step_index` / `step_count` 越界 → `invalid_action_context`。
7. `calibration.values` 与 `calibration.json` 内容逐键一致，含额外键；`sha256` / `mtime` 正确。
8. sidecar 采用原子写：模拟中途失败不留下半个 JSON。
9. `process.output` 原样保存 dumpcap / tcpdump 的收包与丢包统计文本，不生成 `stats` 子对象；超 16 KB 时截断并置 `output_truncated: true`。
10. 输出解码为 UTF-8 → GBK → replace；中文网卡名不乱码。
11. `POST /abort` 空闲时返回 `aborted: false`，不产生 sidecar。
12. 录制中 `POST /abort` → 子进程终止、sidecar `aborted`、`error.code = recorder_aborted`，pcap 与 `process.output` 保留；daemon 不退出。
13. `POST /abort` 带不匹配 `record_id` → `409 record_mismatch`，当前录制不受影响。

### 11.3 重启恢复

1. 目录中存在 `status="recording"` 的 sidecar，启动后变为 `interrupted`，带 `daemon_restarted`。
2. 对应 pcap 不被删除或改写。

### 11.4 command

1. 正常返回 `record_id` / `pcap_file` / `elapsed_seconds` / `sidecar`。
2. ctx 缺少 `action` 或 `step_count` → `invalid_args`，不发请求。
3. 各 daemon code 映射到约定 `CommandError.code`。
4. 连接失败 / 超时 → `recorder_unreachable`。
5. 客户端超时为 `duration + 30`，而不是 `request_timeout_seconds`。
6. sidecar `status == "aborted"` → handler 抛 `CommandError('recorder_aborted', ...)`。

### 11.5 web

1. daemon 在线且空闲 → 显示配置、目录、配额、标定、无录制。
2. daemon 离线 → `offline`，仍展示本地配置与标定。
3. daemon 录制中 → 显示 `record_id` / `pcap_file` / 已录秒数。
4. recorder 配置非法 → `config_error`；artifact / calibration 各自降级展示。
5. `/recorder/frag/card` 可独立渲染并更新。
6. 页面不出现录制/停止/删除按钮。
7. 模板输出经过转义。

### 11.6 artifact 删除保护

1. 待删文件等于 daemon 正在录制的 pcap → 拒绝删除。
2. daemon 不可达 → 允许删除。
3. 其他文件不受影响。

## 12. 验收标准

1. action 中配置 `recorder.record`，执行后输出目录生成同 stem 的 `.pcap` 与 `.record.json`。
2. sidecar 包含 `record_id`、时长、起止时间、网卡/端口/BPF、pcap 大小、`lidar.sn`、`lidar.spin_rpm`、完整 `calibration.values` + `mtime` + `sha256`、`action`（含 `step_index` / `step_count` / 已解析 steps）、`process.output`（含抓包工具打印的收包/丢包统计）。
3. 标定缺失或非法时拒绝录制，不产生 pcap；错误在 action 结果中可见。
4. 雷达待机或不可达时拒绝录制，不产生 pcap；待机错误提示先执行 `lidar.standby(mode=0)`。
5. 同一时刻只允许一个录制；第二个请求得到 `recorder_busy`。
6. 录制期间 `/recorder/` 能看到当前 `record_id` 与已录秒数；artifact 页面无法删除当前 pcap。
7. daemon 重启后，遗留 `recording` sidecar 变为 `interrupted`，pcap 保留。
8. 立即执行与预约执行生成的 sidecar 仅 `action.kind` / `instance_id` / `scheduled_at` 不同，其余结构一致。
9. `device_config.json` 既有键不变；不新增 URL 之外的框架、数据库或第三方依赖。

## 13. 已知限制与后续

- 录制是阻塞步骤：actor 单线程，长录制会推迟其他预约；需要并发时再引入异步记录 ID 机制。
- 单次录制可中止（`POST /abort`，见 5.8）；不支持暂停、续录，中止不保留为可恢复状态。
- 只支持单网卡、单雷达；多雷达需要新增 `recorder.targets[]` 配置与多实例状态机。
- 不解析 pcap 内容，也不把 `process.output` 里的丢包统计结构化；丢包数字仅以工具原始文本保留。
- 预检到 spawn 之间，雷达仍可能被其他客户端切回待机；v1 不消除该竞态。
- 不做标定值的合理性交叉校验（例如 alpha1 与 alpha2 是否互补），只做范围检查。
- `calibration.json` 的写入由人工负责；daemon 不提供写入接口。

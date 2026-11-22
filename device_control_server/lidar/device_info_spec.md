# Lidar 设备卡片查询与展示 Specification

## 1. 目标

在 Lidar Web 卡片中实时查询并展示设备的以下信息：

- `SN`：设备序列号
- `ProdName`：产品名称
- 待机模式（Standby Mode）
- 转速（Spin Rate，rpm）

并提供设置待机模式和转速的能力。每次页面请求都实时访问设备，不缓存、不轮询、不后台化。卡片按设备型号决定哪些功能适用：**支持时请求并显示，不支持时不请求、不显示、也不算错误**。

本规格是 `lidar/` 模块对外行为的全新契约。`lidar` app 的边界、URL、命令名和异常分类保持稳定，但其内部 service / 视图实现可按本契约整体重写，无需区分“原有改动”与“新增扩展”。

## 2. 依据与约束

### 2.1 设备端参考

全部接口与字段名以 `request-pandar/Pandar Console/js/pandar.js` 为准：

| 用途 | 参考位置 | 请求 |
| --- | --- | --- |
| 探活 + 型号 | `loadDeviceInfo()` / 页面启动段 | `GET /pandar.cgi?action=get&object=device_info` |
| 待机查询 | `loadSettingInfo()` | `GET /pandar.cgi?action=get&object=lidar_data&key=standbymode` |
| 待机设置 | 现有实现 | `POST /pandar.cgi?action=set&object=lidar_data&key=standbymode&value={mode}` |
| 转速查询 | `loadStatus()` | `GET /pandar.cgi?action=get&object=lidar_config`（读取 `Body.SpinSpeed`） |
| 转速设置 | `saveSetting()` | `POST /pandar.cgi?action=set&object=lidar&key=spin_speed&value={index}` |

`SpinSpeed` 是索引值，显示用 rpm：`rpm = 2^(index-1) × 300`，UI 已知取值 `1→300`、`2→600`、`3→1200`。

### 2.2 型号 capability 证据

参考页用 `check_item_display(prodName, item)` 按 `ProdName` 隐藏不适用的行：

- `standby-mode-row` 在 `Pandar40AC`、`Pandar40P-AC` 下隐藏 → 这两个型号不支持待机。
- `s_spin_rate` 在 `PandarQT` 下隐藏 → 该型号不支持转速。
- 转速 300 rpm（`SpinSpeed=1`）仅对 `PandarXT`、`PandarXT-16`、`XT32M2X-B01` 可选；其余型号仅 `600`、`1200`。
- 参考页对未列举的 `ProdName` 默认返回“不显示”；本规格为避免新型号被整体隐藏，采用相反默认（见 3.2），属有意偏离，需在评审时确认。

> 注：参考页只证明“界面隐藏”，不证明设备端会返回错误码。各型号在不适用接口上的真实返回值需真机确认；本规格的 capability 表只决定是否发起请求，不能替代真机验证。

### 2.3 必须保持的对外契约

以下内容不是本期改动范围，重写实现时不得破坏：

- `lidar` app 名称、`INSTALLED_APPS`、`config/urls.py` 的 `include('lidar.urls')`。
- URL：`GET /lidar/`、`GET /lidar/frag/card`，不新增 URL。
- `lidar.standby` 命令名、其参数 schema（`mode` 0/1）与成功/失败映射。
- action 成功后 `HX-Trigger: lidar-refresh` 触发卡片刷新的机制。
- `device_config.json` 的 `lidar` 段：`device_ip`、`port`、`request_timeout_seconds`（默认 1.5），不新增配置项。
- 异常体系：`LidarConfigError` / `LidarUnreachableError` / `LidarResponseError` / `LidarArgumentError` 及 `LidarResponseError.error_code`、`.body` 诊断约定。

## 3. 范围

### 3.1 包含

1. service 层：泛化 CGI 请求；提供 `get_device_info()`、`get_standby_mode()` / `set_standby_mode()`、`get_spin_rate()` / `set_spin_rate()`、以及按 `ProdName` 判定的 capability 表。
2. 视图层：以 `device_info` 为探活构造一次“设备快照”，按 capability 请求可选字段，失败时短路或字段级降级。
3. 模板：展示 `ProdName`、`SN`、待机模式、转速；不适用或失败的字段不渲染。
4. 命令层：新增 `lidar.spin` 命令（设置转速），handler 实时校验 capability 与允许取值。
5. 覆盖 service / 视图 / 模板 / 命令的测试。

### 3.2 不包含

- 不展示或处理 `Body` 中除本期字段外的其他字段（`Model`、`SW_Ver`、`Mac` 等）。
- 不新增 URL、配置项、数据库模型、前端框架、第三方依赖。
- 不增加自动轮询、缓存、后台线程或常驻任务；不并发请求设备。
- 不做 `ProdName` 到展示名的映射（如 `PandarQT→PandarQT-64`），页面显示设备返回的原始值。
- 不在本规格内引入除 `standby`、`spin_speed` 外的其他可设置项。
- 未知 `ProdName` 的 capability 默认策略：**默认视为全部支持**（与参考页相反），以便新型号不因未列入表而整体隐藏；评审时确认是否保留。

## 4. 数据模型：设备快照

视图向模板提供一次请求构造出的快照：

```python
{
    'status': 'ok' | 'offline' | 'abnormal' | 'config_error',
    'status_label': '在线' | '离线' | '设备异常' | '配置错误',
    'device': 'host:port' | None,
    'detail': '<错误详情字符串>' | None,
    'device_info': {'SN': '...', 'ProdName': '...'} | None,
    'mode': 0 | 1 | None,
    'mode_label': '运行 In Operation' | '待机 Standby' | None,
    'spin': {'index': 1 | 2 | 3, 'rpm': 300 | 600 | 1200} | None,
}
```

`status` 只由**探活**（`device_info`）结果决定，不随后续字段失败而降级。`detail` 可与 `status == 'ok'` 同时存在：表示某个支持的字段请求失败，此时该字段留空但卡片仍显示已获取到的其他字段。

## 5. 请求顺序、capability 与短路

### 5.1 固定顺序

一次成功刷新按以下顺序**串行**发出请求：

1. `load_config()` —— 失败即返回 `config_error`，**不发任何设备请求**。
2. `get_device_info()` —— **探活**；成功后得到 `ProdName`，据此解析 capability。
3. `get_standby_mode()` —— 仅当 `capability.standby` 为真时请求。
4. `get_spin_rate()`（即 `get_lidar_config` 中的 `SpinSpeed`）—— 仅当 `capability.spin_rate` 为真时请求。

### 5.2 短路与降级

| 阶段 | 结果 | 行为 |
| --- | --- | --- |
| 配置 | `LidarConfigError` | 返回 `config_error`，`device=None`，不请求设备 |
| 探活 `device_info` | `LidarUnreachableError` | 返回 `offline`，**短路**，不再请求 standby / spin |
| 探活 `device_info` | `LidarResponseError` | 返回 `abnormal`，**短路**，不再请求后续 |
| 探活成功 | — | `status='ok'`；继续按 capability 请求可选字段 |
| 可选字段 | capability 为假 | **不请求**，字段为 `None`，模板不渲染，不计入错误 |
| 可选字段 | 请求失败（支持的型号） | 该字段为 `None`、不渲染；**不短路**，继续请求其余可选字段；把首条错误写入 `detail` |

即：**只有探活失败才短路**；可选字段失败只影响自身，不抹掉已经获取的 `device_info` 或其他字段。

### 5.3 延迟

单次超时默认 1.5 秒，串行不并发：

- 探活失败：约 1 个超时周期（1.5s）即返回。
- 探活成功且设备支持全部字段：最坏约 3 个超时周期（4.5s，`device_info` + standby + `lidar_config` 均接近超时）。
- 本期不引入并发或后台查询；字段增多时延迟线性增长，届时再评估并发化。

## 6. Service 层规格

### 6.1 配置

`load_config()` 保持现有行为，返回 `{'host', 'port', 'timeout'}`；失败抛 `LidarConfigError`。不新增配置。

### 6.2 泛化请求

`_request(cfg, query)` 负责构造 `http://{host}:{port}/pandar.cgi?{query}`、按 action 决定 GET/POST（`action=set` 时带空 body）、超时、HTTP/JSON/顶层字典校验。所有字段 getter/setter 共用它。不得改变 standby 既有请求的 action / object / key / value / 方法语义。

### 6.3 capability 表

模块内维护一张按 `ProdName` 判定的能力表，函数形式如 `capabilities(prod_name: str) -> dict`：

```python
{
    'standby': bool,
    'spin_rate': bool,
    'spin_rpms': [300, 600, 1200],   # 该型号允许设置的 rpm 集合
}
```

依据 2.2：

| ProdName | standby | spin_rate | spin_rpms |
| --- | --- | --- | --- |
| `Pandar40AC` | 否 | 是 | 600, 1200 |
| `Pandar40P-AC` | 否 | 是 | 600, 1200 |
| `PandarQT` | 是 | 否 | — |
| `PandarXT` / `PandarXT-16` / `XT32M2X-B01` | 是 | 是 | 300, 600, 1200 |
| 其他 / 未知 | 是（默认） | 是（默认） | 300, 600, 1200（默认） |

capability 只决定“是否发起请求”和“是否允许设置”，**不掩盖请求失败**：capability 为真但设备返回错误，仍按真实失败处理。

### 6.4 `get_device_info()`

```python
get_device_info(cfg=None) -> dict[str, str]
```

- 请求：`GET /pandar.cgi?action=get&object=device_info`，无 `key`/`value`，无 body。
- 校验：复用 HTTP/JSON/顶层校验与 `_check_error_code()`（`str(ErrorCode) == '0'`）；`Body` 必须为字典；`SN`、`ProdName` 必须存在且为非空白字符串。
- 成功仅返回 `{'SN', 'ProdName'}`，丢弃 `Body` 其他字段；任一校验失败整体失败。
- 异常：配置 `LidarConfigError`；网络/超时/非 200 `LidarUnreachableError`；结构/字段/ErrorCode 非法 `LidarResponseError`，错误消息以“查询设备信息”标识。

### 6.5 待机模式

`get_standby_mode(cfg=None) -> int` 与 `set_standby_mode(mode, cfg=None) -> None` 保持现有对外行为：

- 请求：`get&object=lidar_data&key=standbymode` / `set&object=lidar_data&key=standbymode&value={mode}`。
- 真机 `standbymode` 可能返回字符串 `'0'`/`'1'`，需兼容；`bool`/非法值一律 `LidarResponseError`。
- `set` 时 `mode` 只能是 0/1，否则 `LidarArgumentError`。

### 6.6 转速

```python
SPIN_INDEX_TO_RPM = {1: 300, 2: 600, 3: 1200}

get_spin_rate(cfg=None) -> dict   # {'index': int, 'rpm': int}
set_spin_rate(rpm: int, cfg=None) -> None
```

- `get`：`GET /pandar.cgi?action=get&object=lidar_config`，读取 `Body.SpinSpeed`，按 `SPIN_INDEX_TO_RPM` 转 rpm；`SpinSpeed` 缺失、非整数、不在已知映射中 → `LidarResponseError`。
- `set`：`POST /pandar.cgi?action=set&object=lidar&key=spin_speed&value={index}`，其中 `index` 由 `rpm` 反查；`rpm` 不在映射中 → `LidarArgumentError`。
- 型号允许取值由 6.3 的 `spin_rpms` 限制（在命令 handler 中校验，见 7.2）。
- 异常映射与 device_info 一致，错误消息分别以“查询转速”“设置转速”标识。

## 7. 命令层规格

### 7.1 `lidar.standby`（现有，保持不变）

`mode` ∈ {0,1}。handler 调用 `load_config()` + `set_standby_mode()`，异常映射到 `CommandError`：`invalid_args` / `config_error` / `device_unreachable` / `device_error`（携带 `error_code`、`body`）。

### 7.2 `lidar.spin`（新增）

在 `lidar/commands.py` 的 `COMMANDS` 新增：

```python
'lidar.spin': {
    'label': '设置雷达转速',
    'description': '按设备型号支持的 rpm 设置转速',
    'params': [{
        'name': 'rpm',
        'label': '转速',
        'type': 'integer',
        'required': True,
        'choices': [300, 600, 1200],
        'choice_labels': {'300': '300 rpm', '600': '600 rpm', '1200': '1200 rpm'},
        'default': 600,
    }],
    'handler': spin,
}
```

handler 行为：

1. `load_config()`，失败 → `config_error`。
2. `get_device_info()` 取 `ProdName`，解析 capability；若 `spin_rate` 为假 → `CommandError('not_supported', '该型号不支持转速设置')`。
3. 校验 `rpm ∈ capabilities(spin_rpms)`，不满足 → `CommandError('invalid_args', ...)`。
4. `set_spin_rate(rpm)`；网络/响应异常映射为 `device_unreachable` / `device_error`。
5. 成功返回 `{'rpm', 'index', 'device'}`，action 层触发 `lidar-refresh`。

schema 的 `choices` 为全集，真实取值约束在 handler 按型号收紧，避免为静态 schema 引入设备查询。

## 8. Web 展示规格

### 8.1 页面内容

`lidar/templates/lidar/_card_body.html` 渲染顺序，每行独立 `{% if %}` 守卫，值为空或不适用则整行不输出：

| 行 | 模板数据来源 | 守卫条件 |
| --- | --- | --- |
| 状态横幅 | `status_label`、`status` | 始终 |
| 设备地址 | `device` | `device` 存在；否则显示“（未配置设备地址）” |
| 产品名称 | `device_info.ProdName` | `device_info` 存在 |
| 序列号 | `device_info.SN` | `device_info` 存在 |
| 待机模式 | `mode`、`mode_label` | `mode` 非空 |
| 转速 | `spin.rpm` | `spin` 非空 |
| 错误详情横幅 | `detail` | `detail` 非空（即使 `status` 为 ok 也显示） |

值由 Django 模板默认转义，不拼接原始 HTML，不新增 CSS。

### 8.2 上下文构造

`_status_context()` 按第 5 节构造快照：配置错误前置返回；探活失败短路返回；探活成功后按 capability 逐个请求可选字段，单字段失败只写 `detail` 不短路。

### 8.3 更新路径

以下路径共用同一 `_status_context()` 与 `_card_body.html`，行为一致：

1. `GET /lidar/`：首屏同步查询渲染。
2. `GET /lidar/frag/card`：手动“刷新”替换 `#lidar-body`。
3. action 成功后的 `lidar-refresh`：替换同一片段。
4. 门户页 include `lidar/_card.html`：无需独立实现。

不自动定时刷新；所有字段均来自本次请求，不声称是历史缓存值。

## 9. 预期文件变更

| 文件 | 预期变更 |
| --- | --- |
| `lidar/service.py` | 泛化 `_request`；新增 `get_device_info`、capability 表、`get_spin_rate`/`set_spin_rate`；重写或调整 standby 实现以复用泛化请求 |
| `lidar/views.py` | 按设备快照重写 `_status_context()`：探活在前、capability 判定、字段级降级 |
| `lidar/commands.py` | 新增 `lidar.spin` 命令与 handler（含 capability / 取值校验） |
| `lidar/templates/lidar/_card_body.html` | 增加 `ProdName`、`SN`、转速行，每行独立守卫；`detail` 横幅在 ok 时也展示 |
| `lidar/tests.py` | 增加 service / 视图 / 模板 / 命令测试 |

原则上无需修改：`lidar/urls.py`、`lidar/apps.py`、`lidar/models.py`、`lidar/index.html`、`lidar/_card.html`、`device_config.json`、`config/urls.py`、`config/settings.py`、`action/` 模块。

## 10. 测试要求

全部使用 mock / fake 响应，不依赖真实雷达。

### 10.1 Service 测试

1. `get_device_info` 成功仅返回 `SN`、`ProdName`；URL 精确为 `action=get&object=device_info`，无 `key`/`value`，使用配置的 host/port/timeout，方法 GET。
2. `get_device_info`：ErrorCode 非 0 / 非 200 / 连接失败 / 超时 / 非 JSON / 顶层非字典 / `Body` 非字典 / 字段缺失或空白 → 对应异常。
3. `get_spin_rate`：`SpinSpeed` 映射为正确 index/rpm；缺失/非整数/未知值 → `LidarResponseError`；URL 为 `action=get&object=lidar_config`。
4. `set_spin_rate`：rpm 反查 index，URL 为 `set&object=lidar&key=spin_speed&value={index}`，方法 POST；非法 rpm → `LidarArgumentError`。
5. capability 表：对 2.2 各型号返回正确的 `standby`/`spin_rate`/`spin_rpms`；未知型号走默认。
6. standby get/set 请求行为与现有实现一致，不回归（字段类型兼容字符串 `'0'`/`'1'`）。

### 10.2 View / 模板测试

1. 全支持型号且全部成功：显示在线、设备地址、`ProdName`、`SN`、待机模式、转速。
2. 探活不可达 → 显示“离线”与详情，**且断言 standby / spin 未被调用**（短路），不展示任何字段。
3. 探活响应非法 → 显示“设备异常”，同样不请求后续字段。
4. 不支持 standby 的型号（如 `Pandar40AC`）：不调用 `get_standby_mode`，卡片显示在线与其他字段，不渲染待机行。
5. 不支持 spin 的型号（如 `PandarQT`）：不调用 `get_spin_rate`，不渲染转速行。
6. 探活成功但某支持字段请求失败：卡片仍“在线”，该字段不渲染，`detail` 显示错误，其余已获取字段正常显示。
7. 配置错误：不调用任何设备查询，不展示设备信息。
8. `/lidar/frag/card` 用两次不同 mock 返回值验证刷新后内容更新；首屏与片段均不输出 `Body` 中未要求的其他字段。

### 10.3 命令测试

1. `lidar.spin`：支持型号且 rpm 在允许集合内 → 调用 `set_spin_rate` 并返回 `{rpm, index, device}`。
2. 不支持 spin 的型号 → `CommandError('not_supported', ...)`，且不调用 `set_spin_rate`。
3. rpm 不在该型号 `spin_rpms` 内（如对 `Pandar40AC` 传 300）→ `CommandError('invalid_args', ...)`。
4. 网络 / 响应异常映射到 `device_unreachable` / `device_error`（携带 `error_code`、`body`）。
5. `lidar.standby` 行为无回归。

## 11. 验收标准

1. 正常设备下，卡片准确显示 `SN`、`ProdName` 原值、待机模式、转速 rpm。
2. 探活失败时卡片显示离线/设备异常与详情，且不发起 standby / spin 请求。
3. 不支持的功能（按型号）不请求、不显示、不报错；支持的功能请求失败只影响该字段并给出详情，不抹掉其他字段。
4. 首屏、手动刷新、`lidar-refresh` 均实时获取并更新；不出现陈旧或部分拼接的值。
5. `lidar.spin` 能按型号允许值设置转速，不支持或非法取值被明确拒绝；`lidar.standby` 行为无回归。
6. 请求严格符合各 object/key/value 约定，复用现有配置与超时；不新增 URL、配置、依赖、缓存、数据库或自动轮询。
7. 不展示 `Body` 中未要求的其他字段，模板输出经过转义。

## 12. 待确认项

1. 未知 `ProdName` 的 capability 默认“全部支持”是否可接受（3.2）。
2. 各型号在不适用接口上的真实设备返回值，需真机确认以验证 capability 表与异常映射（2.2 注）。
3. 转速已知映射是否只有 300/600/1200（参考 `loadStatus` 公式可推出 2400，但 UI 未提供该选项），需真机确认是否存在 `SpinSpeed=4`。

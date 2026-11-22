# Lidar 设备信息查询与展示 Specification

## 1. 目标

在现有 Lidar Web 卡片中实时查询并展示设备的以下信息：

- `SN`：设备序列号
- `Mac`：设备 MAC 地址
- `ProdName`：产品名称

功能复用现有 `lidar` 配置、CGI 传输、异常分类、页面卡片和 htmx 刷新机制。本规格仅描述后续实现要求，本次不修改功能代码。

## 2. 参考与现状

### 2.1 设备端参考

参考 `request-pandar/Pandar Console/js/pandar.js` 中的 `loadDeviceInfo()`：

```text
GET /pandar.cgi?action=get&object=device_info
```

参考页面从响应 `Body` 中读取 `ProdName`、`Mac`、`SN`。本功能只采用该请求方式和这三个字段，不移植参考页面的其他字段或产品型号特殊判断。

### 2.2 当前服务结构

- `lidar/service.py` 已提供配置读取、CGI 请求、JSON 解析、`Head.ErrorCode` 检查和待机模式查询。
- 当前 `_request()` 的查询参数固定为 `lidar_data/standbymode`，实现时需支持 `device_info` 请求，同时保持待机查询和设置行为不变。
- `lidar/views.py` 的 `_status_context()` 同时服务首屏和 htmx 卡片片段刷新。
- `lidar/templates/lidar/_card_body.html` 是首屏与刷新共用的内容模板。
- `POST` action 成功后会通过 `HX-Trigger: lidar-refresh` 触发 Lidar 卡片刷新。

## 3. 范围

### 3.1 包含

1. 在 service 层增加同步 `get_device_info()` 查询能力。
2. 校验设备响应并提取 `SN`、`Mac`、`ProdName`。
3. 在 Lidar 独立页及复用该卡片的门户页中展示三个字段。
4. 手动点击“刷新”及收到 `lidar-refresh` 事件时重新查询并更新三个字段。
5. 增加 service 和视图/模板测试。

### 3.2 不包含

- 不展示或处理 `Body` 中除 `SN`、`Mac`、`ProdName` 外的字段。
- 不新增设备信息编辑、设置或持久化能力。
- 不新增 JSON API、URL、配置项、数据库模型、前端框架或第三方依赖。
- 不增加自动轮询、缓存、后台线程或常驻任务。
- 不采用参考 JS 中 `PandarQT` 显示为 `PandarQT-64`、以 `Model` 替代 `ProdName` 等特殊映射；页面展示设备返回的原始 `ProdName`。
- 不改变现有待机模式查询、设置及 action command 的对外行为。

## 4. Service 层规格

### 4.1 公共接口

在 `lidar/service.py` 增加：

```python
get_device_info(cfg: dict[str, Any] | None = None) -> dict[str, str]
```

成功时仅返回：

```python
{
    'SN': '<Body.SN>',
    'Mac': '<Body.Mac>',
    'ProdName': '<Body.ProdName>',
}
```

键名与设备响应保持一致。返回字典不包含其他 `Body` 字段。

### 4.2 请求规则

| 项目 | 要求 |
| --- | --- |
| 方法 | HTTP GET |
| 路径 | `/pandar.cgi` |
| Query | `action=get&object=device_info` |
| `key` / `value` | 均不发送 |
| 请求体 | 无 |
| 主机与端口 | 使用现有 `lidar.device_ip`、`lidar.port` |
| 超时 | 使用现有 `lidar.request_timeout_seconds`，默认 1.5 秒 |
| 调用方式 | 当前进程内同步调用，每次实时访问设备，不缓存 |

完整 URL 形式：

```text
http://{host}:{port}/pandar.cgi?action=get&object=device_info
```

实现可适度泛化现有 `_request()` 的 query 构造能力，但不得改变现有 standby 请求的 action、object、key、value 和 GET/POST 语义。

### 4.3 成功响应校验

已知正常响应形如：

```json
{
  "Head": {"ErrorCode": "0", "Message": "Success"},
  "Body": {
    "SN": "device-sn",
    "Mac": "00:11:22:33:44:55",
    "ProdName": "product-name"
  }
}
```

按以下规则处理：

1. 复用现有 HTTP、JSON 和顶层对象校验。
2. 复用 `_check_error_code()`；仅 `Head.ErrorCode` 转换为字符串后等于 `'0'` 时继续。
3. `Body` 必须是字典。
4. `SN`、`Mac`、`ProdName` 必须全部存在，且均为非空字符串；仅包含空白字符也视为非法。
5. 三个字段按设备原值返回，不进行大小写转换、MAC 格式化、产品名映射或其他字段推导。
6. 任一校验失败即整体失败，不返回部分设备信息。

### 4.4 异常规则

沿用现有异常分类，不新增平行异常体系：

- 配置缺失或非法：`LidarConfigError`。
- 网络不可达、超时、非 200 HTTP 响应：`LidarUnreachableError`。
- 非合法 JSON、顶层结构非法、`Head.ErrorCode` 非 0、`Body` 非字典、字段缺失或字段值非法：`LidarResponseError`。
- `LidarResponseError` 应保留现有 `error_code`、`body` 诊断信息约定。
- 错误消息使用“查询设备信息”标识本次操作，便于与“查询待机模式”区分。

## 5. Web 展示规格

### 5.1 页面内容

在 `lidar/templates/lidar/_card_body.html` 中增加三行只读信息，位于设备地址之后、待机模式之前：

| 页面标签 | 模板数据来源 |
| --- | --- |
| 产品名称（ProdName） | `device_info.ProdName` |
| 序列号（SN） | `device_info.SN` |
| MAC 地址（Mac） | `device_info.Mac` |

沿用现有简单文本段落样式，不要求新增 CSS。值由 Django 模板默认转义，不拼接原始 HTML。

### 5.2 视图上下文

扩展 `lidar/views.py` 的 `_status_context()`：

- 增加 `device_info` 上下文键；成功时为 `get_device_info()` 返回的字典，失败或未查询时为 `None`。
- 保持现有调用顺序语义：先查询待机模式，成功后查询设备信息，再返回完整成功上下文。
- `status == 'ok'` 表示本次待机模式和设备信息查询均成功。
- 设备信息查询发生的 `LidarUnreachableError`、`LidarResponseError` 分别沿用现有“离线”“设备异常”映射。
- 采用整卡一致性策略：任一探询失败时，`mode`、`mode_label`、`device_info` 均为空，页面不展示本次未完整获取的字段，只展示对应错误详情；不得保留上一次值。
- 配置错误仍在发出任何设备请求前返回，设备信息同样不展示。

一次成功刷新会顺序发出两个 CGI 请求：待机模式查询和设备信息查询。当前默认单次超时 1.5 秒，因此设备请求部分最坏约为两个超时周期；本期不引入并发或后台查询。

### 5.3 更新路径

以下路径必须使用相同的 `_status_context()` 和 `_card_body.html`，行为一致：

1. `GET /lidar/`：首屏同步查询并显示设备信息。
2. `GET /lidar/frag/card`：点击“刷新”后重新查询并替换 `#lidar-body`。
3. action 成功后的 `lidar-refresh`：重新查询并替换同一片段。
4. 门户页若 include `lidar/_card.html`，无需增加独立实现即可展示相同内容。

不自动定时刷新；设备信息始终来自当前这次页面请求，不声称是历史缓存值。

## 6. 预期文件变更

| 文件 | 预期变更 |
| --- | --- |
| `lidar/service.py` | 泛化内部 CGI query 构造；新增 `get_device_info()` 及响应校验 |
| `lidar/views.py` | 导入并调用 `get_device_info()`；扩展 `_status_context()` |
| `lidar/templates/lidar/_card_body.html` | 增加三个只读字段 |
| `lidar/tests.py` | 增加 service、视图和模板行为测试 |

以下文件原则上无需修改：`lidar/urls.py`、`lidar/commands.py`、`lidar/index.html`、`lidar/_card.html`、`device_config.json`。

## 7. 测试要求

测试全部使用 mock/fake 响应，不依赖真实雷达。

### 7.1 Service 测试

1. 成功响应返回且仅返回 `SN`、`Mac`、`ProdName`，值与 `Body` 一致。
2. 断言请求为 GET，URL 精确包含 `action=get&object=device_info`，不包含 `key`、`value`，并使用配置中的 host、port、timeout。
3. `Head.ErrorCode` 非 0 时抛出 `LidarResponseError`。
4. 非 200、连接失败和超时沿用 `LidarUnreachableError`。
5. 非 JSON、顶层非字典、`Body` 缺失或非字典时抛出 `LidarResponseError`。
6. 分别覆盖三个字段缺失、非字符串、空字符串和纯空白字符串，均整体失败。
7. `Body` 包含额外字段时成功且额外字段不出现在返回值中。
8. 现有 standby 查询和设置测试继续通过，请求行为不回归。

### 7.2 View / 模板测试

1. `/lidar/` 成功时显示在线、设备地址、三个设备信息字段及待机模式。
2. `/lidar/frag/card` 成功时显示相同字段，并可通过两次不同 mock 返回值验证刷新后内容更新。
3. 设备信息查询不可达时显示“离线”和错误详情，不显示三个字段及待机模式。
4. 设备信息响应非法时显示“设备异常”和错误详情，不显示三个字段及待机模式。
5. 配置错误时不调用任何设备查询，不显示设备信息。
6. 首屏和片段均不输出 `Body` 中未要求展示的其他字段。

## 8. 验收标准

1. 正常设备响应下，Web 卡片准确显示 `SN`、`Mac`、`ProdName` 的原值。
2. 独立页首屏、手动刷新和 `lidar-refresh` 刷新均会实时获取并更新设备信息。
3. 设备请求严格为 `GET /pandar.cgi?action=get&object=device_info`，复用现有 lidar 配置和超时。
4. 设备异常、非法响应和配置错误均有明确页面反馈，不展示部分或陈旧的设备信息。
5. 现有待机模式显示、设置及 action 联动行为无回归。
6. 未新增 URL、配置、依赖、缓存、数据库或自动轮询。

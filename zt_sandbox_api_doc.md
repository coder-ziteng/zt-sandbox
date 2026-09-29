# zt-Sandbox HTTP API 参考手册

> 版本：v1 ｜ 2026-09-29 ｜ 对应 commit `ba06a06`（P4 第六刀）
>
> 面向：第三方接入开发者。本文是端点级参考资料，背景与运维请同时阅读 [README.md](README.md) 与 [DESIGN.md](DESIGN.md)。

---

## 0. 速查

| 项 | 值 |
|---|---|
| 控制面 Base URL | `http://${SBX_HOST}:8902` |
| 数据面 Base URL（edge-proxy 子域） | `https://${port}-${sandboxID}.${DOMAIN}/` |
| 数据面直连（同主机） | `http://127.0.0.1:${hostPort}/` |
| 控制面凭据 | `Authorization: Bearer ${API_KEY}` 或 `X-API-KEY: ${API_KEY}` |
| 管理面凭据 | `X-Admin-Token: ${ADMIN_TOKEN}` |
| 公开申请端点 | `POST/GET/DELETE /keys/requests*`（免鉴权；查状态/领取带 `X-Request-Ticket`） |
| 会话头（可选） | `X-Session-Id: ${sessionId}` |
| 数据面鉴权 | `x-access-token: ${envdAccessToken}` |
| 健康检查 | `GET /health`（无鉴权） |
| Prometheus 指标 | `GET /metrics`（无鉴权） |

错误响应统一为 JSON `{code, message, requestID}`，详见 §9。

> **数据面连通性**：edge-proxy 子域走 TCP 443。若客户端与服务器跨网段且中间网关丢弃 443（现象：`8902` 正常、`*.nip.io` 解析正常、但 443 连接超时，官方 SDK 同样受影响），可 SSH 隧道转发到服务器 `:443`，请求覆写 `Host: {port}-{sandboxID}.{DOMAIN}`——edge-proxy 只按 Host 头路由，见 §11.1 与 README §8.10。

---

## 1. 鉴权模型

### 1.1 三层凭据

| 层 | 凭据 | 应用路径 | 来源 |
|---|---|---|---|
| **数据面** | `Authorization: Bearer ${API_KEY}` | `/sandboxes*`、`/v2/*`、`/v3/*`、`/templates/*`、`/internal/*`（proxy 自调） | `SBX_API_KEYS` env 或 `POST /admin/keys` mint |
| **管理面** | `X-Admin-Token: ${ADMIN_TOKEN}` | 仅 `/admin/*` | `SBX_ADMIN_TOKEN` env，或 deploy_server 自动生成 |
| **会话层**（可选） | `X-Session-Id: ${sessionId}` | 绑定过 `sessionId` 的 sandbox 的所有读写 + edge-proxy 数据面转发 | 客户端拥有 |
| **公开申请层**（P4 第八刀） | 无（提交申请）；`X-Request-Ticket: ${ticket}`（查状态/领取/撤回） | 仅 `/keys/requests*` | `POST /keys/requests` 201 一次性下发 |

三类凭据 **不可互换**：
- 数据面 key 在 `/admin/*` 上 → 401
- ADMIN_TOKEN 在 `/sandboxes` 上 → 401（视为无效数据面 key）
- 缺失/错误的 `X-Session-Id` 在已绑定沙箱上 → 403
- `X-Request-Ticket` 仅对其所属申请有效，不能用于数据面或 `/admin/*`

### 1.2 数据面 key 的两种风格

```bash
# 百炼风格
curl -H "Authorization: Bearer sk-xxx..." http://host:8902/v2/templates

# 官方 e2b SDK 风格
curl -H "X-API-KEY: sk-xxx..." http://host:8902/v2/templates
```

两种都接受；同请求中只取一种（Authorization 优先）。

### 1.3 多租户身份 (owner, tenant)

`API_KEYS_JSON=k1:alice:acme;k2:bob:acme;k3:eve:evil` 形式的 key 绑定身份。所有 sandbox-scoped 端点自动按 caller identity 隔离：
- 跨 owner / 跨 tenant 访问沙箱 → 403 (`100011`)
- `/v2/sandboxes` 仅返回自己的沙箱
- 未设 `API_KEYS_JSON` 时退化为单 key 模式（`default/default`），跨人检查失效

---

## 2. 控制面端点 — 模板

### 2.1 `POST /v3/templates` 创建模板

**鉴权**：数据面 key。

**请求体**：

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `name` | string | ✗ | `"template"` | 模板别名（用于响应中的 `alias`） |
| `image` | string | ✗ | `sandbox/code-interpreter:v1` | Docker 镜像 |
| `cpuCount` | int | ✗ | `1` | CPU 配额 |
| `memoryMB` | int | ✗ | `2048` | 内存配额 |
| `diskSizeMB` | int | ✗ | `2048` | 磁盘 |
| `envVars` | object | ✗ | `{}` | 容器启动 env |
| `browserEnabled` | bool | ✗ | 自动（按 image 名包含 `browser` / `all-in-one` 判断） | 是否暴露 3000 端口 |
| `networkPolicy` | object | ✗ | `{"mode":"open"}` | 默认网络策略（mode: open / allowlist / blocked） |
| `startupHooks` | array | ✗ | `[]` | 启动钩子（fail-closed） |
| `periodicHooks` | array | ✗ | `[]` | 周期钩子 |

**响应** `201`：
```json
{
  "templateCode": "tmpl1980ce9bdf6244adb",
  "templateID":   "tmpl1980ce9bdf6244adb",
  "name": "code-interpreter",
  "image": "sandbox/code-interpreter:v1",
  "browserEnabled": false,
  "networkPolicy": {"mode":"open"},
  "startupHooks": [],
  "periodicHooks": []
}
```

### 2.2 `GET /v2/templates` 列表

**鉴权**：数据面 key。**响应** `200`：模板数组，每项字段同上 + `cpuCount`、`memoryMB`、`version`。

### 2.3 `GET /templates/{template_code}` 详情

**响应** `200`：与 list 元素一致，额外含 `diskSizeMB`。404 → `100002`。

### 2.4 `PUT /templates/{template_code}/hooks` 改钩子

**请求体**：`{"startupHooks": [...], "periodicHooks": [...]}`，至少传一个，否则 400 `100004`。
**响应** `200`：`{"templateCode": "...", "startupHooks": [...], "periodicHooks": [...]}`。

### 2.5 `GET /templates/{template_code}/hooks` 读钩子

**响应** `200`：同 PUT 响应。

### 2.6 `DELETE /templates/{template_code}`

如果模板下仍有 running/paused 沙箱 → 409 `100007`。否则 204。

### 2.7 `GET /templates/{template_code}/builds/{build_id}/status`

**响应** `200`：`{buildID, templateCode, status, logs}`。当前镜像预拉，status 立即 `ready`。

---

## 3. 控制面端点 — 沙箱生命周期

### 3.1 `POST /sandboxes` 创建沙箱

**鉴权**：数据面 key。
**可选会话头**：`X-Session-Id`（用于后续端点的归属检查；create 时通常不传）。

**请求体**：

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `templateID` | string | ✓ | — | 模板 code（`tmpl...`）或 name（如 `code-interpreter`）；code 查不到时按 name 回退（2026-09-29 起支持） |
| `timeout` | int | ✗ | `600` | TTL 秒 |
| `metadata` | object | ✗ | `{}` | 自定义标签，原样存储 |
| `env_vars` | object | ✗ | `{}` | 覆盖模板 envVars |
| `sessionId` / `session_id` | string | ✗ | — | chat 会话 ID；同 (owner, tenant, sessionId) 已有 running/paused 沙箱 → 409 `100015` |

**响应** `201`（**含 `envdAccessToken`，仅在此时返回**）：
```json
{
  "clientID": "cli750870b27c7...",
  "sandboxID": "sbxdd12e2311b164bfd9",
  "templateID": "tmpl1980ce9bdf6244adb",
  "alias": "code-interpreter",
  "envdVersion": "0.7.0",
  "domain": "192.168.2.162.nip.io",
  "metadata": {},
  "startedAt": "2026-09-29T09:42:11.882Z",
  "endAt":     "2026-09-29T10:42:11.882Z",
  "state": "running",
  "cpuCount": 1,
  "memoryMB": 2048,
  "diskSizeMB": 2048,
  "features": "envd,jupyter",
  "pauseMode": "stop",
  "sessionID": "chat-A-bafddf55",
  "browserPort": 3000,
  "envdAccessToken": "tok...",
  "hookState": { "startup": { "ran_at": ..., "all_passed": true, ... } }
}
```

字段含义：
- `domain` + `sandboxID` + `browserPort`：拼出 edge-proxy 子域 `{port}-{sandboxID}.{domain}`
- `envdAccessToken`：发往数据面 `/files`、`/process.*` 时的 `x-access-token` 头
- `features`：镜像启用的能力（`envd`、`jupyter`、`browser`）
- `sessionID`：若 create 时传了 `sessionId`，会回显；否则不存在
- `hookState`：仅当模板配置了 startupHooks 且执行过才有

**错误**：
- 400 `100004`：缺 `templateID`
- 404 `100002`：模板不存在
- 409 `100015`：同 session 已有活跃沙箱
- 429 `100009`：`MAX_SANDBOXES` / `MAX_MEMORY_MB` 超限
- 500 `100005`：容器启动失败（镜像拉取/端口）
- 500 `100010`：startup hook 失败（已回滚）

### 3.2 `GET /sandboxes/{sandbox_id}` 查询

**响应** `200`：sandbox JSON（**无 `envdAccessToken`**）。404 `100003`。
绑定 session 时必须带 `X-Session-Id`，否则 403 `100013`。

### 3.3 `GET /sandboxes/{sandbox_id}/health` 健康快照

**响应** `200`（逐探针实测结构；示例为 code-interpreter 模板，browser 未启用 → `null`）：
```json
{
  "sandboxID": "sbx...",
  "state": "running",
  "containerRunning": true,
  "envd":    { "port": 20000, "ok": true, "detail": {"ok": true} },
  "jupyter": { "port": 20001, "ok": true, "detail": {"ok": true, "service": "jupyter", "contexts": 0} },
  "browser": null,
  "ok": true
}
```

- 每个探针 `GET http://127.0.0.1:{hostPort}/health`（3s 超时）。
- 总 `ok` 按沙箱 `features` 聚合（2026-09-29 起）：未启用的服务探针为 `null`，不计入 AND。**总 `.ok` 可直接作为就绪判据**——code-interpreter 实测 create 返回后约 200ms 就绪；带 browser 的模板 Chromium 启动需 30-60s。

### 3.4 `GET /v2/sandboxes` 列表

**Query 参数**：`state=running,paused`（逗号分隔；默认两者）。
**头**：可选 `X-Session-Id`。若提供，列表只包含该 session 的沙箱；否则返回 caller 全部可见沙箱（admin 视图）。
**响应** `200`：sandbox JSON 数组（不含 token）。

### 3.5 `POST /sandboxes/{sandbox_id}/connect` 连接 / 续期

唤醒 paused 沙箱并把 TTL 重置。**请求体**：`{"timeout": 300}`（可选）。
**响应** `200`：sandbox JSON（含 `envdAccessToken` —— 重新签发，注意更新客户端缓存）。

### 3.6 `POST /sandboxes/{sandbox_id}/pause` 暂停

**请求体**：`{"criu": true|false|"auto"}`（可选，默认 auto）。CRIU 不可用时自动 fallback `docker stop`。
**响应** `204`。已 paused 重复调用 → 409。

### 3.7 `POST /sandboxes/{sandbox_id}/resume` 恢复

**请求体**：`{"timeout": 300}`。
**响应** `200`：sandbox JSON（含 `envdAccessToken`）。

### 3.8 `POST /sandboxes/{sandbox_id}/timeout` 续期 TTL

**请求体**：`{"timeout": 300}`。**响应** `204`。

### 3.9 `POST /sandboxes/{sandbox_id}/refreshes` 心跳

只刷新 `last_activity` 时间戳；用于 keep-alive。**响应** `204`。

### 3.10 `DELETE /sandboxes/{sandbox_id}` 销毁

销毁容器 + 释放端口 + 删除 DB 行。**响应** `204`。已销毁重复调用 → 404。

---

## 4. 控制面端点 — 网络策略（P2）

### 4.1 `GET /sandboxes/{sandbox_id}/netpolicy` 读

**响应** `200`：
```json
{"sandboxID":"sbx...", "mode":"allowlist", "ips":["1.2.3.4","5.6.7.8"], "fqdns":["cdn.example.com"]}
```

### 4.2 `POST /sandboxes/{sandbox_id}/netpolicy` 设置

**请求体**：
```json
{
  "mode": "open" | "allowlist" | "blocked",
  "ips": ["10.0.0.0/8", "1.1.1.1"],
  "fqdns": ["pypi.org", "*.cdn.example.com"]
}
```
`fqdns` 中的通配符在 apply 时解析为当前 IP 集合；CDN 切换时手动 refresh。

**响应** `200`：`{"applied": true, "sandboxID": "sbx...", ...}`。

### 4.3 `POST /sandboxes/{sandbox_id}/netpolicy/refresh` 重解析 FQDN

**响应** `200`：`{"sandboxID":..., "added":[...], "removed":[...]}`；如果当前是 open/blocked 模式 → `{"skipped": true, "reason":"..."}`。

### 4.4 `DELETE /sandboxes/{sandbox_id}/netpolicy` 撤销

回到 `open` 模式。**响应** `200`。

---

## 5. 控制面端点 — 诊断与钩子（P3）

### 5.1 `GET /sandboxes/{sandbox_id}/diag` 诊断快照

**Query 参数**：
- `include=processes,stats,logs,connections,envd`（默认全部）
- `logTail=100`（仅 logs 段使用）

**响应** `200`（每段独立采集；某段失败 → `{"error": "..."}` 不影响其它段）：
```json
{
  "sandboxID": "sbx...",
  "processes": { "items": [...] },
  "stats":     { "cpu": 0.4, "mem": 215 },
  "logs":      { "tail": ["...","..."] },
  "connections": { "items": [...] },
  "envd":      { "ready": true }
}
```

### 5.2 `GET /sandboxes/{sandbox_id}/hooks/status` 钩子执行状态

**响应** `200`：`{"sandboxID": "...", "state": { "startup": {...}, "periodic": [...] }}`。

---

## 6. 控制面端点 — Admin Key 管理（P4 第五刀）

**全部以 `X-Admin-Token` 鉴权**，不接受数据面 key（即使 key 与 admin token 同值，也不通过 bearer 头解析）。

### 6.1 `GET /admin/keys` 列表

**Query**：`includeRevoked=true` 包含已撤销。
**响应** `200`：
```json
{
  "keys": [
    {
      "id": "k_0fd793774a72",
      "prefix": "e2b_k_0f",
      "suffix": "EkhU",
      "displayKey": "e2b_k_0f…EkhU",
      "owner": "alice",
      "tenant": "acme",
      "label": "prod-bot",
      "createdAt": 1727596800,
      "revokedAt": null,
      "lastUsedAt": 1727603012
    }
  ]
}
```

**永远不会**返回完整 plaintext。

### 6.2 `POST /admin/keys` 创建

**请求体**：`{"owner": "alice", "tenant": "acme", "label": "可选标签"}`（owner/tenant 必填，否则 400 `100013`）。
**响应** `201`：
```json
{
  "key": "e2b_k_0fd793774a72_...",  // 完整明文，仅此一次
  "meta": { "id": "k_...", "prefix": "...", "suffix": "...", "owner":"...", "tenant":"...", "label":"...", "createdAt": ... },
  "warning": "完整 key 仅此一次返回。请立即保存到安全的地方,事后无法再读取。"
}
```

明文 = `e2b_${key_id}_${random_token_urlsafe(32)}`，DB 仅存 SHA-256 hash。撤销 = 永久销毁。

### 6.3 `DELETE /admin/keys/{key_id}` 撤销

立即生效（下次请求 401）。已撤销再删 → 404 `100014`。**响应** `200`：`{"id": "...", "revoked": true}`。

### 6.4 Key 自助申请审批流（P4 第八刀）

第三方 agent / 调用方**没有**管理凭证，不能直接 mint key；但可以走"申请 → 管理员审批 → 凭 ticket 一次性领取"的自助流。mint 只发生在管理员 approve 一刻；审批面板与 `approve` 响应**均不经手明文**，明文暂存于服务端 outbox，申请人 claim 后立即清除。

**申请方端点（免数据面鉴权）**：

| 端点 | 说明 |
|---|---|
| `POST /keys/requests` | 提交申请。body：`applicant` / `owner` / `tenant` 必填，`label` / `note` 可选。`201` 一次性返回 `requestID`（`kreq` 前缀）+ `ticket` |
| `GET /keys/requests/{rid}` | 带 `X-Request-Ticket` 查进度：`pending` / `approved` / `rejected`（含 `rejectReason`）/ `cancelled` |
| `POST /keys/requests/{rid}/claim` | 带 ticket，审批通过后**一次性**领取明文 key；再领 → 410 |
| `DELETE /keys/requests/{rid}` | 带 ticket，撤回 `pending` 申请 |

```bash
# 提交
curl -sS -X POST http://$HOST:8902/keys/requests \
  -H 'Content-Type: application/json' \
  -d '{"applicant":"my-agent","owner":"alice","tenant":"acme","label":"bot","note":"用途说明"}'
# → {"requestID":"kreq...","ticket":"...","status":"pending","warning":"ticket 仅此一次返回..."}

# 查询 / 领取
curl -sS http://$HOST:8902/keys/requests/kreq... -H "X-Request-Ticket: $TICKET"
curl -sS -X POST http://$HOST:8902/keys/requests/kreq.../claim -H "X-Request-Ticket: $TICKET"
# → {"key":"e2b_k_...","meta":{...},"warning":"完整 key 仅此一次返回..."}
```

**管理员端点（`X-Admin-Token`）**：

| 端点 | 说明 |
|---|---|
| `GET /admin/key-requests?status=pending` | 列表（status 可空 = 全部） |
| `POST /admin/key-requests/{rid}/approve` | 仅 `pending` 可批（并发守卫，二次 → 409）；可带 `{"owner","tenant","label"}` 覆盖签发身份；响应只含 `issuedKeyID` |
| `POST /admin/key-requests/{rid}/reject` | `reason` 必填（400 `100013`），申请人查询时可见 |

**防滥用**：按来源 IP 限流（默认 10 次 / 300s，超限 429 `100016`；env `KEY_REQUEST_RATE_LIMIT` / `KEY_REQUEST_RATE_WINDOW_S`）；同一 owner 待审批上限 5（409 `100016`；env `KEY_REQUEST_MAX_PENDING`）；字段长度 applicant≤128、owner/tenant/label≤64、note≤512（400 `100013`）。ticket 与明文 outbox 在任何列表/查询响应中均不回显。管理面板首页新增「Key 申请审批」卡片，批准/驳回一键操作。

---

## 7. 控制面端点 — Chat-Session 隔离（P4 第六刀）

### 7.1 模型

**Session = Sandbox**。一个 chat 会话独占一个 sandbox；多轮对话复用；不同 session 之间文件 / 进程 / 网络栈彻底隔离。

### 7.2 绑定

`POST /sandboxes` 时携带 `sessionId`（或 `session_id`）。控制面把 `sandboxes.session_id` 写入；之后所有访问该沙箱的请求（含 edge-proxy 数据面）必须带匹配的 `X-Session-Id`。

### 7.3 重复绑定

```
POST /sandboxes {"templateID": "...", "sessionId": "chat-A-7b3c"}
→ 201

POST /sandboxes {"templateID": "...", "sessionId": "chat-A-7b3c"}
→ 409  {"code": 100015, "message": "该 session 已有沙箱 (sandboxID=sbx...); 复用或先销毁"}
```

销毁该 session 的沙箱后，同 sessionId 可以重新绑定（state 必须是 killed/deleted）。

### 7.4 校验矩阵

| 沙箱 session_id | 请求 X-Session-Id | 结果 |
|---|---|---|
| NULL（legacy） | 任意/缺 | ✓ 通过（向后兼容） |
| `chat-A` | `chat-A` | ✓ 通过 |
| `chat-A` | `chat-B` | ✗ 403 `100013` |
| `chat-A` | 缺失 | ✗ 403 `100013` |

### 7.5 作用范围

| 端点 | 校验位置 |
|---|---|
| `GET /sandboxes/{sid}`、`/health`、`/diag`、`/hooks/status`、`/netpolicy`、`/timeout`、`/refreshes`、`/pause`、`/resume`、`/connect`、`DELETE` | `check_owner()` 入口（control plane 进程内） |
| `GET /v2/sandboxes` | 头存在时按 session 过滤；头缺失 = admin 视图 |
| **edge-proxy 数据面** `https://{port}-{sid}.{domain}/...` | proxy 在 forward 之前查 DB 校验 |

---

## 8. 数据面端点（容器内 mini_envd）

数据面 URL 形如 `https://{port}-{sandboxID}.${DOMAIN}/...`，所有请求必须带 `x-access-token: ${envdAccessToken}`，绑定 session 的还需 `X-Session-Id`。

| 服务 | 端口标签 |
|---|---|
| envd 文件 + 进程 | `49983` |
| Jupyter kernel | `49999` |
| Browser CDP | `3000` |

### 8.1 健康检查

```
GET /health  → 200 {"ok": true, "version": "0.7.0"}
```

### 8.2 文件 REST（推荐客户端用这两个）

#### 读

```
GET /files?path=/workspace/foo.txt
→ 200 octet-stream（响应体即文件内容）
→ 403 路径越界 / 404 不存在
```

#### 写

```
POST /files?path=/workspace/foo.txt
Content-Type: application/octet-stream
Body: <raw bytes>
→ 200
→ 403 路径越界 / 500 IO 错误
```

**路径白名单**：只允许 `/workspace/*` 与 `/tmp/*`（resolve 后判断，symlink 解析）。其他路径 → 403。相对路径默认锚定到 `/workspace`。

### 8.3 文件 ConnectRPC（E2B 协议）

`POST /filesystem.Filesystem/{method}`，请求体 `application/json`（也支持 protobuf，按 `Content-Type` 协商）。

| 方法 | 请求 | 响应 |
|---|---|---|
| `MakeDir` | `{"path": "/workspace/dir"}` | `{"entry": {...}}` |
| `Remove` | `{"path": "/workspace/dir"}` | `{}` |
| `Move` | `{"source": "/a", "destination": "/b"}` | `{"entry": {...}}` |
| `Stat` | `{"path": "/workspace/foo.txt"}` | `{"entry": {...}}` |
| `ListDir` | `{"path": "/workspace", "depth": 1}` | `{"entries": [...]}` |

`depth=1` 返回直接子项（E2B 语义）。同路径白名单。

### 8.4 进程 ConnectRPC

`POST /process.Process/{method}`。

| 方法 | 用途 |
|---|---|
| `Start` | 启动进程；返回流式 stdout/stderr/exit |
| `List` | 列出当前 PID |
| `SendInput` | 给 stdin 喂数据 |
| `StreamInput` | 流式 stdin |
| `SendSignal` | 发信号（SIGTERM 等） |
| `CloseStdin` | 关闭 stdin |
| `Connect` | 重新 attach 输出流 |
| `Update` | 调整 tty / env |

E2B 官方 SDK 直接走这套，自定义客户端可用 JSON body（自动转 protobuf）。

### 8.5 Jupyter 内核（port 49999）

兼容 `e2b_code_interpreter` SDK，无需手写 HTTP。不引 SDK 时可直接调以下端点（均需 `x-access-token`）：

| 端点 | 用途 |
|---|---|
| `POST /execute` | 执行代码，响应为 **NDJSON 流**（每行一个事件） |
| `GET /contexts` | 列出内核 context |
| `POST /contexts` | 新建 context（body 可选 `{"cwd": "..."}`），返回 `{"id","language","cwd"}` |
| `DELETE /contexts/{id}` | 销毁 context（内核进程随之退出） |
| `POST /contexts/{id}/restart` | 重启内核 |

`POST /execute` 请求体：`{"code": "...", "context_id": "default"}`（`context_id` 缺省为 `default`，首次调用自动冷启动 IPython kernel）。

NDJSON 事件类型：

| `type` | 字段 | 说明 |
|---|---|---|
| `stdout` / `stderr` | `text`, `timestamp` | 流式输出 |
| `result` | `text` / `html` / `png` 等（按 MIME 映射） | 表达式求值结果 |
| `error` | `name`, `value`, `traceback` | 异常 |
| `number_of_executions` | `execution_count` | 执行序号 |

实测时延（500 元素负载，见 README §6 基线）：首次执行约 **1.0s**（含 kernel 冷启动 ~0.8s），同一 context 内热执行约 **240ms**。单次超 120s 无输出会收到 `TimeoutError` 事件。

### 8.6 Browser CDP（port 3000）

`wss://3000-{sbxID}.{DOMAIN}/devtools/page/...` —— Playwright / Puppeteer 直连即可。WebSocket 升级经过 edge-proxy 透明转发。

---

## 9. 错误码

### 9.1 业务 code

| code | HTTP | 含义 | 触发场景 |
|---|---|---|---|
| 100001 | 401 | API Key 无效 | key 不在 `API_KEYS` / 缺失 `Authorization` |
| 100002 | 404 | 模板不存在 | `templateID` 未注册 |
| 100003 | 404 | 沙箱不存在 | 已销毁 / ID 写错 |
| 100004 | 400 | 参数缺失或非法 | 缺 `templateID` / `owner` / `tenant` 等 |
| 100005 | 500 | 沙箱启动失败 | 镜像拉失败 / 端口冲突 |
| 100006 | 404 / 503 | 恢复失败 | CRIU 镜像损坏 / container 不存在 |
| 100007 | 409 | 有依赖不能删 | 模板下仍有 running/paused 实例 |
| 100008 | 400 | 参数解析错 | `logTail` 非整数等 |
| 100009 | 429 | 配额满 | `MAX_SANDBOXES` / `MAX_MEMORY_MB` 超限 |
| 100010 | 500 | startup hook 失败 | fail-closed 已回滚 |
| 100011 | 403 | 跨 owner/tenant 拒绝 | 多租户隔离 |
| 100012 | 401 / 503 | admin 鉴权失败 | `/admin/*` 缺/错 ADMIN_TOKEN；server 端未启用 admin → 503 |
| 100013 | 403 | session 校验失败 | 绑定沙箱缺 `X-Session-Id` 或不匹配 |
| 100013 | 400 | admin owner/tenant 必填 | `POST /admin/keys` 缺字段 |
| 100014 | 404 | key 不存在或已撤销 | `DELETE /admin/keys/{id}` |
| 100015 | 409 | session 已有活跃沙箱 | 重复 `POST /sandboxes` 用同 sessionId |
| 100016 | 429 / 409 | 申请受限 | IP 限流（10/300s）或 owner 待审批上限 |
| 100017 | 403 / 409 / 410 | ticket 无效 / 状态不符 / 已被领取 | `X-Request-Ticket` 不匹配、claim 时状态非 approved、重复 claim |

> `100013` 同时被 admin "owner/tenant 必填" 与 session 校验占用，区分靠 HTTP 码（400 vs 403）。`100017` 三种语义同码，区分靠 HTTP 码（403 鉴权失败、409 状态不符、410 已领取过）。

### 9.2 错误响应体

```json
{
  "code": 100013,
  "message": "X-Session-Id 不匹配 (caller=chat-B, sandbox=chat-A)",
  "requestID": "req-1727603212345"
}
```

`requestID` 用于运维排错（grep server 日志）。

---

## 10. e2b 官方 SDK 适配

### 10.1 Python `e2b-code-interpreter`

```python
from e2b_code_interpreter import Sandbox

sbx = Sandbox.create(
    api_url="http://${SBX_HOST}:8902",
    api_key="${SBX_E2B_KEY}",   # 必须 e2b_ 前缀；用 /admin/keys mint
    template="code-interpreter",  # 必须先在 /v3/templates 注册
    request_timeout=30,
)

sbx.run_code("print(sum(range(10)))")
sbx.files.write("/workspace/h.txt", b"hello")
print(sbx.files.read("/workspace/h.txt"))

sbx.kill()
```

SDK 在内部用 `X-API-KEY: ${api_key}` 头，本服务兼容（§1.2）。

### 10.2 session 头注入

官方 SDK 没有透传自定义头的口子。两种方案：
1. **每次 create 时通过 `metadata` 传 sessionId**：把 `sessionId` 写进 body，控制面解析
2. **猴子补丁 `httpx` 客户端**：在调用 `Sandbox.create()` 前注入 `X-Session-Id` 到 SDK 的底层 client

推荐 (1)：create 时 `Sandbox.create(metadata={"sessionId": "chat-A"})`，本服务把 `metadata.sessionId` 当 sessionId 处理。

### 10.3 Playwright / Puppeteer 浏览器

```python
from playwright.sync_api import sync_playwright
sbx = Sandbox.create(template="browser", api_key=..., api_url=...)
ws = f"wss://3000-{sbx.sandbox_id}.{sbx_domain}/devtools/page/..."
with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(ws, headers={"x-access-token": sbx._envd_access_token})
    browser.new_page().goto("https://example.com")
```

WebSocket 升级经过 edge-proxy 透明转发；session 绑定的沙箱需要客户端额外注入 `X-Session-Id` 头。

---

## 11. curl 速查

### 11.1 创建一个绑定 session 的沙箱并写文件

```bash
HOST=${SBX_HOST}; API=${SBX_API_KEY}; SESSION="chat-A-$(date +%s)"

# templateID 必须是 tmpl... code，模板名不解析（传 name 会 404 100002）
TID=$(curl -sS http://$HOST:8902/v2/templates -H "Authorization: Bearer $API" \
  | jq -r '.[] | select(.name=="code-interpreter") | .templateID')

# create
RESP=$(curl -sS -X POST http://$HOST:8902/sandboxes \
  -H "Authorization: Bearer $API" -H "Content-Type: application/json" \
  -d "{\"templateID\":\"$TID\",\"sessionId\":\"$SESSION\",\"timeout\":300}")
SBX=$(echo $RESP | jq -r .sandboxID)
TOK=$(echo $RESP | jq -r .envdAccessToken)
DOMAIN=$(echo $RESP | jq -r .domain)

# write file via edge-proxy
curl -sS -k -X POST "https://$DOMAIN/files?path=/workspace/a.txt" \
  -H "Host: 49983-$SBX.$DOMAIN" \
  -H "x-access-token: $TOK" -H "X-Session-Id: $SESSION" \
  --data-binary "hello"

# list /v2 sandboxes scoped to this session
curl -sS http://$HOST:8902/v2/sandboxes \
  -H "Authorization: Bearer $API" -H "X-Session-Id: $SESSION" | jq '.[].sandboxID'

# kill
curl -sS -X DELETE http://$HOST:8902/sandboxes/$SBX \
  -H "Authorization: Bearer $API" -H "X-Session-Id: $SESSION"
```

### 11.2 mint 一个新数据面 key

```bash
ADMIN=${SBX_ADMIN_TOKEN}
curl -sS -X POST http://$HOST:8902/admin/keys \
  -H "X-Admin-Token: $ADMIN" -H "Content-Type: application/json" \
  -d '{"owner":"alice","tenant":"acme","label":"prod"}' | jq
# → 立刻把 .key 字段存好，list 端点之后再也看不到明文
```

### 11.3 撤销 key

```bash
curl -sS -X DELETE http://$HOST:8902/admin/keys/k_0fd793774a72 -H "X-Admin-Token: $ADMIN"
```

### 11.4 设置网络策略（CDN 白名单）

```bash
curl -sS -X POST http://$HOST:8902/sandboxes/$SBX/netpolicy \
  -H "Authorization: Bearer $API" -H "X-Session-Id: $SESSION" \
  -H "Content-Type: application/json" \
  -d '{"mode":"allowlist","fqdns":["pypi.org","*.cdn.jsdelivr.net"]}'
```

---

## 12. 限制与默认值

| 项 | 默认 | 配置 |
|---|---|---|
| 最大活跃沙箱 | 24 | `MAX_SANDBOXES` env |
| 单沙箱内存上限（累加） | 6144 MB | `MAX_MEMORY_MB` env |
| 沙箱 TTL | 600 s | body `timeout` |
| edge-proxy 端口 | 443 (TLS) | 固定 |
| 控制面端口 | 8902 | 固定 |
| 沙箱 host 端口分配 | 20000-20988，步长 3 | 内部 |
| CRIU 检查点目录 | `/var/lib/sbx-checkpoints` | `CHECKPOINT_DIR` env |

---

## 13. 变更与兼容性策略

- **新增端点** 不破坏旧客户端；
- **新增可选字段** 走 body / query，旧请求缺省值即新默认；
- **新增鉴权维度**（如 session）默认对未启用旧沙箱透明（`session_id IS NULL` 视为 legacy）；
- 错误码 **追加**，不复用；
- API 路径前缀 `/v2`、`/v3` 与 E2B 上游对齐，自研扩展走非版本号路径（`/templates/{code}/hooks`、`/admin/keys`、`/sandboxes/{sid}/diag` 等）。

任何破坏性变更在 README §变更日志中声明，并提供至少一个大版本的迁移期。

---

## 14. 进一步阅读

- [README.md](README.md) — 部署、配置、运维、测试矩阵
- [DESIGN.md](DESIGN.md) — 架构决策与权衡
- [sandbox-service/tests/session_smoke.py](sandbox-service/tests/session_smoke.py) — 14 项 session 隔离用例（最权威的"行为定义"）
- [sandbox-service/tests/path_sandbox_smoke.py](sandbox-service/tests/path_sandbox_smoke.py) — 文件路径白名单 14 项
- [sandbox-service/tests/admin_keys_smoke.py](sandbox-service/tests/admin_keys_smoke.py) — Admin Key 9 项

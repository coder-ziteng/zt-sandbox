# zt-Sandbox — 自研云端沙箱服务（兼容 E2B 协议）

> 阿里百炼 Sandbox 的自研替代品，对外协议与 E2B 官方 SDK (`e2b==2.31.0`) 完全兼容。
> 一期代码解释器 + 二期浏览器 + 三期网络白名单 / CRIU / 并发调度，全部跑通。

**目标读者**：接手这个项目的 AI Agent / 人类开发者。读完这篇，你应该能：
1. 知道这套服务是什么、怎么部署、怎么用
2. 从你的 Agent 里直接调用沙箱 API
3. 知道哪些功能能用、哪些有坑、哪些是降级
4. 跑通所有冒烟测试
5. 在需要时定位问题

---

## 0. 速查

| 项 | 值 |
|---|---|
| 服务器 | `${SBX_SSH_HOST}`（内网 Linux，root / 123456） |
| 管控面 | `http://${SBX_SSH_HOST}:8902`（HTTP） |
| 边缘代理 | `https://*.{sandboxID}.${SBX_DOMAIN}`（TLS，自签 CA） |
| 管控面 API Key | `${SBX_API_KEY}`（REST 用，放 `Authorization: Bearer ...`） |
| SDK API Key | `${SBX_E2B_KEY}`（官方 e2b SDK 用，放 `X-API-KEY` 或 `api_key=`） |
| CA 证书（客户端） | 本机 `sandbox-service/certs/ca.pem`，部署脚本会自动下载 |
| 工作目录 | 本机 `E:\work\zt-Sandbox\sandbox-service` |
| 远程目录 | 服务器 `/srv/sandbox-service` |

**鉴权双通道**：管控面同时认 `Authorization: Bearer <key>` 和 `X-API-KEY: <key>` 两种 header。
官方 e2b SDK 只会发后者，所以两个都要支持。`API_KEYS` 环境变量是逗号分隔的列表，目前配的是 `${SBX_API_KEY},${SBX_E2B_KEY}`。

---

## 0.1 选哪种入口（决策树）

| 你的场景 | 推荐入口 | 章节 |
|---|---|---|
| Claude Code / Cursor 中让 Agent 直接跑代码 | **MCP**（stdio，`mcp_server.py`） | §4.7 |
| 已有 Python 代码、想用官方 SDK | **E2B SDK** | §4.1 |
| 已有 Node / Go / Java 代码、不想引 SDK | **REST + ConnectRPC JSON** | §4.6 方式 B |
| 想用 Playwright 跑浏览器 | **CDP 直连**（从 `webSocketDebuggerUrl`） | §4.3 方式 B |
| 只想批量执行、不想引入 SDK | **REST**（`Authorization: Bearer`） | §4.6 方式 B |

---

## 0.2 30 秒上手（Copy-Paste）

**A. 纯 REST（不依赖任何 SDK）**
```bash
curl -sS http://${SBX_SSH_HOST}:8902/health \
  -H "Authorization: Bearer ${SBX_API_KEY}" | jq

# 创建沙箱（默认 code-interpreter 模板）
SID=$(curl -sS -X POST http://${SBX_SSH_HOST}:8902/sandboxes \
  -H "Authorization: Bearer ${SBX_API_KEY}" -H "Content-Type: application/json" \
  -d '{"templateID":"code-interpreter","timeout":600}' | jq -r .sandboxID)
echo "sandbox=$SID"

# 等 health=true
for i in {1..30}; do
  curl -sS http://${SBX_SSH_HOST}:8902/sandboxes/$SID/health \
    -H "Authorization: Bearer ${SBX_API_KEY}" | jq -e '.ok' >/dev/null && break
  sleep 2
done

# 销毁
curl -sS -X DELETE http://${SBX_SSH_HOST}:8902/sandboxes/$SID \
  -H "Authorization: Bearer ${SBX_API_KEY}"
```

**B. Python（E2B 官方 SDK，零代码改动）**
```python
from e2b_code_interpreter import Sandbox
sbx = Sandbox.create(
    api_url=f"http://{SBX_SSH_HOST}:8902",
    api_key="${SBX_E2B_KEY}",   # 需 e2b_ 前缀
    domain="${SBX_DOMAIN}",      # 走 edge-proxy 子域路由
)
print(sbx.run_code("import sys; print(sys.version_info)").text)
sbx.kill()
```

**C. Claude Code / Cursor（MCP）**
把 §4.7.2 的 MCP 配置写入 `~/.claude/mcp_servers.json`（或项目 `.mcp.json`），重启 IDE，然后在对话里说 `用 zt-sandbox 跑 print(1+1)`。

---

## 1. 项目是什么

一句话：**给 AI 智能体用的隔离执行环境**。智能体要跑代码、要操作浏览器、要处理文件——都丢到这个沙箱里，跑完销毁，互不影响。

对标的产品是 [阿里百炼 Sandbox](https://bailian.console.aliyun.com/) 和 [E2B](https://e2b.dev/)。对外接口跟 E2B 的 Python SDK 完全兼容——意味着你**不改一行 SDK 代码**，把 `api_url` 指向我们的服务器就能跑：

```python
from e2b_code_interpreter import Sandbox
sbx = Sandbox.create(
    api_url="http://${SBX_SSH_HOST}:8902",
    api_key="${SBX_E2B_KEY}",
    domain="${SBX_DOMAIN}",
)
sbx.run_code("print(1+1)")
```

---

## 2. 架构

```
                          ┌────────────────────────────┐
   Claude Code/Cursor ───┤  MCP server (stdio, 本地)   │
   E2B SDK / curl    ─────┤                            │
                          └──────────┬─────────────────┘
                                     │
                ┌────────────────────┼────────────────────┐
                │ HTTP :8902 (管控)  │                    │
                ▼                    │                    │
   ┌────────────────────────┐        │                    │
   │  sandbox-control-plane │        │                    │
   │  FastAPI / SQLite     │        │                    │
   │  生命周期 / 模板 / 配额│        │                    │
   └──────────┬─────────────┘        │
              │ docker SDK            │ HTTPS :443 (子域路由)
              ▼                       ▼
   ┌─────────────────────────────────────────────────┐
   │           数据面（每沙箱一个容器, --network=host）│
   │  ┌──────────┐  ┌──────────┐  ┌──────────┐       │
   │  │ mini_envd│  │ jupyter  │  │ chromium │       │
   │  │  :49983  │  │  :49999  │  │  :3000   │       │
   │  └──────────┘  └──────────┘  └──────────┘       │
   └─────────────────────────────────────────────────┘
              ▲                       ▲
              │ HTTP                  │ HTTPS {port}-{sid}.${DOMAIN}
              └───────────────────────┘  (edge-proxy)
```

**三个入口，三条路径**：

| 入口 | 管控面 (`:8902`) | 数据面（容器端口） |
|---|---|---|
| **E2B SDK** | `X-API-KEY` 头，HTTPS 调 `:8902` 创建 | SDK 用 `envdAccessToken` 直连 envd :49983 |
| **REST / curl** | `Authorization: Bearer` 调 `:8902` | HTTPS 走 edge-proxy `:443`（`{port}-{sid}.${DOMAIN}`） |
| **MCP** | 同 REST，stdin/stdout 协议封装 | 同 REST，走 edge-proxy |

**关键设计决策**：

1. **管控面 / 数据面分离**：管控面只管创建/销毁/元数据；拿到 `host + token` 后你的 SDK **直接连容器**，管控面不做数据面代理。跟百炼 / E2B 一致。
2. **自研 mini_envd 替代 E2B 的 envd**：E2B 开源的 envd 是 Go 写的，我们不依赖它，用 Python + FastAPI 重写了相同的 ConnectRPC 接口（`/process.Process/...`、`/filesystem.Filesystem/...`、`/files`）。SDK 完全无感知。
3. **边缘代理按 host header 路由**：`https://3000-{sandboxID}.${SBX_DOMAIN}/...` → 查 SQLite 找 host 端口 → 转发到 `127.0.0.1:{hostPort}`。WebSocket 升级也支持（Playwright 通过 CDP 直连浏览器就靠它）。

---

## 3. 功能矩阵（当前状态）

| 功能 | 状态 | 备注 |
|---|---|---|
| 代码执行 (run_code) | ✅ 已交付 | Jupyter 内核，支持 stateful |
| 命令执行 (commands.run) | ✅ 已交付 | 后台 / 前台都支持 |
| 文件读写 (files.*) | ✅ 已交付 | read/write/list/make_dir/remove |
| 创建 / 列举 / 获取 / 释放沙箱 | ✅ 已交付 | E2B 协议对齐 |
| 暂停 / 恢复 (docker stop/start) | ✅ 已交付 | 文件系统保留，内存不保留 |
| **真 CRIU 暂停 / 恢复** | ✅ **已激活** | 需要 daemon 切到经典驱动（`fix_criu.py` 已处理） |
| 浏览器 (Chromium CDP) | ✅ 已交付 | 通过 Session API 或 Playwright 直连 |
| 多 session 并发浏览器 tab | ✅ 已交付 | 一个沙箱内最多 8 个 session |
| 网络白名单 (iptables) | ✅ 已交付 | 按域名 / CIDR 管控出口 |
| 并发调度 + 准入控制 | ✅ 已交付 | MAX_SANDBOXES=24, MAX_MEMORY_MB=6144 |
| TTL 超时回收 | ✅ 已交付 | 调度器每 15s 扫描 |
| 控制台 UI | ❌ 不支持 | 走 REST / SDK |

---

## 3.1 环境变量

**管控面容器**（`sandbox-control-plane`，写进 `deploy/docker-compose.yml` 或 `.env`）：

| 变量 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `API_KEYS` | ✅ | 空 | 逗号分隔的 key 列表，至少填一个（推荐 `e2b_<随机>` 前缀） |
| `SANDBOX_DOMAIN` | ❌ | `${SBX_SSH_HOST}.nip.io` | edge-proxy 子域路由用的公网域名 |
| `CRIU_ENABLED` | ❌ | `auto` | `auto` / `force` / `off`，CRIU 失败时是否降级 docker stop |
| `MAX_SANDBOXES` | ❌ | `24` | 全局最大并发沙箱数（429 配额满错误码） |
| `MAX_MEMORY_MB` | ❌ | `6144` | 全局内存配额（MB），所有沙箱 memoryMB 之和 |
| `CHECKPOINT_DIR` | ❌ | `/var/lib/sbx-checkpoints` | CRIU checkpoint 落盘目录 |

**MCP server**（本地 Claude Code / Cursor 进程）：

| 变量 | 必填 | 说明 |
|---|---|---|
| `SBX_API_URL` | ✅ | 例：`http://192.168.2.162:8902` |
| `SBX_API_KEY` | ✅ | 管控面 key，对应 `Authorization: Bearer` |
| `SBX_DOMAIN` | ✅ | 边缘代理域名，例：`192.168.2.162.nip.io` |
| `SBX_CA_CERT` | ❌ | 自签 CA 证书绝对路径，推荐必填 |
| `SBX_INSECURE` | ❌ | `1` 关闭 TLS 校验，仅 dev |
| `SBX_HTTP_TIMEOUT` | ❌ | 默认 `120`（秒） |

---

## 3.2 管控面 API 速查

> **鉴权**：所有端点（除 `/health`）都需要 `Authorization: Bearer <key>` **或** `X-API-KEY: <key>`。
> e2b SDK 只发后者，REST/curl 用前者。

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/health` | 健康检查 + 容量 + 能力（`criu` / `netpolicy`） |
| `GET` | `/v2/templates` | 列出已注册模板（含 `browserEnabled` 等） |
| `GET` | `/templates/{code}` | 模板详情 |
| `POST` | `/v3/templates` | 创建/注册模板（支持 `networkPolicy`） |
| `DELETE` | `/templates/{code}` | 删除模板 |
| `GET` | `/templates/{code}/builds/{build_id}/status` | 模板构建进度 |
| `POST` | `/sandboxes` | **创建沙箱**（`templateID` + `timeout`），返回 `sandboxID` + `envdAccessToken` |
| `GET` | `/v2/sandboxes` | 列出所有沙箱 |
| `GET` | `/sandboxes/{id}` | 沙箱详情（含 `state` / `pauseMode`） |
| `GET` | `/sandboxes/{id}/health` | 沙箱就绪探针（`ok` / `services`） |
| `POST` | `/sandboxes/{id}/connect` | 重新拿 token |
| `POST` | `/sandboxes/{id}/pause` | 暂停（CRIU，失败降级 stop） |
| `POST` | `/sandboxes/{id}/resume` | 恢复 |
| `DELETE` | `/sandboxes/{id}` | 销毁 |
| `POST` | `/sandboxes/{id}/timeout` | 续期 TTL（秒） |
| `POST` | `/sandboxes/{id}/refreshes` | 刷新访问 token |
| `GET` / `POST` / `DELETE` | `/sandboxes/{id}/netpolicy` | 网络白名单 |
| `POST` | `/sandboxes/{id}/netpolicy/refresh` | 手动重解析 FQDN（CDN 切换） |

数据面（每个沙箱）：

| 端口 | 协议 | 用途 | 入口 |
| --- | --- | --- | --- |
| 49983 | ConnectRPC JSON + `/files` | envd 文件/进程 | `https://49983-{sid}.${DOMAIN}` |
| 49999 | Jupyter | run_code | `https://49999-{sid}.${DOMAIN}` |
| 3000 | HTTP + WS | Chromium CDP + Session API | `https://3000-{sid}.${DOMAIN}` |

---

## 3.3 错误码

响应体统一格式：`{"code": <int>, "message": <str>, "requestID": <str>}`。

| code | 含义 | HTTP | 触发场景 |
| --- | --- | --- | --- |
| 100001 | API Key 无效 | 401 | key 不在 `API_KEYS` / header 缺失 |
| 100002 | 模版不存在 | 404 | `templateID` 未注册 |
| 100003 | 沙箱不存在 | 404 | 已销毁 / ID 写错 |
| 100004 | 参数缺失或非法 | 400 | 缺 `templateID` / `timeout` 等 |
| 100005 | 沙箱启动失败 | 500 | 镜像拉失败 / 端口冲突 |
| 100006 | 恢复失败 | 404 / 503 | CRIU 镜像损坏 / container 不存在 |
| 100007 | 有依赖不能删 | 409 | 还有子引用 |
| 100009 | 配额满 | 429 | `MAX_SANDBOXES` / `MAX_MEMORY_MB` 超限 |

429 配额满：先 `DELETE` 不用的沙箱，或减小 `memoryMB` 重试。

---

## 4. 给 Agent 的实战手册

### 4.1 创建沙箱 + 跑代码

```python
from e2b_code_interpreter import Sandbox

sbx = Sandbox.create(
    api_url="http://${SBX_SSH_HOST}:8902",
    api_key="${SBX_E2B_KEY}",
    domain="${SBX_DOMAIN}",
    timeout=600,  # 秒
)
print(sbx.sandbox_id)

# 跑 Python
res = sbx.run_code("import sys; print(sys.version)")
print(res.text)
print(res.logs.stdout)

# 跑 shell
out = sbx.commands.run("echo hello && ls /home/user")
print(out.stdout, out.exit_code)

# 文件操作
sbx.files.write("/home/user/workspace/data.json", '{"a": 1}')
content = sbx.files.read("/home/user/workspace/data.json")
entries = sbx.files.list("/home/user/workspace")

# 销毁
sbx.kill()
```

### 4.2 暂停 / 恢复（保留内存的真 CRIU）

```python
from e2b import Sandbox

sbx = Sandbox.create(...)
sbx.commands.run("echo first")

# 暂停（默认走 CRIU，如果不可用会自动降级到 docker stop）
sbx.beta_pause(api_url=..., api_key=..., headers={"Authorization": "Bearer ${SBX_API_KEY}"})

# 恢复
Sandbox._cls_resume(sandbox_id=sbx.sandbox_id, ...)
sbx2 = Sandbox.connect(sandbox_id=sbx.sandbox_id, ...)

# 检查用了哪种模式
info = httpx.get(f"http://${SBX_SSH_HOST}:8902/sandboxes/{sbx.sandbox_id}",
                 headers={"Authorization": "Bearer ${SBX_API_KEY}"}).json()
print(info["pauseMode"])  # "criu" 或 "stop"
```

**关键区别**：
- `pauseMode: "criu"` → 内存状态保留（进程、变量、打开的文件描述符都在）
- `pauseMode: "stop"` → 只保留文件系统，内存里的东西（比如 Jupyter kernel 的变量）会丢

### 4.3 浏览器操作

**方式 A：用我们封装的 Session API（推荐）**

不需要装 Playwright。直接 HTTP 调用：

```python
import httpx

API = "http://${SBX_SSH_HOST}:8902"
KEY = "${SBX_API_KEY}"
DOMAIN = "${SBX_DOMAIN}"
H = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

# 1. 创建带浏览器的沙箱
tpl = next(t for t in httpx.get(f"{API}/v2/templates", headers=H).json()
           if t.get("browserEnabled"))
sbx = httpx.post(f"{API}/sandboxes", headers=H,
                 json={"templateID": tpl["templateCode"], "timeout": 900}).json()
sid = sbx["sandboxID"]
token = sbx["envdAccessToken"]

# 2. 等浏览器就绪
import time
for _ in range(60):
    h = httpx.get(f"{API}/sandboxes/{sid}/health", headers=H).json()
    if h.get("ok"): break
    time.sleep(2)

# 3. 用 Session API
B = f"https://3000-{sid}.{DOMAIN}"
bh = {"X-Access-Token": token, "Content-Type": "application/json"}

session_id = httpx.post(f"{B}/session/create", headers=bh,
                        json={"viewport": {"width": 1280, "height": 800}}).json()["sessionId"]

# 批量动作（goto / fill / click / evaluate / screenshot / cookies / console ...）
r = httpx.post(f"{B}/session/{session_id}/act", headers=bh, json={
    "actions": [
        {"type": "goto", "url": "https://example.com", "waitUntil": "load"},
        {"type": "title"},
        {"type": "screenshot"},
    ]
}).json()
print(r["results"])

# 取 PNG
png = httpx.get(f"{B}/session/{session_id}/screenshot", headers=bh).content

# 取内容（title/url/html/text）
content = httpx.get(f"{B}/session/{session_id}/content", headers=bh).json()

# 取 PDF
pdf = httpx.get(f"{B}/session/{session_id}/pdf", headers=bh).content

# 关闭
httpx.post(f"{B}/session/{session_id}/close", headers=bh)
httpx.delete(f"{API}/sandboxes/{sid}", headers=H)
```

**方式 B：Playwright 直连 CDP**

如果你已经有 Playwright 代码，直接 `connect_over_cdp`：

```python
from playwright.sync_api import sync_playwright

# 先拿到 webSocketDebuggerUrl
ws = httpx.get(f"https://3000-{sid}.{DOMAIN}/json/version").json()["webSocketDebuggerUrl"]

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(ws)
    page = browser.contexts[0].new_page()
    page.goto("https://example.com")
    print(page.title())
    page.screenshot(path="shot.png")
    browser.close()
```

**注意**：Playwright 的 node driver 会吃你机器上的 HTTP 代理环境变量，测试前一定要清掉：
```python
for k in ("HTTP_PROXY","HTTPS_PROXY","http_proxy","https_proxy","ALL_PROXY","all_proxy"):
    os.environ.pop(k, None)
os.environ["NO_PROXY"] = "*"; os.environ["no_proxy"] = "*"
os.environ["NODE_EXTRA_CA_CERTS"] = "/path/to/certs/ca.pem"
```

### 4.4 网络白名单

创建模版时指定 `networkPolicy`：

```python
httpx.post(f"{API}/v3/templates", headers=H, json={
    "name": "restricted",
    "image": "sandbox/code-interpreter:v1",
    "cpuCount": 1, "memoryMB": 1024,
    "networkPolicy": {
        "mode": "allowlist",
        "domains": ["pypi.tuna.tsinghua.edu.cn", "files.pythonhosted.org"],
        "cidrs": []
    }
})
```

实现原理：管控面（跑在 `network_mode: host`）按容器源 IP 在 `DOCKER-USER` 链挂专属链 `SBX_<id>`，白名单外的流量直接 DROP。

P3 起支持**通配符域名**（`*.example.com`）+ **CDN 切换自动跟随**：详见 §4.10。

### 4.5 检查系统健康

```python
cap = httpx.get(f"{API}/health", headers=H).json()
# {
#   "ok": true,
#   "criu": true,            ← CRIU 是否可用
#   "netpolicy": true,       ← iptables 是否可用
#   "capacity": {"max": 24, "used": 3, "cpu": 2, "memoryMB": 2048}
# }
```

---

### 4.6 获取沙箱产出物（文件下载）

代码跑完后，生成在沙箱里的文件怎么拿回来：

**方式 A：用 E2B SDK（最方便）**

```python
from e2b_code_interpreter import Sandbox
sbx = Sandbox.create(api_url="http://${SBX_SSH_HOST}:8902",
                     api_key="${SBX_E2B_KEY}", domain="${SBX_DOMAIN}")

# 读文件（返回字符串）
content = sbx.files.read("/home/user/workspace/output.csv")

# 列出目录
entries = sbx.files.list("/home/user/workspace")
for e in entries:
    print(f"{e.name}  {e.type}  {e.path}")

# 通过命令查看
out = sbx.commands.run("cat /home/user/workspace/output.csv")
print(out.stdout)

sbx.kill()
```

**方式 B：纯 HTTP（不依赖 SDK）**

```python
import httpx
API = "http://${SBX_SSH_HOST}:8902"
DOMAIN = "${SBX_DOMAIN}"
H = {"Authorization": "Bearer ${SBX_API_KEY}"}

# 创建沙箱
sbx = httpx.post(f"{API}/sandboxes", headers=H,
                 json={"templateID": "<tmplID>", "timeout": 600}).json()
sid, token = sbx["sandboxID"], sbx["envdAccessToken"]
bh = {"X-Access-Token": token}

# 读文件
content = httpx.get(f"https://49983-{sid}.{DOMAIN}/files",
                    params={"path": "/home/user/workspace/output.csv"},
                    headers=bh, verify="certs/ca.pem").content

# 上传文件
httpx.post(f"https://49983-{sid}.{DOMAIN}/files",
           params={"path": "/home/user/workspace/input.json"},
           headers=bh, content=b'{"key":"value"}', verify="certs/ca.pem")

# 目录列表
r = httpx.post(f"https://49983-{sid}.{DOMAIN}/filesystem.Filesystem/ListDir",
               json={"path": "/home/user/workspace"},
               headers={**bh, "Content-Type": "application/connect+json"},
               verify="certs/ca.pem")

httpx.delete(f"{API}/sandboxes/{sid}", headers=H)
```

**方式 C：运维提取（沙箱还活着时）**

```bash
ssh root@${SBX_SSH_HOST}
docker cp sbx-{sandboxID}:/home/user/workspace/output.csv ./output.csv
```

**鉴权注意**：文件 API 需要 `X-Access-Token`（创建沙箱响应里的 `envdAccessToken`），每个沙箱随机生成。

---

### 4.7 让 Claude Code / Cursor 直接驱动沙箱（**MCP 集成**）

> 🆕 P3 引入。装一次 MCP 配置，Claude Code 就能在对话里直接 `create_sandbox`、`run_code`、`files_read`，**零代码接入**。

#### 4.7.1 安装依赖

在**本地机器**（Claude Code / Cursor 运行的地方），一次性装好 MCP SDK：

```bash
pip install -r sandbox-service/server/requirements-mcp.txt
```

#### 4.7.2 配置 MCP host

**Claude Code**（`~/.claude/mcp_servers.json` 或项目级 `.mcp.json`）：

```json
{
  "mcpServers": {
    "zt-sandbox": {
      "command": "python",
      "args": ["e:/work/zt-Sandbox/sandbox-service/server/mcp_server.py"],
      "env": {
        "SBX_API_URL": "http://${SBX_SSH_HOST}:8902",
        "SBX_API_KEY": "${SBX_API_KEY}",
        "SBX_DOMAIN": "${SBX_DOMAIN}",
        "SBX_CA_CERT": "e:/work/zt-Sandbox/sandbox-service/certs/ca.pem"
      }
    }
  }
}
```

> 端口/密钥占位符请替换为实际值（见 §0 速查）。`SBX_CA_CERT` 指向自签 CA；如果懒得管，加 `"SBX_INSECURE": "1"` 走明文校验（仅 dev）。

#### 4.7.3 可用 tools（12 个）

| 分类 | Tool | 说明 |
|---|---|---|
| 模板 | `list_templates` | 列出可用模板 |
| 生命周期 | `create_sandbox` | 创建实例（返回 sandbox_id） |
| 生命周期 | `list_sandboxes` / `get_sandbox` | 列表 / 详情 |
| 生命周期 | `kill_sandbox` | 销毁 |
| 生命周期 | `pause_sandbox` / `resume_sandbox` | 暂停（CRIU 保留内存）/ 恢复 |
| 计算 | `run_code` | Jupyter 内核执行 Python（stateful） |
| 计算 | `run_command` | 前台 shell 命令 |
| 文件 | `files_read` / `files_write` / `files_list` | 读 / 写 / 列目录 |

#### 4.7.4 在 Claude Code 里使用

```text
> 用 zt-sandbox 创建一个 Python 沙箱，跑 "import numpy as np; print(np.__version__)"
  → Claude 调用 create_sandbox → run_code → kill_sandbox，整个流程自动完成

> 在那个沙箱里写一个 requirements.txt，pip install requests，然后跑一段请求 https://httpbin.org/ip 的代码
  → Claude 链式调用 create_sandbox → files_write → run_code（多次）→ kill_sandbox

> 暂停这个沙箱，10 秒后恢复，验证 x = 41 还在
  → Claude 调用 pause → 等待 → resume → run_code 验证
```

#### 4.7.5 数据面调用路径

MCP server 不直接连沙箱容器，而是走 **edge-proxy HTTPS 子域路由**（与 e2b SDK 一致）：

```text
MCP server  ──HTTP──▶  control plane :8902   (lifecycle, 元数据)
            ──HTTPS─▶  edge-proxy :443
                          │
                          ▼  subdomain 路由
                  https://{port}-{sandboxID}.{DOMAIN}
                  ├─ 49983 → envd      (files, process)
                  ├─ 49999 → jupyter   (run_code)
                  └─ 3000  → browser   (CDP, screenshot)
```

好处：MCP server 部署在哪都能用，不需要直接连通宿主机 20000+ 端口。

#### 4.7.6 端到端冒烟

```bash
# 在本机跑，需要 SBX_API_URL / SBX_API_KEY / SBX_DOMAIN 三个环境变量
python sandbox-service/tests/mcp_smoke.py
```

会跑完 8 步完整流程：list_templates → create_sandbox → list/get → files 读写 → run_code (stateful) → run_command → pause/resume → kill，验证 Jupyter 变量在 pause/resume 后仍保留。

#### 4.7.7 MCP 排错清单

| 症状 | 检查 |
| --- | --- |
| MCP 启动报 `SBX_API_KEY env var is required` | 配置里 `env.SBX_API_KEY` 是否设了，注意**不要**用 `${}` 占位（除非 IDE 支持 shell 变量展开） |
| MCP 启动报 `SBX_DOMAIN env var is required` | 同上，必须填 `${SBX_DOMAIN}` 实值（不能空） |
| Tool 调用全 401 | `SBX_API_KEY` 不在 `API_KEYS` 列表里 → 服务器侧加 |
| Tool 调用全 SSL 错 | 没配 `SBX_CA_CERT` 且未开 `SBX_INSECURE=1` |
| `run_code` 超时 | 默认 `SBX_HTTP_TIMEOUT=120`；长任务调大或拆段 |
| 文件读出来是乱码 | 大文件用 `run_command: cat` 或 `docker cp`，`files_read` 走 JSON 字符串 |
| 创建沙箱 429 | 配额满 → 让 Claude 先调 `list_sandboxes` 把旧的 `kill` 掉 |

---

### 4.8 生命周期钩子 + watchdog（OSEP-0020 风格）

> 🆕 P3 引入。模板可以挂 **startup 钩子**（创建后顺序执行，失败可 fail-closed 整盘回滚）和 **periodic 钩子**（按 `interval_s` 节拍重跑）。

#### 4.8.1 钩子结构

```json
{
  "name": "marker",
  "command": "mkdir -p /tmp/hook && touch /tmp/hook/ok",
  "cwd": "/var/log",
  "env": {"FOO": "bar"},
  "timeout_s": 30,
  "fail_closed": true,
  "interval_s": 60          // 仅 periodic 用：两次执行至少间隔这么多秒
}
```

| 字段 | 适用 | 默认 | 说明 |
| --- | --- | --- | --- |
| `name` | 全部 | `"?"` | 用于查日志和 hook_state 索引 |
| `command` | 全部 | **必填** | 通过 `/bin/sh -c` 在容器内 `docker exec` 执行 |
| `cwd` / `env` | 全部 | `null` | 可选 |
| `timeout_s` | 全部 | 60 | watchdog 墙钟超时；超时返回 error 但**不杀进程**（需要硬杀请自己在 command 里加 `timeout`） |
| `fail_closed` | startup | `true` | 失败时是否阻塞沙箱发布；`false` 时仅记录，仍继续后续钩子 |
| `interval_s` | periodic | 60 | 两次执行最少间隔多少秒 |

#### 4.8.2 创建带钩子的模板

```bash
curl -X POST http://${SBX_DOMAIN}:8902/v3/templates \
  -H "Authorization: Bearer ${SBX_API_KEYS}" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "with-hooks",
    "image": "sandbox/code-interpreter:v1",
    "cpuCount": 1, "memoryMB": 1024, "diskSizeMB": 1024,
    "startupHooks": [
      {"name": "warm-cache", "command": "mkdir -p /home/user/workspace && ls /home/user/workspace", "timeout_s": 10},
      {"name": "block-if-broken", "command": "exit 0", "fail_closed": true}
    ],
    "periodicHooks": [
      {"name": "tick", "command": "echo $(date -Iseconds) >> /tmp/tick.log", "interval_s": 30}
    ]
  }'
```

#### 4.8.3 单独改钩子（不重建模板）

```bash
# 查
curl -s -H "Authorization: Bearer ${SBX_API_KEYS}" \
  http://${SBX_DOMAIN}:8902/templates/${TMPL_CODE}/hooks | jq

# 改：startupHooks / periodicHooks 至少传一个，未传的保持不变
curl -X PUT -H "Authorization: Bearer ${SBX_API_KEYS}" \
  -H "Content-Type: application/json" \
  -d '{"periodicHooks": [{"name":"tick","command":"echo OK","interval_s":15}]}' \
  http://${SBX_DOMAIN}:8902/templates/${TMPL_CODE}/hooks
```

#### 4.8.4 看执行历史

```bash
curl -s -H "Authorization: Bearer ${SBX_API_KEYS}" \
  http://${SBX_DOMAIN}:8902/sandboxes/${SBX_ID}/hooks/status | jq
```

返回示例：
```json
{
  "sandboxID": "sbx...",
  "configuredStartupHooks": [{"name": "warm-cache", ...}],
  "configuredPeriodicHooks": [{"name": "tick", "interval_s": 30}],
  "hookState": {
    "startup": {
      "ran_at": 1730000000.12,
      "all_passed": true,
      "blocking_failures": [],
      "results": [{"name": "warm-cache", "ok": true, "exit_code": 0, "elapsed_s": 0.04, "stdout": "...", "stderr": ""}]
    },
    "periodic": {
      "last_tick": 1730000030.5,
      "hooks": {
        "tick": {"last_run": 1730000030.5, "last_ok": true, "last_error": "", "elapsed_s": 0.01}
      }
    }
  }
}
```

#### 4.8.5 失败语义

| 场景 | 结果 |
|---|---|
| startup hook 超时 | 单条记录 `ok=false, error="timeout after 30s"`，不影响后续（除非 fail_closed） |
| startup hook 任意一条 `fail_closed=true` 失败 | **整盘回滚**：容器销毁 + 元数据删除 + `POST /sandboxes` 返回 **500 `启动钩子失败: [...]`** |
| startup hook `fail_closed=false` 失败 | 记录在 `hook_state.startup.results`，`all_passed=true`，沙箱正常发布 |
| periodic hook 失败 | 仅记日志 + 更新 `last_ok=false` / `last_error`，**不杀沙箱**（best-effort） |

#### 4.8.6 端到端冒烟

```bash
python sandbox-service/tests/hook_smoke.py
```

跑 5 项：PUT hooks round-trip → startup 成功 → startup fail-closed 回滚 → startup 非阻塞失败容忍 → periodic 触发验证。

---

### 4.9 Ingress on-demand keepalive

> 🆕 P3 引入。**用户视角**：暂停的沙箱对端来说"永远在线"——任何 HTTPS 请求过来都会自动触发唤醒，**不需要显式调 `/connect`**。

#### 4.9.1 行为对比

| 场景 | 没有 keepalive | 有了 keepalive |
|---|---|---|
| 用户在浏览器打开 paused sandbox URL | `ERR_CONNECTION_REFUSED` | 第一次请求 ~1.5s 后页面正常加载（用户无感） |
| 用户 curl paused sandbox 的 envd | `curl: (7) Failed to connect` | curl 拿到 200 |
| WebSocket / CDP | 连接失败 | 自动恢复 + 转发 |
| 后续请求 | 都失败 | 走 fast path（容器已 running） |

实现路径只有 ~10 行：

```text
client ──HTTPS──▶ edge proxy ──open_connection──▶ sandbox host_port
                                        │
                                        └── ECONNREFUSED?
                                                │
                                                ▼
                                  POST /internal/auto-resume
                                                │
                                                ▼
                                  resume sandbox, retry once
```

#### 4.9.2 端点

```bash
# 内部端点（不走 API Key 鉴权，因为只有边缘代理会调）
curl -X POST "http://127.0.0.1:8902/internal/auto-resume?sandbox=$SID&port=$PORT"
# → 200 {"ok":true}                     # 唤醒成功
# → 200 {"ok":true,"alreadyRunning":true} # running 沙箱直接 no-op
# → 404 sandbox not found                # 不存在
# → 409 cannot auto-resume              # 状态非 paused
# → 503 resume failed                   # 容器存在但 _resume_data_plane 失败
```

#### 4.9.3 失败模式

| 触发 | 结果 |
| --- | --- |
| paused 沙箱 → /internal/auto-resume | 同步 resume + 等 envd 就绪；返回 200 |
| running 沙箱 → /internal/auto-resume | no-op，秒回 `{alreadyRunning:true}`（proxy 第一次重试用） |
| 唤醒后 sandbox 仍无法 listen port | 503，proxy 透传给客户端 |
| sandbox row 不存在 | 404（这种情况一般不会出现，因为 proxy 先解析 host header） |

#### 4.9.4 端到端冒烟

```bash
python sandbox-service/tests/keepalive_smoke.py
```

跑 4 项：running 可达 → paused 唤醒（~1.7s wake vs 0.01s fast path）→ 二次唤醒循环 → `/internal/auto-resume` 幂等。

> **⚠️ 运行前提**：因为 Windows 开发机出站 443 受限，这个 smoke 必须在 **Linux server** 上跑（已通过 `sftp` 上传到 `/srv/sandbox-service/tests/keepalive_smoke.py`）。
> 或者从任何能解析 `*.${SBX_DOMAIN}` + 连得上 server:443 的机器跑。

### 4.10 Egress FQDN allowlist（通配符 + CDN 切换）

> 🆕 P3 引入。在 P2 IP/CIDR + 单域名白名单的基础上加两件事：**通配符域名** `*.example.com`、**周期性重解析**（CDN 切换 IP 后自动跟随）。

#### 4.10.1 语法

| 写法 | 含义 |
|---|---|
| `example.com` | 精确匹配，只解析 apex |
| `*.example.com` | 通配，展开为 apex + `www/api/cdn/static/assets/chat` 六个常用前缀（都解析） |
| `10.0.0.0/8` | CIDR 字面量，原样写入 iptables `-d` 规则 |

> **为什么展开固定前缀而不是枚举所有子域？** 没权威方法列出 `*.example.com` 下所有域名。固定前缀覆盖 90% 实际场景，其余子域通常共享 apex 的 CDN 边缘 IP，apex 规则已经够用。

#### 4.10.2 手动 / 自动刷新

```bash
# 手动：每次会 flush+重建当前 SBX_<id> 链
curl -X POST "http://127.0.0.1:8902/sandboxes/$SID/netpolicy/refresh"
# → {"sandboxID":"...","refreshedAt":<unix_ts>,"allowed":[...],"resolved":{...}}
# → 200 {"skipped":true,...}       # open mode / 已销毁的沙箱

# 自动：管控面后台每 5min 跑一次（覆盖所有 allowlist 模式的活跃沙箱）
# 调 NETPOLICY_REFRESH_S=<秒> 改周期
```

CDN 切换场景：原 IP 被回收 / 新 IP 上线 → 下一次 refresh 把新 IP 写进 iptables，沙箱侧无感。

#### 4.10.3 实现细节

- 解析走 `socket.getaddrinfo`；**只保留 IPv4**（iptables-nft 在本部署环境拒绝 IPv6 target，会刷一堆 warning）。
- chain 名 `SBX_<id-last10>`（iptables 链名 ≤ 28 字符限制）。
- 状态保存在管控面进程内的 `_state[sandbox_id]`，重启会丢失（重启后第一次 refresh 会按"已无状态"跳过，沙箱侧旧规则仍在；如需重启后立刻对齐，需要重建沙箱或重新 POST `/netpolicy`）。
- `127.0.0.1` 的 host-network 沙箱**不自动**应用 netpolicy（所有沙箱共享回环，iptables 无意义）；但可以走 `POST /sandboxes/{id}/netpolicy` 手动装——见 fqdn smoke 就是这么验证的。

#### 4.10.4 端到端冒烟

```bash
python sandbox-service/tests/fqdn_smoke.py
```

跑 3 项：

1. **wildcard 展开**：`*.openai.com` + `*.anthropic.com` → apex + `www/api/cdn/...` 都在 `resolved` 里
2. **手动 refresh**：`refreshedAt` 推进、`resolved` 返回新 IP
3. **精确模式不展开**：`example.com` 不会自动塞进 `www.example.com`

---

## 5. 镜像矩阵

统一 base `sandbox/base:v1`（Python 3.11 + mini_envd + Chromium 154），三个 flavour 通过 `SBX_FEATURES` 环境变量选择性拉起服务：

| 镜像 | SBX_FEATURES | 容器端口 |
|---|---|---|
| `sandbox/code-interpreter:v1` | `envd,jupyter` | 49983 / 49999 |
| `sandbox/browser:v1` | `envd,browser` | 49983 / 3000 |
| `sandbox/all-in-one:v1` | `envd,jupyter,browser` | 49983 / 49999 / 3000 |

每个沙箱占宿主机 3 个端口（20000 起，步长 3），映射关系写 SQLite。

---

## 6. 测试套件

所有测试跑在 Windows 开发机（`e:\work\zt-Sandbox\sandbox-service`），对服务器 ${SBX_SSH_HOST} 发请求。

| 测试 | 文件 | 用途 |
|---|---|---|
| P0 端到端 | `tests/e2e_smoke.py` | 代码执行 + 文件 + 命令 + 生命周期（10 项） |
| P1 浏览器基础 | `tests/p1_browser_smoke.py` | CDP 发现 + Playwright 直连 + 便捷端点（12 项） |
| **P1+ 浏览器 session** | `tests/p1b_session_smoke.py` | **长会话 + 多 tab + 并发沙箱（11 项）** |
| P2 全套 | `tests/p2_smoke.py [net|criu|stress]` | 网络白名单 + CRIU 暂停 + 并发压测 |
| 快速 pause 验证 | `tests/quick_pause_test.py` | create→run→pause→connect→run→kill |
| **P3 MCP 集成** | `tests/mcp_smoke.py` | **12 个 MCP tools 端到端（stdio 子进程调用）** |
| **P3 Lifecycle Hook** | `tests/hook_smoke.py` | **startup fail-closed 回滚 + periodic 节拍触发（5 项）** |
| **P3 Ingress Keepalive** | `tests/keepalive_smoke.py` | **paused 沙箱自动唤醒（4 项：wake vs fast path 时延对比 + 二次唤醒循环 + 幂等）** |
| **P3 FQDN Allowlist** | `tests/fqdn_smoke.py` | **通配符展开 + CDN 切换 refresh + 精确模式不展开（3 项）** |

运行：
```bash
# 激活 venv
cd E:\work\zt-Sandbox\sandbox-service
.venv\Scripts\activate

# 跑测试
python tests/e2e_smoke.py
python tests/p1_browser_smoke.py
python tests/p1b_session_smoke.py browser 3    # 浏览器 + 3 并发沙箱
python tests/p2_smoke.py                       # 全套
python tests/p2_smoke.py criu                  # 只跑 CRIU
python tests/mcp_smoke.py                      # MCP 集成冒烟
python tests/hook_smoke.py                     # 钩子 + watchdog
python tests/fqdn_smoke.py                    # FQDN 通配符 + CDN refresh
```

---

## 7. 部署 / 运维脚本

所有脚本都跑在 Windows 开发机，用 paramiko SSH 到服务器：

| 脚本 | 用途 |
|---|---|
| `deploy_server.py` | **首次全量部署**：上传 → 证书 → 依赖（criu / experimental）→ 4 个镜像 → compose → 注册模版 → 下载 CA |
| `fix_criu.py` | **真 CRIU 激活**：禁 containerd-snapshotter → 全量 wipe → 重建 → 自检 |
| `rebuild_p1_images.py` | 只重建沙箱镜像（base + 三个 flavour）+ 重置元数据 |
| `build_cp.py` | 只重建管控面镜像（含 iptables）并重启 |
| `redeploy.py` | 只同步代码 + 重启（跳过镜像构建，最快） |
| `cleanup_reregister.py` | 清理所有沙箱并重新注册模版 |

**常规迭代**：改完代码 → `python redeploy.py`（30 秒）
**改 Dockerfile**：`python rebuild_p1_images.py`（10-15 分钟）
**首次 / 换服务器**：`python deploy_server.py`（20-30 分钟）

---

## 8. 关键坑位（必读）

### 8.1 鉴权双通道
- e2b SDK 只发 `X-API-KEY`
- REST / curl 用 `Authorization: Bearer`
- 管控面两个都认，不要搞混

### 8.2 ConnectRPC 实际走 JSON
mini_envd 的 ConnectRPC 接口编码是 `application/connect+json`，不是 proto。SDK 默认用 JSON 编码，所以没问题；如果你手写客户端，记得用 JSON。

### 8.3 Chromium 130+ 改了 `/json/new`
从 GET 改成了 PUT，关闭也要兼容多方法。`browser_svc.py` 已经处理了，不要自己再去调 CDP。

### 8.4 边缘代理不能剥 `transfer-encoding`
chunked 请求转发时 `transfer-encoding: chunked` 必须保留；WebSocket 必须保留 `Connection: Upgrade` / `Upgrade: websocket` 头并全双工泵送。

### 8.5 本机 HTTP 代理会污染测试
开发机有全局代理，Playwright 的 node driver 会走代理去连 `*.nip.io`（连不上）。测试脚本开头一定要清掉 `HTTP(S)_PROXY`、设 `NO_PROXY=*`、设 `NODE_EXTRA_CA_CERTS` 指向 `certs/ca.pem`。

### 8.6 CRIU 依赖 daemon + 网络配置
Docker 29 默认开 containerd snapshotter，会撞 CRIU 恢复时的 content-store 冲突；bridge 网络则会在 restore 时撞 netns bind-mount 失败。**必须先跑 `fix_criu.py`**。跑完后：
- `/health` 应该报 `"criu": true`
- pause 后沙箱 JSON 的 `pauseMode` 应该是 `"criu"`
- 容器使用 `--network=host`，各服务通过 env 变量绑定分配的端口

**附带限制**：`--network=host` 下所有容器共享 127.0.0.1，目前网络白名单（iptables 按源 IP 过滤）不生效，后续可通过 cgroup 匹配补齐。

### 8.7 compose 没声明 `build:` 段不会被 `--build` 重建
`docker compose up --build` 只重建 compose 文件里有 `build:` 段的服务。`control-plane` 的镜像名是 `sandbox/control-plane:v1`，compose 里没用 `build:`，所以改完 `server/Dockerfile` 必须显式 `docker build` 再 `compose up`（`rebuild_p1_images.py` / `deploy_server.py` 都处理了）。

---

## 9. 与百炼 / 原版 E2B 的已知差异

| 项 | 百炼 | 我们 |
|---|---|---|
| pause 保留内存 | 是（CRIU） | ✅ 已支持（`fix_criu.py` 后） |
| 多地域 | cn-beijing | 单服务器 |
| 控制台 UI | 有 | 无（CLI / REST / SDK） |
| 网络 ACL | 有 | ✅ 已支持（iptables） |
| envd 实现 | 自研 | 自研 mini_envd（Python，ConnectRPC JSON） |
| 浏览器 session | 有 | ✅ 已支持（Session API + CDP） |

---

## 10. 故障排查

**管控面起不来**
```bash
ssh root@${SBX_SSH_HOST}
docker logs sandbox-control-plane --tail 50
```

**沙箱创建失败，报端口耗尽**
- `MAX_SANDBOXES=24`，`MAX_MEMORY_MB=6144`
- 看一下 `http://${SBX_SSH_HOST}:8902/health` 的 `capacity.used`
- 手动清理：`curl -X GET http://${SBX_SSH_HOST}:8902/v2/sandboxes -H 'Authorization: Bearer ${SBX_API_KEY}'` 然后逐个 `DELETE /sandboxes/{id}`

**浏览器连不上 / cdpReady=false**
- 等 60 秒再试，Chromium 启动慢
- `docker logs sbx-{sandboxID}` 看 Chromium 启动日志
- 确认 `SBX_FEATURES` 包含 `browser`

**CRIU 不工作**
- 跑 `python fix_criu.py`，它会做完整自检
- 确认 `docker info` 里**没有** `Starting daemon with containerd snapshotter integration enabled`
- 确认 `criu --version` 能跑出版本号（需要 3.x+）

**Playwright 连不上 CDP**
- 99% 是代理污染。检查 `os.environ` 里的 `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`
- 检查 `NODE_EXTRA_CA_CERTS` 指向了 `certs/ca.pem`
- 直接用 `webSocketDebuggerUrl` 字段（`/json/version` 返回的），不要自己拼

---

## 11. 给 Agent 的最后提示

### 11.1 工作约定（按优先级）

1. **优先用封装好的 Session API**（§4.3 方式 A），不要自己拼 CDP 调用
2. **创建沙箱后等 health 轮询**返回 `ok: true` 再开始用（Chromium 启动慢，要 30-60 秒）
3. **销毁比创建便宜**——不要复用一个脏沙箱，用完就 `kill()`
4. **pause 不等于 kill**——pause 后沙箱还在（占端口 / 占配额），不用就 `kill()`
5. **每个任务新开沙箱**——脏数据 + 内存膨胀让复用得不偿失
6. **批量任务开并发**——`MAX_SANDBOXES=24`，合理并发用满配额
7. **捕到异常立即 abort**——503/429 多半是配额满，先 `DELETE` 旧的再重试

### 11.2 字段速记

| 字段 | 取值 |
| --- | --- |
| 响应错误 `code` | 100001 鉴权 / 100002 模版不存在 / 100003 资源不存在 / 100004 参数错 / 100005 启动失败 / 100006 恢复失败 / 100007 有依赖不能删 / 100009 配额满 |
| 沙箱 `state` | `running` / `paused` |
| 沙箱 `pauseMode` | `criu`（保留内存）/ `stop`（只留文件系统） |
| 模板 `feature` | `"envd,jupyter"` / `"envd,browser"` / `"envd,jupyter,browser"` |
| 模板 `browserEnabled` | `true` 时 Session API 可用 |

### 11.3 常见任务配方

| 任务 | 推荐路径 |
| --- | --- |
| 跑一段 Python 看输出 | `run_code`（MCP）/ `sbx.run_code()`（SDK） |
| 跑 shell 看 stdout | `run_command` / `sbx.commands.run()` |
| 上传/下载文件 | `files_read`/`files_write`（小文件）/ `docker cp`（大文件） |
| 网页截图 | `Session API` + `act:screenshot`（§4.3 方式 A） |
| 跑浏览器脚本 | `Session API` + `act:evaluate`（不需要 Playwright） |
| 保留 Jupyter 变量暂停 | `pause_sandbox`(CRIU) → `resume_sandbox` |
| 限制出网 | 创建模板时 `networkPolicy.mode=allowlist` + `domains` |
| 排查配额 | `GET /health` 看 `capacity.used` → `DELETE` 旧沙箱 |

### 11.4 拿到 token 之后

- `envdAccessToken` 仅本次沙箱有效，跨沙箱不通用
- 过期用 `POST /sandboxes/{id}/refreshes` 续期
- 数据面走 HTTPS edge-proxy，**必须**带 `X-Access-Token: <token>` 头

---

读完这篇还有问题，去翻 `DESIGN.md`（架构细节）或者直接看 `server/main.py`（管控面 API 全在那里）。

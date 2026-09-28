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
| 服务器 | `<internal-host>`（内网 Linux，root / 123456） |
| 管控面 | `http://<internal-host>:8902`（HTTP） |
| 边缘代理 | `https://*.{sandboxID}.<internal-host>.nip.io`（TLS，自签 CA） |
| 管控面 API Key | `<dev-key-redacted>`（REST 用，放 `Authorization: Bearer ...`） |
| SDK API Key | `<e2b-key-redacted>`（官方 e2b SDK 用，放 `X-API-KEY` 或 `api_key=`） |
| CA 证书（客户端） | 本机 `sandbox-service/certs/ca.pem`，部署脚本会自动下载 |
| 工作目录 | 本机 `E:\work\zt-Sandbox\sandbox-service` |
| 远程目录 | 服务器 `/srv/sandbox-service` |

**鉴权双通道**：管控面同时认 `Authorization: Bearer <key>` 和 `X-API-KEY: <key>` 两种 header。
官方 e2b SDK 只会发后者，所以两个都要支持。`API_KEYS` 环境变量是逗号分隔的列表，目前配的是 `<dev-key-redacted>,<e2b-key-redacted>`。

---

## 1. 项目是什么

一句话：**给 AI 智能体用的隔离执行环境**。智能体要跑代码、要操作浏览器、要处理文件——都丢到这个沙箱里，跑完销毁，互不影响。

对标的产品是 [阿里百炼 Sandbox](https://bailian.console.aliyun.com/) 和 [E2B](https://e2b.dev/)。对外接口跟 E2B 的 Python SDK 完全兼容——意味着你**不改一行 SDK 代码**，把 `api_url` 指向我们的服务器就能跑：

```python
from e2b_code_interpreter import Sandbox
sbx = Sandbox.create(
    api_url="http://<internal-host>:8902",
    api_key="<e2b-key-redacted>",
    domain="<internal-host>.nip.io",
)
sbx.run_code("print(1+1)")
```

---

## 2. 架构

```
  你（或你的 Agent）  ──▶  管控面 (FastAPI @ :8902)
                               │  docker SDK
                               ▼
                          数据面（每个沙箱一个容器）
                               │
                               ├── mini_envd :49983  （文件 / 进程 RPC，ConnectRPC JSON）
                               ├── jupyter   :49999  （代码执行，Jupyter 内核协议）
                               └── browser   :3000   （Chromium CDP + Session API）
```

**关键设计决策**：

1. **管控面 / 数据面分离**：管控面只管创建/销毁/元数据；拿到 `host + token` 后你的 SDK **直接连容器**，管控面不做数据面代理。跟百炼 / E2B 一致。
2. **自研 mini_envd 替代 E2B 的 envd**：E2B 开源的 envd 是 Go 写的，我们不依赖它，用 Python + FastAPI 重写了相同的 ConnectRPC 接口（`/process.Process/...`、`/filesystem.Filesystem/...`、`/files`）。SDK 完全无感知。
3. **边缘代理按 host header 路由**：`https://3000-{sandboxID}.<internal-host>.nip.io/...` → 查 SQLite 找 host 端口 → 转发到 `127.0.0.1:{hostPort}`。WebSocket 升级也支持（Playwright 通过 CDP 直连浏览器就靠它）。

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

## 4. 给 Agent 的实战手册

### 4.1 创建沙箱 + 跑代码

```python
from e2b_code_interpreter import Sandbox

sbx = Sandbox.create(
    api_url="http://<internal-host>:8902",
    api_key="<e2b-key-redacted>",
    domain="<internal-host>.nip.io",
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
sbx.beta_pause(api_url=..., api_key=..., headers={"Authorization": "Bearer <dev-key-redacted>"})

# 恢复
Sandbox._cls_resume(sandbox_id=sbx.sandbox_id, ...)
sbx2 = Sandbox.connect(sandbox_id=sbx.sandbox_id, ...)

# 检查用了哪种模式
info = httpx.get(f"http://<internal-host>:8902/sandboxes/{sbx.sandbox_id}",
                 headers={"Authorization": "Bearer <dev-key-redacted>"}).json()
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

API = "http://<internal-host>:8902"
KEY = "<dev-key-redacted>"
DOMAIN = "<internal-host>.nip.io"
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
sbx = Sandbox.create(api_url="http://<internal-host>:8902",
                     api_key="<e2b-key-redacted>", domain="<internal-host>.nip.io")

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
API = "http://<internal-host>:8902"
DOMAIN = "<internal-host>.nip.io"
H = {"Authorization": "Bearer <dev-key-redacted>"}

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
ssh root@<internal-host>
docker cp sbx-{sandboxID}:/home/user/workspace/output.csv ./output.csv
```

**鉴权注意**：文件 API 需要 `X-Access-Token`（创建沙箱响应里的 `envdAccessToken`），每个沙箱随机生成。

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

所有测试跑在 Windows 开发机（`e:\work\zt-Sandbox\sandbox-service`），对服务器 <internal-host> 发请求。

| 测试 | 文件 | 用途 |
|---|---|---|
| P0 端到端 | `tests/e2e_smoke.py` | 代码执行 + 文件 + 命令 + 生命周期（10 项） |
| P1 浏览器基础 | `tests/p1_browser_smoke.py` | CDP 发现 + Playwright 直连 + 便捷端点（12 项） |
| **P1+ 浏览器 session** | `tests/p1b_session_smoke.py` | **长会话 + 多 tab + 并发沙箱（11 项）** |
| P2 全套 | `tests/p2_smoke.py [net|criu|stress]` | 网络白名单 + CRIU 暂停 + 并发压测 |
| 快速 pause 验证 | `tests/quick_pause_test.py` | create→run→pause→connect→run→kill |

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
ssh root@<internal-host>
docker logs sandbox-control-plane --tail 50
```

**沙箱创建失败，报端口耗尽**
- `MAX_SANDBOXES=24`，`MAX_MEMORY_MB=6144`
- 看一下 `http://<internal-host>:8902/health` 的 `capacity.used`
- 手动清理：`curl -X GET http://<internal-host>:8902/v2/sandboxes -H 'Authorization: Bearer <dev-key-redacted>'` 然后逐个 `DELETE /sandboxes/{id}`

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

1. **优先用封装好的 Session API**（§4.3 方式 A），不要自己拼 CDP 调用
2. **创建沙箱后等 health 轮询**返回 `ok: true` 再开始用（Chromium 启动慢，要 30-60 秒）
3. **销毁比创建便宜**——不要复用一个脏沙箱，用完就 `kill()`
4. **pause 不等于 kill**——pause 后沙箱还在（占端口 / 占配额），不用就 `kill()`
5. **错误格式**：`{"code": 100004, "message": "参数缺失", "requestID": "..."}`
   - 100001 鉴权 / 100002 模版不存在 / 100003 资源不存在 / 100004 参数错 / 100005 启动失败
   - 100006 恢复失败 / 100007 有依赖不能删 / 100009 配额满
6. **state 字段**：`running` / `paused`；`pauseMode` 字段：`criu` / `stop`
7. **feature 字段**：`"envd,jupyter"` / `"envd,browser"` / `"envd,jupyter,browser"`

读完这篇还有问题，去翻 `DESIGN.md`（架构细节）或者直接看 `server/main.py`（管控面 API 全在那里）。

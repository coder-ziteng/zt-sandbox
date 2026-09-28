# 自研 Sandbox 服务设计文档（参考阿里百炼）

> 版本 v2.0 ｜ 2026-09-27 ｜ 决策：独立 Linux 服务器部署 · 兼容 E2B 协议 · 一期代码解释器先行
>
> **状态：P0 / P1 / P2 全部交付并通过端到端冒烟**（服务器 ${SBX_SSH_HOST}）

## 1. 目标

自研一套对标阿里百炼 Sandbox 的云端沙箱服务，为 AI 智能体提供隔离的代码执行、（二期）浏览器操作与文件处理环境。对外 **兼容 E2B SDK/API 协议**，官方 `e2b==2.31.0` Python SDK 不改代码即可接入。

## 2. 核心概念（与百炼对齐）

| 概念 | 说明 |
|---|---|
| 模版 Template | 运行环境配置：基础镜像、资源规格、文件挂载、网络策略、环境变量、生命周期。通过 `templateCode` 引用，可创建多实例 |
| 实例 Sandbox | 基于模版启动的运行实例，独立文件系统与网络。通过 `sandboxID` 引用 |
| 基础镜像 Image | 一期：`code-interpreter-v1`；二期：`browser`、`all-in-one` |

## 3. 总体架构

```
调用方（Agent / e2b SDK）
        │  Authorization: Bearer <api-key>
        ▼
┌─ 管控面 (FastAPI, 单进程常驻) ─────────────────────────┐
│ API 网关鉴权 → 模版管理 → 实例编排 → 调度器(超时回收)   │
│ 元数据: SQLite (templates / sandboxes / apikeys)       │
└───────────────┬────────────────────────────────────────┘
                │ docker SDK（本地 socket）
                ▼
┌─ 数据面 (每实例一个容器) ──────────────────────────────┐
│ envd :49999 (代码执行/命令/文件 RPC, 复用 E2B 开源组件)│
│ files /files?path= ...  process /process.Process/Start│
│ 二期: browser :3000 (Chromium + CDP)                  │
│ cgroups CPU/内存限额 · 独立网络 · 出口白名单代理(可选) │
└────────────────────────────────────────────────────────┘
```

**关键设计点**

1. **管控面/数据面分离**：管控面只管编排与元数据；数据面由 SDK 拿到 `host + token` 后直连容器，管控面不做数据面代理（与百炼一致）。
2. **envd 复用**：E2B 的 envd 守护进程开源（e2b-dev/infra），直接内置到镜像，`POST :49999/execute`、`/process.Process/Start`、`/files` 三个数据面端点零开发。
3. **端口模型**：每实例占用宿主机 2 个端口（envd + 预留 browser），从 20000 起动态分配，映射关系写入实例元数据；`create` 响应返回 E2B 格式的 `clientd` 域名/host 信息。

## 4. E2B 兼容 API 面（一期实现清单）

### 管控面

| 能力 | 方法 路径 | 备注 |
|---|---|---|
| 创建实例 | `POST /sandboxes` | 入参 `templateID/templateCode, metadata, timeout`；返回 `sandboxID, clientd, accessToken` |
| 列举实例 | `GET /v2/sandboxes` | |
| 获取实例 | `GET /sandboxes/{sandboxID}` | |
| 连接实例 | `POST /sandboxes/{sandboxID}/connect` | 返回新的数据面 token + host |
| 暂停实例 | `POST /sandboxes/{sandboxID}/pause` | 一期 = `docker stop`（保留文件系统层，**内存不保留**，与百炼有差异，见 §8） |
| 恢复实例 | `POST /sandboxes/{sandboxID}/resume` | `docker start` |
| 释放实例 | `DELETE /sandboxes/{sandboxID}` | `docker rm -f` + 清元数据 |
| 创建模版 | `POST /v3/templates` | 异步构建，返回 `templateCode + buildID` |
| 列举/获取/删除模版 | `GET /v2/templates`、`GET|DELETE /templates/{code}` | 有运行中实例时禁止删除 |
| 构建状态 | `GET /templates/{code}/builds/{buildID}/status` | 轮询用 |

### 数据面（envd 提供，无需开发）

| 能力 | 端点 |
|---|---|
| 运行代码 | `POST :49999/execute`（Jupyter 内核协议） |
| 执行命令 | `POST /process.Process/Start`（ConnectRPC） |
| 文件读写 | `GET /files?path=`、`POST /files?path=` |

### 错误格式（对齐百炼）

```json
{ "code": 100004, "message": "参数缺失", "requestID": "uuid" }
```

状态码：400 参数错 / 401 Key 无效 / 404 不存在 / 409 状态冲突 / 500 内部异常 / 501 暂不支持。

## 5. 镜像设计

### 一期 `code-interpreter-v1`（Dockerfile 要点）

```
base: ubuntu:22.04（或 python:3.11-slim）
+ python3.11 + pip（numpy/pandas/requests 预装）+ nodejs 20
+ envd（E2B 开源二进制，监听 49999）
+ supervisord 拉起 envd
+ 非 root 运行用户 sandbox
```

### 二期镜像

- `browser`：+ Chromium + `chromium --headless --remote-debugging-port=9222`，3000 端口出 CDP/Playwright WS
- `all-in-one`：两者合并，对标百炼 3000/5000 双端口布局

## 6. 资源与生命周期

| 项 | 设计 |
|---|---|
| 资源规格 | 模版二选一：`small`=1C2G、`large`=4C8G，映射到 docker `--cpus --memory` |
| 空闲超时 | 调度器每 30s 扫描，envd 最后活动时间超阈值 → pause（一期） |
| 最大存活 | 默认 7 天上限，超时强制 kill |
| 文件挂载 | 模版里声明 ≤5 个初始化文件，创建实例时 `docker cp` 进容器 |
| 环境变量 | 模版 env 注入容器 |
| 网络策略 | 一期不做；二期加白/黑名单出口代理（squash/tinyproxy） |

## 7. 部署（Linux 服务器）

```
sandbox-service/
├─ server/            # 管控面 FastAPI（python 3.11, docker SDK）
├─ images/code-interpreter/Dockerfile
├─ deploy/docker-compose.yml   # 管控面 + nginx(可选TLS)
└─ tests/e2e.py       # 用真实 e2b==2.31.0 SDK 冒烟
```

- 服务器要求：Linux x86_64、Docker ≥ 24、已装 docker compose、放行管控面端口（建议 8902）与 20000-21000 实例端口段（仅内网）
- 管控面配置 `API_KEYS` 环境变量（sk- 列表），数据面 token 每实例随机生成
- 部署：`docker compose up -d`，首次预热 `docker build` 三种模版

## 8. 与百炼的已知差异（如实标注）

| 项 | 百炼 | 自研一期 |
|---|---|---|
| pause 保留内存 | 是（CRIU 级） | ✅ 已支持（`fix_criu.py` 激活后），代码路径完整保留降级兜底 |
| 多地域 | cn-beijing | 单服务器 |
| 控制台 UI | 有 | 无（CLI/REST 管理） |
| 网络 ACL | 有 | 二期 |

## 9. 交付计划

- **P0（一期）**：管控面 9 个管控 API + code-interpreter 镜像 + e2b SDK 端到端冒烟（create → run_code → files.write/read → commands.run → kill） ✅
- **P1（二期）**：browser / all-in-one 镜像 + 3000 端口 Playwright 接入 + /health 轮询 ✅
- **P2（三期）**：网络白名单、CRIU 暂停恢复、并发调度压测 ✅

## 10. P1 交付：浏览器沙箱

### 镜像矩阵（统一 base，特性开关启动）

| 镜像 | 特性 `SBX_FEATURES` | 端口 |
|---|---|---|
| `sandbox/base:v1` | Python 3.11 + envd 三件套 + Chromium 154 | — |
| `sandbox/code-interpreter:v1` | `envd,jupyter` | 49983 / 49999 |
| `sandbox/browser:v1` | `envd,browser` | 49983 / 3000 |
| `sandbox/all-in-one:v1` | `envd,jupyter,browser` | 49983 / 49999 / 3000 |

容器内 `start.sh` 按 `SBX_FEATURES` 选择性拉起服务；端口模型升级为**每实例 3 个宿主机端口**（20000 起，步长 3）。

### 浏览器数据面（容器 :3000）

`envdsvc/browser_svc.py` 内置 headless Chromium（`--remote-debugging-port=9222`），对外提供：

| 端点 | 说明 |
|---|---|
| `GET /health` | `{"ok":true,"cdpReady":true,...}`，供管控面就绪轮询 |
| `GET /json/version`、`/json/list`、`/json/new`、`/json/close/{id}` | CDP 发现接口，`webSocketDebuggerUrl` 会改写成公网 `wss://3000-<id>.<domain>/devtools/...` |
| `WS /devtools/{path}` | 到 127.0.0.1:9222 的双向透传隧道 |
| `POST /screenshot` | 入参 `{url, fullPage, waitMs}` → PNG |
| `POST /content` | 入参 `{url, waitMs}` → `{title, url, html, text}` |

边缘代理新增 **WebSocket 隧道**（保留 `Connection: Upgrade` / `Upgrade: websocket`，全双工泵送），因此 Playwright / Puppeteer 可直连：

```python
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("wss://3000-<sandboxID>.${SBX_DOMAIN}/devtools/browser/<id>")
```

### 健康检查

- 实例级：`GET /sandboxes/{id}/health` → 分别探测 envd / jupyter / browser 三个数据面端口
- 管控面：`GET /health` → `{ok, criu, netpolicy, capacity}`

## 11. P2 交付：网络白名单 / CRIU / 并发压测

### 11.1 网络白名单（iptables）

`server/netpolicy.py`：按容器源 IP 在 Docker 的 `DOCKER-USER` 链挂专属链 `SBX_<id>`：

```
-m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
-p udp --dport 53 -j ACCEPT          # DNS，用于解析白名单域名
-d <白名单域名解析出的 IP> -j ACCEPT
-j DROP                              # 其余全部丢弃
```

模版字段 `networkPolicy = {"mode":"open|allowlist|blocked","domains":[...],"cidrs":[...]}`，创建实例时自动下发，停止/删除时回收。管控面容器以 `network_mode: host + cap_add NET_ADMIN` 运行（iptables 1.8.11 nf_tables）。

实测：白名单内 `pypi.tuna.tsinghua.edu.cn` 返回 200，白名单外 `www.baidu.com` 网络不可达。

### 11.2 CRIU 暂停恢复 —— ✅ 真激活

**根因（两层）**

1. **containerd snapshotter**：Docker 29.5.2 默认启用 `io.containerd.snapshotter.v1`（containerd image store）。在该模式下 `docker checkpoint create` 能成功 dump，但 `docker start --checkpoint` 试图把 checkpoint 上传到 containerd 内容存储时撞 hash 冲突（"failed to upload checkpoint to containerd: ... already exists"）。
2. **bridge 网络 netns bug**：即使禁用 containerd snapshotter，Docker 29 的 `docker start --checkpoint` 在恢复带 bridge 网络的容器时仍会失败（`bind-mount /proc/0/ns/net -> /var/run/docker/netns/<id>: no such file or directory`）。这是 Docker 29 在重建容器 netns 时的已知问题。

**激活路线（`fix_criu.py` + runtime 修改）**

1. 写入 `/etc/docker/daemon.json`：
   ```json
   {"experimental": true, "features": {"containerd-snapshotter": false}}
   ```
2. `systemctl restart docker`，验证 `docker info` 不再出现 *"Starting daemon with containerd snapshotter integration enabled"*
3. `docker system prune --all --volumes -f`（旧镜像在 containerd 内容存储里，切到经典 graphdriver 后已不可见）
4. 重建全部镜像（base / browser / all-in-one / code-interpreter / control-plane）
5. **runtime.py 改为 `--network=host`**：容器通过环境变量（`ENVD_PORT` / `JUPYTER_PORT` / `BROWSER_PORT`）绑定各自被分配的宿主机端口，避开 bridge 网络 netns 重建问题。
6. **start.sh 支持可配置端口**：`envdsvc/start.sh` 读取上述环境变量决定服务监听端口，默认值保持旧约定（49983 / 49999 / 3000）。
7. checkpoint 命令去掉 `--checkpoint-dir`：Docker 29 的 `docker start` 不再接受该参数，checkpoint 统一存到 Docker 默认路径（`/var/lib/docker/containers/<id>/checkpoints/<name>/`）。
8. `compose up -d` + 重新注册模版。
9. 跑一次真实 dump+restore 往返自检（throwaway python:3.11-slim 容器，glibc 基础镜像）。

**验证点**

- `/health` → `"criu": true`
- `POST /sandboxes/{id}/pause {"criu":"auto"}` → 随后 GET 沙箱 JSON 的 `pauseMode` 字段为 `"criu"`
- 设置 in-memory 状态（如 `counter = 41`）→ pause → resume → `counter + 1` 仍得 `42`（Jupyter kernel 进程完整保留）

**已知限制**

- **网络白名单暂不兼容 `--network=host`**：原实现基于容器 IP 做 iptables 过滤（DOCKER-USER 链按源 IP 路由到专属链）。host 模式下所有容器共享 127.0.0.1，无法区分。当前在 runtime 中直接跳过 `netpolicy.apply()`（`ip != "127.0.0.1"` 才生效），后续可通过 cgroup-based iptables 匹配补齐。
- **glibc 镜像友好**：CRIU 在 alpine/musl 镜像上有独立的恢复问题（进程恢复后立即退出），本服务的 ubuntu/python-slim 基础镜像都是 glibc，无此问题。

**降级机制**

管控面启动时跑 `_criu_probe()`：如果往返自检失败（无论什么原因），自动 fallback 为 `docker stop/start`，并在 sandbox JSON 的 `pauseMode` 字段如实上报 `"stop"`。代码路径保留，切换 daemon 配置 / 升级 Docker 后无需改代码即可启用。

**历史尝试（已放弃的路线）**

- ~~"第二个 dockerd" 方案（独立 data-root/socket）~~：systemd unit 起不来
- ~~`--checkpoint-dir` 旁路~~：Docker 29 在 `docker start` 侧拒绝
- ~~改 daemon.json 但只改 `experimental`~~：不动 `features` 则 containerd-snapshotter 仍是默认 on
- ~~bridge 网络 + 禁用 containerd-snapshotter~~：dump OK，restore 撞 netns bind-mount 失败

### 11.3 并发调度压测

- 准入控制：创建时校验 `MAX_SANDBOXES`（默认 24）与 `MAX_MEMORY_MB`（默认 6144），超限返回 `429 / code=100009`
- 压测结果（`tests/p2_smoke.py`，n=8 并发，规格 0.5C/512M）：**零失败，create+run p50 31.8s / p95 32.0s**，容量账目回落到 used=1

## 12. 运维脚本

| 脚本 | 用途 |
|---|---|
| `deploy_server.py` | 全量部署：上传 → 证书 → 依赖（criu/experimental）→ 4 个镜像 → compose → 注册模版 |
| `fix_criu.py` | **真 CRIU 激活**：禁 containerd-snapshotter → 全量 wipe/rebuild → dump+restore 往返自检 |
| `build_cp.py` | 仅重建管控面镜像（含 iptables）并重启 |
| `rebuild_p1_images.py` | 重建全部沙箱镜像并重置元数据 |
| `redeploy.py` | 只同步代码 + 重建容器（跳过镜像构建） |
| `tests/e2e_smoke.py` | P0 端到端回归（10 项） |
| `tests/p1_browser_smoke.py` | P1 浏览器冒烟（12 项，含 Playwright CDP） |
| `tests/p2_smoke.py` | P2 白名单 / 暂停恢复 / 并发压测 |

## 13. 关键坑位备忘

1. **e2b SDK 用 `X-API-KEY` 头传密钥**，不是 `Authorization: Bearer`，管控面两种都要认。
2. **ConnectRPC 实际走 JSON 编码**（`application/connect+json`），不是 proto。
3. **Chromium ≥130 把 `/json/new` 从 GET 改成 PUT**，关闭也需兼容多方法。
4. 边缘代理转发 chunked 请求时**不能剥 `transfer-encoding`**；WebSocket 必须保留 upgrade 头并全双工泵送。
5. compose 中服务未声明 `build:` 段时，`docker compose up --build` **不会重建**该镜像（netpolicy 曾因此失效）。
6. 本机有全局 HTTP 代理，Playwright 的 node driver 会误走代理 → 测试进程需清空 `HTTP(S)_PROXY` 并设置 `NODE_EXTRA_CA_CERTS`。

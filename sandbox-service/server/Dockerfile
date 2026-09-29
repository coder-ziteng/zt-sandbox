FROM m.daocloud.io/docker.io/library/python:3.11-slim

ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple PYTHONUNBUFFERED=1

# iptables is required by the P2 network allowlist feature (runs against the host
# netns; the container is started with network_mode=host + NET_ADMIN).
RUN apt-get update && apt-get install -y --no-install-recommends iptables iproute2 \
    && rm -rf /var/lib/apt/lists/*

# P3 第六刀: prometheus-client 用于 /metrics 端点,暴露沙箱生命周期 /
# HTTP 流量 / 网络策略等指标。
RUN pip install --no-cache-dir fastapi uvicorn docker httpx prometheus-client

WORKDIR /app

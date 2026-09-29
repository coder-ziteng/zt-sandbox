# Shared base image for all sandbox flavours: python runtime + envd services + headless Chromium.
FROM m.daocloud.io/docker.io/library/python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PYTHONUNBUFFERED=1

# Runtime deps + headless Chromium (used by the browser / all-in-one flavours)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates procps tini curl \
        chromium fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 user

COPY envdsvc/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY envdsvc/mini_envd.py envdsvc/jupyter_svc.py envdsvc/browser_svc.py envdsvc/start.sh /app/
COPY envdsvc/proto /app/proto
RUN chmod +x /app/start.sh && mkdir -p /home/user/workspace /workspace \
    && chown -R user:user /home/user /workspace /app

USER user
WORKDIR /home/user/workspace

EXPOSE 49983 49999 3000

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sh", "/app/start.sh"]

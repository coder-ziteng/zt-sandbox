"""只读侦查：Docker 存储驱动现状 + 切换到 vfs/overlayfs 的影响面评估。"""
import paramiko

def run(cli, cmd, timeout=60):
    _, o, e = cli.exec_command(cmd, timeout=timeout)
    return o.read().decode("utf-8", "ignore"), e.read().decode("utf-8", "ignore")


cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST, username=USER, password=PWD, timeout=10)

print("=== docker info (driver / snapshotter / experimental) ===")
print(run(cli, "docker info --format '{{.Driver}} | {{.DriverStatus}}'")[0].strip())
print(run(cli, "docker info 2>/dev/null | grep -iE 'storage driver|experimental|containerd|snapshotter|Cgroup'")[0].strip())

print("\n=== daemon.json ===")
print(run(cli, "cat /etc/docker/daemon.json 2>/dev/null || echo '(none)'")[0].strip())

print("\n=== 现有容器（切驱动必须重建，影响面） ===")
print(run(cli, "docker ps -a --format '{{.Names}}\\t{{.Image}}\\t{{.Status}}'")[0].strip())

print("\n=== 现有镜像/卷数量 ===")
print("images:", run(cli, "docker images -q | wc -l")[0].strip())
print("volumes:", run(cli, "docker volume ls -q | wc -l")[0].strip())

print("\n=== containerd 版本 & 可用 snapshotter ===")
print(run(cli, "ctr version 2>/dev/null | head -5 || echo 'no ctr'")[0].strip())
print(run(cli, "ctr plugins ls 2>/dev/null | grep -i snapshotter || echo 'no plugins ls'")[0].strip())

print("\n=== 磁盘余量（vfs 会翻倍占用） ===")
print(run(cli, "df -h /var/lib/docker | tail -1")[0].strip())
print(run(cli, "du -sh /var/lib/docker 2>/dev/null || echo n/a")[0].strip())

print("\n=== criu 版本 & 内核支持 ===")
print(run(cli, "criu --version 2>/dev/null | head -2 || echo 'criu not installed'")[0].strip())
print(run(cli, "cat /proc/sys/kernel/unprivileged_userns_clone 2>/dev/null; uname -r")[0].strip())

cli.close()

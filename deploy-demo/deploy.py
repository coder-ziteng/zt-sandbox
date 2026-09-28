#!/usr/bin/env python3
"""
法律法规数据库 - 部署脚本
将项目部署到远程服务器 <internal-host>
"""
import sys
import tarfile
import subprocess
import time
from pathlib import Path
import time
from pathlib import Path

try:
    import paramiko
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "paramiko", "-q"])
    import paramiko

# 远程服务器配置
REMOTE_HOST = "<internal-host>"
REMOTE_USER = "root"
REMOTE_PASSWORD = "<redacted>"
REMOTE_PORT = 22

# 项目路径
PROJECT_DIR = Path(__file__).parent
FRONTEND_DIR = PROJECT_DIR / "vue-frontend"
DIST_DIR = FRONTEND_DIR / "dist"
BACKEND_DIR = PROJECT_DIR / "app"
PACKAGE_NAME = "law_database"

# 远程路径
REMOTE_BASE_DIR = "/home/law_database"
REMOTE_TMP_DIR = "/tmp"
TARBALL_NAME = f"{PACKAGE_NAME}.tar.gz"
LOCAL_TARBALL = PROJECT_DIR / TARBALL_NAME


def run_cmd(cmd, cwd=None, timeout=300):
    """执行本地命令"""
    print(f"[LOCAL] {cmd}")
    result = subprocess.run(
        cmd, shell=True, cwd=cwd or PROJECT_DIR,
        capture_output=True, text=True, timeout=timeout
    )
    if result.returncode != 0:
        print(f"[ERROR] {result.stderr}")
        return False
    return True


def build_frontend():
    """构建前端"""
    print("\n" + "="*50)
    print("1. 构建前端...")
    print("="*50)

    if not FRONTEND_DIR.exists():
        print(f"[ERROR] 前端目录不存在: {FRONTEND_DIR}")
        return False

    # 安装依赖并构建
    if not run_cmd("npm install", cwd=FRONTEND_DIR):
        return False

    if not run_cmd("npm run build", cwd=FRONTEND_DIR):
        return False

    if not DIST_DIR.exists():
        print(f"[ERROR] 构建失败，dist目录不存在")
        return False

    print(f"[OK] 前端构建完成: {DIST_DIR}")
    return True


def create_tarball():
    """创建部署包 - 仅包含构建输出和运行时必要文件"""
    print("\n" + "="*50)
    print("2. 创建部署包...")
    print("="*50)

    tarball_path = PROJECT_DIR / TARBALL_NAME
    if tarball_path.exists():
        tarball_path.unlink()

    def filter_func(tarinfo):
        path = tarinfo.name

        # Skip version control and IDE
        if any(x in path for x in ['.git/', '.claude/', '.trae/', '.venv/', 'node_modules/']):
            return None
        if '__pycache__' in path or path.endswith('.pyc'):
            return None
        if path.endswith('.log') or path.endswith('.sqlite') or path == './law_db.sql':
            return None
        if path == './.env':
            return None
        if path.endswith('.md') or path.endswith('.zip') or path.endswith('.tar.gz'):
            return None
        # Exclude development/test scripts
        if any(path.startswith('./' + x) for x in ['test_', 'check_', 'process_', 'regenerate_', 'update_', 'recreate_', 'run_migration', 'run_init', 'run_scraper', 'import_remote', 'init_db']):
            return None
        # Exclude optional directories
        if any(x in path for x in ['/apiDoc/', '/scripts/', '/downloads/', '/docs/', '/laws/']):
            return None
        # For vue-frontend: only include dist (built output), exclude src, public, README, etc.
        if path.startswith('./vue-frontend/src/') or path.startswith('./vue-frontend/public/'):
            return None
        if path.startswith('./vue-frontend/') and path != './vue-frontend/dist' and not path.startswith('./vue-frontend/dist/'):
            return None
        if '.playwright-cli' in path:
            return None
        return tarinfo

    with tarfile.open(tarball_path, "w:gz") as tar:
        tar.add(PROJECT_DIR, arcname=".", filter=filter_func)

    size = tarball_path.stat().st_size / (1024*1024)
    print(f"[OK] 部署包已创建: {tarball_path} ({size:.1f} MB)")
    return True


def deploy_to_server():
    """部署到远程服务器"""
    print("\n" + "="*50)
    print("3. 上传到远程服务器...")
    print("="*50)

    try:
        import paramiko
    except ImportError:
        print("[INFO] 安装paramiko...")
        subprocess.run([sys.executable, "-m", "pip", "install", "paramiko", "-q"])

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    print(f"[CONNECTING] {REMOTE_USER}@{REMOTE_HOST}:{REMOTE_PORT}")
    client.connect(REMOTE_HOST, port=REMOTE_PORT, username=REMOTE_USER, password=REMOTE_PASSWORD, timeout=30)

    def exec_cmd(cmd):
        """在远程服务器执行命令"""
        print(f"[REMOTE] {cmd}")
        stdin, stdout, stderr = client.exec_command(cmd)
        return stdout.read().decode(), stderr.read().decode()

    # 1. 创建远程目录
    print("\n[STEP 1] 创建远程目录...")
    exec_cmd(f"mkdir -p {REMOTE_BASE_DIR}")

    # 2. 上传tarball
    print("\n[STEP 2] 上传文件...")
    sftp = client.open_sftp()
    remote_tarball = f"{REMOTE_TMP_DIR}/{TARBALL_NAME}"
    print(f"[UPLOAD] {LOCAL_TARBALL} -> {remote_tarball}")
    sftp.put(str(LOCAL_TARBALL), remote_tarball)
    sftp.close()

    # 3. 解压到远程目录（先清空目标目录）
    print("\n[STEP 3] 解压文件...")
    exec_cmd(f"rm -rf {REMOTE_BASE_DIR}/*")
    exec_cmd(f"cd {REMOTE_BASE_DIR} && tar -xzvf {remote_tarball} 2>&1 | tail -20")

    # 4. 设置文件权限
    print("\n[STEP 4] 设置文件权限...")
    exec_cmd(f"chmod -R 755 {REMOTE_BASE_DIR}")

    # 5. 安装Python依赖
    print("\n[STEP 5] 安装Python依赖...")
    stdout, stderr = exec_cmd(f"pip3 install -r {REMOTE_BASE_DIR}/requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple 2>&1 | tail -10")

    # 6. 设置SELinux
    print("\n[STEP 6] 设置SELinux...")
    exec_cmd(f"chcon -R -t httpd_sys_content_t {REMOTE_BASE_DIR}")
    exec_cmd("setsebool -P httpd_read_user_content 1")
    exec_cmd("setsebool -P httpd_can_network_connect 1")

    # 7. 配置Nginx
    print("\n[STEP 7] 配置Nginx...")
    nginx_conf = f'''server {{
    listen 80;
    server_name _;

    location / {{
        root {REMOTE_BASE_DIR}/vue-frontend/dist;
        index index.html;
        try_files $uri $uri/ /index.html;
    }}

    location /api {{
        proxy_pass http://127.0.0.1:9000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }}
}}
'''
    exec_cmd(f"cat > /etc/nginx/conf.d/law_database.conf << 'EOF'\n{nginx_conf}EOF")

    # 8. 重启服务
    print("\n[STEP 8] 重启服务...")
    exec_cmd("nginx -t && nginx -s reload")
    exec_cmd(f"cd {REMOTE_BASE_DIR} && pkill -f 'python3 run.py' || true")
    exec_cmd(f"cd {REMOTE_BASE_DIR} && nohup python3 run.py > app.log 2>&1 &")
    time.sleep(2)

    # 9. 验证
    print("\n[STEP 9] 验证部署...")
    stdout, _ = exec_cmd("curl -s http://localhost | head -5")
    if "<!DOCTYPE html>" in stdout:
        print("[OK] 前端访问正常")
    else:
        print(f"[WARN] 前端访问异常: {stdout[:100]}")

    stdout, _ = exec_cmd("curl -s http://localhost:9000/docs | head -3")
    if "swagger" in stdout.lower() or "<!DOCTYPE html>" in stdout:
        print("[OK] 后端API正常")
    else:
        print(f"[WARN] 后端访问异常: {stdout[:100]}")

    client.close()
    print("\n" + "="*50)
    print("部署完成!")
    print(f"访问地址: http://{REMOTE_HOST}")
    print("="*50)
    return True


def cleanup():
    """清理本地临时文件"""
    if LOCAL_TARBALL.exists():
        LOCAL_TARBALL.unlink()
        print(f"[CLEANUP] 删除本地临时文件: {LOCAL_TARBALL}")


def main():
    print("="*50)
    print("法律法规数据库 - 部署脚本")
    print("="*50)
    print(f"目标服务器: {REMOTE_HOST}")
    print(f"部署路径: {REMOTE_BASE_DIR}")
    print("="*50)

    try:
        # 1. 构建前端
        if not build_frontend():
            sys.exit(1)

        # 2. 创建部署包
        if not create_tarball():
            sys.exit(1)

        # 3. 部署到服务器
        if not deploy_to_server():
            sys.exit(1)

        # 4. 清理
        cleanup()

    except KeyboardInterrupt:
        print("\n[ABORT] 用户取消部署")
        cleanup()
        sys.exit(1)
    except Exception as e:
        print(f"\n[ERROR] {e}")
        cleanup()
        sys.exit(1)


if __name__ == "__main__":
    main()

import paramiko

from deploy_server import API_KEYS, PWD, USER, HOST

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST, username=USER, password=PWD, timeout=10)


def run(cmd, timeout=60):
    _, stdout, stderr = cli.exec_command(cmd, timeout=timeout)
    rc = stdout.channel.recv_exit_status()
    print(f"$ {cmd}\n[rc={rc}]\n{stdout.read().decode()}")


run("docker ps -a --format '{{.Names}} {{.Status}}' | grep -E '^sbx-' | awk '{print $1}' | xargs -r docker rm -f")
run(f"curl -s -X POST http://127.0.0.1:8902/v3/templates -H 'Authorization: Bearer {API_KEYS}' "
    f"-H 'Content-Type: application/json' -d '{{\"name\":\"code-interpreter\",\"image\":\"sandbox/code-interpreter:v1\",\"cpuCount\":1,\"memoryMB\":2048}}'")
run(f"curl -s http://127.0.0.1:8902/v2/templates -H 'Authorization: Bearer {API_KEYS}'")
run("docker ps --format '{{.Names}} {{.Status}}' | grep sandbox")
cli.close()

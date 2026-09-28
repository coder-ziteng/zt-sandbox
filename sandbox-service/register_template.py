import os
import paramiko
import socket
import sys

from deploy_server import API_KEYS, DOMAIN, HOST, PWD, USER

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST, username=USER, password=PWD, timeout=10)


def run(cmd, timeout=60):
    _, stdout, stderr = cli.exec_command(cmd, timeout=timeout)
    rc = stdout.channel.recv_exit_status()
    print(f"$ {cmd}\n[rc={rc}]\n{stdout.read().decode()}")
    e = stderr.read().decode()
    if e.strip():
        print("STDERR:", e[-500:])


run(f"curl -s -X POST http://127.0.0.1:8902/v3/templates -H 'Authorization: Bearer {API_KEYS}' "
    f"-H 'Content-Type: application/json' -d '{{\"name\":\"code-interpreter\",\"image\":\"sandbox/code-interpreter:v1\",\"cpuCount\":1,\"memoryMB\":2048}}'")
run(f"curl -s http://127.0.0.1:8902/v2/templates -H 'Authorization: Bearer {API_KEYS}'")

certs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs")
with paramiko.SFTPClient.from_transport(cli.get_transport()) as sftp:
    os.makedirs(certs_dir, exist_ok=True)
    with sftp.file("/srv/sandbox-service/certs/ca.pem", "rb") as rf:
        with open(os.path.join(certs_dir, "ca.pem"), "wb") as wf:
            wf.write(rf.read())
print("ca.pem saved")
cli.close()

ip = socket.gethostbyname(f"49983-sbxtest123.{DOMAIN}")
print("nip.io DNS resolve ->", ip)

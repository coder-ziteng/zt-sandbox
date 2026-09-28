import paramiko, os, sys, socket

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect("<internal-host>", username="root", password="<redacted>", timeout=10)

def run(cmd, timeout=60):
    _, stdout, stderr = cli.exec_command(cmd, timeout=timeout)
    rc = stdout.channel.recv_exit_status()
    print(f"$ {cmd}\n[rc={rc}]\n{stdout.read().decode()}")
    e = stderr.read().decode()
    if e.strip():
        print("STDERR:", e[-500:])

run("curl -s -X POST http://127.0.0.1:8902/v3/templates -H 'Authorization: Bearer <dev-key-redacted>' "
    "-H 'Content-Type: application/json' -d '{\"name\":\"code-interpreter\",\"image\":\"sandbox/code-interpreter:v1\",\"cpuCount\":1,\"memoryMB\":2048}'")
run("curl -s http://127.0.0.1:8902/v2/templates -H 'Authorization: Bearer <dev-key-redacted>'")

with paramiko.SFTPClient.from_transport(cli.get_transport()) as sftp:
    os.makedirs(r"E:\work\zt-Sandbox\sandbox-service\certs", exist_ok=True)
    with sftp.file("/srv/sandbox-service/certs/ca.pem", "rb") as rf:
        with open(r"E:\work\zt-Sandbox\sandbox-service\certs\ca.pem", "wb") as wf:
            wf.write(rf.read())
print("ca.pem saved")
cli.close()

ip = socket.gethostbyname("49983-sbxtest123.<internal-host>.nip.io")
print("nip.io DNS resolve ->", ip)

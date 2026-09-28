import paramiko, os
cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect("<internal-host>", username="root", password="<redacted>", timeout=10)

def run(cmd, timeout=60):
    _, stdout, stderr = cli.exec_command(cmd, timeout=timeout)
    rc = stdout.channel.recv_exit_status()
    print(f"$ {cmd}\n[rc={rc}]\n{stdout.read().decode()}")
    e = stderr.read().decode()
    if e.strip():
        print("STDERR:", e[-600:])

D = "<internal-host>.nip.io"
run(f"cd /srv/sandbox-service && rm -f certs/* && "
    f"printf 'basicConstraints=critical,CA:TRUE\\nkeyUsage=critical,keyCertSign,cRLSign\\nsubjectKeyIdentifier=hash\\n' > certs/ca.ext && "
    f"printf '[req]\\ndistinguished_name=dn\\nx509_extensions=v3_ca\\nprompt=no\\n[dn]\\nCN=sandbox-dev-ca\\n[v3_ca]\\nbasicConstraints=critical,CA:TRUE\\nkeyUsage=critical,keyCertSign,cRLSign\\nsubjectKeyIdentifier=hash\\n' > certs/ca.cnf && "
    f"printf '[req]\\ndistinguished_name=dn\\nreq_extensions=v3_req\\nprompt=no\\n[dn]\\nCN=*.{D}\\n[v3_req]\\nsubjectAltName=DNS:*.{D},DNS:{D}\\n' > certs/leaf.cnf && "
    f"printf 'basicConstraints=CA:FALSE\\nkeyUsage=digitalSignature,keyEncipherment\\nextendedKeyUsage=serverAuth\\nsubjectAltName=DNS:*.{D},DNS:{D}\\n' > certs/leaf.ext && "
    f"openssl req -x509 -newkey rsa:2048 -keyout certs/ca.key -out certs/ca.pem -days 3650 -nodes -config certs/ca.cnf && "
    f"openssl req -newkey rsa:2048 -keyout certs/sandbox.key -out certs/sandbox.csr -nodes -config certs/leaf.cnf && "
    f"openssl x509 -req -in certs/sandbox.csr -CA certs/ca.pem -CAkey certs/ca.key -CAcreateserial -out certs/sandbox.crt -days 3650 -extfile certs/leaf.ext && "
    f"openssl verify -CAfile certs/ca.pem certs/sandbox.crt", timeout=120)

run("docker restart sandbox-edge-proxy")
import time; time.sleep(3)
run("docker ps --format '{{.Names}} {{.Status}}' | grep sandbox")
run("ss -tlnp | grep ':443' || echo no-443")

with paramiko.SFTPClient.from_transport(cli.get_transport()) as sftp:
    with sftp.file("/srv/sandbox-service/certs/ca.pem", "rb") as rf:
        with open(r"E:\work\zt-Sandbox\sandbox-service\certs\ca.pem", "wb") as wf:
            wf.write(rf.read())
print("ca.pem updated")
cli.close()

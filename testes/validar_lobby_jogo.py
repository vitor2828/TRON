"""Teste TCP com servidor e quatro processos. Executar na pasta TRON."""
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUTPUT = ROOT / "resultados" / "integracao_lobby"


def wait_for(predicate, seconds=8):
    limit = time.monotonic() + seconds
    while time.monotonic() < limit:
        value = predicate()
        if value:
            return value
        time.sleep(0.03)
    raise TimeoutError("Condicao de teste nao ocorreu")


def scenario(case):
    folder = OUTPUT / f"{case}-{time.time_ns()}"
    folder.mkdir(parents=True)
    ready = folder / "servidor.json"
    files, processes = [], []
    server = None

    def launch(cmd, log):
        handle = log.open("w", encoding="utf-8")
        files.append(handle)
        proc = subprocess.Popen([sys.executable, *cmd], cwd=ROOT,
                                stdout=handle, stderr=subprocess.STDOUT)
        processes.append(proc)
        return proc

    try:
        server = launch(["-m", "Network.Server.serverV2", "--host", "127.0.0.1", "--port", "0",
                         "--ready-file", str(ready)], folder / "server.log")
        wait_for(ready.exists)
        port = json.loads(ready.read_text(encoding="utf-8"))["port"]
        clients = []
        for i in range(1, 5):
            log = folder / f"j{i}.log"
            cmd = ["-m", "Network.Client.teste_lobby_jogo", "--server-port", str(port),
                   "--name", f"Teste{i}", "--action", "create" if i == 1 else "join",
                   "--automatic", "--no-render", "--report", str(folder / f"j{i}.json"),
                   "--linger-ms", "300", "--max-runtime", "25",
                   "--width", "25", "--height", "20", "--tick-ms", "100"]
            if i != 1:
                cmd += ["--room", "1"]
            if i == 2 and case == "queda":
                cmd += ["--crash-at", "4"]
            if i == 2 and case == "saida":
                cmd += ["--leave-at", "4"]
            if case == "render":
                cmd.remove("--no-render")
            clients.append(launch(cmd, log))
            if i == 1:
                wait_for(lambda: "SALA 1" in log.read_text(encoding="utf-8"))
            time.sleep(0.1)
        if case == "sem_servidor":
            wait_for(lambda: all("LOBBY_PRONTO" in (folder / f"j{i}.log").read_text(encoding="utf-8") for i in range(1, 5)))
            server.terminate()
            server.wait(timeout=3)
        deadline = time.monotonic() + 30
        for proc in clients:
            proc.wait(timeout=max(0.1, deadline - time.monotonic()))
        reports = {}
        for i, proc in enumerate(clients, 1):
            if i == 2 and case == "queda":
                assert proc.returncode == 77
                continue
            assert proc.returncode == 0, (case, i, (folder / f"j{i}.log").read_text(encoding="utf-8"))
            report = json.loads((folder / f"j{i}.json").read_text(encoding="utf-8"))
            assert report["closed"] and report["error"] is None
            assert report["phase"] == ("LEFT" if i == 2 and case == "saida" else "FINISHED")
            assert report["listener_port"] == report["players"][str(report["player_id"])][1]
            reports[i] = report
        survivors = [r for i, r in reports.items() if not (i == 2 and case == "saida")]
        assert len({r["match_id"] for r in reports.values()}) == 1
        assert len({json.dumps(r["players"], sort_keys=True) for r in reports.values()}) == 1
        assert len({r["final_hash"] for r in survivors}) == 1
        assert all(r["epoch"] == (1 if case == "queda" else 0) for r in survivors)
        print(f"PASSOU {case}: IDs/endpoints comuns, listener preservado e estados iguais", flush=True)
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=4)
        for handle in files:
            handle.close()


def menu_contracts():
    import socket
    folder = OUTPUT / f"menu-{time.time_ns()}"
    folder.mkdir(parents=True)
    ready = folder / "server.json"
    connections, readers = [], []
    with (folder / "server.log").open("w", encoding="utf-8") as log:
        server = subprocess.Popen([sys.executable, "-m", "Network.Server.serverV2",
            "--host", "127.0.0.1", "--port", "0", "--ready-file", str(ready)],
            cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        try:
            wait_for(ready.exists)
            port = json.loads(ready.read_text(encoding="utf-8"))["port"]
            def connect(index):
                sock = socket.create_connection(("127.0.0.1", port), timeout=3)
                connections.append(sock)
                reader = sock.makefile("rb");readers.append(reader)
                sock.sendall((json.dumps(dict(nome=f"Cliente{index}", ip="0.0.0.0", porta=40000+index))+"\n").encode())
                return sock, reader
            def send(client, code, room=None):
                client[0].sendall(code.encode() + ((str(room)+"\n").encode() if room is not None else b""))
            def receive(client, status=None):
                while True:
                    raw = client[1].readline()
                    if not raw:raise EOFError("EOF inesperado")
                    reply=json.loads(raw)
                    if status is None or reply["status"]==status:return reply
            for i in range(1,13):
                client=connect(i);send(client,"1")
                assert receive(client)["id_partida"]==str(i)
                send(client,"4");client[1].close();client[0].close()
            owner=connect(13);send(owner,"1");assert receive(owner)["id_partida"]=="13"
            send(owner,"1");assert receive(owner)["status"]=="400"
            other=connect(14);send(other,"3","999999");assert receive(other)["status"]=="402"
            send(other,"2");assert "13" in receive(other)["partidas"]
            send(other,"3","13");assert receive(other)["status"]=="204"
            send(other,"3","13");assert receive(other)["status"]=="404"
            other[1].close();other[0].close()  # EOF sem comando 4.
            def cleaned():
                send(owner,"2");return receive(owner,"201")["partidas"]["13"]["qtd_jogadores"]==1
            wait_for(cleaned)
            print("PASSOU menu preservado: criar/listar/entrar, sala 13, recusas e limpeza EOF",flush=True)
        finally:
            for reader in readers:reader.close()
            for conn in connections:conn.close()
            server.terminate();server.wait(timeout=4)


def legacy():
    folder = OUTPUT / f"legado-{time.time_ns()}"
    folder.mkdir(parents=True)
    processes, files = [], []
    try:
        for i in range(1, 5):
            handle = (folder / f"j{i}.log").open("w", encoding="utf-8");files.append(handle)
            processes.append(subprocess.Popen([sys.executable, "-m", "Network.Client.teste_jogo_p2p",
                str(i), "--players", "4", "--base-port", "31900", "--automatic", "--no-render",
                "--tick-ms", "50", "--width", "12", "--height", "9", "--linger-ms", "300",
                "--report", str(folder / f"j{i}.json")], cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT))
        for proc in processes:
            proc.wait(timeout=15)
            assert proc.returncode == 0
        reports = [json.loads((folder / f"j{i}.json").read_text(encoding="utf-8")) for i in range(1, 5)]
        assert len({r["final_hash"] for r in reports}) == 1
        print("PASSOU legado: teste_jogo_p2p inalterado funciona com manager.start()", flush=True)
    finally:
        for proc in processes:
            if proc.poll() is None:proc.terminate();proc.wait(timeout=4)
        for handle in files:handle.close()


if __name__ == "__main__":
    menu_contracts()
    legacy()
    for name in ("normal", "sem_servidor", "queda", "saida", "render"):
        scenario(name)

import socket
import threading
import json
import argparse
from pathlib import Path

HOST = '0.0.0.0'
PORT = 5000
QUANTIDADE_CLIENTES_SALA = 4

lock = threading.Lock()

clientes = {}

partidas = {}

cliente_counter = 0
partida_counter = 0
_envio_locks = {}

class Partida: 
    id = "0"
    jogadores = []

    def __init__(self, id_partida, jogador):
        self.id = id_partida
        self.jogadores = [jogador]
        self.jogador_counter = 1
        self.iniciada = False

class Cliente :
    conexao = ""
    ip_p2p = ""
    porta_p2p = ""
    nome = ""

    def __init__(self, conexao, nome, ip_p2p, porta_p2p):
        self.conexao = conexao
        self.nome = nome
        self.ip_p2p = ip_p2p
        self.porta_p2p = porta_p2p
        self.partida_id = ""

class ErroProtocolo(Exception):
    def __init__(self, codigo, mensagem=""):
        self.codigo = codigo
        super().__init__(mensagem)

def envia_pacote (pacote, conn):
    mensagem_json = json.dumps(pacote) + "\n"
    with lock:
        envio_lock = _envio_locks.get(conn)
    if envio_lock is None:
        raise OSError("Conexao do lobby encerrada")
    with envio_lock:
        conn.sendall(mensagem_json.encode('utf-8'))

def criar_partida(cliente, id_partida):
    with lock:
        if cliente.partida_id:
            raise ErroProtocolo("400", "Voce ja esta em uma partida")
        nova_partida = Partida(id_partida, cliente)
        partidas[id_partida] = nova_partida
        cliente.partida_id = id_partida
        print(f"Partida criada por: {cliente.nome}", flush=True)


def listar_partida():
    try:
        with lock:
            resumo = {}
            for p_id, partida in partidas.items():
                if partida.iniciada:
                    continue
                nomes = []

                for j in partida.jogadores:
                    nomes.append(j.nome)

                resumo[p_id] = {
                    "qtd_jogadores": len(partida.jogadores),
                    "jogadores": nomes
                }
            return resumo
    except:
        raise ErroProtocolo("401","Erro ao listar partidas")

def entrar_partida(cliente, id_partida):
    with lock:
        global partidas  
        if cliente.partida_id:
            raise ErroProtocolo("404", "Voce ja esta em uma partida")
        if id_partida not in partidas:
            raise ErroProtocolo("402",f"A partida {id_partida} não existe.")
        
        partida = partidas[id_partida]
            
        if partida.iniciada or len(partida.jogadores) >= 4:
            raise ErroProtocolo("403", "A partida já está cheia.")
                
        partida.jogadores.append(cliente)
        cliente.partida_id = id_partida
        iniciou = len(partida.jogadores) == QUANTIDADE_CLIENTES_SALA
        if iniciou:
            partida.iniciada = True
        print(f"{cliente.nome} entrou na partida {id_partida}.", flush=True)
        return list(partida.jogadores), iniciou


def _ler_linha(leitor, limite=4096):
    raw = leitor.readline(limite + 1)
    if not raw:
        raise EOFError("Conexao encerrada")
    if not raw.endswith(b"\n") or len(raw) > limite:
        raise ValueError("Linha incompleta ou muito grande")
    return raw[:-1].decode("utf-8").strip()


def _fechar(conn):
    try:
        conn.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    conn.close()


def _remover_cliente(cliente, id_cliente):
    with lock:
        clientes.pop(id_cliente, None)
        if cliente is not None:
            partida = partidas.get(cliente.partida_id)
            if partida is not None:
                if not partida.iniciada:
                    partida.jogadores = [j for j in partida.jogadores if j is not cliente]
                if not partida.jogadores or (partida.iniciada and
                        not any(d["cliente"] in partida.jogadores for d in clientes.values())):
                    partidas.pop(partida.id, None)

def trocar_pares(jogadores, numeropartida):
    composicao = {
        str(index + 1): {"ip_jogador": j.ip_p2p, "porta_jogador": j.porta_p2p}
        for index, j in enumerate(jogadores)
    }
    for index, jogador in enumerate(jogadores):
            info = {"id_partida": numeropartida, "player_id": index + 1, "jogadores": composicao}
            pacote = {"tipo_mensagem": "1", "status": "203", "info": info}
            try:
                envia_pacote(pacote, jogador.conexao)
            except OSError:
                _fechar(jogador.conexao)
                print(f"Informacoes enviadas para partida {numeropartida}.", flush=True)


def receber_cliente(conn, endereco, id_cliente):
    cliente = None
    leitor = conn.makefile("rb")
    try:
        dados = json.loads(_ler_linha(leitor))
        nome, porta = dados["nome"], dados["porta"]
        if (type(nome) is not str or not nome.strip() or len(nome) > 40 or
                any(ord(c) < 32 or ord(c) == 127 for c in nome) or
                type(porta) is not int or not 1 <= porta <= 65535):
            raise ValueError("Cadastro invalido")
        cliente = Cliente(conn, nome, endereco[0], porta)
        with lock:
            if any((d["cliente"].ip_p2p, d["cliente"].porta_p2p) == (endereco[0], porta)
                   for d in clientes.values()):
                raise ValueError("Endpoint P2P ja cadastrado")
            clientes[id_cliente] = {"informacoes": list(endereco), "cliente": cliente}
        print(f"Cliente {id_cliente} conectado: {endereco}", flush=True)
        numeropartida = ""

        while True:
            msg = leitor.read(1).decode("ascii")
            if not msg:
                return

            match msg:
                case "1":
                    try:
                        global partida_counter
                        with lock:
                            partida_counter += 1
                            nova_id = str(partida_counter)
                        criar_partida(cliente, nova_id)
                        numeropartida = nova_id

                        pacote = {
                            "tipo_mensagem": "0",
                            "status": "200",
                            "id_partida": numeropartida
                        }

                        envia_pacote(pacote, conn)

                    except ErroProtocolo as e:
                        print(e)

                        pacote = {
                            "tipo_mensagem": "0",
                            "status": e.codigo
                        }

                        envia_pacote(pacote, conn)

                case "2":

                    try:
                        pacote = {
                            "tipo_mensagem": "0",
                            "status": "201",
                            "partidas": listar_partida()
                        }

                        envia_pacote(pacote, conn)

                    except ErroProtocolo as e:
                        print(e)

                        pacote = {
                            "tipo_mensagem": "0",
                            "status": e.codigo
                        }

                        envia_pacote(pacote, conn)

                case "3":

                    try:
                        id_solicitada = _ler_linha(leitor, 32)
                        if not id_solicitada.isdecimal():
                            raise ErroProtocolo("402", "ID de partida invalido")
                        jogadores, iniciou = entrar_partida(cliente, id_solicitada)
                        numeropartida = id_solicitada
                        envia_pacote({"tipo_mensagem": "0", "status": "204"}, conn)

                        for jogador in jogadores:

                            if jogador.conexao != cliente.conexao:
                                pacote = {
                                    "tipo_mensagem": "1",
                                    "status": "202",
                                    "msg": f"{cliente.nome} entrou na partida"
                                }

                                try:
                                    envia_pacote(pacote, jogador.conexao)
                                except OSError:
                                    _fechar(jogador.conexao)

                        if iniciou:
                            trocar_pares(jogadores, numeropartida)
                            conn.close()
                            return
                    
                    except ErroProtocolo as e:
                        print(e)

                        pacote = {
                            "tipo_mensagem": "0",
                            "status": e.codigo
                        }

                        envia_pacote(pacote, conn)
                    
                case "4":

                    if len(partidas[numeropartida].jogadores) < 3:
                        pacote = {
                            "tipo_mensagem": "0",
                            "status": "406"
                        }

                        envia_pacote(pacote, conn)
                        continue

                    jogadores = partidas[numeropartida].jogadores
                    trocar_pares(jogadores, numeropartida)
                    conn.close()
                    return
                case "5":
                    print(f"Conexao com cliente encerrada: {conn}", flush=True)
                    return
                case _:
                    raise ValueError("Comando desconhecido")
    except (EOFError, OSError):
        pass
    except (ValueError, KeyError, UnicodeError) as error:
        print(f"Erro de protocolo: {error}", flush=True)
    finally:
        _fechar(conn)
        leitor.close()
        _remover_cliente(cliente, id_cliente)
        with lock:
            _envio_locks.pop(conn, None)


def executar_servidor(host=HOST, port=PORT, ready_file=None):
    global cliente_counter
    servidor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    servidor.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    servidor.bind((host, port))
    servidor.listen()
    servidor.settimeout(0.25)
    threads = []
    if ready_file:
        Path(ready_file).write_text(json.dumps({"port": servidor.getsockname()[1]}), encoding="utf-8")
    print(f"Servidor iniciado em {servidor.getsockname()}", flush=True)
    try:
        while True:
            try:
                conn, endereco = servidor.accept()
            except socket.timeout:
                continue
            with lock:
                _envio_locks[conn] = threading.Lock()
            thread = threading.Thread(target=receber_cliente, args=(conn, endereco, cliente_counter))
            cliente_counter += 1
            thread.start()
            threads = [t for t in threads if t.is_alive()] + [thread]
    except KeyboardInterrupt:
        pass
    finally:
        servidor.close()
        with lock:
            connections = list(_envio_locks)
        for conn in connections:
            _fechar(conn)
        for thread in threads:
            thread.join(timeout=2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Lobby TRON de quatro jogadores")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--ready-file")
    args = parser.parse_args()
    executar_servidor(args.host, args.port, args.ready_file)

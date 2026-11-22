## o foco deste arquivo eh lancar a conexao do cliente ao servidor
## para criar partidas

import socket
import sys, select
import os, subprocess
import json
import threading
import queue

HOST_SERVIDOR = '127.0.0.1' ## criar um processo de descobrimento de servidor na rede local
PORT_SERVIDOR = 5000

fila_menu = queue.Queue()
fila_partida = queue.Queue()
evento_encerrar = threading.Event()
informacoes_partida = {}

class LobbyCancelled(Exception):
    pass


_erro_lobby = None
_entrada_teste = None


def _encerrar(erro=None):
    global _erro_lobby
    if erro is not None:
        _erro_lobby = erro
    evento_encerrar.set()
    fila_menu.put(None)
    fila_partida.put(None)


def _ler_entrada(prompt=""):
    if _entrada_teste is not None:
        return _entrada_teste(prompt, evento_encerrar)
    print(prompt, end="", flush=True)
    texto = ""
    while not evento_encerrar.is_set():
        if os.name == "nt":
            import msvcrt
            if msvcrt.kbhit():
                tecla = msvcrt.getwch()
                if tecla in ("\x00", "\xe0"):
                    if msvcrt.kbhit():
                        msvcrt.getwch()
                elif tecla == "\x03":
                    _encerrar()
                    return None
                elif tecla in ("\r", "\n"):
                    print()
                    return texto.strip()
                elif tecla == "\b":
                    if texto:
                        texto = texto[:-1]
                        print("\b \b", end="", flush=True)
                elif tecla.isprintable():
                    texto += tecla
                    print(tecla, end="", flush=True)
        elif select.select([sys.stdin], [], [], 0.05)[0]:
            linha = sys.stdin.readline()
            if not linha:
                _encerrar()
                return None
            return linha.strip()
        evento_encerrar.wait(0.03)
    return None


def _menu_protegido(cliente):
    try:
        trata_menu(cliente)
    except (OSError, ValueError) as erro:
        if not evento_encerrar.is_set():
            _encerrar(erro)


def limpa_tela():
    subprocess.run('cls' if os.name== 'nt' else 'clear', shell = True)

def printa_menu():
    print("1 - Criar partida\n")
    print("2 - Listar partidas\n")
    print("3 - Entrar em partida\n")
    print("4 - Sair\n")

def recebe_mensagens(cliente):
    cliente.settimeout(0.5)
    buffer = b""
    while not evento_encerrar.is_set():
        try:
            dados_brutos = cliente.recv(2048)
            if not dados_brutos:
                _encerrar(ConnectionError("Servidor encerrou a conexao"))
                return
            buffer += dados_brutos
            while b"\n" in buffer:
                linha, buffer = buffer.split(b"\n", 1)
                if not linha.strip():
                    continue
                if len(linha) > 65536:
                    raise ValueError("Mensagem muito grande")
                dados = json.loads(linha.decode("utf-8"))
                if dados["tipo_mensagem"] == "0":
                    fila_menu.put(dados)
                elif dados["tipo_mensagem"] == "1":
                    fila_partida.put(dados)
                    if dados["status"] == "203":
                        return  # Nao confunde EOF posterior com falha de inicio.
            if len(buffer) > 65536:
                raise ValueError("Mensagem muito grande")
        except socket.timeout:
            continue
        except (OSError, ValueError, KeyError, TypeError) as erro:
            if not evento_encerrar.is_set():
                _encerrar(erro)
            return


def trata_partida():
    
    while not evento_encerrar.is_set():
            msg = fila_partida.get()

            if msg is None:
                break

            if msg["status"] == "202":
                print(msg["msg"])

            if msg["status"] == "203":
                evento_encerrar.set()
                fila_menu.put(None)
                fila_partida.put(None)

                global informacoes_partida
                informacoes_partida = msg["info"]
                break

def trata_menu(cliente):

    em_partida = False
    id_partida = ""

    while not evento_encerrar.is_set():

        comando = _ler_entrada()
        if comando is None:
            break
        if comando:

            match comando:

                case "1":
                    if (em_partida == True):
                        print("Você já está em uma partida.")
                        continue
                    
                    cliente.sendall(comando.encode())

                    msg = fila_menu.get()

                    if msg is None:
                        break

                    if msg["status"] == "200":
                        print("Partida criada com sucesso!")
                        em_partida = True
                        id_partida = msg["id_partida"]
                        print(f"SALA {id_partida}", flush=True)

                    if msg["status"] == "400":
                        print("Houve um erro ao criar a partida... Por favor, tente novamente.")

                    continue

                case "2":
                    cliente.sendall(comando.encode())

                    msg = fila_menu.get()

                    if msg is None:
                        break

                    if msg["status"] == "401":
                        print("Houve um erro ao listar as partidas... Por favor, tente novamente.")
                        continue;

                    if msg["status"] == "201":
                        for p_id, partida in msg["partidas"].items():
                            print(f"ID da partida: {p_id}")
                            print(f"Número de jogadores: {partida['qtd_jogadores']}\n")

                            contador_jogadores = 1

                            for nome in partida["jogadores"]:
                                print(f"{contador_jogadores}- {nome}")
                                contador_jogadores += 1

                case "3":
                    if em_partida == True:
                        print("Usuário já está em uma partida. Saia da atual primeiro.")
                        continue

                    msg = _ler_entrada("Digite o ID da partida que voce quer entrar: ")
                    if msg is None:
                        break
                    cliente.sendall(comando.encode())
                    id_partida = msg
                    cliente.sendall((msg + '\n').encode('utf-8'))

                    msg = fila_menu.get()

                    if msg is None:
                        break

                    if msg["status"] == "204":
                        em_partida = True
                    if msg["status"] == "404":
                        print("Voce ja esta em uma partida")
                    if (msg["status"] == "402"):
                        print("A partida não existe")
                        em_partida = False

                    if (msg["status"] == "403"):
                        print("A partida está cheia")
                        em_partida = False

                case "4":
                    cliente.sendall(b"4")
                    _encerrar()
                    break

                case _:
                    limpa_tela()
                    print("Número escolhido não corresponde a nenhuma opção. Reveja o menu:\n\n")
                    printa_menu()


def lobby(host=HOST_SERVIDOR, port=PORT_SERVIDOR, *, nome=None, entrada=None):
    """Retorno original preservado: [socket_p2p, informacoes_partida].
    """
    global fila_menu, fila_partida, informacoes_partida, _erro_lobby, _entrada_teste
    fila_menu, fila_partida = queue.Queue(), queue.Queue()
    informacoes_partida, _erro_lobby = {}, None
    _entrada_teste = entrada
    evento_encerrar.clear()
    cliente = socket_p2p = None
    t_mensagem = t_menu = None
    try:
        cliente = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        cliente.settimeout(5)
        cliente.connect((host, port))
        socket_p2p = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        socket_p2p.bind(("0.0.0.0", 0))
        socket_p2p.listen()
        print("Conectado ao servidor!", flush=True)
        if nome is None:
            print("Seja bem-vindo! Primeiro, qual seu nome?")
            nome = input("Nome: ")
        pacote = {"nome": nome, "ip": socket_p2p.getsockname()[0],
                  "porta": socket_p2p.getsockname()[1]}
        msg = json.dumps(pacote) + "\n"
        cliente.sendall(msg.encode("utf-8"))
        t_mensagem = threading.Thread(target=recebe_mensagens, args=[cliente], daemon=True)
        t_menu = threading.Thread(target=_menu_protegido, args=[cliente], daemon=True)
        print("MENU:\n", flush=True)
        printa_menu()
        t_mensagem.start()
        t_menu.start()
        trata_partida()
    finally:
        _encerrar()
        if cliente is not None:
            try:
                cliente.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            cliente.close()
        if t_mensagem is not None and t_mensagem.ident is not None:
            t_mensagem.join(timeout=2)
        if t_menu is not None and t_menu.ident is not None:
            t_menu.join(timeout=2)
        alive = any(t is not None and t.is_alive() for t in (t_mensagem, t_menu))
        if (alive or not informacoes_partida) and socket_p2p is not None:
            socket_p2p.close()
        if alive:
            raise RuntimeError("Thread do lobby nao encerrou")
    if _erro_lobby is not None:
        raise ConnectionError(f"Falha no lobby: {_erro_lobby}")
    if not informacoes_partida:
        raise LobbyCancelled("Lobby cancelado")
    return [socket_p2p, informacoes_partida]

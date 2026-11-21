## o foco deste arquivo eh lancar a conexao do cliente ao servidor
## para criar partidas

import socket
import sys, select
import os, subprocess
import json
import threading
import queue

HOST_SERVIDOR = '0.0.0.0' ## criar um processo de descobrimento de servidor na rede local
PORT_SERVIDOR = 5000

fila_menu = queue.Queue()
fila_partida = queue.Queue()
evento_encerrar = threading.Event()
informacoes_partida = {}

def limpa_tela():
    subprocess.run('cls' if os.name== 'nt' else 'clear', shell = True)

def printa_menu():
    print("1 - Criar partida\n")
    print("2 - Listar partidas\n")
    print("3 - Entrar em partida\n")
    print("4 - Sair\n")

def recebe_mensagens(cliente):
    cliente.settimeout(0.5)
    buffer = ""

    while not evento_encerrar.is_set():
        try:
            dados_brutos = cliente.recv(2048).decode('utf-8')
            
            if not dados_brutos:
                print("Servidor encerrou a conexão.")
                break

            buffer += dados_brutos

            while "\n" in buffer:
                linha, buffer = buffer.split("\n", 1)
                linha = linha.strip()
                
                if not linha:
                    continue

                dados = json.loads(linha)

                if dados["tipo_mensagem"] == "0":
                    fila_menu.put(dados)
                elif dados["tipo_mensagem"] == "1":
                    fila_partida.put(dados)

        except socket.timeout:
            continue
        except json.JSONDecodeError as e:
            print(f"[ERRO] JSON inválido recebido: {e} | Conteúdo: {linha}")
        except OSError as e:
            print(f"[ERRO] Falha no socket: {e}")

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

    while not evento_encerrar.is_set():

        comando, _, _ = select.select([sys.stdin], [], [], 0.1)

        if comando:
            comando = str(sys.stdin.readline().strip())

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

                    if msg["status"] == "400":
                        print("Houve um erro ao criar a partida... Por favor, tente novamente.")

                    continue

                case "2":
                    cliente.sendall(comando.encode())

                    msg = fila_menu.get()

                    if msg["status"] == "401":
                        print("Houve um erro ao listar as partidas... Por favor, tente novamente.")
                        continue;

                    if msg["status"] == "201":
                        for p_id, partida in msg["partidas"].items():
                            print(f"ID da partida: {p_id}")
                            print(f"Número de jogadores: {partida["qtd_jogadores"]}\n")

                            contador_jogadores = 1

                            for nome in partida["jogadores"]:
                                print(f"{contador_jogadores}- {nome}")
                                contador_jogadores += 1

                case "3":
                    if em_partida == True:
                        print("Usuário já está em uma partida. Saia da atual primeiro.")
                        continue

                    em_partida = True
                    
                    cliente.sendall(comando.encode())
                    msg = input("Digite o ID da partida que você quer entrar: ")
                    cliente.sendall(msg.encode('utf-8'))

                case "4":
                    cliente.sendall(comando.encode())
                    cliente.close()
                    sys.exit(0)

                case _:
                    limpa_tela()
                    print("Número escolhido não corresponde a nenhuma opção. Reveja o menu:\n\n")
                    printa_menu()


def lobby():

    try:
        cliente = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        cliente.connect((HOST_SERVIDOR, PORT_SERVIDOR))

        socket_p2p = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        socket_p2p.bind(("0.0.0.0", 0))
        socket_p2p.listen()

        print("Conectado ao servidor!")

    except:
        print("Houve uma falha na conexão ao servidor. Tente novamente.")
        socket_p2p.close()
        cliente.close()
        sys.exit(1)


    print("Seja bem-vindo! Primeiro, qual seu nome?")
    nome = input("Nome: ")

    pacote = {
        "nome": nome,
        "ip": socket_p2p.getsockname()[0],
        "porta": socket_p2p.getsockname()[1]
    }

    msg = json.dumps(pacote) + "\n"
    cliente.sendall(msg.encode('utf-8'))
    limpa_tela()

    thread = threading.Thread(
        target=recebe_mensagens,
        args=[cliente],
        daemon=True
    )

    thread.start()

    print("MENU:\n\n")
    printa_menu()

    thread = threading.Thread(
        target = trata_menu,
        args=[cliente],
        daemon=True
    )

    thread.start()

    trata_partida()

    cliente.close()

    return [socket_p2p, informacoes_partida]

print(lobby())


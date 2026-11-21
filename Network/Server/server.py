import socket
import threading
import json

HOST = '127.0.0.1'
PORT = 5000
QUANTIDADE_CLIENTES_SALA = 4

lock = threading.Lock()

clientes = {}

partidas = {}

cliente_counter = 0
partida_counter = 0

class Partida: 
    id = "0"
    jogadores = []

    def __init__(self, id_partida, jogador):
        self.id = id_partida
        self.jogadores = [jogador]

class Cliente :
    conexao = ""
    nome = ""

    def __init__(self, conexao, nome):
        self.conexao = conexao
        self.nome = nome

class ErroProtocolo(Exception):
    def __init__(self, codigo, mensagem=""):
        self.codigo = codigo
        super().__init__(mensagem)

def envia_pacote (pacote, conn):
    mensagem_json = json.dumps(pacote) + "\n"
    conn.sendall(mensagem_json.encode('utf-8'))

def criar_partida(cliente, id_partida):
    try:
        with lock:
            nova_partida = Partida(id_partida, cliente)
            partidas[id_partida] = nova_partida
            print(f"Partida criada por: {cliente.nome}")

    except:
        raise ErroProtocolo("400","Partida não pôde ser criada")


def listar_partida():
    try:
        with lock:
            resumo = {}
            for p_id, partida in partidas.items():
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
    try:
        with lock:
            global partidas  
            if id_partida not in partidas:
                raise ErroProtocolo("402",f"A partida {id_partida} não existe.")
            
            partida = partidas[id_partida]
            
            if len(partida.jogadores) >= 4:
                raise ErroProtocolo("403", "A partida já está cheia.")
                
            partida.jogadores.append(cliente)
            print(f"{cliente.nome} entrou na partida {id_partida}.")

    except:
        raise ErroProtocolo("404","Erro ao entrar na partida")

def receber_cliente(conn, endereco, id_cliente):

    with lock:
        msg = conn.recv(50).decode()

        cliente = Cliente(conn, msg)

        clientes[id_cliente] = {
            "informacoes": [endereco[0], endereco[1]],
            "cliente": cliente
        }

        print(f"Cliente {id_cliente} conectado: {clientes[id_cliente]['informacoes']}")

    while True:
        msg = conn.recv(1).decode()

        match msg:
            case "1":
                try:
                    global partida_counter
                    partida_counter += 1
                    criar_partida(cliente, str(partida_counter))

                    pacote = {
                        "tipo_mensagem": "0",
                        "status": "200"
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

                numeropartida = conn.recv(1).decode()
                entrar_partida(cliente, numeropartida)

                for jogador in partidas[numeropartida].jogadores:
                    pacote = {
                        "tipo_mensagem": "1",
                        "status": "202",
                        "msg": f"{cliente.nome} entrou na partida"
                    }

                    envia_pacote(pacote, jogador.conexao)

                if len(partidas[numeropartida].jogadores) == QUANTIDADE_CLIENTES_SALA:
                    for jogador in partidas[numeropartida].jogadores:

                        info = {}

                        conexao_counter = 0

                        for outros_jogadores in partidas[numeropartida].jogadores:
                            if outros_jogadores.conexao != jogador.conexao:
                                info[str(conexao_counter)] = {
                                    "ip_jogador": outros_jogadores.conexao.getpeername()[0],
                                    "porta_jogador": outros_jogadores.conexao.getpeername()[1],
                                }
                                conexao_counter += 1

                        pacote = {
                            "tipo_mensagem": "1",
                            "status": "203",
                            "info": info
                        }

                        envia_pacote(pacote, jogador.conexao)    
                        print(f"Informações enviadas para partida {numeropartida}. Conexões encerradas.")

                    conn.close()
                    return

            case "4":
                print(f"Conexão com cliente encerrada: f{conn}")
                conn.close()
                return


servidor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
servidor.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
servidor.bind((HOST, PORT))
servidor.listen()

print("Servidor iniciado. Esperando jogadores...")

threads = []

while True:
    try:
        conn, endereco = servidor.accept()

        thread = threading.Thread(
            target=receber_cliente,
            args=(conn, endereco, cliente_counter)
        )
        cliente_counter += 1

        thread.start()
        threads.append(thread)

        print(threading.enumerate())

    except KeyboardInterrupt:
        print("\nFechando servidor...")
        servidor.close()
import socket
import threading
import time

HOST = '0.0.0.0'
PORT = 5000
QUANTIDADE_CLIENTES_SALA = 4

lock = threading.Lock()



clientes = {}

partidas = {}

class Partida: 
    id = 0
    jogadores = []

    def __init__(self, id, jogador):
        self.id = id
        self.jogadores = [jogador]

class Cliente :
    conexao = ""
    nome = ""

    def __init__(self, conexao, nome):
        self.conexao = conexao
        self.nome = nome

def criar_partida(cliente, id):
    try:
        with lock:
            nova_partida = Partida(id, cliente)
            partidas[id] = nova_partida
    except:
        mensagem = f"ERRO AO CRIAR PARTIDA"
        conn.sendall(mensagem.encode('utf-8'))


def listar_partida():
    try:
        with lock:
            resumo = {}
            for p_id, partida in partidas.items():
                resumo[p_id] = {
                    "qtd_jogadores": len(partida.jogadores),
                    "jogadores": [j.nome for j in partida.jogadores]
                }
            return resumo
    except:
        mensagem = f"ERRO AO LISTAR PARTIDA"
        conn.sendall(mensagem.encode('utf-8'))

def entrar_partida(cliente, id_partida):
    try:
        with lock:  
            if id_partida not in partidas:
                mensagem = f"ERRO: A PARTIDA NÃO EXISTE"
                raise Exception(f"A partida {id_partida} não existe.")
            
            partida = partidas[id_partida]
            
            if len(partida.jogadores) >= 4:
                mensagem = f"ERRO: A PARTIDA ESTÁ CHEIA"
                raise Exception("A partida já está cheia.")
                
            partida.jogadores.append(cliente)
            print(f"{cliente.nome} entrou na partida {id_partida}.")
            mensagem = f"SUCESSO"
            conn.sendall(mensagem.encode('utf-8'))
            return True
    except Exception as e:
        conn.sendall(mensagem.encode('utf-8'))
        return False

def receber_cliente(conn, endereco, id_cliente):

    with lock:
        clientes[id_cliente] = {
            "informacoes": [endereco[0], endereco[1]],
            "conexao": conn
        }


        print(f"Cliente {id_cliente} conectado: {clientes[id_cliente]['informacoes']}")

    intervalo = 5.0
    ultimo_envio = time.time()

    while True:
        msg = servidor.recv(1).decode()
        match msg:
            case 1:
                partida_counter +=1
                criar_partida(conn, partida_counter)
            case 2:
                mensagem_dict = listar_partida()
                mensagem_bytes = str(mensagem_dict).encode('utf-8')
                conn.sendall(mensagem_bytes)
            case 3:
                numeropartida = servidor.recv(1).decode()
                entrar_partida(conn, numeropartida)
                if len(partidas[numeropartida].jogadores) == QUANTIDADE_CLIENTES_SALA:
                    for jogador in partidas[numeropartida].jogadores:

                        info = {}

                        for outro_id, outro_dados in partidas[numeropartida].jogadores:

                            if outro_id != id_cliente:
                                info[outro_id] = outro_dados["informacoes"]

                        dados["conexao"].sendall(str(info).encode())
                        
                        dados["conexao"].close()
                        
                        print(f"Informações enviadas para partida {numeropartida}. Conexões encerradas.")              


#   while len(clientes) < QUANTIDADE_CLIENTES_SALA:
#      agora = time.time()
#        if agora - ultimo_envio >= intervalo:
#            conn.sendall("0".encode())
#            ultimo_envio = agora

#    conn.sendall("1".encode())

servidor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
servidor.bind((HOST, PORT))
servidor.listen()

cliente_counter = 0
partida_counter = 0

print("Servidor iniciado. Esperando jogadores...")

threads = []

while True:
    try:
        servidor.settimeout(10.0)
        conn, endereco = servidor.accept()

        thread = threading.Thread(
            target=receber_cliente,
            args=(conn, endereco, cliente_counter)
        )
        cliente_counter += 1

        thread.start()
        threads.append(thread)

    except socket.timeout:
        print("Esperando outros jogadores...")
    except KeyboardInterrupt:
        print("\n[AVISO] Servidor interrompido pelo usuário com Ctrl+C!")
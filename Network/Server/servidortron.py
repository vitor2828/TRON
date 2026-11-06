import socket
import threading
import time

HOST = '0.0.0.0'
PORT = 5000
QUANTIDADE_CLIENTES = 4

clientes = {}
lock = threading.Lock()

def receber_cliente(conn, endereco, id_cliente):

    with lock:
        clientes[id_cliente] = {
            "informacoes": [endereco[0], endereco[1]],
            "conexao": conn
        }

        print(f"Cliente {id_cliente} conectado: {clientes[id_cliente]['informacoes']}")

    intervalo = 5.0
    ultimo_envio = time.time()

    while len(clientes) < QUANTIDADE_CLIENTES:
        agora = time.time()
        if agora - ultimo_envio >= intervalo:
            conn.sendall("0".encode())
            ultimo_envio = agora

    conn.sendall("1".encode())

servidor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
servidor.bind((HOST, PORT))
servidor.listen()

cliente_counter = 0

print("Servidor iniciado. Esperando jogadores...")

threads = []

while len(clientes) < QUANTIDADE_CLIENTES:
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

print("\nQuatro jogadores conectados!")

# Envia as informações dos outros 3 clientes
for id_cliente, dados in clientes.items():

    mensagem = {}

    for outro_id, outro_dados in clientes.items():
        if outro_id != id_cliente:
            mensagem[outro_id] = outro_dados["informacoes"]

    dados["conexao"].sendall(str(mensagem).encode())

    dados["conexao"].close()

print("Informações enviadas. Conexões encerradas.")

servidor.close()

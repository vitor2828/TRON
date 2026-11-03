
import socket
import threading

HOST = '0.0.0.0'
PORT = 5000
QUANTIDADE_CLIENTES = 4

clientes = {}
lock = threading.Lock()

def receber_cliente(conn, endereco):
    id_cliente = conn.recv(1024).decode().strip()

    with lock:
        clientes[id_cliente] = {
            "informacoes": [endereco[0], endereco[1]],
            "conexao": conn
        }

        print(f"Cliente {id_cliente} conectado: {clientes[id_cliente]['informacoes']}")


servidor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
servidor.bind((HOST, PORT))
servidor.listen(QUANTIDADE_CLIENTES)

print("Servidor iniciado. Esperando jogadores...")

threads = []

while len(clientes) < QUANTIDADE_CLIENTES:
    try:
        servidor.settimeout(10)
        conn, endereco = servidor.accept()

        thread = threading.Thread(
            target=receber_cliente,
            args=(conn, endereco)
        )

        thread.start()
        threads.append(thread)

    except socket.timeout:
        print("Esperando outros jogadores")

    for thread in threads:
        thread.join(timeout=0.1)

# Aguarda todos os registros
for thread in threads:
    thread.join()

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

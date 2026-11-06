## o foco deste arquivo eh lancar a conexao do cliente ao servidor
## para criar partidas

import socket
import sys

HOST_SERVIDOR = '0.0.0.0' ## criar um processo de descobrimento de servidor na rede local
PORT_SERVIDOR = 5000

try:
    cliente = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    cliente.connect((HOST_SERVIDOR, PORT_SERVIDOR))
    print("Conectado ao servidor!")

except:
    print("Houve uma falha na conexão ao servidor. Tente novamente.")
    cliente.close()
    sys.exit(1)

msg = "0";

while msg != "1":
    msg = cliente.recv(1).decode()
    if (msg == "0"):
        print("Esperando outros jogadores para iniciar a partida...")

print("Iniciando partida!") # delegar para as funcoes de conexao peer to peer
cliente.close()
import socket
import threading
import time
from queue import Queue

from .peer import Peer


class P2PManager:

    def __init__(self, player_id, players):
        self.player_id = player_id
        self.players = players

        self.peers = {}
        self.threads = []
        self.accept_thread = None

        self.peers_lock = threading.Lock()
        self.messages = Queue()

        self.running = False
        self.server_socket = None

    def start(self):
        self.running = True

        ip, port = self.players[self.player_id]

        self.server_socket = socket.socket(
            socket.AF_INET,
            socket.SOCK_STREAM
        )

        self.server_socket.setsockopt(
            socket.SOL_SOCKET,
            socket.SO_REUSEADDR,
            1
        )

        self.server_socket.bind((ip, port))
        self.server_socket.listen()
        self.server_socket.settimeout(0.5)

        print(f"Jogador {self.player_id} escutando em {ip}:{port}")

        self.accept_thread = threading.Thread(
            target=self.accept_connections
        )

        self.accept_thread.start()


    def accept_connections(self):

        while self.running:
            try:
                conn, addr = self.server_socket.accept()

            except socket.timeout:
                continue

            except OSError:
                break

            peer = Peer(None, conn)

            try:
                conn.settimeout(5)

                message = peer.receive()

                if message is None or message.get("type") != "HELLO":
                    raise ValueError("HELLO inválido")

                remote_id = message.get("player_id")

                if (
                    type(remote_id) is not int
                    or remote_id not in self.players
                    or remote_id >= self.player_id
                ):
                    
                    raise ValueError("ID de jogador inválido")

                peer.player_id = remote_id

                conn.settimeout(None)

                self.register_peer(peer)

                print(f"Jogador {remote_id} conectado: {addr}")

            except (OSError, ValueError, ConnectionError):
                peer.close()

    

    def connect_to_peers(self):

        for remote_id, address in self.players.items():

            if remote_id <= self.player_id:
                continue

            ip, port = address

            while self.running:

                sock = socket.socket(
                    socket.AF_INET,
                    socket.SOCK_STREAM
                )

                try:
                    sock.settimeout(2)
                    sock.connect((ip, port))

                    peer = Peer(remote_id, sock)

                    peer.send({
                        "type": "HELLO",
                        "player_id": self.player_id
                    })

                    sock.settimeout(None)

                    if self.register_peer(peer):
                        print(f"Conectado ao jogador {remote_id}")

                    break

                except (OSError, ConnectionError):
                    sock.close()
                    time.sleep(1)

    def register_peer(self, peer):

        with self.peers_lock:

            if not self.running or peer.player_id in self.peers:
                peer.close()
                return False

            self.peers[peer.player_id] = peer

            receive_thread = threading.Thread(
                target=self.receive_loop,
                args=(peer,)
            )

            self.threads.append(receive_thread)
            receive_thread.start()

        return True

    def receive_loop(self, peer):

        try:
            while self.running:
                message = peer.receive()

                if message is None:
                    break

                self.messages.put((
                    peer.player_id,
                    message
                ))

        except OSError as error:
            if self.running:
                print(
                    f"Erro com jogador {peer.player_id}: {error}"
                )

        except ValueError as error:
            print(
                f"Mensagem inválida do jogador {peer.player_id}: {error}"
            )

        finally:
            with self.peers_lock:
                if self.peers.get(peer.player_id) is peer:
                    del self.peers[peer.player_id]

            peer.close()

            # Só notifica a perda de um peer se nós ainda estamos no jogo
            if self.running:
                self.messages.put((
                    peer.player_id,
                    {"type": "DISCONNECTED"}
                ))

    def send_to(self, player_id, message):

        with self.peers_lock:
            peer = self.peers.get(player_id)

        if peer is None:
            print(f"Jogador {player_id} não conectado")
            return

        try:
            peer.send(message)

        except (OSError, ConnectionError) as error:
            print(f"Erro ao enviar para {player_id}: {error}")

    def broadcast(self, message):

        with self.peers_lock:
            player_ids = list(self.peers.keys())

        for player_id in player_ids:
            self.send_to(player_id, message)

    def connected_players(self):

        with self.peers_lock:
            return sorted(self.peers.keys())
        
    def close(self):

        # 1. Impede novas tentativas e novos registros.
        with self.peers_lock:
            self.running = False

        # 2. Interrompe a espera por novas conexões.
        if self.server_socket is not None:
            self.server_socket.close()

        # 3. Aguarda a thread de aceitação terminar.
        if self.accept_thread is not None:
            self.accept_thread.join()

        # 4. Obtém os peers e as leitoras já criadas.
        with self.peers_lock:
            peers = list(self.peers.values())
            threads = list(self.threads)
            self.peers.clear()

        # 5. Desbloqueia as leitoras presas em recv().
        for peer in peers:
            peer.close()

        # 6. Aguarda as leitoras terminarem.
        for thread in threads:
            thread.join()
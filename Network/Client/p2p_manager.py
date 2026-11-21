from collections import deque
from dataclasses import dataclass, field
from queue import Queue
import socket
import threading
import time

from .peer import Peer
from . import protocolo as protocol


# 1. Estado privado de um canal: fila FIFO e prazo do envio em andamento.
@dataclass
class _Channel:
    peer: Peer
    outgoing: deque = field(default_factory=deque)
    sending_deadline: float | None = None
    closed: bool = False


class P2PManager:
    def __init__(self, player_id, players, match_id, *, connect_window=15.0,
                 connect_timeout=2.0, handshake_timeout=5.0, send_timeout=2.0,
                 retry_interval=0.25, monitor_interval=0.05, queue_limit=64):
        # 2. Configuracao e estruturas compartilhadas.
        if type(players) is not dict or not 3 <= len(players) <= 4:
            raise ValueError("players deve mapear 3 ou 4 jogadores")
        if any(type(i) is not int or i < 0 for i in players):
            raise ValueError("IDs devem ser inteiros nao negativos")
        if type(player_id) is not int or player_id not in players:
            raise ValueError("ID local ausente de players")
        for address in players.values():
            if (type(address) not in (tuple, list) or len(address) != 2
                    or type(address[0]) is not str or not address[0]
                    or type(address[1]) is not int or not 1 <= address[1] <= 65535):
                raise ValueError("Endereco exige IP IPv4 numerico e porta 1..65535")
            # Nao fazer DNS em uma thread cujo prazo de parada precisa ser limitado.
            socket.inet_pton(socket.AF_INET, address[0])
        protocol.make_hello(match_id, player_id)
        for duration in (connect_window, connect_timeout, handshake_timeout,
                         send_timeout, retry_interval, monitor_interval):
            if type(duration) not in (int, float) or not 0 < duration < float("inf"):
                raise ValueError("Prazos devem ser numeros positivos finitos")
        if type(queue_limit) is not int or queue_limit <= 0:
            raise ValueError("queue_limit deve ser inteiro positivo")
        self.player_id = player_id
        self.players = {i: tuple(address) for i, address in players.items()}
        self.match_id = match_id
        self.connect_window = connect_window
        self.connect_timeout = connect_timeout
        self.handshake_timeout = handshake_timeout
        self.send_timeout = send_timeout
        self.retry_interval = retry_interval
        self.monitor_interval = monitor_interval
        self.queue_limit = queue_limit
        self.messages = Queue()  # Um unico consumidor: o loop da GameSession.
        self.running = False
        self.server_socket = None
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._channels = {}
        self._pending = {}  # Socket de connect/HELLO -> prazo absoluto.
        self._threads = []
        self._errors = {}
        self._started = False
        self._connectors_started = False

    # 3. Toda thread criada pelo manager e registrada antes de comecar.
    def _spawn_locked(self, name, target, args=()):
        if not self.running:
            return
        thread = threading.Thread(name=f"p2p-{self.player_id}-{name}",
                                  target=target, args=args, daemon=False)
        self._threads.append(thread)
        try:
            thread.start()
        except Exception:
            self._threads.remove(thread)
            raise

    def start(self, *, connect=True):
        """Abre escuta e, por padrao, inicia conectores. Instancia de uso unico."""
        with self._condition:
            if self._started:
                raise RuntimeError("Use uma nova instancia para iniciar outra vez")
            self._started = True
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind(self.players[self.player_id])
                listener.listen()
                listener.settimeout(0.2)
            except Exception:
                listener.close()
                raise
            self.server_socket = listener
            self.running = True
            self._spawn_locked("accept", self.accept_connections)
            self._spawn_locked("monitor", self._monitor)
        if connect:
            self.connect_to_peers()

    def connect_to_peers(self):
        """Inicia um conector por ID maior; chamadas repetidas nao duplicam workers."""
        with self._condition:
            if not self.running:
                raise RuntimeError("Chame start antes de conectar")
            if self._connectors_started:
                return
            self._connectors_started = True
            for remote_id in sorted(self.players):
                if remote_id > self.player_id:
                    self._spawn_locked(f"connect-{remote_id}", self._connect_one,
                                       (remote_id,))

    # 4. Sockets ainda nao identificados tambem sao rastreados e fechados na parada.
    def _track_pending(self, sock, deadline):
        with self._condition:
            if not self.running:
                return False
            self._pending[sock] = deadline
            return True

    @staticmethod
    def _close_socket(sock):
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()

    def _release_pending(self, sock):
        with self._condition:
            self._pending.pop(sock, None)
        self._close_socket(sock)

    def accept_connections(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.server_socket.accept()
            except socket.timeout:
                continue
            except OSError as error:
                if self.running:
                    with self._lock:
                        self._errors["listener"] = str(error)
                return
            with self._condition:
                if not self.running:
                    conn.close()
                    return
                # Limita sockets/workers em negociacao, mesmo antes do HELLO.
                if len(self._pending) >= 2 * len(self.players):
                    conn.close()
                    continue
                self._pending[conn] = time.monotonic() + self.handshake_timeout
                self._spawn_locked("hello", self._accept_hello, (conn,))

    def _accept_hello(self, conn):
        peer = Peer(None, conn)
        registered = False
        try:
            conn.settimeout(self.handshake_timeout)
            message = peer.receive()
            protocol.validate_origin(message, self.match_id, self.players)
            remote_id = message["sender_id"]
            if remote_id >= self.player_id:
                raise protocol.ProtocolError("Apenas ID menor pode iniciar o canal")
            peer.player_id = remote_id
            conn.settimeout(None)
            registered = self._register_peer(peer)
        except (OSError, ValueError) as error:
            with self._lock:
                self._errors["handshake"] = str(error)
        finally:
            if not registered:
                self._release_pending(conn)

    def _connect_one(self, remote_id):
        deadline = time.monotonic() + self.connect_window
        last_error = "Prazo de conexao esgotado"
        while not self._stop.is_set() and time.monotonic() < deadline:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            registered = False
            try:
                remaining = deadline - time.monotonic()
                if not self._track_pending(sock, min(
                        deadline, time.monotonic() + self.connect_timeout
                        + self.handshake_timeout)):
                    sock.close()
                    return
                sock.settimeout(min(self.connect_timeout, max(0.001, remaining)))
                sock.connect(self.players[remote_id])
                remaining = max(0.001, deadline - time.monotonic())
                sock.settimeout(min(self.handshake_timeout, remaining))
                peer = Peer(remote_id, sock)
                peer.send(protocol.make_hello(self.match_id, self.player_id))
                sock.settimeout(None)
                registered = self._register_peer(peer)
                # Recusa por duplicata/parada nao exige insistir no mesmo canal.
                return
            except (OSError, ValueError) as error:
                last_error = str(error)
            finally:
                if not registered:
                    self._release_pending(sock)
            self._stop.wait(self.retry_interval)
        with self._lock:
            active = self.running
            if active:
                self._errors[remote_id] = last_error
        if active:
            self.messages.put((remote_id, {"type": "DISCONNECTED"}))

    # 5. Publicacao do canal e inicio de um leitor e um escritor exclusivos.
    def _register_peer(self, peer):
        with self._condition:
            deadline = self._pending.pop(peer.sock, None)
            if (not self.running or peer.player_id in self._channels
                    or deadline is None or time.monotonic() >= deadline):
                return False
            channel = _Channel(peer)
            self._channels[peer.player_id] = channel
            self._spawn_locked(f"receive-{peer.player_id}", self._receive_loop,
                               (channel,))
            self._spawn_locked(f"send-{peer.player_id}", self._send_loop, (channel,))
            self._condition.notify_all()
            return True

    def _receive_loop(self, channel):
        peer = channel.peer
        reason = "Fim do fluxo remoto"
        try:
            while not self._stop.is_set():
                message = peer.receive()
                if message is None:
                    break
                protocol.validate_origin(message, self.match_id, self.players,
                                         channel_sender_id=peer.player_id)
                self.messages.put((peer.player_id, message))
        except (OSError, ValueError) as error:
            reason = str(error)
        finally:
            self._drop(channel, reason)

    def _drop(self, channel, reason):
        """Um unico caminho remove o canal e notifica uma vez, se ainda ativo."""
        with self._condition:
            remote_id = channel.peer.player_id
            if self._channels.get(remote_id) is not channel:
                return
            del self._channels[remote_id]
            channel.closed = True
            channel.outgoing.clear()
            self._errors[remote_id] = reason
            notify = self.running
            self._condition.notify_all()
        channel.peer.close()  # Desbloqueia tanto leitura quanto envio.
        if notify:
            self.messages.put((remote_id, {"type": "DISCONNECTED"}))

    # 6. API de envio: True significa ACEITO NA FILA, nao recebido pelo outro peer.
    def send_to(self, player_id, message):
        if type(player_id) is not int or player_id == self.player_id or player_id not in self.players:
            raise ValueError("Destino deve ser um participante remoto")
        protocol.validate_origin(message, self.match_id, self.players,
                                 channel_sender_id=self.player_id)
        # Verifica tamanho e cria uma copia que o chamador nao pode alterar.
        copied = protocol.decode_message(protocol.encode_message(message)[:-1])
        with self._condition:
            channel = self._channels.get(player_id)
            if not self.running or channel is None:
                return False
            pending_count = len(channel.outgoing) + (channel.sending_deadline is not None)
            if pending_count < self.queue_limit:
                channel.outgoing.append((copied, time.monotonic() + self.send_timeout))
                self._condition.notify_all()
                return True
        self._drop(channel, "Fila de envio cheia")
        return False

    def broadcast(self, message):
        """ID remoto -> aceito na fila. Inclui False para participantes ausentes."""
        return {i: self.send_to(i, message) for i in sorted(self.players)
                if i != self.player_id}

    def _send_loop(self, channel):
        while not self._stop.is_set():
            with self._condition:
                while not channel.closed and self.running and not channel.outgoing:
                    self._condition.wait()
                if channel.closed or not self.running:
                    return
                message, deadline = channel.outgoing.popleft()
                channel.sending_deadline = deadline
            try:
                if time.monotonic() >= deadline:
                    self._drop(channel, "Prazo de envio excedido na fila")
                    return
                channel.peer.send(message)
            except (OSError, ValueError) as error:
                self._drop(channel, str(error))
                return
            finally:
                with self._condition:
                    channel.sending_deadline = None
                    self._condition.notify_all()

    # 7. Monitor: prazo TOTAL do handshake e do envio, sem alterar recv().
    def _monitor(self):
        while not self._stop.wait(self.monitor_interval):
            now = time.monotonic()
            with self._condition:
                pending = [sock for sock, deadline in self._pending.items()
                           if now >= deadline]
                for sock in pending:
                    self._pending.pop(sock, None)
                expired = []
                for channel in self._channels.values():
                    deadline = channel.sending_deadline
                    if deadline is None and channel.outgoing:
                        deadline = channel.outgoing[0][1]
                    if deadline is not None and now >= deadline:
                        expired.append(channel)
            for sock in pending:
                self._close_socket(sock)
            for channel in expired:
                self._drop(channel, "Prazo total de envio excedido")

    def connected_players(self):
        with self._lock:
            return sorted(self._channels)

    def errors(self):
        """Fotografia dos ultimos erros locais, apenas para diagnostico."""
        with self._lock:
            return self._errors.copy()

    def flush(self, timeout=2.0):
        """Aguarda envios locais, inclusive aos sobreviventes se um canal caiu.

        Retorna False se faltar participante, ocorrer queda ou expirar o prazo.
        Nao confirma recebimento remoto e nao impede novos enfileiramentos.
        """
        if type(timeout) not in (int, float) or not 0 <= timeout < float("inf"):
            raise ValueError("timeout deve ser numero finito nao negativo")
        deadline = time.monotonic() + timeout
        expected = set(self.players) - {self.player_id}
        with self._condition:
            failed = False
            while True:
                if not self.running:
                    return False
                if set(self._channels) != expected:
                    failed = True
                if all(not ch.outgoing and ch.sending_deadline is None
                       for ch in self._channels.values()):
                    return not failed
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)

    # 8. Parada: sinalizar, fechar TODOS os sockets e so entao aguardar workers.
    def close(self, timeout=5.0):
        """Chamar pela thread principal; flush deve ocorrer antes, se desejado."""
        if type(timeout) not in (int, float) or not 0 <= timeout < float("inf"):
            raise ValueError("timeout deve ser numero finito nao negativo")
        with self._condition:
            if threading.current_thread() in self._threads:
                raise RuntimeError("Nao chame close de um worker do manager")
            self.running = False
            self._stop.set()
            channels = list(self._channels.values())
            self._channels.clear()
            pending = list(self._pending)
            self._pending.clear()
            for channel in channels:
                channel.closed = True
                channel.outgoing.clear()
            threads = list(self._threads)
            self._condition.notify_all()
        if self.server_socket is not None:
            self._close_socket(self.server_socket)
        for sock in pending:
            self._close_socket(sock)
        for channel in channels:
            channel.peer.close()
        deadline = time.monotonic() + timeout
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        alive = [thread.name for thread in threads if thread.is_alive()]
        if alive:
            raise RuntimeError(f"Threads nao encerraram no prazo: {alive}")

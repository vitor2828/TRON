import socket
import threading

from .protocolo import (
    DELIMITER,
    MAX_MESSAGE_SIZE,
    encode_message,
    decode_message
)


class Peer:

    def __init__(self, player_id, sock):
        self.player_id = player_id
        self.sock = sock
        self.connected = True

        self.buffer = bytearray()

        self.send_lock = threading.Lock()

    def send(self, message):
        if not self.connected:
            raise ConnectionError("Peer desconectado")

        data = encode_message(message)

        with self.send_lock:
            try:
                self.sock.sendall(data)

            except OSError:
                self.close()
                raise

    def receive(self):
        if not self.connected:
            return None

        while True:
            pos = self.buffer.find(DELIMITER)

            if pos != -1:
                data = bytes(self.buffer[:pos])

                del self.buffer[:pos + len(DELIMITER)]

                if len(data) > MAX_MESSAGE_SIZE:
                    raise ValueError("Mensagem muito grande")

                return decode_message(data)

            if len(self.buffer) > MAX_MESSAGE_SIZE:
                raise ValueError("Mensagem muito grande")

            try:
                data = self.sock.recv(1024)

            except OSError:
                self.close()
                raise

            if not data:
                self.close()
                return None

            self.buffer.extend(data)

    def close(self):
        if not self.connected:
            return

        self.connected = False

        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

        self.sock.close()
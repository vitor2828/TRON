"""Contratos sem sockets reais, preservando as funcoes atuais do lobby."""
from copy import deepcopy
import io
import json
import queue
import threading
import unittest
from unittest.mock import patch

from Network.Client import lobbyWIn as client
from Network.Client import lobby_adapter as adapter
from Network.Server import serverV2 as server


class FakeSocket:
    def __init__(self, chunks=()):
        self.chunks = list(chunks)
        self.sent = []

    def settimeout(self, _):
        pass

    def recv(self, _):
        return self.chunks.pop(0) if self.chunks else b""

    def sendall(self, value):
        self.sent.append(value)


class FakeListener:
    def __init__(self):
        self.closed = False

    def getsockname(self):
        return "0.0.0.0", 6002

    def close(self):
        self.closed = True


class TestIntegracaoMinima(unittest.TestCase):
    def setUp(self):
        client.evento_encerrar.clear()
        client.fila_menu, client.fila_partida = queue.Queue(), queue.Queue()
        client._erro_lobby = None
        client.informacoes_partida = {}

    def test_json_utf8_fragmentado_e_dois_quadros(self):
        a = json.dumps(dict(tipo_mensagem="0", status="201", texto="João"), ensure_ascii=False).encode()+b"\n"
        b = json.dumps(dict(tipo_mensagem="1", status="203", info={"teste": 1})).encode()+b"\n"
        split = a.index("ã".encode())+1
        client.recebe_mensagens(FakeSocket([a[:split], a[split:]+b]))
        self.assertEqual(client.fila_menu.get_nowait()["texto"], "João")
        client.trata_partida()
        self.assertEqual(client.informacoes_partida, {"teste": 1})
        self.assertTrue(client.evento_encerrar.is_set())
        self.assertIsNone(client._erro_lobby)

    def test_eof_desbloqueia_as_duas_filas(self):
        client.recebe_mensagens(FakeSocket())
        self.assertTrue(client.evento_encerrar.is_set())
        self.assertIsNone(client.fila_menu.get_nowait())
        self.assertIsNone(client.fila_partida.get_nowait())
        self.assertIsInstance(client._erro_lobby, ConnectionError)

    def test_comandos_originais_id_lf_e_saida(self):
        sock = FakeSocket()
        client.fila_menu.put({"status": "204"})
        with patch.object(client, "_ler_entrada", side_effect=["3", "12", "4"]):
            client.trata_menu(sock)
        self.assertEqual(sock.sent, [b"3", b"12\n", b"4"])
        self.assertTrue(client.evento_encerrar.is_set())

    def test_servidor_leitura_mista_sem_perder_bytes(self):
        reader = io.BytesIO(b'{"nome":"J1"}\n312\n4')
        self.assertEqual(json.loads(server._ler_linha(reader))["nome"], "J1")
        self.assertEqual(reader.read(1), b"3")
        self.assertEqual(server._ler_linha(reader, 32), "12")
        self.assertEqual(reader.read(1), b"4")
        with self.assertRaises(EOFError):
            server._ler_linha(reader)

    def test_adaptador_sem_nomes_preserva_socket(self):
        info = dict(id_partida="12", player_id=2, jogadores={
            str(i): dict(ip_jogador="127.0.0.1", porta_jogador=6000+i) for i in range(1,5)})
        with patch.object(adapter.socket, "socket", FakeListener):
            listener = FakeListener()
            value = adapter.adaptar_lobby([listener, info], dict(tick_ms=1000))
            self.assertIs(value.listener, listener)
            self.assertFalse(listener.closed)
            self.assertEqual(value.players[2], ("127.0.0.1", 6002))
            self.assertFalse(hasattr(value, "names"))
            self.assertEqual(value.config["tick_ms"], 1000)
            value.close()
            self.assertTrue(listener.closed)
            for mutate in (lambda d:d.update(player_id=True),
                           lambda d:d["jogadores"]["2"].update(porta_jogador=9999),
                           lambda d:d["jogadores"]["1"].update(ip_jogador="0.0.0.0")):
                bad = deepcopy(info)
                mutate(bad)
                listener = FakeListener()
                with self.assertRaises(ValueError):
                    adapter.adaptar_lobby([listener, bad])
                self.assertTrue(listener.closed)


if __name__ == "__main__":
    unittest.main()

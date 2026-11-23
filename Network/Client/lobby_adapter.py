"""Adapta o retorno original do lobby sem mudar seu contrato de lista."""
from dataclasses import dataclass
import ipaddress
import socket

from .protocolo import PROTOCOL_VERSION
from .simulation import RULES_VERSION

DEFAULT_CONFIG = dict(protocol_version=PROTOCOL_VERSION, rules_version=RULES_VERSION,
    width=40, height=25, tick_ms=250, prepare_timeout_ms=60000,
    phase_timeout_ms=8000, recovery_timeout_ms=15000, probe_timeout_ms=1500)


@dataclass
class LobbyResult:
    match_id: str
    player_id: int
    players: dict
    config: dict
    listener: socket.socket | None

    def close(self):
        if self.listener is not None:
            self.listener.close()
            self.listener = None


def _inteiro(value, label, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label} invalido")
    return value


def adaptar_lobby(retorno, config=None):
    """Valida [listener, info]. Em erro fecha o listener recebido.

    """
    if not isinstance(retorno, (list, tuple)) or len(retorno) != 2:
        raise ValueError("Retorno do lobby deve conter socket e informacoes")
    listener, info = retorno
    try:
        if type(info) is not dict or set(info) != {"id_partida", "player_id", "jogadores"}:
            raise ValueError("Informacoes de partida invalidas")
        match_id = info["id_partida"]
        if type(match_id) is not str or not 1 <= len(match_id) <= 64 or match_id.strip() != match_id:
            raise ValueError("ID de partida invalido")
        local = _inteiro(info["player_id"], "player_id", 1, 4)
        if (
            type(info["jogadores"]) is not dict
            or set(info["jogadores"]) not in (
                {"1", "2", "3"},
                {"1", "2", "3", "4"},
            )
        ):
            raise ValueError("Exigidos três ou quatro IDs de jogador")
        players = {}
        for key, player in info["jogadores"].items():
            if type(player) is not dict or set(player) != {"ip_jogador", "porta_jogador"}:
                raise ValueError("Endereco do jogador invalido")
            host = player["ip_jogador"]
            if type(host) is not str:
                raise ValueError("IPv4 deve ser texto")
            ip = ipaddress.IPv4Address(host)
            if ip.is_unspecified or ip.is_multicast or str(ip) == "255.255.255.255":
                raise ValueError("IPv4 nao pode ser usado como destino P2P")
            players[int(key)] = (str(ip), _inteiro(player["porta_jogador"], "porta", 1, 65535))
        if local not in players:
            raise ValueError("ID local não pertence à partida")
        if len(set(players.values())) != len(players):
            raise ValueError("Endpoints repetidos")
        values = dict(DEFAULT_CONFIG)
        if config is not None:
            if type(config) is not dict or set(config) - values.keys():
                raise ValueError("Configuracao desconhecida")
            values.update(config)
        if values["rules_version"] != RULES_VERSION or type(values["rules_version"]) is not str:
            raise ValueError("Regras incompativeis")
        if _inteiro(values["protocol_version"], "protocol_version", 1, 1000) != PROTOCOL_VERSION:
            raise ValueError("Protocolo incompativel")
        _inteiro(values["width"], "width", 4, 200)
        _inteiro(values["height"], "height", 4, 100)
        _inteiro(values["tick_ms"], "tick_ms", 10, 10000)
        for key in ("prepare_timeout_ms", "phase_timeout_ms", "recovery_timeout_ms", "probe_timeout_ms"):
            _inteiro(values[key], key, 1, 3600000)
        if not isinstance(listener, socket.socket) or listener.getsockname()[1] != players[local][1]:
            raise ValueError("Porta reservada difere da anunciada")
        return LobbyResult(match_id, local, players, values, listener)
    except Exception:
        if isinstance(listener, socket.socket):
            listener.close()
        raise

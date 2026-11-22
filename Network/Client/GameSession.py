"""Sincronizacao lockstep diretamente sobre snapshots de simulation.py.

Uso em UMA thread: start(), handle_message(), submit_move() e update().
Nao acessa sockets, nao cria threads e nao fecha o P2PManager.
Mensagens a enviar sao retiradas por drain_outgoing().

"""

from collections import deque
from copy import deepcopy
import time

from . import protocolo as protocol
from .recovery import RecoveryMixin
from .simulation import (
    DIRECTIONS, EMPTY, RULES_VERSION, normalizar_estado, validar_movimento,
    calcular_destino, simulate_tick as simulate_arena_tick,
)


TERMINAL_STATES = {"FINISHED", "ABORTED", "LEFT"}
SNAPSHOT_FORMAT = RULES_VERSION


class GameSession(RecoveryMixin):
    """Controla mensagens e barreiras; simulation calcula o jogo.

    config exige rules_version (str), tick_ms, prepare_timeout_ms e
    phase_timeout_ms (inteiros positivos). Valores extras devem ser JSON
    canonico e identicos em todos. Tempo local so controla prazos, nao a arena.

    snapshot: {tick, players, grid, result}. players contem {id,x,y,direction,
    alive}; grid usa "EMPTY", "WALL" ou ID inteiro do dono do rastro.
    result e None, ou {"winner_id": ID} / {"winner_id": None} para empate.
    """

    # 1. Configuracao comum, copias de estado e estruturas locais.
    def __init__(self, player_id, participants, match_id, config,
                 initial_state, simulate_tick=None, *, clock=time.monotonic):
        ids = list(participants)
        if (not 3 <= len(ids) <= 4 or any(type(i) is not int or i < 0 for i in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError("Participantes: 3 ou 4 IDs inteiros unicos")
        if type(player_id) is not int or player_id not in ids:
            raise ValueError("player_id deve pertencer aos participantes")
        protocol.make_hello(match_id, player_id)  # Valida ID e partida.
        # O argumento explicito continua util para testes; nao e mais obrigatorio.
        if simulate_tick is None:
            simulate_tick = simulate_arena_tick
        if not callable(simulate_tick) or not callable(clock):
            raise TypeError("simulate_tick e clock devem ser funcoes")
        if type(config) is not dict:
            raise ValueError("config deve ser dicionario")
        if type(config.get("rules_version")) is not str or not config["rules_version"]:
            raise ValueError("rules_version deve ser uma string nao vazia")
        for key in ("tick_ms", "prepare_timeout_ms", "phase_timeout_ms"):
            if type(config.get(key)) is not int or config[key] <= 0:
                raise ValueError(f"{key} deve ser inteiro positivo")

        self.player_id = player_id
        self.participants = frozenset(ids)
        self.active_participants = frozenset(ids)
        self.epoch = 0
        self.coordinator_id = min(ids)
        self.match_id = match_id
        self.config = deepcopy(config)
        self._simulate_tick = simulate_tick
        self._clock = clock
        self._confirmed = self._normalize_snapshot(initial_state, expected_tick=0)
        if self._confirmed["result"] is not None or any(
                not p["alive"] for p in self._confirmed["players"]):
            raise ValueError("A partida deve iniciar com todas as motos vivas")
        # Posicoes, grade e ocupacao ja foram conferidas por simulation.
        self.config_hash = protocol.calculate_hash({
            "snapshot_format": SNAPSHOT_FORMAT,
            "session_protocol": protocol.PROTOCOL_VERSION,
            "match_id": match_id, "participants": sorted(ids),
            "config": self.config, "initial_state": self._confirmed,
        })
        self.phase = "CONNECTING"
        self.confirmed_tick = 0
        self.abort_info = None
        self._candidate = None
        self._ready = {}
        self._moves = {}
        self._hashes = {}
        self._outgoing = deque()
        self._declarations = {}  # tick -> {id: (tipo, payload)}
        self._round_members = {}  # membros anteriores ao ciclo, inclusive quem sai
        self._leave_requested = False
        self._leave_acks = {}  # tick -> {sender: (player_id, digest)} para a saida local
        self._leave_wait_tick = None
        self._leave_digest = None
        self._finish_acks = {}
        self._finish_digest = None
        now = clock()
        self._deadline = now + config["prepare_timeout_ms"] / 1000
        self._not_before = now
        self._init_recovery()

    @property
    def current_tick(self):
        return self.confirmed_tick + 1

    def snapshot(self):
        """Copia do ultimo estado CONFIRMADO, para renderizacao/consulta."""
        return deepcopy(self._confirmed)

    def local_player(self):
        """Copia da moto local confirmada; nao permite editar o estado da sessao."""
        return deepcopy(next(p for p in self._confirmed["players"]
                             if p["id"] == self.player_id))

    # 2. Copia normalizada diretamente, sem construir entidades.
    def _normalize_snapshot(self, snapshot, expected_tick):
        protocol.canonical_bytes(snapshot)
        value = normalizar_estado(snapshot)
        if type(value["tick"]) is not int or value["tick"] != expected_tick:
            raise ValueError("Ciclo incorreto no snapshot")
        if {p["id"] for p in value["players"]} != self.participants:
            raise ValueError("IDs do snapshot diferem dos participantes")
        return value

    def _validate_transition(self, candidate, moves, previous=None):
        """Confere o vinculo entre o ciclo anterior e o resultado do simulador."""
        if previous is None:
            previous = self._confirmed
        if (len(candidate["grid"]) != len(previous["grid"])
                or len(candidate["grid"][0]) != len(previous["grid"][0])):
            raise ValueError("A arena nao pode mudar de tamanho")
        for old, new in zip(previous["players"], candidate["players"]):
            if not old["alive"]:
                if new != old:
                    raise ValueError("Uma moto eliminada nao pode reviver ou mover")
                continue
            target = moves[old["id"]]
            delta = (target["x"] - old["x"], target["y"] - old["y"])
            if ((new["x"], new["y"]) != (target["x"], target["y"])
                    or DIRECTIONS[new["direction"]] != delta):
                raise ValueError("Simulador nao aplicou a proposta daquele jogador")
        for old_row, new_row in zip(previous["grid"], candidate["grid"]):
            for old_cell, new_cell in zip(old_row, new_row):
                if old_cell != EMPTY and new_cell != old_cell:
                    raise ValueError("Parede ou rastro anterior foi alterado")

    # 3. A aplicacao chama start somente quando sua malha local esta formada.
    def start(self):
        if self.phase != "CONNECTING":
            raise RuntimeError("start so pode ser chamado uma vez, em CONNECTING")
        self.update()
        if self.phase == "ABORTED":
            return
        self.phase = "PREPARING"
        self._ready[self.player_id] = self.config_hash
        self._emit(protocol.make_ready(
            self.match_id, self.player_id, self.config_hash))
        self._progress()

    def drain_outgoing(self):
        """Retira mensagens a transmitir por broadcast, na ordem de producao."""
        messages = list(self._outgoing)
        self._outgoing.clear()
        return messages

    # 4. Proposta local: uma por ciclo; nao muda a arena imediatamente.
    def needs_local_move(self):
        """Permite ao loop saber se deve capturar/enviar uma nova proposta."""
        alive = next(p["alive"] for p in self._confirmed["players"]
                     if p["id"] == self.player_id)
        return (self.phase == "WAITING_MOVES" and alive and not self._leave_requested
                and self.player_id not in self._moves.get(self.current_tick, {}))

    def submit_direction(self, direction=None):
        """Converte uma intencao em MOVE; nao le teclas nem altera a moto.

        Aceita nome cardinal. None mantem a direcao confirmada.
        Uma proposta enviada nao pode ser substituida neste mesmo ciclo.
        """
        self.update()
        move = calcular_destino(self._confirmed, self.player_id, direction)
        self.submit_move(move["x"], move["y"])

    def submit_move(self, x, y):
        self.update()
        if self.phase != "WAITING_MOVES":
            raise RuntimeError("A sessao nao esta aguardando movimentos")
        tick = self.current_tick
        message = protocol.make_move(self.match_id, self.player_id, tick, x, y)
        if self._leave_requested:
            raise RuntimeError("Saida solicitada; nao aceita novas propostas")
        self._validate_move(self.player_id, message["data"])
        bucket = self._moves.setdefault(tick, {})
        previous = bucket.get(self.player_id)
        if previous is not None:
            if previous != message["data"]:
                raise ValueError("Movimento local ja escolhido para este ciclo")
            return  # Repeticao identica nao gera novo envio.
        self._record_declaration(self.player_id, message)
        bucket[self.player_id] = message["data"].copy()
        self._emit(message)
        self._progress()

    def _validate_move(self, sender_id, move):
        player = next(p for p in self._confirmed["players"] if p["id"] == sender_id)
        validar_movimento(player, move)
        # Nao verifica ocupacao/limites: isso pertence ao calculo de colisoes.

    # 5. Declaracoes exclusivas: MOVE, IDLE ou LEAVE_REQUEST, uma por membro/ciclo.
    def _record_declaration(self, sender, message):
        tick = message["tick"]
        declaration = (message["type"], message["data"].copy())
        bucket = self._declarations.setdefault(tick, {})
        if sender in bucket and bucket[sender] != declaration:
            raise protocol.ProtocolError("Duas declaracoes diferentes no mesmo ciclo")
        bucket[sender] = declaration

    def _handle_normal_message(self, channel_sender_id, message):
        if self.phase in TERMINAL_STATES:
            return
        try:
            if channel_sender_id == self.player_id:
                raise protocol.ProtocolError("Mensagem recebida do proprio jogador")
            protocol.validate_origin(message, self.match_id, self.participants,
                                     channel_sender_id)
            kind, tick, data = message["type"], message["tick"], message["data"]
            if kind in {protocol.FINISH_ACK, protocol.FINISH_COMMIT}:
                if tick < self.confirmed_tick:
                    return
                if tick > self.current_tick:
                    raise protocol.ProtocolError("Confirmacao final fora da janela")
                if kind == protocol.FINISH_ACK:
                    bucket = self._finish_acks.setdefault(tick, {})
                    if channel_sender_id in bucket and bucket[channel_sender_id] != data["state_hash"]:
                        raise protocol.ProtocolError("FINISH_ACK contraditorio")
                    bucket[channel_sender_id] = data["state_hash"]
                    self._finish_progress()
                else:
                    if (self.phase != "WAITING_FINISH" or data["state_hash"] != self._finish_digest
                            or data["ack_ids"] != sorted(self.active_participants)):
                        raise protocol.ProtocolError("Certificado final invalido")
                    self.phase = "FINISHED"
                return
            if kind == protocol.LEAVE_ACK:
                if data["player_id"] != self.player_id:
                    return  # ACK de outro participante em um broadcast legado.
                if tick < self.confirmed_tick or tick > self.current_tick:
                    raise protocol.ProtocolError("LEAVE_ACK fora da janela")
                bucket = self._leave_acks.setdefault(tick, {})
                value = (data["player_id"], data["state_hash"])
                if channel_sender_id in bucket and bucket[channel_sender_id] != value:
                    raise protocol.ProtocolError("LEAVE_ACK contraditorio")
                bucket[channel_sender_id] = value
                self._check_leave_acks()
                return
            # Mensagens em voo de quem ja saiu nao podem reinseri-lo nas barreiras.
            if channel_sender_id not in self.active_participants:
                return
            if kind == protocol.ABORT:
                self._abort(data["reason"], "ABORT remoto", data["related_tick"],
                            notify=False, sender_id=channel_sender_id)
                return
            if kind == protocol.READY:
                if data["config_hash"] != self.config_hash:
                    self._abort("CONFIG_MISMATCH", "Configuracoes iniciais diferem", 0)
                    return
                self._ready[channel_sender_id] = data["config_hash"]
            else:
                if tick <= self.confirmed_tick:
                    return
                if tick > self.current_tick + 1:
                    raise protocol.ProtocolError("Mensagem mais de um ciclo a frente")
                if channel_sender_id not in self._ready:
                    raise protocol.ProtocolError("Mensagem de ciclo antes de READY")
                if kind in {protocol.MOVE, protocol.IDLE, protocol.LEAVE_REQUEST}:
                    self._record_declaration(channel_sender_id, message)
                    if kind == protocol.MOVE:
                        self._moves.setdefault(tick, {})[channel_sender_id] = data.copy()
                elif kind == protocol.STATE_HASH:
                    bucket = self._hashes.setdefault(tick, {})
                    if channel_sender_id in bucket and bucket[channel_sender_id] != data["state_hash"]:
                        self._abort("STATE_MISMATCH", "Hash contraditorio", tick)
                        return
                    bucket[channel_sender_id] = data["state_hash"]
            self._progress()
        except protocol.ProtocolError as error:
            self._abort("INVALID_MESSAGE", str(error), self.current_tick)

    # 6. A lista COMPLETA de declaracoes fixa as saidas antes de simular.
    def _progress(self):
        if self._recovery is not None or self._detection is not None:
            return
        if self.phase == "PREPARING" and set(self._ready) == self.participants:
            self._enter_moves()
        if self.phase == "WAITING_MOVES":
            tick = self.current_tick
            declarations = self._declarations.get(tick, {})
            if not self.active_participants <= declarations.keys():
                return
            if set(declarations) != self.active_participants:
                self._abort("INVALID_MESSAGE", "Declaracao de membro removido", tick)
                return
            if self._clock() < self._not_before:
                return
            moves = self._moves.get(tick, {})
            alive = {p["id"] for p in self._confirmed["players"] if p["alive"]}
            leaving = {i for i, (kind, _) in declarations.items()
                       if kind == protocol.LEAVE_REQUEST}
            try:
                for sender, (kind, data) in declarations.items():
                    if kind == protocol.MOVE:
                        self._validate_move(sender, data)
                    elif kind == protocol.IDLE and sender in alive:
                        raise ValueError("Moto viva nao pode declarar IDLE")
                if set(moves) != alive - leaving:
                    raise ValueError("Faltou MOVE de moto viva ou sobrou MOVE")
            except ValueError as error:
                self._abort("INVALID_MOVE", str(error), tick)
                return
            try:
                ordered = {i: deepcopy(moves[i]) for i in sorted(moves)}
                candidate = self._simulate_departures(ordered, leaving)
                candidate = self._normalize_snapshot(candidate, tick)
                next_members = self.active_participants - leaving
                digest = self._state_digest(candidate, next_members)
            except Exception:
                self._abort("STATE_MISMATCH", "Erro local na simulacao", tick)
                raise
            self._candidate = candidate
            self._round_members[tick] = self.active_participants
            self._hashes.setdefault(tick, {})[self.player_id] = digest
            self._emit(protocol.make_state_hash(
                self.match_id, self.player_id, tick, digest))
            self.phase = "WAITING_HASHES"
            self._deadline = self._clock() + self.config["phase_timeout_ms"] / 1000
        if self.phase == "WAITING_HASHES":
            tick = self.current_tick
            hashes = self._hashes[tick]
            members = self._round_members[tick]
            own = hashes[self.player_id]
            if not set(hashes) <= members or any(d != own for d in hashes.values()):
                self._abort("STATE_MISMATCH", "Estados ou membros diferem", tick)
                return
            if set(hashes) != members:
                return
            leaving = {i for i, (kind, _) in self._declarations[tick].items()
                       if kind == protocol.LEAVE_REQUEST}
            self._confirmed = self._candidate
            self._candidate = None
            self.confirmed_tick = tick
            previous_hash = self._history[tick - 1]["state_hash"]
            self.active_participants = members - leaving
            self.coordinator_id = min(self.active_participants, default=None)
            self._save_checkpoint(previous_hash, members,
                                  self._declarations[tick], hashes)
            # Cada antigo membro confirma a saida a TODOS que sairam, inclusive
            # outras saidas simultaneas. Ninguem fecha apenas por enviar o hash.
            for departed in sorted(leaving):
                if departed != self.player_id:
                    self._emit(protocol.make_leave_ack(
                        self.match_id, self.player_id, tick, departed, own))
            self._moves.pop(tick, None)
            self._hashes.pop(tick, None)
            self._declarations.pop(tick, None)
            if self.player_id in leaving:
                self._leave_wait_tick = tick
                self._leave_digest = own
                self.phase = "WAITING_LEAVE_ACK"
                self._deadline = self._clock() + self.config["phase_timeout_ms"] / 1000
                self._check_leave_acks()
            elif self._confirmed["result"] is not None:
                self.phase = "WAITING_FINISH"
                self._finish_digest = own
                self._finish_acks.setdefault(tick, {})[self.player_id] = own
                self._emit(protocol.make_message(protocol.FINISH_ACK, self.match_id,
                                                self.player_id, tick, {"state_hash": own}))
                self._deadline = self._clock() + self.config["phase_timeout_ms"] / 1000
                self._finish_progress()
            else:
                self._enter_moves()
            # Basta manter o ciclo anterior para rotear hashes ainda na fila.
            for old_tick in list(self._round_members):
                if old_tick < tick:
                    self._round_members.pop(old_tick)

    def _finish_progress(self):
        if self.phase != "WAITING_FINISH":
            return
        acks = self._finish_acks.get(self.confirmed_tick, {})
        if not set(acks) <= self.active_participants or any(d != self._finish_digest for d in acks.values()):
            raise protocol.ProtocolError("Confirmacoes finais divergentes")
        if set(acks) == self.active_participants:
            self._emit(protocol.make_message(protocol.FINISH_COMMIT, self.match_id,
                                            self.player_id, self.confirmed_tick,
                                            {"state_hash": self._finish_digest,
                                             "ack_ids": sorted(acks)}))
            self.phase = "FINISHED"

    def _simulate_departures(self, moves, leaving, previous=None):
        """Saida antes do movimento: conserva posicao, direcao e todo o rastro."""
        base = deepcopy(self._confirmed if previous is None else previous)
        for player in base["players"]:
            if player["id"] in leaving:
                player["alive"] = False
        survivors = [p["id"] for p in base["players"] if p["alive"]]
        # Se as saidas ja definem o vencedor, nenhuma moto anda neste ciclo.
        if len(survivors) <= 1:
            base["tick"] += 1
            base["result"] = {"winner_id": survivors[0] if survivors else None}
            return base
        # O validador compara o resultado com a base efetiva apos as saidas.
        candidate = self._simulate_tick(deepcopy(base), moves)
        self._validate_transition(candidate, moves, previous=base)
        return candidate

    def _enter_moves(self):
        self.phase = "WAITING_MOVES"
        now = self._clock()
        self._not_before = now + self.config["tick_ms"] / 1000
        self._deadline = self._not_before + self.config["phase_timeout_ms"] / 1000
        if self._leave_requested:
            self._publish_leave()
        elif not self.local_player()["alive"]:
            message = protocol.make_idle(self.match_id, self.player_id, self.current_tick)
            self._record_declaration(self.player_id, message)
            self._emit(message)

    # 7. Saida e assincrona: o chamador continua bombeando mensagens ate LEFT.
    def leave(self):
        if self.phase in TERMINAL_STATES or self._leave_requested:
            return
        if self.phase in {"CONNECTING", "PREPARING"}:
            self._abort("PEER_LEFT", "Saida antes de iniciar a partida", 0)
            return
        self._leave_requested = True
        if (self.phase == "WAITING_MOVES" and self.player_id not in
                self._declarations.get(self.current_tick, {})):
            self._publish_leave()
        # Se MOVE/IDLE ja saiu, nao o substitui: aplica no ciclo seguinte.

    def _publish_leave(self):
        message = protocol.make_leave_request(self.match_id, self.player_id,
                                              self.current_tick)
        self._record_declaration(self.player_id, message)
        self._emit(message)

    def _check_leave_acks(self):
        if self.phase != "WAITING_LEAVE_ACK":
            return
        members = self._round_members[self._leave_wait_tick] - {self.player_id}
        acks = self._leave_acks.get(self._leave_wait_tick, {})
        if any(sender not in members or digest != self._leave_digest
               for sender, (_, digest) in acks.items()):
            self._abort("STATE_MISMATCH", "ACK de saida invalido", self.confirmed_tick)
        elif set(acks) == members:
            self.phase = "LEFT"

    def _normal_recipients_for(self, message):
        """Destinatarios para manager.send_to; nao transmitir a quem ja saiu."""
        if message["type"] == protocol.LEAVE_ACK:
            return frozenset({message["data"]["player_id"]})
        if message["type"] == protocol.STATE_HASH:
            return self._round_members.get(message["tick"], self.active_participants) - {self.player_id}
        return self.active_participants - {self.player_id}

    def notify_disconnected(self, player_id):
        self._handle_disconnect(player_id)

    def update(self):
        if self.phase in TERMINAL_STATES:
            return
        if self._recovery is not None:
            self._drive_recovery()
            return
        if self._detection is not None:
            self._drive_detection()
            return
        self._progress()
        if self.phase in TERMINAL_STATES or self._clock() < self._deadline:
            return
        if self.phase == "WAITING_MOVES":
            missing = self.active_participants - self._declarations.get(self.current_tick, {}).keys()
        elif self.phase == "WAITING_HASHES":
            missing = self._round_members[self.current_tick] - self._hashes.get(self.current_tick, {}).keys()
        elif self.phase == "WAITING_FINISH":
            missing = self.active_participants - self._finish_acks.get(self.confirmed_tick, {}).keys()
        else:
            self._abort("TIMEOUT", "Prazo da fase excedido", self.current_tick)
            return
        if missing:
            self._begin_detection(missing)
        else:
            self._abort("RECOVERY_FAILED", "Falta de progresso sem participante ausente", self.current_tick)

    def _abort(self, reason, detail, related_tick, *, notify=True, sender_id=None):
        if self.phase in TERMINAL_STATES:
            return
        self.phase = "ABORTED"
        self.abort_info = {"reason": reason, "detail": detail,
                           "related_tick": related_tick, "sender_id": sender_id}
        self._candidate = None
        self._moves.clear()
        self._hashes.clear()
        self._outgoing.clear()  # Nao enviar movimentos antigos depois da falha.
        if notify:
            self._emit(protocol.make_abort(
                self.match_id, self.player_id, self.confirmed_tick, reason, related_tick))

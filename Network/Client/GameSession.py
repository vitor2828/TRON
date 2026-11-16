from collections import deque
from copy import deepcopy
import time

from . import protocolo as protocol


DIRECTIONS = {"UP": (0, -1), "DOWN": (0, 1),
              "LEFT": (-1, 0), "RIGHT": (1, 0)}
TERMINAL_STATES = {"FINISHED", "ABORTED"}


class GameSession:
    """Controla mensagens e barreiras; simulate_tick calcula o jogo.

    config exige rules_version (str), tick_ms, prepare_timeout_ms e
    phase_timeout_ms (inteiros positivos). Valores extras devem ser JSON
    canonico e identicos em todos. Tempo local so controla prazos, nao a arena.

    snapshot: {tick, players, grid, result}. players contem {id,x,y,direction,
    alive}; grid usa "EMPTY", "WALL" ou ID inteiro do dono do rastro.
    result e None, ou {"winner_id": ID} / {"winner_id": None} para empate.
    """

    # 1. Configuracao comum, copias de estado e estruturas locais.
    def __init__(self, player_id, participants, match_id, config,
                 initial_state, simulate_tick, *, clock=time.monotonic):
        ids = list(participants)
        if (not 3 <= len(ids) <= 4 or any(type(i) is not int or i < 0 for i in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError("Participantes: 3 ou 4 IDs inteiros unicos")
        if type(player_id) is not int or player_id not in ids:
            raise ValueError("player_id deve pertencer aos participantes")
        protocol.make_hello(match_id, player_id)  # Valida ID e partida.
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
        self.match_id = match_id
        self.config = deepcopy(config)
        self._simulate_tick = simulate_tick
        self._clock = clock
        self._confirmed = self._normalize_snapshot(initial_state, expected_tick=0)
        if self._confirmed["result"] is not None or any(
                not p["alive"] for p in self._confirmed["players"]):
            raise ValueError("A partida deve iniciar com todas as motos vivas")
        height, width = len(self._confirmed["grid"]), len(self._confirmed["grid"][0])
        if any(not (0 <= p["x"] < width and 0 <= p["y"] < height)
               for p in self._confirmed["players"]):
            raise ValueError("Posicoes iniciais devem estar dentro da grade")
        positions = [(p["x"], p["y"]) for p in self._confirmed["players"]]
        if len(set(positions)) != len(positions):
            raise ValueError("Posicoes iniciais devem ser distintas")
        self.config_hash = protocol.calculate_hash({
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
        now = clock()
        self._deadline = now + config["prepare_timeout_ms"] / 1000
        self._not_before = now

    @property
    def current_tick(self):
        return self.confirmed_tick + 1

    def snapshot(self):
        """Copia do ultimo estado CONFIRMADO, para renderizacao/consulta."""
        return deepcopy(self._confirmed)

    # 2. Contrato do snapshot; normaliza a ordem dos jogadores antes do hash.
    def _normalize_snapshot(self, snapshot, expected_tick):
        if type(snapshot) is not dict or set(snapshot) != {"tick", "players", "grid", "result"}:
            raise ValueError("Snapshot exige tick, players, grid e result")
        value = deepcopy(snapshot)
        protocol.canonical_bytes(value)
        if type(value["tick"]) is not int or value["tick"] != expected_tick:
            raise ValueError("Ciclo incorreto no snapshot")
        players = value["players"]
        if type(players) is not list or len(players) != len(self.participants):
            raise ValueError("Snapshot deve conter todos os jogadores")
        for player in players:
            if type(player) is not dict or set(player) != {"id", "x", "y", "direction", "alive"}:
                raise ValueError("Campos invalidos no jogador")
            if any(type(player[k]) is not int for k in ("id", "x", "y")):
                raise ValueError("id, x e y devem ser inteiros")
            if (type(player["direction"]) is not str
                    or player["direction"] not in DIRECTIONS
                    or type(player["alive"]) is not bool):
                raise ValueError("Direcao ou situacao da moto invalida")
        ids = [p["id"] for p in players]
        if len(set(ids)) != len(ids) or set(ids) != self.participants:
            raise ValueError("IDs do snapshot diferem dos participantes")
        players.sort(key=lambda player: player["id"])
        grid = value["grid"]
        if type(grid) is not list or not grid or type(grid[0]) is not list or not grid[0]:
            raise ValueError("Grade deve ser uma lista nao vazia de linhas")
        for row in grid:
            if type(row) is not list or len(row) != len(grid[0]):
                raise ValueError("Grade deve ser retangular")
            for cell in row:
                if not ((type(cell) is str and cell in {"EMPTY", "WALL"})
                        or (type(cell) is int and cell in self.participants)):
                    raise ValueError("Celula invalida: use EMPTY, WALL ou ID")
        alive = [p["id"] for p in players if p["alive"]]
        result = value["result"]
        if len(alive) > 1:
            if result is not None:
                raise ValueError("Mais de uma moto viva: resultado deve ser None")
        else:
            if type(result) is not dict or set(result) != {"winner_id"}:
                raise ValueError("Fim da partida exige winner_id")
            winner = alive[0] if alive else None
            actual = result["winner_id"]
            if type(actual) is not type(winner) or actual != winner:
                raise ValueError("Vencedor nao corresponde as motos vivas")
        return value

    # 3. A aplicacao chama start somente quando sua malha local esta formada.
    def start(self):
        if self.phase != "CONNECTING":
            raise RuntimeError("start so pode ser chamado uma vez, em CONNECTING")
        self.update()
        if self.phase == "ABORTED":
            return
        self.phase = "PREPARING"
        self._ready[self.player_id] = self.config_hash
        self._outgoing.append(protocol.make_ready(
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
        return (self.phase == "WAITING_MOVES" and alive
                and self.player_id not in self._moves.get(self.current_tick, {}))

    def submit_move(self, x, y):
        self.update()
        if self.phase != "WAITING_MOVES":
            raise RuntimeError("A sessao nao esta aguardando movimentos")
        tick = self.current_tick
        message = protocol.make_move(self.match_id, self.player_id, tick, x, y)
        self._validate_move(self.player_id, message["data"])
        bucket = self._moves.setdefault(tick, {})
        previous = bucket.get(self.player_id)
        if previous is not None:
            if previous != message["data"]:
                raise ValueError("Movimento local ja escolhido para este ciclo")
            return  # Repeticao identica nao gera novo envio.
        bucket[self.player_id] = message["data"].copy()
        self._outgoing.append(message)
        self._progress()

    def _validate_move(self, sender_id, move):
        player = next(p for p in self._confirmed["players"] if p["id"] == sender_id)
        if not player["alive"]:
            raise ValueError("Moto eliminada nao envia MOVE")
        delta = (move["x"] - player["x"], move["y"] - player["y"])
        dx, dy = DIRECTIONS[player["direction"]]
        if delta not in DIRECTIONS.values() or delta == (-dx, -dy):
            raise ValueError("MOVE deve dar um passo e nao pode inverter a direcao")
        # Nao verifica ocupacao/limites: isso pertence ao calculo de colisoes.

    # 5. Uma unica entrada para mensagens de canais ja identificados.
    def handle_message(self, channel_sender_id, message):
        self.update()
        if self.phase in TERMINAL_STATES:
            return
        try:
            if (type(channel_sender_id) is not int
                    or channel_sender_id not in self.participants
                    or channel_sender_id == self.player_id):
                raise protocol.ProtocolError("Canal remoto desconhecido")
            protocol.validate_origin(message, self.match_id, self.participants,
                                     channel_sender_id)
            kind, tick = message["type"], message["tick"]
            data = message["data"]
            if kind == protocol.ABORT:
                self._abort(data["reason"], "ABORT remoto", data["related_tick"],
                            notify=False, sender_id=channel_sender_id)
                return
            if kind == protocol.LEAVE_REQUEST:
                self._abort("PEER_LEFT", "Participante solicitou saida", tick,
                            sender_id=channel_sender_id)
                return
            if kind == protocol.READY:
                digest = data["config_hash"]
                if digest != self.config_hash:
                    self._abort("CONFIG_MISMATCH", "Configuracoes iniciais diferem", 0)
                    return
                self._ready[channel_sender_id] = digest
            else:
                if tick <= self.confirmed_tick:
                    return  # Ciclo antigo: nao modifica o estado confirmado.
                if tick > self.current_tick + 1:
                    raise protocol.ProtocolError("Mensagem mais de um ciclo a frente")
                if channel_sender_id not in self._ready:
                    raise protocol.ProtocolError("MOVE/HASH recebido antes de READY")
                if kind == protocol.MOVE:
                    bucket = self._moves.setdefault(tick, {})
                    if channel_sender_id in bucket and bucket[channel_sender_id] != data:
                        self._abort("CONFLICTING_MOVE", "MOVE remoto contraditorio", tick)
                        return
                    bucket[channel_sender_id] = data.copy()
                elif kind == protocol.STATE_HASH:
                    bucket = self._hashes.setdefault(tick, {})
                    digest = data["state_hash"]
                    if channel_sender_id in bucket and bucket[channel_sender_id] != digest:
                        self._abort("STATE_MISMATCH", "Hash remoto contraditorio", tick)
                        return
                    bucket[channel_sender_id] = digest
            self._progress()
        except protocol.ProtocolError as error:
            self._abort("INVALID_MESSAGE", str(error), self.current_tick)

    # 6. Barreiras: READY -> MOVE -> simulacao -> HASH -> confirmacao.
    def _progress(self):
        if self.phase == "PREPARING" and set(self._ready) == self.participants:
            self._enter_moves()
        if self.phase == "WAITING_MOVES":
            tick = self.current_tick
            moves = self._moves.get(tick, {})
            alive = {p["id"] for p in self._confirmed["players"] if p["alive"]}
            try:
                for sender, move in moves.items():
                    self._validate_move(sender, move)
            except ValueError as error:
                self._abort("INVALID_MOVE", str(error), tick)
                return
            if set(moves) == alive and self._clock() >= self._not_before:
                # Falhas da funcao da arena sao bugs locais, nao mensagens invalidas.
                # O estado confirmado fica intacto e a excecao chega ao chamador.
                try:
                    ordered_moves = {i: deepcopy(moves[i]) for i in sorted(moves)}
                    candidate = self._simulate_tick(deepcopy(self._confirmed), ordered_moves)
                    candidate = self._normalize_snapshot(candidate, tick)
                    if (len(candidate["grid"]) != len(self._confirmed["grid"])
                            or len(candidate["grid"][0]) != len(self._confirmed["grid"][0])):
                        raise ValueError("A arena nao pode mudar de tamanho")
                    for old, new in zip(self._confirmed["players"], candidate["players"]):
                        if not old["alive"] and new["alive"]:
                            raise ValueError("Moto eliminada nao pode reviver")
                    digest = protocol.calculate_hash(candidate)
                except Exception:
                    self._abort("STATE_MISMATCH", "Erro local na simulacao", tick)
                    raise
                self._candidate = candidate
                self._hashes.setdefault(tick, {})[self.player_id] = digest
                self._outgoing.append(protocol.make_state_hash(
                    self.match_id, self.player_id, tick, digest))
                self.phase = "WAITING_HASHES"
                self._deadline = self._clock() + self.config["phase_timeout_ms"] / 1000
        if self.phase == "WAITING_HASHES":
            tick = self.current_tick
            hashes = self._hashes[tick]
            own = hashes[self.player_id]
            if any(digest != own for digest in hashes.values()):
                self._abort("STATE_MISMATCH", "Estados calculados diferem", tick)
                return
            if set(hashes) == self.participants:
                self._confirmed = self._candidate
                self._candidate = None
                self.confirmed_tick = tick
                self._moves.pop(tick, None)
                self._hashes.pop(tick, None)
                if self._confirmed["result"] is not None:
                    self.phase = "FINISHED"
                    self._moves.clear()
                    self._hashes.clear()
                else:
                    self._enter_moves()

    def _enter_moves(self):
        self.phase = "WAITING_MOVES"
        now = self._clock()
        self._not_before = now + self.config["tick_ms"] / 1000
        self._deadline = self._not_before + self.config["phase_timeout_ms"] / 1000

    # 7. Atualizacao periodica: nao ha um while bloqueante dentro da sessao.
    def update(self):
        if self.phase in TERMINAL_STATES:
            return
        if self._clock() >= self._deadline:
            self._abort("TIMEOUT", "Prazo da fase excedido", self.current_tick)
            return
        self._progress()

    # 8. Saida/falha: nesta versao todos interrompem; nao ha acordo de remocao.
    def notify_disconnected(self, player_id):
        if self.phase not in TERMINAL_STATES and player_id in self.participants:
            if player_id != self.player_id:
                self._abort("PEER_DISCONNECTED", "Canal remoto perdido", self.current_tick,
                            sender_id=player_id)

    def leave(self):
        if self.phase in TERMINAL_STATES:
            return
        self._abort("PEER_LEFT", "Saida local solicitada", self.current_tick)
        self._outgoing.appendleft(protocol.make_leave_request(
            self.match_id, self.player_id, self.confirmed_tick))

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
            self._outgoing.append(protocol.make_abort(
                self.match_id, self.player_id, self.confirmed_tick, reason, related_tick))

"""Regras deterministicas TRON sobre snapshots.
"""

from collections import Counter


# 1. Contrato do estado e das regras; cores pertencem exclusivamente ao render.
RULES_VERSION = "tron-snapshots-v1"
EMPTY = "EMPTY"
WALL = "WALL"
DIRECTIONS = {"UP": (0, -1), "DOWN": (0, 1),
              "LEFT": (-1, 0), "RIGHT": (1, 0)}
_DIRECTION_NAMES = {delta: name for name, delta in DIRECTIONS.items()}
_STATE_FIELDS = {"tick", "players", "grid", "result"}
_PLAYER_FIELDS = {"id", "x", "y", "direction", "alive"}


def _integer(value, name, minimum=None):
    if type(value) is not int or (minimum is not None and value < minimum):
        raise ValueError(f"{name} deve ser inteiro" +
                         (f" >= {minimum}" if minimum is not None else ""))


# 2. Validacao SOMENTE LEITURA: nao copia, normaliza nem muda o estado.
def validar_estado(estado):
    """Retorna None se valido; levanta ValueError se o snapshot for invalido.

    Aceita 1..4 jogadores para testes locais. A partida P2P e seu construtor
    exigem 3 ou 4. A grade inclui as celulas atuais das motos como ocupadas.
    """
    if type(estado) is not dict or set(estado) != _STATE_FIELDS:
        raise ValueError("Estado exige tick, players, grid e result")
    _integer(estado["tick"], "tick", 0)
    players = estado["players"]
    if type(players) is not list or not 1 <= len(players) <= 4:
        raise ValueError("Estado exige de 1 a 4 jogadores")
    ids = []
    for player in players:
        if type(player) is not dict or set(player) != _PLAYER_FIELDS:
            raise ValueError("Jogador exige id, x, y, direction e alive")
        _integer(player["id"], "id", 0)
        _integer(player["x"], "x")
        _integer(player["y"], "y")
        if type(player["direction"]) is not str or player["direction"] not in DIRECTIONS:
            raise ValueError("Direcao invalida")
        if type(player["alive"]) is not bool:
            raise ValueError("alive deve ser booleano")
        ids.append(player["id"])
    if len(set(ids)) != len(ids):
        raise ValueError("IDs repetidos")
    grid = estado["grid"]
    if type(grid) is not list or len(grid) < 3 or type(grid[0]) is not list or len(grid[0]) < 3:
        raise ValueError("Grade deve ter pelo menos 3 linhas e 3 colunas")
    width = len(grid[0])
    for row in grid:
        if type(row) is not list or len(row) != width:
            raise ValueError("Grade deve ser retangular")
        for cell in row:
            if not ((type(cell) is str and cell in {EMPTY, WALL})
                    or (type(cell) is int and cell in ids)):
                raise ValueError("Celula deve ser EMPTY, WALL ou ID conhecido")
    positions = []
    for player in players:
        if not player["alive"]:
            continue  # O destino final de uma moto eliminada pode ser externo.
        x, y = player["x"], player["y"]
        if not (0 <= x < width and 0 <= y < len(grid)):
            raise ValueError("Moto viva fora da grade")
        if type(grid[y][x]) is not int or grid[y][x] != player["id"]:
            raise ValueError("Celula de moto viva deve conter seu ID")
        positions.append((x, y))
    if len(set(positions)) != len(positions):
        raise ValueError("Motos vivas nao podem compartilhar posicao")
    alive = [p["id"] for p in players if p["alive"]]
    result = estado["result"]
    if len(alive) > 1:
        if result is not None:
            raise ValueError("Partida com mais de uma moto viva exige result=None")
    else:
        expected = alive[0] if alive else None
        if type(result) is not dict or set(result) != {"winner_id"}:
            raise ValueError("Fim da partida exige winner_id")
        if type(result["winner_id"]) is not type(expected) or result["winner_id"] != expected:
            raise ValueError("Resultado incompativel com as motos vivas")


# 3. Copia normalizada; listas/dicionarios novos, mesmo se a entrada tem aliases.
def normalizar_estado(estado):
    """Valida, copia e ordena jogadores por ID; nao altera a entrada."""
    validar_estado(estado)
    return {
        "tick": estado["tick"],
        "players": [dict(player) for player in sorted(
            estado["players"], key=lambda player: player["id"])],
        "grid": [list(row) for row in estado["grid"]],
        "result": None if estado["result"] is None else dict(estado["result"]),
    }


# 4. Estado inicial: IDs reais, posicoes/direcoes distintas, nenhuma aleatoriedade.
def criar_estado_inicial(participantes, largura=20, altura=15):
    ids = list(participantes)
    if not 3 <= len(ids) <= 4:
        raise ValueError("Partida P2P exige 3 ou 4 participantes")
    for player_id in ids:
        _integer(player_id, "ID", 0)
    if len(set(ids)) != len(ids):
        raise ValueError("IDs devem ser distintos")
    _integer(largura, "largura", 4)
    _integer(altura, "altura", 4)
    ids.sort()
    positions = [(1, 1), (1, altura - 2), (largura - 2, 1), (largura - 2, altura - 2)]
    names = ["RIGHT", "UP", "DOWN", "LEFT"]
    grid = [[WALL if x in (0, largura-1) or y in (0, altura-1) else EMPTY
             for x in range(largura)] for y in range(altura)]
    players = []
    for index, player_id in enumerate(ids):
        x, y = positions[index]
        players.append(dict(id=player_id, x=x, y=y, direction=names[index], alive=True))
        grid[y][x] = player_id
    state = dict(tick=0, players=players, grid=grid, result=None)
    validar_estado(state)
    return state


# 5. Uma proposta nao e um estado final: valida passo/inversao, nao colisao.
def validar_movimento(jogador, movimento):
    """Recebe um jogador de estado validado e {x,y}; retorna a nova direcao."""
    if type(jogador) is not dict or set(jogador) != _PLAYER_FIELDS:
        raise ValueError("Jogador invalido")
    _integer(jogador["id"], "id", 0)
    _integer(jogador["x"], "x")
    _integer(jogador["y"], "y")
    if jogador["alive"] is not True:
        raise ValueError("Moto eliminada nao pode propor movimento")
    old_direction = jogador["direction"]
    if type(old_direction) is not str or old_direction not in DIRECTIONS:
        raise ValueError("Direcao anterior invalida")
    if type(movimento) is not dict or set(movimento) != {"x", "y"}:
        raise ValueError("Movimento exige exatamente x e y")
    _integer(movimento["x"], "x")
    _integer(movimento["y"], "y")
    delta = (movimento["x"] - jogador["x"], movimento["y"] - jogador["y"])
    dx, dy = DIRECTIONS[old_direction]
    if delta not in _DIRECTION_NAMES or delta == (-dx, -dy):
        raise ValueError("Movimento deve dar um passo e nao inverter a direcao")
    return _DIRECTION_NAMES[delta]


def calcular_destino(estado, player_id, direcao=None):
    """Constroi {x,y} para a moto local. None mantem a direcao; nao muda estado."""
    validar_estado(estado)
    _integer(player_id, "player_id", 0)
    player = next((p for p in estado["players"] if p["id"] == player_id), None)
    if player is None:
        raise ValueError("Jogador desconhecido")
    if direcao is None:
        direcao = player["direction"]
    if type(direcao) is not str or direcao not in DIRECTIONS:
        raise ValueError("Direcao deve ser um nome cardinal")
    dx, dy = DIRECTIONS[direcao]
    move = {"x": player["x"] + dx, "y": player["y"] + dy}
    validar_movimento(player, move)
    return move


# 6. Um ciclo completo; nenhuma modificacao nos objetos fornecidos pelo chamador.
def simulate_tick(estado_anterior, movimentos):
    """Estado + uma proposta por moto viva -> novo snapshot independente."""
    state = normalizar_estado(estado_anterior)
    if state["result"] is not None:
        raise ValueError("A partida ja terminou")
    alive = {p["id"]: p for p in state["players"] if p["alive"]}
    if type(movimentos) is not dict or any(type(i) is not int for i in movimentos):
        raise ValueError("Movimentos devem ser um dicionario com IDs inteiros")
    if set(movimentos) != set(alive):
        raise ValueError("Forneca exatamente uma proposta por moto viva")
    targets, names = {}, {}
    for player_id, player in alive.items():
        names[player_id] = validar_movimento(player, movimentos[player_id])
        targets[player_id] = (movimentos[player_id]["x"], movimentos[player_id]["y"])

    # Avalia todos os destinos na mesma grade anterior, sem escrever nela.
    grid = state["grid"]
    height, width = len(grid), len(grid[0])
    counts = Counter(targets.values())
    deaths, new_cells = set(), {}
    for player_id, (x, y) in targets.items():
        inside = 0 <= x < width and 0 <= y < height
        if not inside or grid[y][x] != EMPTY or counts[(x, y)] > 1:
            deaths.add(player_id)
        if inside and grid[y][x] == EMPTY:
            # Convencao preservada: colisao conjunta em celula livre deixa rastro.
            # O menor ID representa essa celula; nao recebe vitoria ou pontos.
            new_cells[(x, y)] = min(player_id, new_cells.get((x, y), player_id))

    # So depois de decidir todas as mortes aplicamos os efeitos no NOVO estado.
    for player_id, player in alive.items():
        player["x"], player["y"] = targets[player_id]
        player["direction"] = names[player_id]
        player["alive"] = player_id not in deaths
    for (x, y), owner in new_cells.items():
        grid[y][x] = owner
    state["tick"] += 1
    survivors = [p["id"] for p in state["players"] if p["alive"]]
    state["result"] = None if len(survivors) > 1 else {
        "winner_id": survivors[0] if survivors else None,
    }
    validar_estado(state)
    return state

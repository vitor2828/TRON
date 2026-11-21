import argparse
from collections import Counter
import json
import math
from pathlib import Path
from queue import Empty
import time
import traceback

from .GameSession import GameSession, DIRECTIONS, TERMINAL_STATES
from .p2p_manager import P2PManager
from . import protocolo as protocol


# 1. Parametros: opcoes comuns devem coincidir em todos os processos.
def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("player_id", type=int)
    parser.add_argument("--players", type=int, choices=(3, 4), default=3)
    parser.add_argument("--match-id", default="teste-etapa3")
    parser.add_argument("--cycles", type=int, default=10,
                        help="Ciclo em que todas as motos atingem a parede")
    parser.add_argument("--base-port", type=int, default=6000)
    parser.add_argument("--addresses", type=Path,
                        help="JSON: ID como string -> [IPv4, porta], para teste em LAN")
    parser.add_argument("--tick-ms", type=int, default=150)
    parser.add_argument("--prepare-timeout", type=float, default=60.0)
    parser.add_argument("--phase-timeout", type=float, default=8.0)
    parser.add_argument("--linger-ms", type=int, default=1000,
                        help="Manter canais abertos por este tempo apos FINISHED")
    parser.add_argument("--fault", choices=("none", "config", "state", "leave"),
                        default="none")
    parser.add_argument("--fault-player", type=int, default=2)
    parser.add_argument("--fault-tick", type=int, default=3)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--report", type=Path, help="Salvar resumo local em JSON")
    args = parser.parse_args(argv)
    if not 1 <= args.player_id <= args.players:
        parser.error("player_id deve estar entre 1 e --players")
    if not 2 <= args.cycles <= 200:
        parser.error("--cycles deve estar entre 2 e 200")
    if not 1 <= args.base_port <= 65535 - args.players:
        parser.error("--base-port invalida")
    if not 10 <= args.tick_ms <= 10000:
        parser.error("--tick-ms deve estar entre 10 e 10000")
    for value in (args.prepare_timeout, args.phase_timeout):
        if not math.isfinite(value) or value < 0.1:
            parser.error("Prazos devem ser finitos e >= 0.1 segundo")
    if not 0 <= args.linger_ms <= 60000:
        parser.error("--linger-ms deve estar entre 0 e 60000")
    if args.fault != "none":
        if not 1 <= args.fault_player <= args.players:
            parser.error("--fault-player fora dos participantes")
        if not 1 <= args.fault_tick <= args.cycles:
            parser.error("--fault-tick fora dos ciclos do teste")
    return args


# 2. Enderecos e estado inicial identicos; cada moto segue por uma linha propria.
def load_players(args):
    ids = set(range(1, args.players + 1))
    if args.addresses is None:
        return {i: ("127.0.0.1", args.base_port + i) for i in sorted(ids)}
    raw = json.loads(args.addresses.read_text(encoding="utf-8-sig"))
    if type(raw) is not dict or set(raw) != {str(i) for i in ids}:
        raise ValueError("O JSON de enderecos deve conter exatamente os IDs esperados")
    # P2PManager validara cada IPv4 e porta.
    return {i: raw[str(i)] for i in sorted(ids)}


def build_initial_state(player_count, cycles):
    width, height = cycles + 2, player_count + 2
    grid = [["EMPTY" for _ in range(width)] for _ in range(height)]
    for y in range(height):
        for x in range(width):
            if x in (0, width - 1) or y in (0, height - 1):
                grid[y][x] = "WALL"
    players = [dict(id=i, x=1, y=i, direction="RIGHT", alive=True)
               for i in range(1, player_count + 1)]
    for player in players:
        grid[player["y"]][player["x"]] = player["id"]
    return dict(tick=0, players=players, grid=grid, result=None)


# 3. Simulador pequeno para o teste: destinos -> colisoes -> aplicacao simultanea.
def simulate_tick(snapshot, moves):
    """Recebe a copia fornecida pela sessao; nao usa rede, relogio ou teclado."""
    grid = snapshot["grid"]
    height, width = len(grid), len(grid[0])
    players = snapshot["players"]
    targets = {p["id"]: (moves[p["id"]]["x"], moves[p["id"]]["y"])
               for p in players if p["alive"]}
    counts = Counter(targets.values())
    deaths = set()
    # Nao modificar a grade enquanto estamos avaliando destinos.
    for player_id, (x, y) in targets.items():
        if (not (0 <= x < width and 0 <= y < height)
                or grid[y][x] != "EMPTY" or counts[(x, y)] > 1):
            deaths.add(player_id)
    directions = {delta: name for name, delta in DIRECTIONS.items()}
    for player in players:
        if not player["alive"]:
            continue
        x, y = targets[player["id"]]
        delta = (x - player["x"], y - player["y"])
        player["direction"] = directions[delta]
        grid[player["y"]][player["x"]] = player["id"]
        player["x"], player["y"] = x, y
        player["alive"] = player["id"] not in deaths
        if player["alive"]:
            grid[y][x] = player["id"]
    snapshot["tick"] += 1
    alive = [p["id"] for p in players if p["alive"]]
    snapshot["result"] = None if len(alive) > 1 else {
        "winner_id": alive[0] if alive else None,
    }
    return snapshot


# 4. Controlador do teste: apenas a thread principal consome messages.
def run(args):
    def log(text):
        print(f"[J{args.player_id}] {text}", flush=True)

    players = load_players(args)
    config = dict(rules_version="teste-session-linhas-v1", tick_ms=args.tick_ms,
                  prepare_timeout_ms=round(args.prepare_timeout * 1000),
                  phase_timeout_ms=round(args.phase_timeout * 1000))
    if args.fault == "config" and args.player_id == args.fault_player:
        config["injected_mismatch"] = True  # Altera somente o hash deste processo.
    injected = args.fault == "config" and args.player_id == args.fault_player

    def simulation(snapshot, moves):
        nonlocal injected
        result = simulate_tick(snapshot, moves)
        if (args.fault == "state" and args.player_id == args.fault_player
                and result["tick"] == args.fault_tick):
            result["grid"][0][0] = "EMPTY"  # Estado valido estruturalmente, mas diferente.
            injected = True
            log(f"FALHA INJETADA: estado diferente no ciclo {result['tick']}")
        return result

    session = GameSession(args.player_id, players, args.match_id, config,
                          build_initial_state(args.players, args.cycles), simulation)
    manager = P2PManager(args.player_id, players, args.match_id,
                         connect_window=args.prepare_timeout)
    sent, received = Counter(), Counter()
    previous_phase, previous_tick = None, -1
    finish_deadline = None
    error = None
    flushed = False
    closed = False
    # Limite externo adicional; os prazos da sessao continuam sendo a regra normal.
    run_deadline = time.monotonic() + args.prepare_timeout + args.cycles * (
        args.tick_ms / 1000 + 2 * args.phase_timeout) + args.linger_ms / 1000 + 5

    def transmit():
        for message in session.drain_outgoing():
            accepted = manager.broadcast(message)
            sent[message["type"]] += 1
            if args.verbose:
                log(f"TX {message['type']} ciclo={message['tick']} filas={accepted}")
            failures = [i for i, ok in accepted.items() if not ok]
            if failures:
                for i in failures:
                    session.notify_disconnected(i)
                return  # Nao enviar acoes antigas apos o aborto; ABORT sera drenado depois.

    try:
        log(f"Partida={args.match_id} participantes={sorted(players)} endereco={players[args.player_id]}")
        log(f"CONFIG_HASH={session.config_hash}")
        log("Movimentos automaticos; Ctrl+C solicita saida controlada.")
        manager.start()
        while True:
            if time.monotonic() >= run_deadline:
                raise TimeoutError("Limite externo de execucao excedido")
            if (session.phase == "CONNECTING"
                    and set(manager.connected_players()) == set(players) - {args.player_id}):
                log(f"MALHA COMPLETA: {manager.connected_players()}")
                session.start()
            for _ in range(64):
                try:
                    sender, message = manager.messages.get_nowait()
                except Empty:
                    break
                if message == {"type": "DISCONNECTED"}:
                    log(f"EVENTO LOCAL: canal de {sender} indisponivel")
                    session.notify_disconnected(sender)
                else:
                    received[message["type"]] += 1
                    if args.verbose:
                        log(f"RX de={sender} {message['type']} ciclo={message['tick']}")
                    session.handle_message(sender, message)
            session.update()
            if session.needs_local_move():
                if (args.fault == "leave" and args.player_id == args.fault_player
                        and session.current_tick == args.fault_tick):
                    injected = True
                    log(f"FALHA INJETADA: saida antes do ciclo {session.current_tick}")
                    session.leave()
                else:
                    player = next(p for p in session.snapshot()["players"]
                                  if p["id"] == args.player_id)
                    dx, dy = DIRECTIONS[player["direction"]]
                    session.submit_move(player["x"] + dx, player["y"] + dy)
            transmit()  # Inclusive o ultimo hash se a fase acaba de virar FINISHED.
            if session.phase != previous_phase:
                log(f"FASE: {session.phase}")
                previous_phase = session.phase
            if session.confirmed_tick != previous_tick:
                state = session.snapshot()
                log(f"CONFIRMADO ciclo={state['tick']} hash={protocol.calculate_hash(state)}")
                previous_tick = session.confirmed_tick
            if session.phase == "ABORTED":
                log(f"INTERROMPIDO: {session.abort_info}")
                break
            if session.phase == "FINISHED":
                if finish_deadline is None:
                    log(f"RESULTADO: {session.snapshot()['result']}")
                    finish_deadline = time.monotonic() + args.linger_ms / 1000
                if time.monotonic() >= finish_deadline:
                    break
            time.sleep(0.005)  # Devolve CPU; nao bloqueia esperando uma tecla.
    except KeyboardInterrupt:
        log("Ctrl+C: encerrando a participacao local.")
        session.leave()
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
        if session.phase not in TERMINAL_STATES:
            session.leave()
    finally:
        # Ainda pode haver LEAVE/ABORT ou hash final pendente na sessao.
        try:
            transmit()
            flushed = manager.flush(timeout=2.0)
        except Exception as exc:
            error = error or f"Falha no envio final: {exc}"
        try:
            manager.close()
            closed = True
            log("MANAGER ENCERRADO: workers aguardados.")
        except Exception as exc:
            error = error or f"Falha no encerramento: {exc}"
            traceback.print_exc()

    state = session.snapshot()
    success = (error is None and closed and session.phase == "FINISHED"
               and state["tick"] == args.cycles
               and state["result"] == {"winner_id": None})
    report = dict(player_id=args.player_id, phase=session.phase,
                  confirmed_tick=session.confirmed_tick,
                  final_hash=protocol.calculate_hash(state), config_hash=session.config_hash,
                  result=state["result"], abort_info=session.abort_info, error=error,
                  fault_injected=injected, accepted_broadcasts=dict(sent),
                  received_messages=dict(received), flushed=flushed, closed=closed,
                  transport_errors=manager.errors(), success=success)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    log("TESTE CONCLUIDO COM SUCESSO" if success else "TESTE TERMINOU SEM SUCESSO NORMAL")
    # Falha proposital tambem retorna 1: o guia informa o motivo esperado.
    return 0 if success else (2 if error else 1)


# 5. Ponto de entrada: importar o modulo nao inicia sockets nem executa a partida.
def main(argv=None):
    args = parse_args(argv)
    try:
        return run(args)
    except (ValueError, OSError, protocol.ProtocolError) as error:
        print(f"Configuracao invalida: {error}", flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

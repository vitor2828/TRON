"""Partida local P2P de diagnostico, sem alterar __main__.py e sem lobby.

"""

import argparse
from contextlib import nullcontext
import json
import os
from pathlib import Path
from queue import Empty
import sys
import time
import traceback

from .GameSession import GameSession, TERMINAL_STATES
from .p2p_manager import P2PManager
from . import protocolo
from .simulation import RULES_VERSION, DIRECTIONS, criar_estado_inicial, calcular_destino
from Render.renderFinal import Renderizador


# 1. Teclado nao bloqueante, com restauracao do terminal em Linux/macOS.
class Teclado:
    def __init__(self, automatico=False):
        self.automatico = automatico
        self._previous = None

    def __enter__(self):
        if self.automatico:
            return self
        if not sys.stdin.isatty():
            raise RuntimeError("Teclado exige terminal interativo; use --automatic")
        if os.name != "nt":
            import termios
            import tty
            self._previous = termios.tcgetattr(sys.stdin.fileno())
            try:
                tty.setcbreak(sys.stdin.fileno())
            except Exception:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._previous)
                raise
        return self

    def ler(self):
        if self.automatico:
            return []
        keys = []
        for _ in range(32):
            if os.name == "nt":
                import msvcrt
                if not msvcrt.kbhit():
                    break
                key = msvcrt.getwch()
                if key in ("\x00", "\xe0"):
                    if msvcrt.kbhit():
                        msvcrt.getwch()  # Ignora teclas especiais do Windows.
                    continue
            else:
                import select
                if not select.select([sys.stdin], [], [], 0)[0]:
                    break
                key = os.read(sys.stdin.fileno(), 1).decode("utf-8", errors="ignore")
            keys.append(key.lower())
        return keys

    def __exit__(self, *_):
        if self._previous is not None:
            import termios
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._previous)


KEYS = {"w": "UP", "a": "LEFT", "s": "DOWN", "d": "RIGHT"}


# 2. Parametros de rede e arena; o mesmo contrato deve ser usado por todos.
def argumentos(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("player_id", type=int)
    parser.add_argument("--players", type=int, choices=(3, 4), default=3)
    parser.add_argument("--match-id", default="jogo-local-p2p")
    parser.add_argument("--base-port", type=int, default=6000)
    parser.add_argument("--addresses", type=Path)
    parser.add_argument("--width", type=int, default=20)
    parser.add_argument("--height", type=int, default=15)
    parser.add_argument("--tick-ms", type=int, default=250)
    parser.add_argument("--prepare-timeout", type=int, default=60)
    parser.add_argument("--phase-timeout", type=int, default=8)
    parser.add_argument("--max-runtime", type=int, default=300)
    parser.add_argument("--linger-ms", type=int, default=1000)
    parser.add_argument("--automatic", action="store_true")
    parser.add_argument("--no-render", action="store_true")
    parser.add_argument("--script", type=Path, help="JSON: ID -> ciclo -> direcao")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--leave-at", type=int, help="Injecao local de saida antes deste ciclo")
    parser.add_argument("--recovery-timeout", type=int, default=15)
    parser.add_argument("--probe-timeout-ms", type=int, default=1500)
    parser.add_argument("--crash-at", type=int, help="Encerra abruptamente no ciclo indicado (teste)")
    parser.add_argument("--crash-phase", choices=("before_move", "after_move", "hashes", "spectator"),
                        default="before_move")
    args = parser.parse_args(argv)
    if not 1 <= args.player_id <= args.players:
        parser.error("ID fora dos participantes")
    if not 4 <= args.width <= 200 or not 4 <= args.height <= 100:
        parser.error("Dimensoes permitidas: largura 4..200, altura 4..100")
    if not 1 <= args.base_port <= 65535-args.players:
        parser.error("Porta base invalida")
    if not 10 <= args.tick_ms <= 10000:
        parser.error("tick-ms deve estar entre 10 e 10000")
    if min(args.prepare_timeout, args.phase_timeout, args.max_runtime) <= 0:
        parser.error("Prazos devem ser positivos")
    if not 0 <= args.linger_ms <= 60000:
        parser.error("linger-ms invalido")
    if args.leave_at is not None and args.leave_at < 1:
        parser.error("leave-at deve ser >=1")
    if args.recovery_timeout <= 0 or args.probe_timeout_ms <= 0:
        parser.error("Prazos de recuperacao devem ser positivos")
    if args.crash_at is not None and args.crash_at < 1:
        parser.error("crash-at deve ser >=1")
    return args


def carregar_enderecos(args):
    ids = range(1, args.players+1)
    if args.addresses is None:
        return {i: ("127.0.0.1", args.base_port+i) for i in ids}
    data = json.loads(args.addresses.read_text(encoding="utf-8-sig"))
    if type(data) is not dict or set(data) != {str(i) for i in ids}:
        raise ValueError("Mapa de enderecos deve conter exatamente os IDs da partida")
    return {i: data[str(i)] for i in ids}


def carregar_script(path, player_count):
    if path is None:
        return {}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if type(data) is not dict:
        raise ValueError("Script deve ser objeto ID -> ciclo -> direcao")
    result = {}
    for player_id, entries in data.items():
        if player_id not in {str(i) for i in range(1,player_count+1)} or type(entries) is not dict:
            raise ValueError("Jogador ou tabela invalida no script")
        result[int(player_id)] = {}
        for tick, direction in entries.items():
            if not tick.isdecimal() or int(tick) < 1 or str(int(tick)) != tick:
                raise ValueError("Ciclo do script deve ser inteiro positivo sem zeros a esquerda")
            if type(direction) is not str or direction not in DIRECTIONS:
                raise ValueError("Direcao invalida no script")
            result[int(player_id)][int(tick)] = direction
    return result


# 3. Loop de integracao: um unico consumidor da fila e um unico escritor da tela.
def executar(args):
    players = carregar_enderecos(args)
    script = carregar_script(args.script, args.players)
    config = dict(rules_version=RULES_VERSION, tick_ms=args.tick_ms,
                  prepare_timeout_ms=args.prepare_timeout*1000,
                  phase_timeout_ms=args.phase_timeout*1000,
                  recovery_timeout_ms=args.recovery_timeout*1000,
                  probe_timeout_ms=args.probe_timeout_ms)
    initial = criar_estado_inicial(players, args.width, args.height)
    session = GameSession(args.player_id, players, args.match_id, config, initial)
    manager = P2PManager(args.player_id, players, args.match_id,
                         connect_window=args.prepare_timeout)
    history, actions = [], []
    pending = None
    last_tick = -1
    last_phase = None
    finish_deadline = None
    error = None
    closed = False
    start = time.monotonic()

    def transmitir():
        for message in session.drain_outgoing():
            for recipient in session.recipients_for(message):
                if not manager.send_to(recipient, message):
                    session.notify_disconnected(recipient)

    try:
        # O modo automatico torna possivel repetir o teste sem teclado real.
        visual = nullcontext(None) if args.no_render else Renderizador(fps=15)
        with Teclado(args.automatic) as keyboard, visual as screen:
            manager.start()
            while True:
                if time.monotonic()-start > args.max_runtime:
                    raise TimeoutError("Limite externo de execucao excedido")
                for key in keyboard.ler():
                    if key in ("q", "\x1b", "\x03"):
                        session.leave()
                    elif key in KEYS and session.local_player()["alive"]:
                        desired = KEYS[key]
                        try:
                            calcular_destino(session.snapshot(), args.player_id, desired)
                        except ValueError:
                            continue  # Ignora inversao; nao altera a proposta ja enviada.
                        pending = desired
                if (session.phase == "CONNECTING" and
                        set(manager.connected_players()) == set(players)-{args.player_id}):
                    session.start()
                for _ in range(64):
                    try:
                        sender, message = manager.messages.get_nowait()
                    except Empty:
                        break
                    if message == {"type": "DISCONNECTED"}:
                        session.notify_disconnected(sender)
                    else:
                        session.handle_message(sender, message)
                session.update()
                if (args.crash_at and args.crash_phase == "before_move"
                        and session.current_tick >= args.crash_at and session.needs_local_move()):
                    print("FALHA INJETADA antes do MOVE", flush=True)
                    os._exit(77)
                if session.needs_local_move():
                    tick = session.current_tick
                    if args.leave_at == tick:
                        session.leave()
                    else:
                        direction = script.get(args.player_id, {}).get(tick, pending)
                        try:
                            calcular_destino(session.snapshot(), args.player_id, direction)
                        except ValueError:
                            if tick in script.get(args.player_id, {}):
                                raise  # Um roteiro invalido e erro do teste, nao e ignorado.
                            direction = None  # Tecla pendente pode ficar invalida apos confirmar.
                        session.submit_direction(direction)
                        actions.append(dict(tick=tick, data=direction))
                        pending = None
                transmitir()
                if args.crash_at and session.current_tick >= args.crash_at:
                    trigger = ((args.crash_phase == "after_move" and args.player_id in
                                session._moves.get(session.current_tick, {})) or
                               (args.crash_phase == "hashes" and session.phase == "WAITING_HASHES") or
                               (args.crash_phase == "spectator" and not session.local_player()["alive"]))
                    if trigger:
                        manager.flush(timeout=1)
                        print("FALHA INJETADA " + args.crash_phase, flush=True)
                        os._exit(77)
                state = session.snapshot()
                if (session.epoch, session.confirmed_tick) != last_tick:
                    record = dict(epoch=session.epoch, tick=state["tick"], hash=protocolo.calculate_hash(state))
                    history.append(record)
                    if args.no_render:
                        print(f"J{args.player_id} CONFIRMADO {record}", flush=True)
                    last_tick = (session.epoch, session.confirmed_tick)
                if args.no_render:
                    if session.phase != last_phase:
                        print(f"J{args.player_id} FASE {session.phase}", flush=True)
                else:
                    screen.desenhar(state, args.player_id, fase=session.phase,
                                    conectados=manager.connected_players(), abort_info=session.abort_info)
                last_phase = session.phase
                if session.phase == "ABORTED":
                    break
                if session.phase in {"FINISHED", "LEFT"}:
                    if finish_deadline is None:
                        finish_deadline = time.monotonic()+args.linger_ms/1000
                    if time.monotonic() >= finish_deadline:
                        break
                time.sleep(.005)
    except KeyboardInterrupt:
        session.leave()
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        if session.phase not in TERMINAL_STATES:
            session.leave()
        traceback.print_exc()
    finally:
        try:
            transmitir()
            manager.flush(timeout=2)
        except Exception as exc:
            error = error or str(exc)
        try:
            manager.close()
            closed = True
        except Exception as exc:
            error = error or str(exc)
    final = session.snapshot()
    report = dict(player_id=args.player_id, phase=session.phase,
                  confirmed_tick=session.confirmed_tick, epoch=session.epoch,
                  coordinator_id=session.coordinator_id, active_participants=sorted(session.active_participants),
                  recoveries=session.recovery_log, result=final["result"],
                  final_hash=protocolo.calculate_hash(final), history=history,
                  actions=actions, abort_info=session.abort_info, error=error,
                  closed=closed, transport_errors=manager.errors())
    if args.report:
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding="utf-8")
    print(f"J{args.player_id}: {session.phase}, ciclo={session.confirmed_tick}, "
          f"resultado={final['result']}, erro={error}",flush=True)
    return 2 if error else (0 if session.phase in {"FINISHED", "LEFT"} and closed else 1)


def main(argv=None):
    args = argumentos(argv)
    try:
        return executar(args)
    except (ValueError, OSError) as error:
        print(f"Falha de configuracao: {error}",file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

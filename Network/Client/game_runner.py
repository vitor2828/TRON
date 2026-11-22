"""Loop reutilizavel da partida; entrada e o resultado validado do lobby."""
from contextlib import nullcontext
import json
import os
from pathlib import Path
from queue import Empty
import sys
import time
import traceback
from types import SimpleNamespace

from .GameSession import GameSession, TERMINAL_STATES
from .p2p_manager import P2PManager
from . import protocolo
from .simulation import RULES_VERSION, DIRECTIONS, criar_estado_inicial, calcular_destino
from ...Render.renderFinal import Renderizador


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



def executar_partida(result, *, automatic=False, no_render=False, report=None,
                     max_runtime=300, linger_ms=1000, crash_at=None,
                     crash_phase="before_move", leave_at=None):
    """Consome o listener de LobbyResult e devolve um relatorio da partida.

    Usa a sessao v3 existente. Nao importa nem altera teste_jogo_p2p.py.
    """
    try:
        if result.listener is None:
            raise ValueError("Listener ja transferido ou fechado")
        if max_runtime <= 0 or linger_ms < 0:
            raise ValueError("Prazos locais invalidos")
        options = dict(automatic=automatic, no_render=no_render,
                       report=Path(report) if report else None, max_runtime=max_runtime,
                       linger_ms=linger_ms, crash_at=crash_at, crash_phase=crash_phase,
                       leave_at=leave_at)
        return _executar_partida(result, options)
    finally:
        result.close()  # Cobre erro de validacao/construcao anterior ao start.


# 3. Loop de integracao: um unico consumidor da fila e um unico escritor da tela.
def _executar_partida(result, options):
    args = SimpleNamespace(player_id=result.player_id, **options)
    players = result.players
    script = {}
    config = result.config
    initial = criar_estado_inicial(players, config["width"], config["height"])
    session = GameSession(result.player_id, players, result.match_id, config, initial)
    manager = P2PManager(result.player_id, players, result.match_id,
                         connect_window=config["prepare_timeout_ms"] / 1000)
    listener_port = result.listener.getsockname()[1]
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
            manager.start(listener=result.listener)
            result.listener = None  # Ownership passou ao manager; nenhuma reabertura da porta.
            while True:
                if time.monotonic()-start > args.max_runtime:
                    raise TimeoutError("Limite externo de execucao excedido")
                try:
                    keys = keyboard.ler()
                except KeyboardInterrupt:
                    keys = ["q"]
                for key in keys:
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
    report = dict(match_id=result.match_id, listener_port=listener_port,
                  players={str(i): list(address) for i,address in players.items()},
                  player_id=args.player_id, phase=session.phase,
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
    return report


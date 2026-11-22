"""Novo teste: lobby original adaptado -> jogo P2P. Executar na pasta TRON."""
import argparse
from collections import deque

from .lobbyWIn import lobby, LobbyCancelled
from .lobby_adapter import adaptar_lobby
from .game_runner import executar_partida


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-host", default="127.0.0.1")
    parser.add_argument("--server-port", type=int, default=5000)
    parser.add_argument("--name")
    parser.add_argument("--action", choices=("create", "join"))
    parser.add_argument("--room")
    parser.add_argument("--width", type=int, default=40)
    parser.add_argument("--height", type=int, default=25)
    parser.add_argument("--tick-ms", type=int, default=250)
    parser.add_argument("--automatic", action="store_true")
    parser.add_argument("--no-render", action="store_true")
    parser.add_argument("--report")
    parser.add_argument("--max-runtime", type=int, default=300)
    parser.add_argument("--linger-ms", type=int, default=1000)
    parser.add_argument("--crash-at", type=int)
    parser.add_argument("--crash-phase", choices=("before_move", "after_move", "hashes", "spectator"), default="before_move")
    parser.add_argument("--leave-at", type=int)
    args = parser.parse_args()
    if args.action == "join" and (args.room is None or not args.room.isdecimal()):
        parser.error("--action join exige --room numerico")
    if not 1 <= args.server_port <= 65535 or args.max_runtime <= 0 or args.linger_ms < 0:
        parser.error("Porta ou prazos invalidos")
    if not 4 <= args.width <= 200 or not 4 <= args.height <= 100 or not 10 <= args.tick_ms <= 10000:
        parser.error("Dimensoes ou intervalo invalidos")
    if any(v is not None and v < 1 for v in (args.crash_at, args.leave_at)):
        parser.error("Ciclo de teste deve ser positivo")
    entrada = None
    if args.action:
        # Usa o MESMO trata_menu(), apenas fornece as linhas sem teclado fisico.
        linhas = deque(["1"] if args.action == "create" else ["3", args.room])
        def entrada(prompt, evento):
            if linhas:
                return linhas.popleft()
            if not evento.wait(args.max_runtime):
                raise TimeoutError("Prazo da entrada automatizada do lobby excedido")
            return None
    try:
        retorno = lobby(args.server_host, args.server_port, nome=args.name, entrada=entrada)
        result = adaptar_lobby(retorno, dict(width=args.width, height=args.height, tick_ms=args.tick_ms))
        print(f"LOBBY_PRONTO partida={result.match_id} jogador={result.player_id} "
              f"porta={result.listener.getsockname()[1]}", flush=True)
        report = executar_partida(result, automatic=args.automatic, no_render=args.no_render,
            report=args.report, max_runtime=args.max_runtime, linger_ms=args.linger_ms,
            crash_at=args.crash_at, crash_phase=args.crash_phase, leave_at=args.leave_at)
        return 2 if report["error"] else (0 if report["phase"] in {"FINISHED", "LEFT"} and report["closed"] else 1)
    except (LobbyCancelled, KeyboardInterrupt):
        print("Teste cancelado", flush=True)
        return 1
    except (ValueError, OSError, EOFError, RuntimeError) as error:
        print(f"Falha na integracao: {error}", flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

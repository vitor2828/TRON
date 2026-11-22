import argparse
from pathlib import Path

from .Network.Client.lobbyWIn import lobby, LobbyCancelled
from .Network.Client.lobby_adapter import adaptar_lobby, DEFAULT_CONFIG
from .Network.Client.game_runner import executar_partida


def argumentos(argv=None):
    parser = argparse.ArgumentParser(description="TRON: lobby e jogo P2P para quatro jogadores")
    parser.add_argument("--server-host", default="127.0.0.1", help="IP/nome da maquina do lobby")
    parser.add_argument("--server-port", type=int, default=5000)
    parser.add_argument("--name", help="Nome no lobby; se ausente, sera perguntado")
    parser.add_argument("--width", type=int, default=DEFAULT_CONFIG["width"])
    parser.add_argument("--height", type=int, default=DEFAULT_CONFIG["height"])
    parser.add_argument("--tick-ms", type=int, default=DEFAULT_CONFIG["tick_ms"])
    parser.add_argument("--report", type=Path, help="Arquivo JSON opcional de diagnostico")
    parser.add_argument("--max-runtime", type=int, default=3600,
                        help="Limite local da partida em segundos (padrao: 3600)")
    args = parser.parse_args(argv)
    if not args.server_host.strip() or not 1 <= args.server_port <= 65535:
        parser.error("Endereco ou porta do lobby invalido")
    if not 4 <= args.width <= 200 or not 4 <= args.height <= 100:
        parser.error("Dimensoes permitidas: largura 4..200, altura 4..100")
    if not 10 <= args.tick_ms <= 10000 or args.max_runtime <= 0:
        parser.error("tick-ms deve estar entre 10 e 10000; max-runtime deve ser positivo")
    return args


def main(argv=None):
    args = argumentos(argv)
    retorno = resultado = None
    try:
        # 1. O lobby conserva seu retorno [listener, informacoes_partida].
        retorno = lobby(args.server_host, args.server_port, nome=args.name)
        # 2. A adaptacao fica fora do codigo dos colegas.
        resultado = adaptar_lobby(retorno, dict(width=args.width, height=args.height,
                                               tick_ms=args.tick_ms))
        # 3. O runner assume a escuta e executa o fluxo P2P ja validado.
        relatorio = executar_partida(resultado, report=args.report,
                                     max_runtime=args.max_runtime)
        if relatorio["error"]:
            return 2
        return 0 if relatorio["phase"] in {"FINISHED", "LEFT"} and relatorio["closed"] else 1
    except LobbyCancelled:
        print("Participacao no lobby cancelada.")
        return 1
    except KeyboardInterrupt:
        print("Execucao interrompida.")
        return 130
    except (ValueError, OSError, EOFError, RuntimeError) as error:
        print(f"Nao foi possivel executar a partida: {error}")
        return 2
    finally:
        if resultado is not None:
            resultado.close()  # Inofensivo se o listener ja passou ao manager.
        elif retorno is not None:
            retorno[0].close()  # Cobre falha de adaptacao anterior a transferencia.


if __name__ == "__main__":
    raise SystemExit(main())

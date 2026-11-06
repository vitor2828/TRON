import sys
import threading

from queue import Empty

from .p2p_manager import P2PManager


PLAYERS = {
    1: ("127.0.0.1", 6001),
    2: ("127.0.0.1", 6002),
    3: ("127.0.0.1", 6003),
    4: ("127.0.0.1", 6004)
}

def print_messages(p2p):

    while p2p.running:

        try:
            sender_id, message = p2p.messages.get(timeout=0.5)

        except Empty:
            continue

        if not p2p.running:
            break

        print(f"\nJogador {sender_id}: {message}")

def main():

    if len(sys.argv) != 2:
        print("Uso: python -m Network.test_mesh <player_id>")
        sys.exit(1)

    try:
        player_id = int(sys.argv[1])

    except ValueError:
        print("O ID deve ser um número inteiro.")
        sys.exit(1)

    if player_id not in PLAYERS:
        print("ID inválido.")
        sys.exit(1)

    p2p = P2PManager(player_id, PLAYERS)
    connect_thread = None
    print_thread = None

    try:
        p2p.start()

        connect_thread = threading.Thread(
            target=p2p.connect_to_peers,
        )

        connect_thread.start()

        print_thread = threading.Thread(
            target=print_messages,
            args=(p2p,),
        )

        print_thread.start()

        print("Comandos disponíveis:")
        print("  /list")
        print("  /send <id> <mensagem>")
        print("  /all <mensagem>")
        print("  /quit")

        while True:

            command = input(f"Jogador {player_id}> ").strip()

            if command == "/quit":
                p2p.broadcast({
                    "type": "QUIT",
                    "player_id": player_id
                })

                break

            elif command == "/list":
                print("Conectados:", p2p.connected_players())

            elif command.startswith("/all "):
                text = command[5:]

                p2p.broadcast({
                    "type": "MESSAGE",
                    "text": text
                })

            elif command.startswith("/send "):

                parts = command.split(" ", 2)

                if len(parts) != 3:
                    print("Uso: /send <id> <mensagem>")
                    continue

                try:
                    remote_id = int(parts[1])

                except ValueError:
                    print("ID inválido.")
                    continue

                p2p.send_to(remote_id, {
                    "type": "MESSAGE",
                    "text": parts[2]
                })

            else:
                print("Comando inválido.")

    except KeyboardInterrupt:
        print("\nEncerrando jogador...")

    finally:
        p2p.close()

        if connect_thread is not None:
            connect_thread.join()

        if print_thread is not None:
            print_thread.join()


if __name__ == "__main__":
    main()
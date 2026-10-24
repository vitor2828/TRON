from .Entities.entities import Cor, Arena, Jogador, Direcao
from .Render import render
import time
from blessed import Terminal


def main():
    ######################################################################3333
    # (INICIO)Parte do lobby de criação e geração de jogadores

    LARGURA = 50
    ALTURA = 50

    #COORDENADAS_INICIAIS = [(1, 1), (1, ALTURA-2), (LARGURA-2, 1), (LARGURA-2, ALTURA-2)]

    #DIRECOES_INICIAIS = [Direcao.RIGHT, Direcao.DOWN, Direcao.UP, Direcao.LEFT]

    jogador1 = Jogador(1, 1, Cor.RED, Direcao.RIGHT)
    jogador2 = Jogador(1, ALTURA-2, Cor.GREEN, Direcao.UP)
    jogador3 = Jogador(LARGURA-2, 1, Cor.YELLOW, Direcao.DOWN)
    jogador4 = Jogador(LARGURA-2, ALTURA-2, Cor.BLUE, Direcao.LEFT)

    jogadores = []
    jogadores.append(jogador1)
    jogadores.append(jogador2)
    jogadores.append(jogador3)
    jogadores.append(jogador4)

    # (FIM)
    ###################################################################3

    arena = Arena(jogadores, LARGURA, ALTURA)

    term = Terminal()
    with term.cbreak():
        while True:
            # Pooling de teclado
            if jogador1.get_isAlive():
                tecla = term.inkey(timeout=0.1)
                if tecla.lower() == "w" and jogador1.direcao != Direcao.DOWN:
                    jogador1.set_direcao(Direcao.UP)
                elif tecla.lower() == "a" and jogador1.direcao != Direcao.RIGHT:
                    jogador1.set_direcao(Direcao.LEFT)
                elif tecla.lower() == "s" and jogador1.direcao != Direcao.UP:
                    jogador1.set_direcao(Direcao.DOWN)
                elif tecla.lower() == "d" and jogador1.direcao != Direcao.LEFT:
                    jogador1.set_direcao(Direcao.RIGHT)

            # Renderização
            render.limpar_tela()
            arena.show()

            vivos = 0
            for j in jogadores:
                if j.get_isAlive():
                    vivos += 1

            if vivos <= 1:
                print("Jogo encerrou")
                break

            arena.atualiza()
            time.sleep(0.5)


if __name__ == "__main__":
    main()
# classe com informacoes basicas de jogador, tambem para os oponentes
from enum import Enum
from random import randint

class Cor(Enum): # talvez seja interessante colocar o valor hexadecimal da cor para facilitar para a interface
    BLANK = -1
    AZUL = 0
    VERMELHO = 1
    AMARELO = 2
    VERDE = 3

class Direcao(Enum):
    DIREITA = 0
    BAIXO = 1
    ESQUERDA = 2
    CIMA = 3

class Jogador:

    pos_x = 0 # o valor deve ser um numero inteiro, pois eh um endereco da grid
    pos_y = 0 # por enquanto, o valor estah em zero. No entanto, deve ser um valor aleatorio
    # cujos limites sao as dimensoes da grid. Deve haver um metodo para 

    cor = Cor.BLANK # a cor serah definida ao entrar no lobby e passada na instancia do objeto. Acho que quatro opcoes sao ideais (ex.: azul, vermelho, amarelo, verde)
    direcao = randint(0, 3) # seta aleatoriamente uma das direcoes
    isAlive = True

    def __init__(self, pos_x, pos_y, cor):
        self.pos_x = pos_x
        self.pos_y = pos_y
        self.cor = cor

    def atualiza_pos(self):
        match self.direcao:
            case Direcao.DIREITA:
                self.pos_x += 1

            case Direcao.BAIXO:
                self.pos_y += 1

            case Direcao.ESQUERDA:
                self.pos_x -= 1

            case Direcao.CIMA:
                self.pos_y -= 1






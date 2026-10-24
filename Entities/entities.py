class Cor:
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    RESET = "\033[0m"

class Direcao:
    UP = (0, -1)
    DOWN = (0, 1)
    LEFT = (-1, 0)
    RIGHT = (1, 0)
    NEUTRO = (0, 0)

class Jogador:
    def __init__(self, pos_x: int, pos_y: int, cor: Cor, direcao: Direcao):
        self.pos_x = pos_x
        self.pos_y = pos_y
        self.cor = cor
        self.isAlive = True
        self.direcao = direcao

    def get_isAlive(self):
        return self.isAlive

    def set_direcao(self, direcao):
        self.direcao = direcao

    def move(self):
        dx, dy = self.direcao
        self.pos_x += dx
        self.pos_y += dy

class Arena:
    def __init__(self, jogadores, largura, altura):
        self.jogadores = jogadores
        self.LARGURA = largura
        self.ALTURA = altura
        self.grid = [[" " for _ in range(self.LARGURA)] for _ in range(self.ALTURA)]
        for i in range(self.ALTURA):
            for j in range(self.LARGURA):
                if i == 0 or j == 0 or i == self.ALTURA-1 or j == self.LARGURA-1:
                    self.grid[i][j] = "/"

    def show(self):
        for j in self.jogadores:
            if(j.get_isAlive()):
                self.grid[j.pos_y][j.pos_x] = f"{j.cor}&{Cor.RESET}"

        for i in range(self.ALTURA):
            for j in range(self.LARGURA):
                print(self.grid[i][j], end=" ")
            print()

    def atualiza(self):
        for j in self.jogadores:
            if(j.get_isAlive()):
                self.grid[j.pos_y][j.pos_x] = f"{j.cor}*{Cor.RESET}"
                j.move()
                if "/" in self.grid[j.pos_y][j.pos_x] or "*" in self.grid[j.pos_y][j.pos_x]:
                    j.isAlive = False
    

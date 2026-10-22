# TRON

Uma implementação multijogador de TRON - Redes de Computadores UnB 26.2

## Componentes do Grupo

Danilo Lins Castro
Eduardo Sankievicz Lima
João Pedro Barros Silva Azevedo
João Vítor da Silva Lima

## Requisitos Gerais

### Arquitetura híbrida:
O sistema deverá conter um servidor de lobby, responsável por registrar jogadores, criar e listar partidas e permitir que os jogadores descubram os demais participantes de uma partida. Uma vez iniciada a partida, a comunicação do jogo deverá ocorrer diretamente entre os pares, sem passar pelo servidor de lobby. A falha ou o encerramento do servidor de lobby não poderá interromper as partidas em andamento.

### Número de jogadores:
Cada partida deverá suportar no mínimo 3 e no máximo 4 jogadores, cada um executando em um processo separado.

### Protocolo de aplicação: 
O grupo deverá projetar e especificar o protocolo de aplicação utilizado tanto na comunicação com o lobby quanto na comunicação entre os pares. A especificação deverá descrever os tipos de mensagens, o formato e o significado de cada campo, a máquina de estados de cada participante e o tratamento de mensagens inválidas, inesperadas ou ausentes. A especificação deverá ser escrita de forma que outro grupo conseguisse implementar um participante compatível apenas a partir dela.

### Protocolo de transporte:
O grupo deverá escolher o protocolo de transporte (TCP, UDP ou uma combinação de ambos) e justificar essa escolha com base nas características do jogo. Caso o protocolo de transporte escolhido não ofereça alguma garantia necessária ao jogo, essa garantia deverá ser implementada pelo grupo na camada de aplicação.

### Tolerância a falhas:
A desconexão ou falha de um jogador durante a partida deverá ser detectada e tratada, sem que os demais jogadores fiquem travados indefinidamente. O comportamento adotado em cada caso de falha deverá ser definido pelo grupo e descrito na especificação.

### Configuração:
Endereços e portas do lobby e dos jogadores deverão ser configuráveis, e não fixos no código. O sistema deverá funcionar com os processos executando em máquinas diferentes de uma mesma rede.

### Bibliotecas:
A comunicação deverá ser implementada diretamente com a API de sockets da linguagem escolhida. Não será permitido o uso de bibliotecas ou frameworks que automatizem a comunicação em rede, a sincronização de estado ou o multijogador, como bibliotecas de rede de motores de jogos, bibliotecas de RPC, middlewares de mensageria ou WebSockets. Será permitido utilizar bibliotecas auxiliares apenas para funcionalidades que não sejam o foco do trabalho, como interface no terminal, parsing de JSON, serialização simples de dados, funções de hash e geração de logs. A linguagem de programação é de livre escolha do grupo.

## Requisitos Específicos

A primeira opção consiste na implementação de um jogo multijogador em tempo real, no qual cada jogador controla uma moto que se desloca continuamente por uma arena em formato de grade. As regras do jogo são as seguintes:

1. A arena é uma grade retangular, com dimensões definidas pelo grupo, e cada jogador inicia em uma posição e direção distintas.
2. O jogo avança em ciclos de tempo fixos. A cada ciclo, todas as motos andam exatamente uma célula na direção em que estão. A taxa de ciclos é definida pelo grupo.
3. O jogador não pode parar nem alterar a velocidade da moto. A única ação possível é mudar a direção para cima, baixo, esquerda ou direita, sem inverter o sentido em que a moto se desloca.
4. Toda célula pela qual uma moto passa fica ocupada permanentemente pelo seu rastro.
5. Uma moto é eliminada ao entrar em uma célula ocupada por qualquer rastro, ao sair da arena ou ao colidir com outra moto. Se duas ou mais motos entrarem na mesma célula no mesmo ciclo, todas elas são eliminadas.
6. O rastro de uma moto eliminada permanece na arena.
7. Vence o último jogador restante. Se todos os jogadores restantes forem eliminados no mesmo ciclo, a partida termina empatada.

Além dos requisitos comuns, esta opção deverá atender aos requisitos a seguir. Todos os jogadores deverão chegar ao mesmo resultado sobre quais motos colidiram, em qual ciclo e quem venceu a partida. Divergências entre jogadores quanto a esses eventos não serão aceitas. O grupo deverá definir qual participante, ou qual mecanismo, determina o estado do jogo, e justificar essa decisão considerando os atrasos da rede.

A desconexão de um jogador deverá resultar na eliminação de sua moto, e a partida deverá continuar para os demais. Caso a arquitetura escolhida dependa de um participante específico para coordenar a partida, a falha desse participante deverá, no mínimo, encerrar a partida de forma controlada para todos os jogadores restantes.




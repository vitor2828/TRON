"""Apresentacao somente leitura dos snapshots confirmados do TRON.
"""

import os
import re
import sys
import time

from Network.Client.simulation import validar_estado, WALL


# 1. Somente apresentacao: nenhum destes textos/codigos entra no estado logico.
RESET = "\033[0m"
BOLD = "\033[1m"
PALETTE = ("\033[31m", "\033[32m", "\033[33m", "\033[34m")
HOME = "\033[H"
CLEAR = "\033[2J"
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"
ANSI_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
HEADS = {"UP": "^", "DOWN": "v", "LEFT": "<", "RIGHT": ">"}
PHASES = {
    "CONNECTING": "Conectando aos participantes",
    "PREPARING": "Conferindo a configuracao inicial",
    "WAITING_MOVES": "Aguardando propostas do ciclo",
    "WAITING_FINISH": "Confirmando que os demais receberam o resultado final",
    "WAITING_HASHES": "Conferindo os hashes do ciclo",
    "FAILURE_CHECK": "Verificando quais participantes estao responsivos",
    "RECOVERY_WAIT": "Falha detectada: aguardando o coordenador",
    "RECOVERY_COLLECT": "Recuperacao: verificando a falha e reunindo evidencias",
    "RECOVERY_ACK": "Recuperacao: conferindo a proposta de remocao",
    "RECOVERY_APPLY": "Recuperacao: aplicando decisao e aguardando retomada",
    "WAITING_LEAVE_ACK": "Aguardando confirmacao da sua saida",
    "LEFT": "Sua saida foi confirmada",
    "FINISHED": "Partida encerrada",
    "ABORTED": "Partida interrompida",
}
REASONS = {
    "CONFIG_MISMATCH": "Configuracoes iniciais diferentes",
    "INVALID_MESSAGE": "Mensagem invalida",
    "INVALID_MOVE": "Movimento invalido",
    "CONFLICTING_MOVE": "Propostas contraditorias",
    "STATE_MISMATCH": "Estados calculados diferentes",
    "PEER_LEFT": "Um participante saiu",
    "PEER_DISCONNECTED": "Um canal de comunicacao foi perdido",
    "COORDINATOR_LOST": "O coordenador ficou indisponivel",
    "RECOVERY_FAILED": "Nao foi possivel concluir a recuperacao",
    "TIMEOUT": "Prazo de espera excedido",
}


def _safe_text(value):
    """Detalhes de diagnostico nao podem introduzir comandos ANSI no terminal."""
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", str(value))[:160]


def _paint(text, color, enabled, *, bold=False):
    if not enabled:
        return text
    return f"{BOLD if bold else ''}{color}{text}{RESET}"


# 2. Snapshot -> texto. Funcao sem efeitos no terminal ou nos dados do chamador.
def formatar_estado(estado, id_local, *, fase=None, conectados=None,
                    abort_info=None, usar_cores=False):
    """Devolve um quadro completo; use session.snapshot() como estado.

    fase/conectados/abort_info sao metadados LOCAIS, fora do hash da arena.
    Os IDs conectados sao uma fotografia informativa, nao uma barreira READY.
    """
    validar_estado(estado)  # Somente leitura; nao constroi entidades nem copia a grade.
    ordenados = sorted(estado["players"], key=lambda jogador: jogador["id"])
    jogadores = {j["id"]: j for j in ordenados}
    cores = {j["id"]: PALETTE[index] for index, j in enumerate(ordenados)}
    if type(id_local) is not int or id_local not in jogadores:
        raise ValueError("id_local deve identificar um jogador do snapshot")
    if type(usar_cores) is not bool:
        raise ValueError("usar_cores deve ser booleano")
    if fase is None:
        fase = "FINISHED" if estado["result"] is not None else "WAITING_MOVES"
    if type(fase) is not str or fase not in PHASES:
        raise ValueError("Fase desconhecida")
    if fase == "FINISHED" and estado["result"] is None:
        raise ValueError("FINISHED exige um resultado confirmado")
    if abort_info is not None and type(abort_info) is not dict:
        raise ValueError("abort_info deve ser dicionario ou None")
    local = jogadores[id_local]
    linhas = [f"TRON | Voce: J{id_local} | Ciclo confirmado: {estado['tick']}",
              f"Estado: {PHASES[fase]}"]
    if conectados is not None:
        ids = list(conectados)
        if any(type(i) is not int or i not in jogadores or i == id_local for i in ids):
            raise ValueError("conectados deve conter apenas IDs remotos conhecidos")
        resumo = ", ".join(f"J{i}" for i in sorted(set(ids))) or "nenhum"
        linhas.append(f"Canais locais registrados: {resumo}")
    cabecas = {(j["x"], j["y"]): j for j in ordenados if j["alive"]}
    for y, row in enumerate(estado["grid"]):
        celulas = []
        for x, cell in enumerate(row):
            if (x, y) in cabecas:
                jogador = cabecas[(x, y)]
                char = _paint(HEADS[jogador["direction"]], cores[jogador["id"]], usar_cores,
                              bold=jogador["id"] == id_local)
            elif type(cell) is int:
                char = _paint("*", cores[cell], usar_cores)
            else:
                char = "#" if cell == WALL else " "
            celulas.append(char)
        linhas.append(" ".join(celulas))
    linhas.append("Legenda: # parede | * rastro | ^ v < > motos vivas")
    for jogador in ordenados:
        texto = (f"J{jogador['id']}{' (voce)' if jogador['id'] == id_local else ''}"
                 f": {'viva' if jogador['alive'] else 'eliminada'}"
                 f" | ({jogador['x']}, {jogador['y']})"
                 f" | {jogador['direction']}")
        linhas.append(_paint(texto, cores[jogador["id"]], usar_cores,
                             bold=jogador["id"] == id_local))
    if fase == "ABORTED":
        info = abort_info or {}
        reason = _safe_text(info.get("reason", "Nao informado"))
        linhas.append(f"Interrupcao: {REASONS.get(reason, reason)}")
        if info.get("detail"):
            linhas.append(f"Detalhe: {_safe_text(info['detail'])}")
    elif estado["result"] is not None:
        vencedor = estado["result"]["winner_id"]
        linhas.append("Resultado: EMPATE" if vencedor is None
                      else f"Resultado: J{vencedor} venceu")
    elif (fase.startswith("RECOVERY_") or fase == "FAILURE_CHECK"):
        linhas.append("Simulacao pausada. Aguardando acordo entre os participantes.")
    elif fase == "WAITING_LEAVE_ACK":
        linhas.append("Sua moto foi eliminada. Aguarde os demais confirmarem sua saida.")
    elif fase == "LEFT":
        linhas.append("Voce saiu. Os demais jogadores podem continuar a partida.")
    elif not local["alive"]:
        linhas.append("Sua moto foi eliminada. Voce continua acompanhando a partida.")
    if not (fase.startswith("RECOVERY_") or fase == "FAILURE_CHECK") and fase not in {"FINISHED", "ABORTED", "WAITING_LEAVE_ACK", "LEFT"}:
        linhas.append("WASD: propor direcao local | Q/Esc: solicitar saida")
    return "\n".join(linhas)


# 3. Preparacao de ANSI: sem subprocess e com restauracao do modo do Windows.
def _is_tty(stream):
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError):
        return False


def _prepare_ansi(stream):
    """Devolve (habilitado, funcao_de_restauracao). Nao usa bibliotecas externas."""
    noop = lambda: None
    if not _is_tty(stream) or os.environ.get("TERM") == "dumb":
        return False, noop
    if os.name != "nt":
        return True, noop
    try:
        import ctypes
        from ctypes import wintypes
        import msvcrt
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetConsoleMode.restype = wintypes.BOOL
        kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.SetConsoleMode.restype = wintypes.BOOL
        handle = wintypes.HANDLE(msvcrt.get_osfhandle(stream.fileno()))
        original = wintypes.DWORD()
        if not kernel.GetConsoleMode(handle, ctypes.byref(original)):
            return False, noop
        if not kernel.SetConsoleMode(handle, original.value | 0x0004):
            return False, noop
        return True, lambda: kernel.SetConsoleMode(handle, original.value)
    except (OSError, AttributeError, ValueError):
        return False, noop


def limpar_tela(*, file=None):
    """Compatibilidade: limpa um terminal ANSI; nao emite escapes em arquivos."""
    stream = sys.stdout if file is None else file
    enabled, restore = _prepare_ansi(stream)
    try:
        if enabled:
            stream.write(CLEAR + HOME)
            stream.flush()
        return enabled
    finally:
        restore()


# 4. Desenho avulso: util para diagnostico, StringIO ou um arquivo de texto.
def renderizar(estado, id_local, *, fase=None, conectados=None, abort_info=None,
               usar_cores=None, limpar=False, file=None):
    stream = sys.stdout if file is None else file
    if usar_cores is not None and type(usar_cores) is not bool:
        raise ValueError("usar_cores deve ser True, False ou None")
    enabled, restore = _prepare_ansi(stream)
    try:
        texto = formatar_estado(estado, id_local, fase=fase, conectados=conectados,
                                abort_info=abort_info,
                                usar_cores=enabled and usar_cores is not False)
        if limpar and enabled:
            stream.write(CLEAR + HOME)
        stream.write(texto + "\n")
        stream.flush()
    finally:
        try:
            if enabled:
                stream.write(RESET)
                stream.flush()
        finally:
            restore()


# 5. Renderizacao continua: limite de quadros, cursor e liberacao do terminal.
class Renderizador:
    def __init__(self, *, fps=15, usar_cores=True, file=None, clock=time.monotonic):
        if type(fps) not in (int, float) or not 0 < fps <= 120:
            raise ValueError("fps deve estar entre 0 e 120, excluindo zero")
        if type(usar_cores) is not bool or not callable(clock):
            raise ValueError("usar_cores deve ser booleano e clock deve ser funcao")
        self.stream = sys.stdout if file is None else file
        self.intervalo = 1 / fps
        self.usar_cores = usar_cores
        self._clock = clock
        self._ansi = False
        self._restore = lambda: None
        self._started = False
        self._closed = False
        self._last_time = float("-inf")
        self._last_text = None

    def _start(self):
        if self._closed:
            raise RuntimeError("Renderizador ja foi fechado")
        if not self._started:
            self._ansi, self._restore = _prepare_ansi(self.stream)
            self._started = True
            if self._ansi:
                try:
                    self.stream.write(CLEAR + HOME + HIDE_CURSOR)
                    self.stream.flush()
                except Exception:
                    self._restore()
                    self._closed = True
                    raise

    def _fit_terminal(self, texto):
        """Se o quadro nao cabe, mostra aviso em vez de deixar o desenho rolar."""
        if not self._ansi:
            return texto
        try:
            size = os.get_terminal_size(self.stream.fileno())
        except (OSError, AttributeError, ValueError):
            return texto
        linhas = texto.splitlines()
        largura = max(len(ANSI_PATTERN.sub("", line)) for line in linhas)
        altura = len(linhas)
        if largura < size.columns and altura < size.lines:
            return texto
        aviso = ["TRON: aumente o tamanho do terminal.",
                 f"Necessario: {largura + 1} colunas x {altura + 1} linhas.",
                 f"Atual: {size.columns} colunas x {size.lines} linhas."]
        return "\n".join(line[:max(0, size.columns-1)]
                         for line in aviso[:max(1, size.lines-1)])

    def desenhar(self, estado, id_local, *, fase=None, conectados=None,
                 abort_info=None, forcar=False):
        """Retorna True se escreveu um quadro; False se pulou por repeticao/FPS."""
        self._start()
        texto = formatar_estado(estado, id_local, fase=fase, conectados=conectados,
                                abort_info=abort_info,
                                usar_cores=self._ansi and self.usar_cores)
        texto = self._fit_terminal(texto)
        now = self._clock()
        if not forcar:
            if texto == self._last_text:
                return False
            terminal = fase in {"FINISHED", "ABORTED"} or (
                fase is None and estado["result"] is not None)
            if not terminal and now - self._last_time < self.intervalo:
                return False
        if self._ansi:
            # Reposiciona sem limpar tudo; apaga sobras de linhas anteriores.
            self.stream.write(HOME + "\n".join(line + "\033[K" for line in texto.splitlines())
                              + "\033[J")
        else:
            # Em arquivo/pipe, conserva quadros como texto comum, sem cursor ANSI.
            self.stream.write(texto + "\n\n")
        self.stream.flush()
        self._last_time, self._last_text = now, texto
        return True

    def fechar(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self._started and self._ansi:
                self.stream.write(RESET + SHOW_CURSOR + "\n")
                self.stream.flush()
        finally:
            self._restore()

    def __enter__(self):
        self._start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.fechar()
        return False  # Nao esconde excecoes da aplicacao.

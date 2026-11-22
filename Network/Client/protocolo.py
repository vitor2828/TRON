"""
Este modulo valida o FORMATO. A sessao valida estados, movimentos, barreiras,
duplicatas, timeouts e comparacao dos hashes. Nao abre sockets nem cria threads.

"""

from copy import deepcopy
import hashlib
import json
import re


# 1. Framing e vocabulario do protocolo.
PROTOCOL_VERSION = 3
DELIMITER = b"\n"
MAX_MESSAGE_SIZE = 4096  # Conteudo, sem o LF final.

HELLO = "HELLO"
READY = "READY"
MOVE = "MOVE"
STATE_HASH = "STATE_HASH"
LEAVE_REQUEST = "LEAVE_REQUEST"
IDLE = "IDLE"
LEAVE_ACK = "LEAVE_ACK"
FINISH_ACK = "FINISH_ACK"
FINISH_COMMIT = "FINISH_COMMIT"
ABORT = "ABORT"

FAILURE_REPORT = "FAILURE_REPORT"
RECOVERY_BEGIN = "RECOVERY_BEGIN"
RECOVERY_STATUS = "RECOVERY_STATUS"
RECOVERY_PROPOSE = "RECOVERY_PROPOSE"
RECOVERY_ACK = "RECOVERY_ACK"
RECOVERY_COMMIT = "RECOVERY_COMMIT"
RECOVERY_APPLIED = "RECOVERY_APPLIED"
RECOVERY_RESUME = "RECOVERY_RESUME"
RECOVERY_PROBE = "RECOVERY_PROBE"
RECOVERY_ALIVE = "RECOVERY_ALIVE"
RECOVERY_TYPES = frozenset({FAILURE_REPORT, RECOVERY_BEGIN, RECOVERY_STATUS,
    RECOVERY_PROPOSE, RECOVERY_ACK, RECOVERY_COMMIT, RECOVERY_APPLIED,
    RECOVERY_RESUME, RECOVERY_PROBE, RECOVERY_ALIVE})

MESSAGE_TYPES = RECOVERY_TYPES | frozenset({HELLO, READY, MOVE, STATE_HASH, LEAVE_REQUEST, IDLE, LEAVE_ACK, FINISH_ACK, FINISH_COMMIT, ABORT})
ABORT_REASONS = frozenset({
    "CONFIG_MISMATCH", "INVALID_MESSAGE", "INVALID_MOVE", "CONFLICTING_MOVE",
    "STATE_MISMATCH", "PEER_LEFT", "PEER_DISCONNECTED", "TIMEOUT",
    "COORDINATOR_LOST", "RECOVERY_FAILED",
})
ENVELOPE_FIELDS = frozenset({
    "version", "type", "match_id", "sender_id", "tick", "epoch", "data",
})
HASH_PATTERN = re.compile(r"[0-9a-f]{64}")


class ProtocolError(ValueError):
    """Mensagem ou conteudo fora do contrato do protocolo."""


# 2. Validadores pequenos, reutilizados pelas mensagens normais e de recuperacao.
def _require(condition, description):
    if not condition:
        raise ProtocolError(description)


def _check_fields(value, expected, label):
    _require(type(value) is dict, f"{label} deve ser um objeto")
    _require(set(value) == set(expected), f"Campos invalidos em {label}")


def _check_integer(value, label, minimum=None):
    _require(type(value) is int, f"{label} deve ser inteiro, sem booleanos")
    if minimum is not None:
        _require(value >= minimum, f"{label} deve ser >= {minimum}")


def _check_hash(value, label):
    _require(
        type(value) is str and HASH_PATTERN.fullmatch(value) is not None,
        f"{label} deve conter 64 caracteres hexadecimais minusculos",
    )


# 3. Validacao estrutural do envelope e do payload.
def validate_message(message):
    """Valida sem alterar o objeto; retorna None ou levanta ProtocolError."""
    _check_fields(message, ENVELOPE_FIELDS, "mensagem")
    _check_integer(message["version"], "version")
    _require(message["version"] == PROTOCOL_VERSION, "Versao nao suportada")

    kind = message["type"]
    _require(type(kind) is str and kind in MESSAGE_TYPES, "Tipo desconhecido")
    match_id = message["match_id"]
    _require(
        type(match_id) is str and 1 <= len(match_id) <= 64
        and match_id == match_id.strip(),
        "match_id deve ter 1 a 64 caracteres, sem espacos nas extremidades",
    )
    _check_integer(message["sender_id"], "sender_id", minimum=0)
    _check_integer(message["tick"], "tick", minimum=0)
    _check_integer(message["epoch"], "epoch", minimum=0)
    payload = message["data"]
    _require(type(payload) is dict, "data deve ser um objeto")

    if kind in RECOVERY_TYPES:
        _validate_recovery(kind, payload)
    elif kind in {HELLO, LEAVE_REQUEST, IDLE}:
        _check_fields(payload, set(), "data")
    elif kind == READY:
        _check_fields(payload, {"config_hash"}, "data")
        _check_hash(payload["config_hash"], "config_hash")
    elif kind == MOVE:
        _check_fields(payload, {"x", "y"}, "data")
        # Negativos sao permitidos: sair da arena pode eliminar a moto.
        _check_integer(payload["x"], "x")
        _check_integer(payload["y"], "y")
    elif kind in {STATE_HASH, FINISH_ACK}:
        _check_fields(payload, {"state_hash"}, "data")
        _check_hash(payload["state_hash"], "state_hash")
    elif kind == FINISH_COMMIT:
        _check_fields(payload, {"state_hash", "ack_ids"}, "data")
        _check_hash(payload["state_hash"], "state_hash")
        ids = payload["ack_ids"]
        _require(type(ids) is list and 1 <= len(ids) <= 4 and
                 all(type(i) is int and i >= 0 for i in ids) and ids == sorted(set(ids)),
                 "ack_ids exige IDs unicos ordenados")
    elif kind == LEAVE_ACK:
        _check_fields(payload, {"player_id", "state_hash"}, "data")
        _check_integer(payload["player_id"], "player_id", minimum=0)
        _check_hash(payload["state_hash"], "state_hash")
    elif kind == ABORT:
        _check_fields(payload, {"reason", "related_tick"}, "data")
        reason = payload["reason"]
        _require(
            type(reason) is str and reason in ABORT_REASONS,
            "Motivo de interrupcao desconhecido",
        )
        if payload["related_tick"] is not None:
            _check_integer(payload["related_tick"], "related_tick", minimum=0)

    if kind in {HELLO, READY}:
        _require(message["tick"] == 0 and message["epoch"] == 0,
                 "HELLO e READY pertencem ao ciclo e epoca 0")
    elif kind in {MOVE, STATE_HASH, IDLE, LEAVE_REQUEST, LEAVE_ACK, FINISH_ACK, FINISH_COMMIT}:
        _require(message["tick"] >= 1, "Mensagem de ciclo exige tick >= 1")


def _validate_recovery(kind, data):
    common = {"recovery_id", "failed_id"}
    extra = {
        FAILURE_REPORT: set(),
        RECOVERY_BEGIN: {"members", "coordinator_id"},
        RECOVERY_STATUS: {"part", "total", "content"},
        RECOVERY_PROPOSE: {"base_tick", "base_hash", "state_hash", "members",
                           "coordinator_id", "new_epoch"},
        RECOVERY_ACK: {"state_hash"}, RECOVERY_COMMIT: {"state_hash"},
        RECOVERY_APPLIED: {"state_hash"}, RECOVERY_RESUME: {"state_hash"},
        RECOVERY_PROBE: {"recipient_id"}, RECOVERY_ALIVE: {"recipient_id"},
    }[kind]
    _check_fields(data, common | extra, "data")
    _require(type(data["recovery_id"]) is str and 1 <= len(data["recovery_id"]) <= 80,
             "recovery_id invalido")
    _check_integer(data["failed_id"], "failed_id", 0)
    if "members" in extra:
        ids = data["members"]
        _require(type(ids) is list and 1 <= len(ids) <= 4 and
                 all(type(i) is int and i >= 0 for i in ids) and ids == sorted(set(ids)),
                 "members exige IDs ordenados e unicos")
    for key in ("coordinator_id", "base_tick", "new_epoch", "recipient_id"):
        if key in extra:
            _check_integer(data[key], key, 0)
    for key in ("base_hash", "state_hash"):
        if key in extra:
            _check_hash(data[key], key)
    if kind == RECOVERY_STATUS:
        _check_integer(data["part"], "part", 0)
        _check_integer(data["total"], "total", 1)
        _require(data["part"] < data["total"] <= 64, "Partes invalidas")
        _require(type(data["content"]) is str and len(data["content"]) <= 1000,
                 "Fragmento de status invalido")


def make_recovery(kind, match_id, sender_id, tick, epoch, recovery_id, failed_id, **data):
    _require(kind in RECOVERY_TYPES, "Tipo de recuperacao invalido")
    return make_message(kind, match_id, sender_id, tick,
                        {"recovery_id": recovery_id, "failed_id": failed_id, **data},
                        epoch=epoch)


# 4. Construcao das mensagens sem repetir o envelope nos chamadores.
def make_message(kind, match_id, sender_id, tick, data, *, epoch=0):
    """Cria e valida uma mensagem, copiando recursivamente o payload."""
    _require(type(data) is dict, "data deve ser um objeto")
    message = {
        "version": PROTOCOL_VERSION,
        "type": kind,
        "match_id": match_id,
        "sender_id": sender_id,
        "tick": tick,
        "epoch": epoch,
        "data": deepcopy(data),
    }
    validate_message(message)
    return message


def make_hello(match_id, sender_id):
    return make_message(HELLO, match_id, sender_id, 0, {})


def make_ready(match_id, sender_id, config_hash):
    return make_message(READY, match_id, sender_id, 0, {"config_hash": config_hash})


def make_move(match_id, sender_id, tick, x, y):
    return make_message(MOVE, match_id, sender_id, tick, {"x": x, "y": y})


def make_state_hash(match_id, sender_id, tick, state_hash):
    return make_message(
        STATE_HASH, match_id, sender_id, tick, {"state_hash": state_hash},
    )


def make_idle(match_id, sender_id, tick):
    """Jogador morto, ainda conectado, declara participacao neste ciclo."""
    return make_message(IDLE, match_id, sender_id, tick, {})


def make_leave_request(match_id, sender_id, effective_tick):
    """Declaracao exclusiva de saida neste ciclo; substitui MOVE ou IDLE."""
    return make_message(LEAVE_REQUEST, match_id, sender_id, effective_tick, {})


def make_leave_ack(match_id, sender_id, tick, player_id, state_hash):
    """Emitido depois de confirmar o ciclo que removeu player_id."""
    return make_message(LEAVE_ACK, match_id, sender_id, tick,
                        {"player_id": player_id, "state_hash": state_hash})


def make_abort(match_id, sender_id, last_confirmed_tick, reason, related_tick=None):
    return make_message(
        ABORT, match_id, sender_id, last_confirmed_tick,
        {"reason": reason, "related_tick": related_tick},
    )


# 5. Serializacao deterministica, tambem utilizada para calcular hashes.
def _check_canonical_value(value):
    """Aceita JSON sem floats; rejeita objetos, tuplas, sets e chaves nao string."""
    if value is None or type(value) in {str, int, bool}:
        return
    if type(value) is list:
        for item in value:
            _check_canonical_value(item)
        return
    if type(value) is dict:
        for key, item in value.items():
            _require(type(key) is str, "Chaves JSON devem ser strings")
            _check_canonical_value(item)
        return
    raise ProtocolError(f"Tipo fora da representacao canonica: {type(value).__name__}")


def canonical_bytes(value):
   
    try:
        _check_canonical_value(value)
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
    except (RecursionError, UnicodeError) as error:
        raise ProtocolError("Conteudo recursivo, profundo ou Unicode invalido") from error


def calculate_hash(snapshot):
    """SHA-256 do snapshot padronizado; nao inclui envelope nem limite de 4096."""
    _require(type(snapshot) is dict, "O snapshot deve ser um objeto")
    return hashlib.sha256(canonical_bytes(snapshot)).hexdigest()


# 6. Framing: o Peer fornece um quadro SEM LF para decode_message.
def encode_message(message):
    """Valida, codifica e acrescenta LF. Nao envia nada pelo socket."""
    validate_message(message)
    raw = canonical_bytes(message)
    _require(len(raw) <= MAX_MESSAGE_SIZE, "Mensagem muito grande")
    return raw + DELIMITER


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, f"Chave JSON repetida: {key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise ProtocolError(f"Constante JSON invalida: {value}")


def decode_message(data):
    """Decodifica exatamente um conteudo de quadro, ja separado pelo Peer."""
    _require(type(data) is bytes, "O quadro deve ser bytes")
    _require(0 < len(data) <= MAX_MESSAGE_SIZE, "Quadro vazio ou muito grande")
    _require(DELIMITER not in data, "O Peer deve remover o LF do quadro")
    try:
        message = json.loads(
            data.decode("utf-8"), object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ProtocolError("UTF-8/JSON invalido ou estrutura profunda") from error
    validate_message(message)
    return message


# 7. Validacao de origem: chamada pelo manager/sessao, nao automaticamente pelo Peer.
def validate_origin(message, match_id, participants, channel_sender_id=None):
    """Valida partida, pertencimento e identidade do canal.

    No HELLO inicial, channel_sender_id ainda pode ser None. A regra menor ID
    conecta ao maior e a recusa de canais duplicados continuam no manager.
    """
    validate_message(message)
    _require(message["match_id"] == match_id, "Mensagem de outra partida")
    _require(message["sender_id"] in participants, "Participante desconhecido")
    if channel_sender_id is None:
        _require(message["type"] == HELLO, "Canal nao identificado exige HELLO")
    else:
        _require(
            message["sender_id"] == channel_sender_id,
            "sender_id nao corresponde ao canal",
        )
        _require(message["type"] != HELLO, "HELLO repetido em canal identificado")

import hashlib
import json
import re


# 1. Framing e vocabulario do protocolo.
PROTOCOL_VERSION = 1
DELIMITER = b"\n"
MAX_MESSAGE_SIZE = 4096  # Conteudo, sem o LF final.

HELLO = "HELLO"
READY = "READY"
MOVE = "MOVE"
STATE_HASH = "STATE_HASH"
LEAVE_REQUEST = "LEAVE_REQUEST"
ABORT = "ABORT"

MESSAGE_TYPES = frozenset({HELLO, READY, MOVE, STATE_HASH, LEAVE_REQUEST, ABORT})
ABORT_REASONS = frozenset({
    "CONFIG_MISMATCH", "INVALID_MESSAGE", "INVALID_MOVE", "CONFLICTING_MOVE",
    "STATE_MISMATCH", "PEER_LEFT", "PEER_DISCONNECTED", "TIMEOUT",
})
ENVELOPE_FIELDS = frozenset({
    "version", "type", "match_id", "sender_id", "tick", "data",
})
HASH_PATTERN = re.compile(r"[0-9a-f]{64}")


class ProtocolError(ValueError):
    """Mensagem ou conteudo fora do contrato do protocolo."""


# 2. Validadores pequenos, reutilizados pelas seis mensagens.
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
    payload = message["data"]
    _require(type(payload) is dict, "data deve ser um objeto")

    if kind in {HELLO, LEAVE_REQUEST}:
        _check_fields(payload, set(), "data")
    elif kind == READY:
        _check_fields(payload, {"config_hash"}, "data")
        _check_hash(payload["config_hash"], "config_hash")
    elif kind == MOVE:
        _check_fields(payload, {"x", "y"}, "data")
        # Negativos sao permitidos: sair da arena pode eliminar a moto.
        _check_integer(payload["x"], "x")
        _check_integer(payload["y"], "y")
    elif kind == STATE_HASH:
        _check_fields(payload, {"state_hash"}, "data")
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
        _require(message["tick"] == 0, "HELLO e READY pertencem ao ciclo 0")
    elif kind in {MOVE, STATE_HASH}:
        _require(message["tick"] >= 1, "MOVE e STATE_HASH comecam no ciclo 1")


# 4. Construcao das mensagens sem repetir o envelope nos chamadores.
def make_message(kind, match_id, sender_id, tick, data):
    """Cria e valida uma mensagem, copiando o payload desta versao (plano)."""
    _require(type(data) is dict, "data deve ser um objeto")
    message = {
        "version": PROTOCOL_VERSION,
        "type": kind,
        "match_id": match_id,
        "sender_id": sender_id,
        "tick": tick,
        "data": data.copy(),
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


def make_leave_request(match_id, sender_id, last_confirmed_tick):
    return make_message(LEAVE_REQUEST, match_id, sender_id, last_confirmed_tick, {})


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
    """JSON canonico desta versao: UTF-8, chaves ordenadas, sem espacos.

    Listas PRESERVAM sua ordem. O chamador deve ordenar jogadores por ID e
    fornecer a grade por linhas, com valores padronizados. Nao passar Arena,
    Jogador, cores de terminal ou horarios locais diretamente.
    """
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

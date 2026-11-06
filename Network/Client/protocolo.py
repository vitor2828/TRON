import json

DELIMITER = b"\n"
MAX_MESSAGE_SIZE = 4096


def encode_message(message):
    """
    Converte um dicionário Python em bytes,
    adicionando o delimitador de mensagem.
    """

    json_string = json.dumps(message)

    data = json_string.encode("utf-8")

    if len(data) > MAX_MESSAGE_SIZE:
        raise ValueError("Mensagem muito grande")

    return data + DELIMITER


def decode_message(data):
    """
    Converte bytes recebidos em um dicionário Python.
    """

    message = json.loads(data.decode("utf-8"))

    if not isinstance(message, dict):
        raise ValueError("A mensagem deve ser um objeto JSON")

    return message
PING = "ping"
PONG = "pong"

ERROR_PREFIX = "error:"

DEFAULT_TIMEOUT = 5.0
POLL_INTERVAL = 0.1


class ProtocolError(Exception):
    """Базовая ошибка протокола."""
    pass


class InvalidMessageError(ProtocolError):
    """Получено некорректное сообщение."""
    pass


class ProtocolTimeoutError(ProtocolError):
    """Истекло время ожидания сообщения."""
    pass


def make_ping():
    """Формирование запроса ping для отправки серверу через общий файл."""
    return PING


def make_pong():
    """Формирование запроса pong для отправки клиенту через общий файл»"""
    return PONG


def make_error(message):
    """Создание сообщения об ошибке."""
    return f"{ERROR_PREFIX}{message}"


def validate_message(message):
    """Проверка корректности сообщения и соответствия установленному формату протокола."""
    if not isinstance(message, str):
        raise InvalidMessageError(
            "Сообщение должно быть строкой."
        )

    message = message.strip()

    if not message:
        raise InvalidMessageError(
            "Получено пустое сообщение."
        )

    if message not in (PING, PONG) and not is_error(message):
        raise InvalidMessageError(
            f"Неизвестное сообщение: {message}"
        )

    return True


def is_error(message):
    """Определение наличия сообщения об ошибке по установленному префиксу."""
    return (
        isinstance(message, str)
        and message.startswith(ERROR_PREFIX)
    )

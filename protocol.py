"""Протокол обмена ping/pong между несколькими клиентами и серверами по TCP.

Формат кадра (framing), так как TCP - это поток байтов без границ сообщений:

    +---------+-----------------+------------------+
    | версия  | длина (4 байта) | полезная нагрузка|
    | 1 байт  | big-endian      | UTF-8, N байт    |
    +---------+-----------------+------------------+

Сообщения (текст внутри кадра):
    "ping"                  - запрос клиента
    "pong"                  - ответ сервера
    "error:<код>:<текст>"   - сообщение об ошибке
"""

import struct

# --- Сообщения ---------------------------------------------------------

PING = "ping"
PONG = "pong"

ERROR_PREFIX = "error:"
ERROR_SEPARATOR = ":"

# --- Кодировка и framing ----------------------------------------------

PROTOCOL_VERSION = 1
ENCODING = "utf-8"

HEADER_FORMAT = ">BI"  # версия (1 байт) + длина (4 байта, big-endian)
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
MAX_MESSAGE_SIZE = 1024  # максимальный размер полезной нагрузки, байт

# --- Тайминги (секунды) и повторы -------------------------------------

DEFAULT_TIMEOUT = 5.0
CONNECT_TIMEOUT = 3.0   # ожидание установки соединения
READ_TIMEOUT = 5.0      # ожидание ответа / очередной порции данных
IDLE_TIMEOUT = 30.0     # сервер закрывает «молчащих» клиентов
SEND_TIMEOUT = 5.0      # ожидание отправки (медленный получатель)

MAX_RETRIES = 3         # число повторов клиента на один сервер
BACKOFF_BASE = 0.5      # первая пауза перед повтором
BACKOFF_FACTOR = 2.0    # множитель экспоненциального роста паузы
BACKOFF_MAX = 8.0       # потолок паузы

# --- Лимиты сервера ----------------------------------------------------

MAX_CONNECTIONS = 50    # при превышении клиент получает error:busy
LISTEN_BACKLOG = 128


# =======================================================================
# Иерархия ошибок
# =======================================================================

class ProtocolError(Exception):
    """Базовая ошибка протокола.

    Атрибут retryable подсказывает клиенту, имеет ли смысл повторять запрос
    (на том же или на другом сервере).
    """

    code = "protocol"
    retryable = False


# --- Ошибки соединения (в основном на стороне клиента) ----------------

class ProtocolConnectionRefusedError(ProtocolError):
    """Сервер не запущен или порт закрыт."""
    code = "refused"
    retryable = True


class ProtocolTimeoutError(ProtocolError):
    """Истекло время ожидания (подключения, чтения или записи)."""
    code = "timeout"
    retryable = True


class ProtocolConnectionResetError(ProtocolError):
    """Соединение сброшено собеседником (RST) или закрыто им досрочно."""
    code = "reset"
    retryable = True


class ProtocolBrokenPipeError(ProtocolError):
    """Запись в соединение, которое уже закрыто собеседником."""
    code = "broken_pipe"
    retryable = True


# --- Ошибки формата сообщений -----------------------------------------

class PartialMessageError(ProtocolError):
    """Соединение закрыто посреди сообщения (получен неполный кадр)."""
    code = "partial"
    retryable = True


class MessageTooLargeError(ProtocolError):
    """Размер сообщения превышает MAX_MESSAGE_SIZE."""
    code = "too_large"


class InvalidMessageError(ProtocolError):
    """Получено некорректное сообщение."""
    code = "invalid"


class EncodingError(InvalidMessageError):
    """Полезная нагрузка не является корректным UTF-8."""
    code = "bad_encoding"


class UnknownCommandError(InvalidMessageError):
    """Корректное по формату, но неизвестное серверу сообщение."""
    code = "unknown_command"


class ProtocolVersionMismatchError(ProtocolError):
    """Версия протокола собеседника не поддерживается."""
    code = "version"


# --- Состояние сервера ------------------------------------------------

class ServerBusyError(ProtocolError):
    """Сервер перегружен (достигнут MAX_CONNECTIONS)."""
    code = "busy"
    retryable = True  # имеет смысл повторить позже или на другом сервере


# Соответствие кода в сообщении error:<код>:<текст> классу исключения
ERROR_CODES = {
    cls.code: cls
    for cls in (
        MessageTooLargeError,
        InvalidMessageError,
        EncodingError,
        UnknownCommandError,
        ProtocolVersionMismatchError,
        PartialMessageError,
        ServerBusyError,
    )
}


# =======================================================================
# Формирование и разбор сообщений
# =======================================================================

def make_ping():
    """Формирование запроса ping для отправки серверу."""
    return PING


def make_pong():
    """Формирование ответа pong для отправки клиенту."""
    return PONG


def make_error(code, message=""):
    """Создание сообщения об ошибке: error:<код>:<текст>."""
    return f"{ERROR_PREFIX}{code}{ERROR_SEPARATOR}{message}"


def make_error_from_exception(exc):
    """Сообщение об ошибке на основе исключения протокола."""
    return make_error(exc.code, str(exc))


def is_error(message):
    """Определение наличия сообщения об ошибке по установленному префиксу."""
    return isinstance(message, str) and message.startswith(ERROR_PREFIX)


def parse_error(message):
    """Разбор сообщения об ошибке. Возвращает кортеж (код, текст)."""
    if not is_error(message):
        raise InvalidMessageError("Сообщение не является ошибкой.")

    body = message[len(ERROR_PREFIX):]
    code, _, text = body.partition(ERROR_SEPARATOR)
    return code, text


def raise_if_error(message):
    """Если пришло error:..., выбрасывает соответствующее исключение."""
    if is_error(message):
        code, text = parse_error(message)
        exc_class = ERROR_CODES.get(code, ProtocolError)
        raise exc_class(text or f"Удалённая сторона сообщила об ошибке: {code}")


# =======================================================================
# Проверка сообщений
# =======================================================================

def validate_message(message):
    """Проверка корректности сообщения и соответствия формату протокола."""
    if not isinstance(message, str):
        raise InvalidMessageError("Сообщение должно быть строкой.")

    message = message.strip()

    if not message:
        raise InvalidMessageError("Получено пустое сообщение.")

    if len(message.encode(ENCODING)) > MAX_MESSAGE_SIZE:
        raise MessageTooLargeError(
            f"Сообщение больше {MAX_MESSAGE_SIZE} байт."
        )

    if message not in (PING, PONG) and not is_error(message):
        raise InvalidMessageError(f"Неизвестное сообщение: {message}")

    return True


def validate_request(message):
    """Проверка запроса на стороне сервера: допустим только ping."""
    validate_message(message)
    message = message.strip()

    if message != PING:
        raise UnknownCommandError(f"Неизвестная команда: {message}")

    return True


def validate_response(message):
    """Проверка ответа на стороне клиента: pong или ошибка сервера.

    Если пришла ошибка сервера, выбрасывается соответствующее исключение.
    """
    validate_message(message)
    message = message.strip()

    raise_if_error(message)

    if message != PONG:
        raise InvalidMessageError(f"Ожидался {PONG}, получено: {message}")

    return True


# =======================================================================
# Кодирование и декодирование кадров
# =======================================================================

def encode_frame(message):
    """Упаковка сообщения в кадр: заголовок + UTF-8 нагрузка."""
    validate_message(message)

    payload = message.strip().encode(ENCODING)
    header = struct.pack(HEADER_FORMAT, PROTOCOL_VERSION, len(payload))
    return header + payload


def parse_header(header):
    """Разбор заголовка кадра. Возвращает длину полезной нагрузки."""
    if len(header) != HEADER_SIZE:
        raise PartialMessageError("Неполный заголовок кадра.")

    version, length = struct.unpack(HEADER_FORMAT, header)

    if version != PROTOCOL_VERSION:
        raise ProtocolVersionMismatchError(
            f"Версия протокола {version}, поддерживается {PROTOCOL_VERSION}."
        )

    if length == 0:
        raise InvalidMessageError("Получен пустой кадр.")

    if length > MAX_MESSAGE_SIZE:
        raise MessageTooLargeError(
            f"Заявленный размер {length} байт превышает {MAX_MESSAGE_SIZE}."
        )

    return length


def decode_payload(payload):
    """Декодирование нагрузки кадра из UTF-8 в строку."""
    try:
        message = payload.decode(ENCODING)
    except UnicodeDecodeError as exc:
        raise EncodingError(f"Некорректный UTF-8: {exc}") from exc

    validate_message(message)
    return message.strip()


class FrameDecoder:
    """Накопитель байтов: собирает кадры из потока TCP.

    Решает три проблемы потока: одно сообщение может прийти по частям,
    несколько сообщений - одним пакетом, а соединение может закрыться
    посреди сообщения.
    """

    def __init__(self):
        self._buffer = bytearray()

    def feed(self, data):
        """Добавляет полученные байты, возвращает список готовых сообщений."""
        self._buffer.extend(data)
        messages = []

        while len(self._buffer) >= HEADER_SIZE:
            length = parse_header(bytes(self._buffer[:HEADER_SIZE]))
            total = HEADER_SIZE + length

            if len(self._buffer) < total:
                break  # кадр ещё не пришёл целиком

            payload = bytes(self._buffer[HEADER_SIZE:total])
            del self._buffer[:total]
            messages.append(decode_payload(payload))

        return messages

    @property
    def has_partial(self):
        """True, если в буфере лежит неполный кадр."""
        return len(self._buffer) > 0

    def check_eof(self):
        """Вызывается при закрытии соединения собеседником.

        Если в буфере остался неполный кадр, выбрасывает PartialMessageError.
        """
        if self.has_partial:
            raise PartialMessageError(
                f"Соединение закрыто, получено только {len(self._buffer)} байт кадра."
            )


# =======================================================================
# Тайминги
# =======================================================================

def backoff_delay(attempt):
    """Пауза перед повтором (attempt начинается с 0): 0.5, 1, 2, 4, ... до BACKOFF_MAX."""
    return min(BACKOFF_BASE * (BACKOFF_FACTOR ** attempt), BACKOFF_MAX)

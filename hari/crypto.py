"""Personal text is encrypted before it is written to the database.

Columns typed `EncryptedText` (chat memory, request bodies and decisions, step proofs, applicants'
answers) are stored as "enc1:<Fernet token>" when DATA_ENCRYPTION_KEY is set: AES-128-CBC with an
HMAC-SHA256 check, so a copied database, a backup or a leaked database password shows no readable
text and a changed value is detected. The app holds the key, so this does not protect against someone
who controls the running bot.

- No key set: text is stored as before (tests, local development). Live mode requires a key.
- Rows written before the key was set stay readable and are encrypted the next time they are saved.
- The key must never change or be lost: encrypted text cannot be read without it. A value that cannot
  be decrypted is shown as UNREADABLE, never as an error that stops the bot.
"""

from __future__ import annotations

import base64
import hashlib
from functools import lru_cache

from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from hari.config import settings

PREFIX = "enc1:"
UNREADABLE = "[шифрланган матнни ўқиб бўлмади]"


@lru_cache(maxsize=4)
def _fernet(secret: str):  # noqa: ANN202
    from cryptography.fernet import Fernet  # here: only needed once a key is set

    # Any string Render generates works as the secret: it is stretched to the 32-byte key Fernet needs.
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest()))


def _key() -> str:
    return settings.data_encryption_key.get_secret_value().strip()


def encrypt(text: str) -> str:
    secret = _key()
    if not secret or text.startswith(PREFIX):
        return text
    return PREFIX + _fernet(secret).encrypt(text.encode("utf-8")).decode("ascii")


def decrypt(stored: str) -> str:
    if not stored.startswith(PREFIX):
        return stored  # written before encryption was turned on
    secret = _key()
    if not secret:
        return UNREADABLE
    from cryptography.fernet import InvalidToken

    try:
        return _fernet(secret).decrypt(stored[len(PREFIX):].encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return UNREADABLE


class EncryptedText(TypeDecorator):
    """A Text column whose value is encrypted at rest. Never filter or search on it in SQL."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):  # noqa: ANN001, ANN201
        return None if value is None else encrypt(str(value))

    def process_result_value(self, value, dialect):  # noqa: ANN001, ANN201
        return None if value is None else decrypt(value)

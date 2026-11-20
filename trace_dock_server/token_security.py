"""Compact tokens that can recover their email without a database lookup."""
import base64
import re
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


TOKEN_PREFIX = "td3_"
_TOKEN_BODY = re.compile(r"^[A-Za-z0-9_-]+$")
_COMPACT_AAD = b"trace-dock/token-email/v3"
_COMPACT_KDF_SALT = b"trace-dock/token-email-key/v3"
_COMPACT_NONCE_BYTES = 8
_COMPACT_TAG_BYTES = 12


def _encryption_key(pepper, salt, info):
    if isinstance(pepper, str):
        pepper = pepper.encode()
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=info,
    ).derive(pepper)


def issue_token(email, pepper):
    payload = email.encode("utf-8")
    if not payload or len(payload) > 255:
        raise ValueError("email length is outside the compact token limit")
    nonce = secrets.token_bytes(_COMPACT_NONCE_BYTES)
    encryptor = Cipher(
        algorithms.AES(_encryption_key(pepper, _COMPACT_KDF_SALT, _COMPACT_AAD)),
        modes.GCM(nonce),
    ).encryptor()
    encryptor.authenticate_additional_data(_COMPACT_AAD)
    ciphertext = encryptor.update(payload) + encryptor.finalize()
    encrypted = ciphertext + encryptor.tag[:_COMPACT_TAG_BYTES]
    body = base64.urlsafe_b64encode(nonce + encrypted).rstrip(b"=").decode("ascii")
    return TOKEN_PREFIX + body


def recover_email(token, pepper):
    if not token.startswith(TOKEN_PREFIX):
        raise ValueError("token 格式无效")
    body = token[len(TOKEN_PREFIX):]
    if not body or not _TOKEN_BODY.fullmatch(body):
        raise ValueError("token 格式无效")
    try:
        encoded = body.encode("ascii")
        encrypted = base64.b64decode(encoded + b"=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        if len(encrypted) < _COMPACT_NONCE_BYTES + _COMPACT_TAG_BYTES + 1:
            raise ValueError("token 格式无效")
        nonce = encrypted[:_COMPACT_NONCE_BYTES]
        ciphertext = encrypted[_COMPACT_NONCE_BYTES:-_COMPACT_TAG_BYTES]
        tag = encrypted[-_COMPACT_TAG_BYTES:]
        decryptor = Cipher(
            algorithms.AES(_encryption_key(pepper, _COMPACT_KDF_SALT, _COMPACT_AAD)),
            modes.GCM(nonce, tag, min_tag_length=_COMPACT_TAG_BYTES),
        ).decryptor()
        decryptor.authenticate_additional_data(_COMPACT_AAD)
        payload = decryptor.update(ciphertext) + decryptor.finalize()
        email = payload.decode("utf-8")
    except (InvalidTag, UnicodeDecodeError, ValueError) as error:
        raise ValueError("token 无效或 TOKEN_PEPPER 不匹配") from error
    if not email:
        raise ValueError("token 内容无效")
    return email

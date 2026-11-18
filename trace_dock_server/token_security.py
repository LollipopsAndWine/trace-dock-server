"""Versioned tokens that can recover their email without a database lookup."""
import base64
import json
import re
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


TOKEN_PREFIX = "td2_"
_TOKEN_BODY = re.compile(r"^[A-Za-z0-9_-]+$")
_AAD = b"trace-dock/token-email/v2"
_KDF_SALT = b"trace-dock/token-email-key/v2"


def _encryption_key(pepper):
    if isinstance(pepper, str):
        pepper = pepper.encode()
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_KDF_SALT,
        info=_AAD,
    ).derive(pepper)


def issue_token(email, pepper):
    payload = json.dumps(
        {"email": email, "secret": secrets.token_urlsafe(32)},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    nonce = secrets.token_bytes(12)
    encrypted = AESGCM(_encryption_key(pepper)).encrypt(nonce, payload, _AAD)
    body = base64.urlsafe_b64encode(nonce + encrypted).rstrip(b"=").decode("ascii")
    return TOKEN_PREFIX + body


def recover_email(token, pepper):
    if not token.startswith(TOKEN_PREFIX):
        raise ValueError("旧格式 token 不包含可恢复的邮箱")
    body = token[len(TOKEN_PREFIX):]
    if not body or not _TOKEN_BODY.fullmatch(body):
        raise ValueError("token 格式无效")
    try:
        encoded = body.encode("ascii")
        encrypted = base64.b64decode(encoded + b"=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        if len(encrypted) < 12 + 16:
            raise ValueError("token 格式无效")
        payload = AESGCM(_encryption_key(pepper)).decrypt(encrypted[:12], encrypted[12:], _AAD)
        values = json.loads(payload)
    except (InvalidTag, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("token 无效或 TOKEN_PEPPER 不匹配") from error
    email = values.get("email") if isinstance(values, dict) else None
    if not isinstance(email, str) or not email:
        raise ValueError("token 内容无效")
    return email

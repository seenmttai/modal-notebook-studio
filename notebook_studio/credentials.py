from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re


USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,31}$")


def password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310_000)
    return "pbkdf2_sha256$310000$%s$%s" % (
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, rounds, salt_text, digest_text = encoded.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(rounds))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _fernet(master_key: str):
    if len(master_key) < 32:
        raise ValueError("MODAL_CREDENTIAL_ENCRYPTION_KEY must be at least 32 characters.")
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise RuntimeError("Install the project dependencies to enable encrypted credentials.") from exc
    key = base64.urlsafe_b64encode(hashlib.sha256(master_key.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_modal_credentials(master_key: str, token_id: str, token_secret: str) -> tuple[str, str]:
    cipher = _fernet(master_key)
    return (
        cipher.encrypt(token_id.encode("utf-8")).decode("ascii"),
        cipher.encrypt(token_secret.encode("utf-8")).decode("ascii"),
    )


def decrypt_modal_credentials(master_key: str, token_id_ciphertext: str, token_secret_ciphertext: str) -> tuple[str, str]:
    cipher = _fernet(master_key)
    return (
        cipher.decrypt(token_id_ciphertext.encode("ascii")).decode("utf-8"),
        cipher.decrypt(token_secret_ciphertext.encode("ascii")).decode("utf-8"),
    )

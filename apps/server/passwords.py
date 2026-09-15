"""Password hashing primitives for PicoPG accounts."""

import base64
import hashlib
import hmac
import secrets


SCHEME = "scrypt-v1"
N = 1 << 14
R = 8
P = 1
SALT_BYTES = 16
KEY_BYTES = 32


def hash_password(password):
    """Return a versioned scrypt hash for a password string."""
    password_bytes = _encode_password(password)
    salt = secrets.token_bytes(SALT_BYTES)
    digest = _derive(password_bytes, salt)
    return "$".join(
        (
            SCHEME,
            str(N),
            str(R),
            str(P),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        )
    )


def verify_password(password, encoded_hash):
    """Return whether a password matches a supported encoded hash."""
    password_bytes = _encode_password(password)

    try:
        scheme, n, r, p, salt_text, digest_text = encoded_hash.split("$")
        if (scheme, n, r, p) != (SCHEME, str(N), str(R), str(P)):
            return False

        salt = base64.b64decode(salt_text, validate=True)
        expected = base64.b64decode(digest_text, validate=True)
    except (AttributeError, TypeError, ValueError):
        return False

    if len(salt) != SALT_BYTES or len(expected) != KEY_BYTES:
        return False

    actual = _derive(password_bytes, salt)
    return hmac.compare_digest(actual, expected)


def _encode_password(password):
    if not isinstance(password, str):
        raise TypeError("password must be a string")
    return password.encode("utf-8")


def _derive(password, salt):
    return hashlib.scrypt(password, salt=salt, n=N, r=R, p=P, dklen=KEY_BYTES)

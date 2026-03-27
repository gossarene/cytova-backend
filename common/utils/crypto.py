"""
Cytova — Cryptographic Utilities

Centralised helpers for token generation, hashing, and comparison.
All security-sensitive operations must go through these functions —
never use random or hashlib directly in domain code.
"""
import base64
import hashlib
import hmac
import json
import secrets


def generate_secure_token(length: int = 32) -> str:
    """
    Generate a cryptographically secure URL-safe random token.
    Used for: refresh tokens, password reset tokens, email verification tokens.

    Args:
        length: Number of random bytes (before base64 encoding). Default 32
                produces a 43-character URL-safe string.
    """
    return secrets.token_urlsafe(length)


def hash_token(token: str) -> str:
    """
    One-way SHA-256 hash of a token for safe database storage.
    The plaintext token is sent to the client and never persisted.

    Args:
        token: The plaintext token string.

    Returns:
        64-character hex digest.
    """
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def constant_time_compare(val1: str, val2: str) -> bool:
    """
    Compare two strings in constant time to prevent timing-based attacks.
    Use this whenever comparing a client-supplied value against a stored secret.
    """
    if isinstance(val1, str):
        val1 = val1.encode('utf-8')
    if isinstance(val2, str):
        val2 = val2.encode('utf-8')
    return hmac.compare_digest(val1, val2)


def encode_cursor(data: dict) -> str:
    """
    Encode a dict into an opaque base64 cursor string for pagination.
    Clients must treat this as opaque — never parse or construct cursors.
    """
    json_bytes = json.dumps(data, separators=(',', ':')).encode('utf-8')
    return base64.urlsafe_b64encode(json_bytes).decode('utf-8')


def decode_cursor(cursor: str) -> dict:
    """
    Decode a base64 pagination cursor back to its dict representation.
    Returns an empty dict on any invalid or malformed input — never raises.
    """
    try:
        json_bytes = base64.urlsafe_b64decode(cursor.encode('utf-8'))
        return json.loads(json_bytes.decode('utf-8'))
    except Exception:
        return {}

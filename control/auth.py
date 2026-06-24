"""
auth.py — Token generation and verification for tinybrain-control

Two token types:
1. enrollment_token  — UUID4, one-time use, identifies a tenant slot.
2. agent_token       — 32 random bytes as hex (256 bits of entropy),
                       long-lived, identifies a specific agent.

Why SHA-256 and not bcrypt for agent tokens?
- bcrypt is designed to be slow (to resist brute-force of low-entropy secrets
  like passwords). Agent tokens are 256-bit random values — there is no
  dictionary to attack. SHA-256 is sufficient and fast enough that it adds
  no measurable latency per request.
- bcrypt on every API request would add ~100-300ms per call, which is
  unacceptable for a polling endpoint that fires every few seconds.
"""

import hashlib
import secrets
import uuid


def generate_enrollment_token() -> str:
    """
    UUID4 as the enrollment token.
    UUID4 is 122 bits of randomness — enough for a one-time-use token
    that lives for minutes to hours. It's also URL-safe and human-readable
    in logs (with appropriate masking).
    """
    return str(uuid.uuid4())


def generate_agent_token() -> str:
    """
    32 cryptographically random bytes, hex-encoded = 64-char string.
    secrets.token_hex uses os.urandom() — cryptographically secure on
    all modern OSes. Never use random.token_hex() for security material.
    """
    return secrets.token_hex(32)


def hash_token(plaintext_token: str) -> str:
    """
    SHA-256 of the token, hex-encoded.
    This is what gets stored in the database.
    On every authenticated request, we hash the incoming bearer token
    and compare it against this stored hash — the plaintext never persists.
    """
    return hashlib.sha256(plaintext_token.encode()).hexdigest()


def extract_bearer_token(authorization_header: str | None) -> str | None:
    """
    Parse 'Bearer <token>' from the Authorization header.
    Returns None if the header is missing or malformed — callers
    are responsible for returning 401 in that case.
    """
    if not authorization_header:
        return None
    parts = authorization_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def generate_id(prefix: str) -> str:
    """
    Generate a prefixed ID: e.g. "tnt_a1b2c3d4e5f6...".
    Prefixed IDs appear in logs and are immediately identifiable
    by type — a pattern from Stripe's API that dramatically improves
    debuggability. uuid4().hex strips the hyphens for a cleaner string.
    """
    return f"{prefix}{uuid.uuid4().hex}"

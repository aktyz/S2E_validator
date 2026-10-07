"""Base58, the Bitcoin alphabet Solana uses for public keys.

Twenty lines of stdlib rather than a dependency: the validator only needs it to
name a key, never to sign with one.
"""

from __future__ import annotations

ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {char: value for value, char in enumerate(ALPHABET)}


def encode(data: bytes) -> str:
    number = int.from_bytes(data, "big")
    digits = []
    while number:
        number, remainder = divmod(number, 58)
        digits.append(ALPHABET[remainder])
    # Each leading zero byte is written as a leading "1".
    leading_zeros = len(data) - len(data.lstrip(b"\0"))
    return "1" * leading_zeros + "".join(reversed(digits))


def decode(text: str) -> bytes:
    number = 0
    for char in text:
        if char not in _INDEX:
            raise ValueError(f"not base58: {char!r}")
        number = number * 58 + _INDEX[char]
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    leading_ones = len(text) - len(text.lstrip("1"))
    return b"\0" * leading_ones + body

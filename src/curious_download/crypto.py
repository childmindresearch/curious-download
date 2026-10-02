"""Decryption of Curious answers.

Answers are encrypted on the respondent's device with AES-256-CBC. The AES key
is sha256 of a Diffie-Hellman shared secret between the respondent's key pair
and the applet's key pair. The applet's private key is derived from the applet
password, exactly as the admin panel does it
(mindlogger-admin/src/shared/utils/encryption/encryption.ts).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# How the admin panel's `${key1}${key2}` turns the two SHA-512 digests into a
# string. In the browser, Buffer comes from the `buffer` npm polyfill, which
# replaces every byte of an invalid UTF-8 sequence with U+FFFD. Native Node and
# Python replace each maximal invalid subsequence with a single U+FFFD. The two
# give different keys for many passwords, so both are tried and the one that
# reproduces the applet's stored public key wins.
KEY_VARIANT_BROWSER = "browser"
KEY_VARIANT_NODE = "node"
KEY_VARIANTS = (KEY_VARIANT_BROWSER, KEY_VARIANT_NODE)


class DecryptionError(Exception):
    """Raised when an answer cannot be decrypted with the applet key."""


class AppletPasswordError(Exception):
    """Raised when the applet password does not match the applet's public key."""


def _polyfill_utf8_decode(buf: bytes) -> str:
    """Port of `utf8Slice` from the `buffer` npm package (v6), used by the admin panel."""
    out: list[str] = []
    i = 0
    end = len(buf)
    while i < end:
        first = buf[i]
        code_point: int | None = None
        if first > 0xEF:
            size = 4
        elif first > 0xDF:
            size = 3
        elif first > 0xBF:
            size = 2
        else:
            size = 1

        if i + size <= end:
            if size == 1:
                if first < 0x80:
                    code_point = first
            elif size == 2:
                second = buf[i + 1]
                if second & 0xC0 == 0x80:
                    temp = (first & 0x1F) << 0x6 | (second & 0x3F)
                    if temp > 0x7F:
                        code_point = temp
            elif size == 3:
                second, third = buf[i + 1], buf[i + 2]
                if second & 0xC0 == 0x80 and third & 0xC0 == 0x80:
                    temp = (first & 0xF) << 0xC | (second & 0x3F) << 0x6 | (third & 0x3F)
                    if temp > 0x7FF and (temp < 0xD800 or temp > 0xDFFF):
                        code_point = temp
            else:
                second, third, fourth = buf[i + 1], buf[i + 2], buf[i + 3]
                if second & 0xC0 == 0x80 and third & 0xC0 == 0x80 and fourth & 0xC0 == 0x80:
                    temp = (first & 0xF) << 0x12 | (second & 0x3F) << 0xC | (third & 0x3F) << 0x6 | (fourth & 0x3F)
                    if 0xFFFF < temp < 0x110000:
                        code_point = temp

        if code_point is None:
            code_point = 0xFFFD
            size = 1
        out.append(chr(code_point))
        i += size
    return "".join(out)


def _digest_to_js_string(digest: bytes, variant: str) -> str:
    if variant == KEY_VARIANT_BROWSER:
        return _polyfill_utf8_decode(digest)
    if variant == KEY_VARIANT_NODE:
        return digest.decode("utf-8", errors="replace")
    raise ValueError(f"Unknown key variant: {variant}")


def applet_private_key(applet_password: str, account_id: str, variant: str = KEY_VARIANT_BROWSER) -> int:
    """Derive the applet's DH private key from its password (admin `getPrivateKey`)."""
    key1 = hashlib.sha512(applet_password.encode("utf-8")).digest()
    key2 = hashlib.sha512(account_id.encode("utf-8")).digest()
    as_string = _digest_to_js_string(key1, variant) + _digest_to_js_string(key2, variant)
    return int.from_bytes(as_string.encode("utf-8"), "big")


def _int_to_bytes(value: int) -> bytes:
    return value.to_bytes((value.bit_length() + 7) // 8 or 1, "big")


def _parse_key_material(value: str | Sequence[int]) -> bytes:
    """Keys and DH parameters travel as JSON arrays of byte values."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            # The admin falls back to the raw string, which Buffer.from() encodes as UTF-8.
            return value.encode("utf-8")
    return bytes(value)


def _aes_key_candidates(shared_secret: int, prime_length: int) -> list[bytes]:
    """The device library may or may not left-pad the shared secret to the prime's length."""
    raw = _int_to_bytes(shared_secret)
    padded = raw.rjust(prime_length, b"\x00")
    candidates = [hashlib.sha256(padded).digest()]
    if padded != raw:
        candidates.append(hashlib.sha256(raw).digest())
    return candidates


def decrypt_with_key(key: bytes, text: str) -> str:
    """Decrypt an "ivHex:cipherHex" string produced by the app."""
    try:
        iv_hex, cipher_hex = text.split(":", 1)
        iv = bytes.fromhex(iv_hex)
        data = bytes.fromhex(cipher_hex)
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        padded = decryptor.update(data) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        plain = unpadder.update(padded) + unpadder.finalize()
    except (ValueError, TypeError) as e:
        raise DecryptionError(str(e)) from e
    return plain.decode("utf-8", errors="replace")


def encrypt_with_key(key: bytes, text: str, iv: bytes) -> str:
    """Encrypt like the app does. Only used to build test data."""
    padder = padding.PKCS7(128).padder()
    padded = padder.update(text.encode("utf-8")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return f"{iv.hex()}:{(encryptor.update(padded) + encryptor.finalize()).hex()}"


@dataclass
class AppletDecryptor:
    """Decrypts answers for one applet. Build it with `from_password`."""

    private_key: int
    prime: int
    prime_length: int
    key_variant: str
    _key_cache: dict[str, list[bytes]] = field(default_factory=dict, repr=False)

    @classmethod
    def from_password(cls, applet_password: str, encryption: dict) -> AppletDecryptor:
        """Derive the applet key and check it against the applet's stored public key.

        `encryption` is the applet's `encryption` object from the API:
        {publicKey, prime, base, accountId}, where the first three are JSON byte arrays.
        """
        prime_bytes = _parse_key_material(encryption["prime"])
        prime = int.from_bytes(prime_bytes, "big")
        base = int.from_bytes(_parse_key_material(encryption["base"]), "big")
        expected_public = int.from_bytes(_parse_key_material(encryption["publicKey"]), "big")

        for variant in KEY_VARIANTS:
            private_key = applet_private_key(applet_password, encryption["accountId"], variant)
            if pow(base, private_key, prime) == expected_public:
                return cls(private_key, prime, len(prime_bytes), variant)
        raise AppletPasswordError("The applet password is incorrect.")

    def _keys_for(self, user_public_key: str) -> list[bytes]:
        keys = self._key_cache.get(user_public_key)
        if keys is None:
            other = int.from_bytes(_parse_key_material(user_public_key), "big")
            keys = _aes_key_candidates(pow(other, self.private_key, self.prime), self.prime_length)
            self._key_cache[user_public_key] = keys
        return keys

    def decrypt(self, user_public_key: str, text: str) -> str:
        """Decrypt one encrypted field of an answer (answer, events, or identifier)."""
        keys = self._keys_for(user_public_key)
        last_error: DecryptionError | None = None
        for index, key in enumerate(keys):
            try:
                plain = decrypt_with_key(key, text)
            except DecryptionError as e:
                last_error = e
                continue
            if index:
                # Remember which padding convention worked for this respondent.
                keys.insert(0, keys.pop(index))
            return plain
        raise DecryptionError(f"Cannot decrypt answer data: {last_error}")

    def decrypt_json(self, user_public_key: str, text: str):
        """Decrypt a field that holds JSON (answers and events)."""
        keys = self._keys_for(user_public_key)
        last_error: Exception | None = None
        for index, key in enumerate(keys):
            try:
                value = json.loads(decrypt_with_key(key, text))
            except (DecryptionError, json.JSONDecodeError) as e:
                last_error = e
                continue
            if index:
                keys.insert(0, keys.pop(index))
            return value
        raise DecryptionError(f"Cannot decrypt answer data: {last_error}")

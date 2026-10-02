"""Key derivation and decryption, checked against Node's crypto (tests/js_oracle/crypto_oracle.js)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from curious_download.crypto import (
    KEY_VARIANT_BROWSER,
    KEY_VARIANT_NODE,
    AppletDecryptor,
    AppletPasswordError,
    DecryptionError,
    _int_to_bytes,
    _polyfill_utf8_decode,
    applet_private_key,
    encrypt_with_key,
)

ORACLE = Path(__file__).parent / "js_oracle" / "crypto_oracle.js"
PRIME = json.loads((Path(__file__).parent / "fixtures" / "prime.json").read_text())
ACCOUNT_ID = "6056fc79-931a-412e-b15b-d5798c826a23"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is needed for the reference crypto")


def _encryption(public_key: list[int]) -> dict:
    return {
        "publicKey": json.dumps(public_key),
        "prime": json.dumps(PRIME),
        "base": json.dumps([2]),
        "accountId": ACCOUNT_ID,
    }


def _run_oracle(applet_password: str, plaintexts: list[str]) -> dict:
    job = {
        "prime": PRIME,
        "base": [2],
        "appletPassword": applet_password,
        "accountId": ACCOUNT_ID,
        "respondent": {"userId": "user-1", "email": "p@example.org", "password": "pw"},
        "plaintexts": plaintexts,
    }
    result = subprocess.run(["node", str(ORACLE)], input=json.dumps(job), capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


# -- the browser Buffer polyfill's UTF-8 decoding ----------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"abc", "abc"),
        ("é€😀".encode(), "é€😀"),
        (b"\xe2\x82\x41", "��A"),  # truncated 3-byte sequence: one U+FFFD per byte
        (b"\xf0\x90\x80\x41", "���A"),  # truncated 4-byte sequence
        (b"\xe2\x82", "��"),  # truncated at the end
        (b"\xc0\x80", "��"),  # overlong
        (b"\xed\xa0\x80", "���"),  # surrogate
        (b"\xff", "�"),
    ],
)
def test_polyfill_utf8_decode(raw, expected):
    assert _polyfill_utf8_decode(raw) == expected


def test_variants_differ_for_typical_passwords():
    """The reason both variants exist: they give different keys for most passwords."""
    passwords = [f"password-{i}" for i in range(20)]
    differing = [
        p
        for p in passwords
        if applet_private_key(p, ACCOUNT_ID, KEY_VARIANT_BROWSER) != applet_private_key(p, ACCOUNT_ID, KEY_VARIANT_NODE)
    ]
    assert differing


# -- interoperability with Node -----------------------------------------------


@needs_node
@pytest.mark.parametrize("applet_password", ["Test-Password-1", "password-3", "ünïcødé pass", "a"])
def test_node_derived_applet_key_matches_node_variant(applet_password):
    out = _run_oracle(applet_password, [])
    node_key = int.from_bytes(bytes.fromhex(out["appletPrivateKeyHex"]), "big")
    assert applet_private_key(applet_password, ACCOUNT_ID, KEY_VARIANT_NODE) == node_key


@needs_node
@pytest.mark.parametrize("applet_password", ["Test-Password-1", "password-3", "ünïcødé pass"])
def test_decrypts_answers_encrypted_like_the_app(applet_password):
    answers = [{"value": 1, "text": "ünïcødé ✓"}, None, "free text", {"value": [0, 2]}]
    events = [{"type": "SET_ANSWER", "screen": "a/b", "time": 1749138592012}]
    big = "x" * 50_000  # the app encrypts in 10 KB chunks
    out = _run_oracle(applet_password, [json.dumps(answers), json.dumps(events), big])

    decryptor = AppletDecryptor.from_password(applet_password, _encryption(out["appletPublicKey"]))
    user_key = json.dumps(out["userPublicKey"])
    assert decryptor.decrypt_json(user_key, out["ciphertexts"][0]) == answers
    assert decryptor.decrypt_json(user_key, out["ciphertexts"][1]) == events
    assert decryptor.decrypt(user_key, out["ciphertexts"][2]) == big
    # Node's native Buffer decodes like Python, so this applet was created with the node variant
    # unless the two variants happen to coincide for this password.
    browser = applet_private_key(applet_password, ACCOUNT_ID, KEY_VARIANT_BROWSER)
    node = applet_private_key(applet_password, ACCOUNT_ID, KEY_VARIANT_NODE)
    assert decryptor.key_variant == (KEY_VARIANT_BROWSER if browser == node else KEY_VARIANT_NODE)


@needs_node
def test_wrong_applet_password_is_rejected():
    out = _run_oracle("right password", [])
    with pytest.raises(AppletPasswordError):
        AppletDecryptor.from_password("wrong password", _encryption(out["appletPublicKey"]))


# -- pure Python ---------------------------------------------------------------


def _python_applet(password: str, variant: str) -> tuple[dict, int]:
    prime = int.from_bytes(bytes(PRIME), "big")
    private = applet_private_key(password, ACCOUNT_ID, variant)
    public = list(_int_to_bytes(pow(2, private, prime)))
    return _encryption(public), prime


def test_browser_variant_applet_is_detected():
    encryption, _ = _python_applet("password-3", KEY_VARIANT_BROWSER)
    assert AppletDecryptor.from_password("password-3", encryption).key_variant == KEY_VARIANT_BROWSER


def test_unpadded_shared_secret_fallback():
    """If the device didn't left-pad a shared secret that starts with 0x00, decryption still works."""
    encryption, prime = _python_applet("pw", KEY_VARIANT_BROWSER)
    decryptor = AppletDecryptor.from_password("pw", encryption)
    for n in range(5000):
        user_private = int.from_bytes(hashlib.sha512(f"user-{n}".encode()).digest(), "big")
        user_public = pow(2, user_private, prime)
        shared = pow(user_public, decryptor.private_key, prime)
        if len(_int_to_bytes(shared)) < len(PRIME):
            break
    else:
        pytest.skip("no short shared secret found")
    unpadded_key = hashlib.sha256(_int_to_bytes(shared)).digest()
    text = encrypt_with_key(unpadded_key, json.dumps(["ok"]), os.urandom(16))
    assert decryptor.decrypt_json(json.dumps(list(_int_to_bytes(user_public))), text) == ["ok"]


def test_garbage_raises_decryption_error():
    encryption, _ = _python_applet("pw", KEY_VARIANT_BROWSER)
    decryptor = AppletDecryptor.from_password("pw", encryption)
    with pytest.raises(DecryptionError):
        decryptor.decrypt_json("[1,2,3]", "00" * 16 + ":" + "11" * 32)

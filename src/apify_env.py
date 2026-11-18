"""Minimal Apify platform helpers that need no Apify SDK.

The Actor runs on the RocketRide engine's embedded Python, so instead of
installing the Apify SDK we read the few environment variables we need, fetch
the Actor input over the Apify API, and decrypt secret input fields with the
same scheme the SDK uses (RSA-OAEP wrapped AES-256-GCM).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger('rocketride-actor')

_B64 = '[-A-Za-z0-9+/]*={0,3}'
_ENCRYPTED_RE = re.compile(f'^(ENCRYPTED_VALUE|ENCRYPTED_JSON):(?:({_B64}):)?({_B64}):({_B64})$')
_KEY_LEN = 32
_IV_LEN = 16
_TAG_LEN = 16


def env(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def is_at_home() -> bool:
    return env('APIFY_IS_AT_HOME') == '1'


def is_standby() -> bool:
    return env('APIFY_META_ORIGIN') == 'STANDBY'


def api_base() -> str:
    return (env('APIFY_API_BASE_URL', default='https://api.apify.com') or '').rstrip('/')


def api_headers() -> dict[str, str]:
    token = env('APIFY_TOKEN')
    return {'Authorization': f'Bearer {token}'} if token else {}


def _private_decrypt(enc_password: str, enc_value: str, private_key: Any) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    password = private_key.decrypt(
        base64.b64decode(enc_password),
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA1()), algorithm=hashes.SHA1(), label=None),
    )
    if len(password) != _KEY_LEN + _IV_LEN:
        raise ValueError('Secret input decryption failed: invalid password length')
    blob = base64.b64decode(enc_value)
    data, tag = blob[:-_TAG_LEN], blob[-_TAG_LEN:]
    key, iv = password[:_KEY_LEN], password[_KEY_LEN:]
    decryptor = Cipher(algorithms.AES(key), modes.GCM(iv, tag)).decryptor()
    return (decryptor.update(data) + decryptor.finalize()).decode('utf-8')


def decrypt_secrets(data: dict[str, Any]) -> dict[str, Any]:
    key_file = env('APIFY_INPUT_SECRETS_PRIVATE_KEY_FILE')
    passphrase = env('APIFY_INPUT_SECRETS_PRIVATE_KEY_PASSPHRASE')
    has_secret = any(isinstance(v, str) and _ENCRYPTED_RE.fullmatch(v) for v in data.values())
    if not has_secret:
        return data
    if not key_file or not passphrase:
        log.warning('Input has encrypted secrets but no decryption key is available; ignoring them.')
        return {k: v for k, v in data.items() if not (isinstance(v, str) and _ENCRYPTED_RE.fullmatch(v))}

    from cryptography.hazmat.primitives import serialization

    private_key = serialization.load_pem_private_key(base64.b64decode(key_file), password=passphrase.encode('utf-8'))
    out = dict(data)
    for k, v in data.items():
        if isinstance(v, str):
            m = _ENCRYPTED_RE.fullmatch(v)
            if m:
                plain = _private_decrypt(m.group(3), m.group(4), private_key)
                out[k] = json.loads(plain) if m.group(1) == 'ENCRYPTED_JSON' else plain
    return out


def load_input() -> dict[str, Any]:
    """Actor input: ACTOR_INPUT_JSON env (tests) > local storage file > Apify API."""
    raw = env('ACTOR_INPUT_JSON')
    if raw:
        return decrypt_secrets(json.loads(raw))

    input_key = env('ACTOR_INPUT_KEY', 'APIFY_INPUT_KEY', default='INPUT')
    if not is_at_home():
        storage = Path(env('APIFY_LOCAL_STORAGE_DIR', 'CRAWLEE_STORAGE_DIR', default='./storage') or './storage')
        path = storage / 'key_value_stores' / 'default' / f'{input_key}.json'
        if path.exists():
            return decrypt_secrets(json.loads(path.read_text() or '{}'))
        return {}

    store_id = env('ACTOR_DEFAULT_KEY_VALUE_STORE_ID', 'APIFY_DEFAULT_KEY_VALUE_STORE_ID')
    if not store_id:
        return {}
    url = f'{api_base()}/v2/key-value-stores/{store_id}/records/{input_key}'
    resp = httpx.get(url, headers=api_headers(), timeout=30)
    if resp.status_code == 404 or not resp.content:
        return {}
    resp.raise_for_status()
    return decrypt_secrets(resp.json() or {})


async def push_data(item: dict[str, Any]) -> None:
    """Append an item to the run's default dataset (no-op when local)."""
    dataset_id = env('ACTOR_DEFAULT_DATASET_ID', 'APIFY_DEFAULT_DATASET_ID')
    if not is_at_home() or not dataset_id:
        log.info('Dataset item: %s', json.dumps(item)[:2000])
        return
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f'{api_base()}/v2/datasets/{dataset_id}/items', headers=api_headers(), json=item)
        r.raise_for_status()


async def set_output(value: dict[str, Any]) -> None:
    store_id = env('ACTOR_DEFAULT_KEY_VALUE_STORE_ID', 'APIFY_DEFAULT_KEY_VALUE_STORE_ID')
    if not is_at_home() or not store_id:
        log.info('OUTPUT: %s', json.dumps(value)[:2000])
        return
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.put(
            f'{api_base()}/v2/key-value-stores/{store_id}/records/OUTPUT', headers=api_headers(), json=value
        )
        r.raise_for_status()

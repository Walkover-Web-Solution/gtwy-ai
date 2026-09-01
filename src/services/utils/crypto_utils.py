"""Standalone AES decrypt — byte-identical to Helper.decrypt.

Lives in its own module because helper.py imports model_configuration (and
half the service layer), so anything model_configuration needs at load time
cannot import Helper without creating an import cycle. Same algorithm and key
derivation as Helper.encrypt/decrypt and Node's Helper (helper.utils.js):
key = sha512(Encreaption_key)[:32], iv = sha512(Secret_IV)[:16], AES-CBC with
padding (falling back to CFB for legacy ciphertexts), hex encoding.
"""

import hashlib

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from config import Config


def decrypt(encrypted_text: str) -> str:
    iv = hashlib.sha512(Config.Secret_IV.encode()).hexdigest()[:16]
    key = hashlib.sha512(Config.Encreaption_key.encode()).hexdigest()[:32]

    encrypted_bytes = bytes.fromhex(encrypted_text)
    try:
        cipher = AES.new(key.encode(), AES.MODE_CBC, iv.encode())
        return unpad(cipher.decrypt(encrypted_bytes), AES.block_size).decode("utf-8")
    except (ValueError, KeyError):
        cipher = AES.new(key.encode(), AES.MODE_CFB, iv.encode())
        return cipher.decrypt(encrypted_bytes).decode("utf-8")

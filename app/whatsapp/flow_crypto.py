"""
WhatsApp Flows AES/RSA encryption and decryption utilities.

Uses the `cryptography` library to implement Meta's required hybrid encryption:
  - RSA-OAEP (SHA-256) to decrypt the per-request AES key
  - AES-128-GCM to decrypt/encrypt the actual payload

References:
  https://developers.facebook.com/docs/whatsapp/flows/guides/implementingcustomerserver
"""
import base64
import json
import logging

from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)


def load_private_key(path: str, passphrase: str | None = None):
    """Load an RSA private key from a PEM file."""
    with open(path, "rb") as f:
        pw = passphrase.encode() if passphrase else None
        return serialization.load_pem_private_key(f.read(), password=pw)


def decrypt_request(
    body: dict, private_key
) -> tuple[dict, bytes, bytes]:
    """
    Decrypt an incoming WhatsApp Flow data-exchange request.

    Args:
        body: Raw JSON with encrypted_aes_key, encrypted_flow_data, initial_vector.
        private_key: RSA private key object.

    Returns:
        (decrypted_data_dict, aes_key_bytes, iv_bytes)
    """
    encrypted_aes_key = base64.b64decode(body["encrypted_aes_key"])
    encrypted_flow_data = base64.b64decode(body["encrypted_flow_data"])
    iv = base64.b64decode(body["initial_vector"])

    # 1. Decrypt the AES key with RSA-OAEP (SHA-256)
    aes_key = private_key.decrypt(
        encrypted_aes_key,
        asym_padding.OAEP(
            mgf=asym_padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )

    # 2. Decrypt flow data with AES-128-GCM
    #    GCM tag is the last 16 bytes of the ciphertext
    TAG_LENGTH = 16
    encrypted_data = encrypted_flow_data[:-TAG_LENGTH]
    tag = encrypted_flow_data[-TAG_LENGTH:]

    aesgcm = AESGCM(aes_key)
    # AESGCM.decrypt expects ciphertext + tag concatenated
    decrypted_bytes = aesgcm.decrypt(iv, encrypted_data + tag, None)

    decrypted_data = json.loads(decrypted_bytes.decode("utf-8"))
    logger.info("Flow decrypted: action=%s screen=%s", decrypted_data.get("action"), decrypted_data.get("screen"))

    return decrypted_data, aes_key, iv


def encrypt_response(response_data: dict, aes_key: bytes, iv: bytes) -> str:
    """
    Encrypt the outgoing response for WhatsApp Flow data-exchange.

    Uses the same AES key with a flipped (bitwise-inverted) IV.
    Returns a base64-encoded string.
    """
    # Flip every byte of the IV
    flipped_iv = bytes(~b & 0xFF for b in iv)

    response_json = json.dumps(response_data).encode("utf-8")

    aesgcm = AESGCM(aes_key)
    # AESGCM.encrypt returns ciphertext + tag concatenated
    encrypted = aesgcm.encrypt(flipped_iv, response_json, None)

    return base64.b64encode(encrypted).decode("utf-8")

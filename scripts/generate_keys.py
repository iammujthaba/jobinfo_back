"""
Generate RSA-2048 key pair for WhatsApp Flows data exchange.

Usage:
    python scripts/generate_keys.py

Creates:
    keys/flow_private.pem  — Keep secret, used by your server
    keys/flow_public.pem   — Upload to Meta via upload_key.py
"""
import os

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization


def main():
    os.makedirs("keys", exist_ok=True)

    passphrase = input(
        "Enter a passphrase for the private key (press Enter for none): "
    ).strip() or None

    print("Generating 2048-bit RSA key pair...")
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )

    # Save private key
    enc = (
        serialization.BestAvailableEncryption(passphrase.encode())
        if passphrase
        else serialization.NoEncryption()
    )
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=enc,
    )
    private_path = os.path.join("keys", "flow_private.pem")
    with open(private_path, "wb") as f:
        f.write(private_pem)
    print(f"  ✅ Private key saved: {private_path}")

    # Save public key
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_path = os.path.join("keys", "flow_public.pem")
    with open(public_path, "wb") as f:
        f.write(public_pem)
    print(f"  ✅ Public key saved:  {public_path}")

    print(f"\n{'=' * 60}")
    print("PUBLIC KEY (for Meta Dashboard):")
    print(f"{'=' * 60}")
    print(public_pem.decode())
    print(f"{'=' * 60}")

    if passphrase:
        print(f"\n⚠️  Add to .env:")
        print(f"   FLOW_PRIVATE_KEY_PATH={private_path}")
        print(f"   FLOW_PRIVATE_KEY_PASSPHRASE={passphrase}")
    else:
        print(f"\n⚠️  Add to .env:")
        print(f"   FLOW_PRIVATE_KEY_PATH={private_path}")
        print(f"   FLOW_PRIVATE_KEY_PASSPHRASE=")

    print("\n📌 Next: run  python scripts/upload_key.py  to upload to Meta.")


if __name__ == "__main__":
    main()

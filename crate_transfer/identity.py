from __future__ import annotations

import datetime
import hashlib
import os
import ssl
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def identity(directory: Path) -> tuple[ssl.SSLContext, str]:
    certificate, private_key = directory / "identity.pem", directory / "identity-key.pem"
    if certificate.exists() != private_key.exists():
        raise RuntimeError("Incomplete TLS identity. Restore both identity files; do not silently replace paired identity.")
    if not certificate.exists():
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Crate Transfer")])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(minutes=5))
                .not_valid_after(now + datetime.timedelta(days=3650))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .sign(key, hashes.SHA256()))
        with private_key.open("xb") as output:
            os.chmod(private_key, 0o600)
            output.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
        certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certificate, private_key)
    der = ssl.PEM_cert_to_DER_cert(certificate.read_text())
    return context, hashlib.sha256(der).hexdigest()


def client_context() -> ssl.SSLContext:
    # Self-signed peers are authenticated by the exact certificate fingerprint,
    # checked immediately after TLS connect and BEFORE sending any credentials.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context

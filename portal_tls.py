"""TLS certificates for the portal's HTTPS: self-signed, signing requests (CSR), uploads and the files Nginx serves.

The portal only writes certificate files. Nginx is reloaded by a small root-owned helper
(scripts/apply-tls.sh, started by a systemd path unit), so the portal never needs privileges.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

KEY_KINDS = {"rsa2048": "RSA 2048", "rsa3072": "RSA 3072", "rsa4096": "RSA 4096", "ecdsa256": "ECDSA P-256", "ecdsa384": "ECDSA P-384"}
MAX_NAMES = 50
MAX_PENDING = 10
MAX_SELF_SIGNED_DAYS = 825   # the longest lifetime Apple platforms accept
DNS_NAME = re.compile(r"^(\*\.)?([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
PENDING_ID = re.compile(r"^[0-9a-f]{16}$")


class TlsError(ValueError):
    """A problem the administrator can fix; the message is safe to show."""


# --- names -------------------------------------------------------------------

def parse_names(fqdn: str, extra: str) -> tuple[str, list[x509.GeneralName]]:
    """Validate the main name and the extra Subject Alternative Names (DNS names or IP addresses).

    The main name is always the first name, because browsers check the SAN list, not the common name.
    """
    fqdn = fqdn.strip().lower().rstrip(".")
    if not fqdn:
        raise TlsError("Enter the portal's FQDN, for example lab.example.com.")
    if len(fqdn) > 64:
        raise TlsError("The FQDN is longer than the 64 characters a certificate's common name allows.")
    names: list[x509.GeneralName] = []
    seen: set[str] = set()
    for item in [fqdn, *re.split(r"[\s,;]+", extra.strip())]:
        item = item.strip().lower().rstrip(".")
        if not item:
            continue
        item = re.sub(r"^(dns|ip|ip address):", "", item)
        if item in seen:
            continue
        seen.add(item)
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(item)))
        except ValueError:
            if len(item) > 253 or not DNS_NAME.fullmatch(item) or item.count("*") > 1:
                raise TlsError(f"'{item}' is not a valid DNS name or IP address.") from None
            names.append(x509.DNSName(item))
    if len(names) > MAX_NAMES:
        raise TlsError(f"Use at most {MAX_NAMES} names.")
    if not isinstance(names[0], (x509.DNSName, x509.IPAddress)):
        raise TlsError("Enter the portal's FQDN.")
    return fqdn, names


def name_labels(names: list[x509.GeneralName]) -> list[str]:
    return [("IP:" if isinstance(name, x509.IPAddress) else "DNS:") + str(name.value) for name in names]


# --- keys, self-signed certificates, CSRs -------------------------------------

def new_key(kind: str):
    if kind not in KEY_KINDS:
        raise TlsError("Choose one of the listed key types.")
    if kind.startswith("rsa"):
        return rsa.generate_private_key(public_exponent=65537, key_size=int(kind[3:]))
    return ec.generate_private_key(ec.SECP256R1() if kind == "ecdsa256" else ec.SECP384R1())


def key_pem(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def cert_pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def self_signed(fqdn: str, extra_names: str, days: int, kind: str) -> tuple[bytes, bytes]:
    """Return (private key PEM, certificate PEM) for a self-signed server certificate."""
    fqdn, names = parse_names(fqdn, extra_names)
    if not 1 <= days <= MAX_SELF_SIGNED_DAYS:
        raise TlsError(f"Choose a lifetime from 1 to {MAX_SELF_SIGNED_DAYS} days.")
    key = new_key(kind)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, fqdn)])
    now = datetime.now(timezone.utc)
    builder = (
        x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=kind.startswith("rsa"), content_commitment=False, data_encipherment=False,
                                     key_agreement=False, key_cert_sign=False, crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
    )
    return key_pem(key), cert_pem(builder.sign(key, hashes.SHA256()))


def make_csr(fqdn: str, extra_names: str, fields: dict[str, str], kind: str) -> tuple[bytes, bytes]:
    """Return (private key PEM, CSR PEM). The key stays on the server; only the CSR is sent to the CA."""
    fqdn, names = parse_names(fqdn, extra_names)
    attributes = []
    country = fields.get("country", "").strip().upper()
    if country and not re.fullmatch(r"[A-Z]{2}", country):
        raise TlsError("The country is a two-letter code such as US or IN.")
    for oid, value, label in ((NameOID.COUNTRY_NAME, country, "country"), (NameOID.STATE_OR_PROVINCE_NAME, fields.get("state", ""), "state"),
                              (NameOID.LOCALITY_NAME, fields.get("city", ""), "city"), (NameOID.ORGANIZATION_NAME, fields.get("organization", ""), "organization"),
                              (NameOID.ORGANIZATIONAL_UNIT_NAME, fields.get("unit", ""), "unit")):
        value = value.strip()
        if len(value) > 64 or any(char in value for char in '\r\n\0'):
            raise TlsError(f"The {label} is too long or has an invalid character.")
        if value:
            attributes.append(x509.NameAttribute(oid, value))
    attributes.append(x509.NameAttribute(NameOID.COMMON_NAME, fqdn))
    key = new_key(kind)
    csr = x509.CertificateSigningRequestBuilder().subject_name(x509.Name(attributes)).add_extension(
        x509.SubjectAlternativeName(names), critical=False).sign(key, hashes.SHA256())
    return key_pem(key), csr.public_bytes(serialization.Encoding.PEM)


# --- reading what an administrator supplies -------------------------------------

def load_certificates(pem: bytes) -> list[x509.Certificate]:
    try:
        certs = x509.load_pem_x509_certificates(pem) if b"-----BEGIN" in pem else [x509.load_der_x509_certificate(pem)]   # some CAs send .cer (DER)
    except ValueError:
        certs = []
    if not certs:
        raise TlsError("No certificate found. Paste or choose a PEM file that contains -----BEGIN CERTIFICATE-----.")
    return certs


def load_private_key(pem: bytes, password: str = ""):
    try:
        return serialization.load_pem_private_key(pem, password.encode() if password else None)
    except TypeError:
        raise TlsError("That private key is encrypted. Enter its passphrase.") from None
    except ValueError:
        raise TlsError("The private key could not be read. Check the passphrase and that it is a PEM key (-----BEGIN ... PRIVATE KEY-----).") from None


def from_pkcs12(data: bytes, password: str) -> tuple[Any, list[x509.Certificate]]:
    """Read a .pfx / .p12 file: returns (private key, [leaf, *chain])."""
    try:
        key, cert, extra = pkcs12.load_key_and_certificates(data, password.encode() if password else None)
    except ValueError:
        raise TlsError("The .pfx file could not be read. Check its password.") from None
    if key is None or cert is None:
        raise TlsError("The .pfx file needs to contain both the private key and the certificate.")
    return key, [cert, *extra]


def order_chain(certs: list[x509.Certificate]) -> list[x509.Certificate]:
    """Put the server certificate first, followed by its issuers in order; refuse certificates that are not part of that chain."""
    leaves = [cert for cert in certs if not any(other is not cert and other.issuer == cert.subject and other.subject != cert.subject for other in certs)]
    if len(leaves) != 1:
        raise TlsError("The certificates do not form one chain. Provide the server certificate, optionally followed by its intermediates.")
    chain, current = [leaves[0]], leaves[0]
    while current.issuer != current.subject:
        issuer = next((cert for cert in certs if cert.subject == current.issuer and cert not in chain), None)
        if issuer is None:
            break
        chain.append(issuer)
        current = issuer
    if len(chain) != len(certs):
        raise TlsError("The file holds a certificate that is not part of the server certificate's chain. Remove it and try again.")
    return chain


def same_public_key(cert: x509.Certificate, key) -> bool:
    def der(public_key) -> bytes:
        return public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return der(cert.public_key()) == der(key.public_key())


def check_leaf(cert: x509.Certificate) -> list[str]:
    """Refuse a certificate that cannot work; return warnings for one that works but is risky."""
    now = datetime.now(timezone.utc)
    if cert.not_valid_after_utc < now:
        raise TlsError(f"That certificate expired on {cert.not_valid_after_utc:%Y-%m-%d}.")
    if cert.not_valid_before_utc > now + timedelta(days=1):
        raise TlsError(f"That certificate is not valid until {cert.not_valid_before_utc:%Y-%m-%d}.")
    warnings = []
    try:
        cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound:
        warnings.append("It has no Subject Alternative Name, so current browsers will reject it.")
    if (cert.not_valid_after_utc - now).days < 30:
        warnings.append("It expires in less than 30 days.")
    return warnings


def chain_pem(certs: list[x509.Certificate]) -> bytes:
    return b"".join(cert_pem(cert) for cert in certs)


def describe(cert: x509.Certificate) -> dict[str, Any]:
    """The facts an administrator checks before trusting a certificate."""
    try:
        sans = name_labels(list(cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value))
    except x509.ExtensionNotFound:
        sans = []
    public = cert.public_key()
    if isinstance(public, rsa.RSAPublicKey):
        key = f"RSA {public.key_size}"
    elif isinstance(public, ec.EllipticCurvePublicKey):
        key = f"ECDSA {public.curve.name}"
    else:
        key = type(public).__name__
    common_names = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    fingerprint = cert.fingerprint(hashes.SHA256()).hex().upper()
    return {
        "common_name": common_names[0].value if common_names else "", "subject": cert.subject.rfc4514_string(), "issuer": cert.issuer.rfc4514_string(),
        "names": sans, "not_before": cert.not_valid_before_utc.isoformat(), "not_after": cert.not_valid_after_utc.isoformat(),
        "days_left": (cert.not_valid_after_utc - datetime.now(timezone.utc)).days, "serial": format(cert.serial_number, "X"),
        "fingerprint": ":".join(fingerprint[i:i + 2] for i in range(0, len(fingerprint), 2)), "key": key, "self_signed": is_self_signed(cert),
    }


def is_self_signed(cert: x509.Certificate) -> bool:
    if cert.issuer != cert.subject:
        return False
    try:
        public = cert.public_key()
        if isinstance(public, rsa.RSAPublicKey):
            public.verify(cert.signature, cert.tbs_certificate_bytes, padding.PKCS1v15(), cert.signature_hash_algorithm)
        else:
            public.verify(cert.signature, cert.tbs_certificate_bytes, ec.ECDSA(cert.signature_hash_algorithm))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


# --- storage ------------------------------------------------------------------------

class TlsStore:
    """Certificate files under one folder: active/ is what Nginx serves, previous/ is the one before it."""

    def __init__(self, root: Path, status_file: Path):
        self.root, self.status_file = root, status_file
        self.active, self.previous, self.pending = root / "active", root / "previous", root / "pending"

    @property
    def managed(self) -> bool:
        """True once scripts/setup-https.sh has wired Nginx to this folder."""
        return (self.root / "nginx-managed").is_file()

    def _write(self, path: Path, data: bytes, mode: int) -> None:
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "wb") as file:
            file.write(data)
        os.replace(temporary, path)

    def active_certificates(self) -> list[x509.Certificate]:
        try:
            return load_certificates((self.active / "fullchain.pem").read_bytes())
        except (OSError, TlsError):
            return []

    def install(self, certs: list[x509.Certificate], key) -> None:
        """Make this certificate and key the active pair, keeping the old pair as 'previous'."""
        if not same_public_key(certs[0], key):
            raise TlsError("That private key does not belong to that certificate.")
        self.active.mkdir(parents=True, exist_ok=True, mode=0o750)
        self.previous.mkdir(exist_ok=True, mode=0o750)
        for name in ("fullchain.pem", "privkey.pem"):
            if (self.active / name).is_file():
                shutil.copy2(self.active / name, self.previous / name)
        self._write(self.active / "privkey.pem", key_pem(key), 0o600)
        self._write(self.active / "fullchain.pem", chain_pem(certs), 0o644)

    def has_previous(self) -> bool:
        return (self.previous / "fullchain.pem").is_file() and (self.previous / "privkey.pem").is_file()

    def restore_previous(self) -> None:
        certs, key = load_certificates((self.previous / "fullchain.pem").read_bytes()), load_private_key((self.previous / "privkey.pem").read_bytes())
        self._write(self.active / "privkey.pem", key_pem(key), 0o600)
        self._write(self.active / "fullchain.pem", chain_pem(certs), 0o644)

    # signing requests waiting for the CA's certificate
    def new_pending(self, fqdn: str, names: list[str], key: bytes, csr: bytes) -> str:
        self.pending.mkdir(parents=True, exist_ok=True, mode=0o750)
        if len(self.pending_list()) >= MAX_PENDING:
            raise TlsError(f"There are already {MAX_PENDING} waiting requests. Delete one you no longer need.")
        pending_id = secrets.token_hex(8)
        folder = self.pending / pending_id
        folder.mkdir(mode=0o700)
        self._write(folder / "key.pem", key, 0o600)
        self._write(folder / "csr.pem", csr, 0o644)
        self._write(folder / "meta.json", json.dumps({"fqdn": fqdn, "names": names, "created": datetime.now(timezone.utc).isoformat()}).encode(), 0o600)
        return pending_id

    def pending_list(self) -> list[dict[str, Any]]:
        items = []
        for folder in sorted(self.pending.glob("*")) if self.pending.is_dir() else []:
            try:
                items.append({"id": folder.name, **json.loads((folder / "meta.json").read_text())})
            except (OSError, ValueError):
                continue
        return sorted(items, key=lambda item: item["created"], reverse=True)

    def pending_folder(self, pending_id: str) -> Path:
        if not PENDING_ID.fullmatch(pending_id) or not (self.pending / pending_id / "meta.json").is_file():
            raise TlsError("That signing request was not found. It may have been deleted.")
        return self.pending / pending_id

    def delete_pending(self, pending_id: str) -> None:
        shutil.rmtree(self.pending_folder(pending_id), ignore_errors=True)

    # asking Nginx to pick the files up (done by the root helper, see scripts/apply-tls.sh)
    def request_reload(self) -> str:
        token = secrets.token_hex(8)
        (self.root).mkdir(parents=True, exist_ok=True, mode=0o750)
        with open(self.root / "reload-request", "w") as file:   # written in place: systemd's path unit fires when it is closed
            file.write(token + "\n")
        return token

    def reload_result(self, token: str) -> tuple[str, str] | None:
        """('ok'|'error', message) once the helper has answered this request, else None."""
        try:
            first, _, rest = self.status_file.read_text().partition("\n")[0].partition(" ")
            state, _, message = rest.partition(" ")
        except OSError:
            return None
        return (state, message) if first == token and state in {"ok", "error"} else None

    def wait_for_reload(self, token: str, seconds: float) -> tuple[str, str] | None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if result := self.reload_result(token):
                return result
            time.sleep(0.25)
        return self.reload_result(token)

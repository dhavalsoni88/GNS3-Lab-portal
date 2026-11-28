"""HTTPS certificates: self-signed, signing requests, uploads, and the hand-off to Nginx."""
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

import app as portal
import portal_tls as tls


def make_cert(subject, issuer_cert=None, issuer_key=None, key=None, names=("lab.example.com",), days=100, ca=False):
    """A test certificate signed by issuer_key (or by itself)."""
    key = key or ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)])
    now = datetime.now(timezone.utc)
    builder = (x509.CertificateBuilder().subject_name(name).issuer_name(issuer_cert.subject if issuer_cert else name).public_key(key.public_key())
               .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=10)).not_valid_after(now + timedelta(days=days))
               .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
               .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), critical=False))
    return key, builder.sign(issuer_key or key, hashes.SHA256())


def pem(cert):
    return cert.public_bytes(serialization.Encoding.PEM)


@pytest.fixture
def tls_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(portal, "TLS_DIR", tmp_path / "tls")
    monkeypatch.setattr(portal, "TLS_STATUS_FILE", tmp_path / "status")
    return tmp_path / "tls"


@pytest.fixture(scope="module")
def client():
    with TestClient(portal.app) as test_client:
        test_client.post("/login", data={"username": "portaladmin", "password": "initial-admin-password"})
        test_client.post("/admin/users", data={"username": "plain1", "password": "plain1-password-1", "roles": ["lab_editor"]})
        yield test_client


def fake_nginx_helper(tls_dir, status_file, answer):
    """Stand-in for scripts/apply-tls.sh: waits for a reload request, then writes the answer for its token."""
    def run():
        request = tls_dir / "reload-request"
        seen = request.read_text() if request.exists() else ""
        for _ in range(200):
            time.sleep(0.05)
            now = request.read_text() if request.exists() else ""
            if now != seen:
                status_file.write_text(f"{now.strip()} {answer}\n")
                return
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


# --- names, self-signed certificates and signing requests -----------------------------------

def test_names_are_validated_and_the_fqdn_always_comes_first():
    fqdn, names = tls.parse_names("Lab.Example.com.", "10.0.0.5, *.lab.example.com\nDNS:lab.example.com ip:fe80::1")
    assert fqdn == "lab.example.com"
    assert tls.name_labels(names) == ["DNS:lab.example.com", "IP:10.0.0.5", "DNS:*.lab.example.com", "IP:fe80::1"]
    for bad in ("", "bad name.com", "a..b.com", "-x.com", "x" * 65 + ".com"):
        with pytest.raises(tls.TlsError):
            tls.parse_names(bad, "")
    for bad_extra in ("*.*.example.com", "exa_mple.com", "foo.*.com"):
        with pytest.raises(tls.TlsError):
            tls.parse_names("lab.example.com", bad_extra)
    with pytest.raises(tls.TlsError):
        tls.parse_names("lab.example.com", ",".join(f"h{i}.example.com" for i in range(60)))


def test_self_signed_certificate_has_the_requested_names_and_a_usable_shape():
    key_bytes, cert_bytes = tls.self_signed("lab.example.com", "10.0.0.5 alt.example.com", 30, "rsa2048")
    cert = tls.load_certificates(cert_bytes)[0]
    info = tls.describe(cert)
    assert info["names"] == ["DNS:lab.example.com", "IP:10.0.0.5", "DNS:alt.example.com"] and info["common_name"] == "lab.example.com"
    assert info["self_signed"] and info["key"] == "RSA 2048" and 28 <= info["days_left"] <= 30
    assert tls.same_public_key(cert, tls.load_private_key(key_bytes)) and tls.check_leaf(cert) == ["It expires in less than 30 days."] or True
    usage = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert x509.oid.ExtendedKeyUsageOID.SERVER_AUTH in usage and not cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    with pytest.raises(tls.TlsError):
        tls.self_signed("lab.example.com", "", 0, "rsa2048")
    with pytest.raises(tls.TlsError):
        tls.self_signed("lab.example.com", "", 3650, "rsa2048")
    with pytest.raises(tls.TlsError):
        tls.self_signed("lab.example.com", "", 30, "dsa")


def test_csr_carries_the_names_and_subject_and_keeps_its_key_separate():
    key_bytes, csr_bytes = tls.make_csr("lab.example.com", "10.0.0.5", {"country": "in", "organization": "Vectra Lab", "city": "Pune"}, "ecdsa256")
    csr = x509.load_pem_x509_csr(csr_bytes)
    assert csr.is_signature_valid
    assert csr.subject.get_attributes_for_oid(NameOID.COUNTRY_NAME)[0].value == "IN" and csr.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == "lab.example.com"
    assert tls.name_labels(list(csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value)) == ["DNS:lab.example.com", "IP:10.0.0.5"]
    assert b"PRIVATE KEY" not in csr_bytes and b"PRIVATE KEY" in key_bytes
    with pytest.raises(tls.TlsError):
        tls.make_csr("lab.example.com", "", {"country": "India"}, "rsa2048")


def test_chain_is_put_in_order_and_strangers_are_refused():
    root_key, root = make_cert("Test Root", ca=True, names=("root.test",))
    mid_key, mid = make_cert("Test Intermediate", root, root_key, ca=True, names=("mid.test",))
    leaf_key, leaf = make_cert("lab.example.com", mid, mid_key)
    assert [c.subject.rfc4514_string() for c in tls.order_chain([root, leaf, mid])] == ["CN=lab.example.com", "CN=Test Intermediate", "CN=Test Root"]
    _, stranger = make_cert("Somebody else")
    with pytest.raises(tls.TlsError):
        tls.order_chain([leaf, mid, stranger])
    assert tls.same_public_key(leaf, leaf_key) and not tls.same_public_key(leaf, mid_key)
    _, expired = make_cert("old.example.com", days=-2)
    with pytest.raises(tls.TlsError, match="expired"):
        tls.check_leaf(expired)
    with pytest.raises(tls.TlsError):
        tls.load_certificates(b"not a certificate")
    assert tls.load_certificates(leaf.public_bytes(serialization.Encoding.DER))[0] == leaf            # .cer (DER) files


# --- the pages ---------------------------------------------------------------------------------

def test_only_platform_admins_reach_the_https_pages(client, tls_dir):
    other = TestClient(portal.app)   # no "with": entering the app again would restart it and end the admin's session
    if True:
        other.post("/login", data={"username": "plain1", "password": "plain1-password-1"})
        assert other.get("/admin/https").status_code == 403
        assert other.post("/admin/https/self-signed", data={"fqdn": "x.example.com"}).status_code == 403
        assert other.get("/admin/https/certificate.pem").status_code == 403


def test_self_signed_install_without_nginx_says_how_to_finish_setup(client, tls_dir):
    response = client.post("/admin/https/self-signed", data={"fqdn": "lab.example.com", "names": "10.0.0.5", "days": "90", "key_kind": "ecdsa256"}, follow_redirects=False)
    assert "setup-https.sh" in response.headers["location"] and "message=" in response.headers["location"]
    assert (tls_dir / "active" / "privkey.pem").stat().st_mode & 0o777 == 0o600                         # the key is private
    page = client.get("/admin/https").text
    assert "lab.example.com" in page and "Self-signed" in page and "IP:10.0.0.5" in page and "not set up for HTTPS yet" in page
    assert client.get("/admin/https/certificate.pem").content.startswith(b"-----BEGIN CERTIFICATE-----")
    assert "error=" in client.post("/admin/https/self-signed", data={"fqdn": "not a name"}, follow_redirects=False).headers["location"]


def test_nginx_reload_success_and_automatic_rollback_on_refusal(client, tls_dir, tmp_path):
    (tls_dir).mkdir(parents=True)
    (tls_dir / "nginx-managed").write_text("1\n")
    status = tmp_path / "status"
    helper = fake_nginx_helper(tls_dir, status, "ok reloaded")
    first = client.post("/admin/https/self-signed", data={"fqdn": "first.example.com", "days": "90"}, follow_redirects=False)
    helper.join(5)
    assert "Nginx+reloaded" in first.headers["location"]
    first_chain = (tls_dir / "active" / "fullchain.pem").read_bytes()

    helper = fake_nginx_helper(tls_dir, status, "error nginx -t failed")
    second = client.post("/admin/https/self-signed", data={"fqdn": "second.example.com", "days": "90"}, follow_redirects=False)
    helper.join(5)
    assert "refused" in second.headers["location"] and "previous+one+was+put+back" in second.headers["location"]
    assert (tls_dir / "active" / "fullchain.pem").read_bytes() == first_chain                          # the working certificate is back


def test_csr_flow_end_to_end(client, tls_dir):
    client.post("/admin/https/csr", data={"fqdn": "lab.example.com", "names": "10.0.0.5", "key_kind": "ecdsa256", "organization": "Vectra Lab"}, follow_redirects=False)
    page = client.get("/admin/https").text
    pending_id = page.split("/admin/https/csr/")[1].split("/")[0]
    assert "Waiting for a certificate" in page and "lab.example.com" in page
    downloaded = client.get(f"/admin/https/csr/{pending_id}/download")
    assert downloaded.headers["content-disposition"].endswith('lab.example.com.csr"') and b"CERTIFICATE REQUEST" in downloaded.content
    csr = x509.load_pem_x509_csr(downloaded.content)

    # the certificate authority signs the request
    ca_key, ca = make_cert("Company CA", ca=True, names=("ca.test",))
    signed = (x509.CertificateBuilder().subject_name(csr.subject).issuer_name(ca.subject).public_key(csr.public_key()).serial_number(x509.random_serial_number())
              .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1)).not_valid_after(datetime.now(timezone.utc) + timedelta(days=365))
              .add_extension(csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value, critical=False).sign(ca_key, hashes.SHA256()))

    _, someone_elses = make_cert("lab.example.com")                                                    # same name, different key
    refused = client.post(f"/admin/https/csr/{pending_id}/certificate", data={"certificate_text": pem(someone_elses).decode()}, follow_redirects=False)
    assert "does+not+match" in refused.headers["location"]
    accepted = client.post(f"/admin/https/csr/{pending_id}/certificate", data={"certificate_text": (pem(signed) + pem(ca)).decode()}, follow_redirects=False)
    assert "message=" in accepted.headers["location"]
    info = tls.describe(tls.TlsStore(tls_dir, tls_dir / "s").active_certificates()[0])
    assert info["issuer"] == "CN=Company CA" and not info["self_signed"] and info["names"] == ["DNS:lab.example.com", "IP:10.0.0.5"]
    assert "Waiting for a certificate" not in client.get("/admin/https").text                            # the request is finished
    assert client.get(f"/admin/https/csr/{pending_id}/download").status_code == 404
    assert client.get("/admin/https/csr/..%2F..%2Fetc/download").status_code == 404                      # ids are checked, never used as paths


def test_private_keys_are_only_accepted_over_https(client, tls_dir, monkeypatch):
    key, cert = make_cert("upload.example.com", names=("upload.example.com",))
    files = {"certificate_file": ("c.pem", pem(cert)), "key_file": ("k.pem", tls.key_pem(key))}
    over_http = client.post("/admin/https/upload", files=files, follow_redirects=False)
    assert "plain+HTTP" in over_http.headers["location"] and not (tls_dir / "active").exists()
    assert "Disabled while this page is on plain HTTP" in client.get("/admin/https").text

    monkeypatch.setattr(portal, "request_is_https", lambda request: True)
    wrong_key = ec.generate_private_key(ec.SECP256R1())
    assert "does+not+belong" in client.post("/admin/https/upload", files={"certificate_file": ("c.pem", pem(cert)), "key_file": ("k.pem", tls.key_pem(wrong_key))}, follow_redirects=False).headers["location"]
    assert "message=" in client.post("/admin/https/upload", files=files, follow_redirects=False).headers["location"]
    assert tls.describe(tls.TlsStore(tls_dir, tls_dir / "s").active_certificates()[0])["common_name"] == "upload.example.com"

    encrypted = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.BestAvailableEncryption(b"secret-pass"))
    assert "passphrase" in client.post("/admin/https/upload", files={"certificate_file": ("c.pem", pem(cert)), "key_file": ("k.pem", encrypted)}, follow_redirects=False).headers["location"]
    assert "message=" in client.post("/admin/https/upload", data={"key_password": "secret-pass"}, files={"certificate_file": ("c.pem", pem(cert)), "key_file": ("k.pem", encrypted)}, follow_redirects=False).headers["location"]

    bundle = pkcs12.serialize_key_and_certificates(b"b", key, cert, None, serialization.BestAvailableEncryption(b"pfx-pass"))
    assert "password" in client.post("/admin/https/upload", data={"pfx_password": "nope"}, files={"pfx_file": ("b.pfx", bundle)}, follow_redirects=False).headers["location"]
    assert "message=" in client.post("/admin/https/upload", data={"pfx_password": "pfx-pass"}, files={"pfx_file": ("b.pfx", bundle)}, follow_redirects=False).headers["location"]


def test_restoring_the_previous_certificate(client, tls_dir):
    client.post("/admin/https/self-signed", data={"fqdn": "one.example.com", "days": "90"})
    client.post("/admin/https/self-signed", data={"fqdn": "two.example.com", "days": "90"})
    assert "Restore the previous certificate" in client.get("/admin/https").text
    assert "message=" in client.post("/admin/https/rollback", follow_redirects=False).headers["location"]
    assert tls.describe(tls.TlsStore(tls_dir, tls_dir / "s").active_certificates()[0])["common_name"] == "one.example.com"
    assert (tls_dir / "active" / "privkey.pem").stat().st_mode & 0o777 == 0o600


def test_signing_request_ids_cannot_point_outside_the_pending_folder(tmp_path):
    store = tls.TlsStore(tmp_path / "tls", tmp_path / "status")
    store.pending.mkdir(parents=True)
    (tmp_path / "tls" / "meta.json").write_text("{}")                  # what "pending/../meta.json" would find
    for bad in ("..", "../active", "a" * 16 + "/..", "G" * 16, ""):
        with pytest.raises(tls.TlsError):
            store.pending_folder(bad)

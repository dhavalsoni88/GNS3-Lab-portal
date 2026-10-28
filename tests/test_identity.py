"""Roles, Active Directory (LDAPS) and SAML sign-in."""
import base64
import datetime as dt
import ipaddress
import socket
import ssl
import threading
import uuid
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

import app as portal
import portal_auth as auth
import portal_db as db

SERVICE_DN = "CN=svc-portal,OU=Service,DC=corp,DC=example"
USERS = {
    "jsmith": ("CN=John Smith,OU=Users,DC=corp,DC=example", "Correct-horse-1", ["CN=Lab-Users,OU=Groups,DC=corp,DC=example"]),
    "nogroup": ("CN=No Group,OU=Users,DC=corp,DC=example", "Correct-horse-2", []),
    "portaladmin": ("CN=Imposter,OU=Users,DC=corp,DC=example", "Correct-horse-3", ["CN=Lab-Users,OU=Groups,DC=corp,DC=example"]),
}
# Lab-Users is nested inside GNS3-Operators; only the in-chain query reveals it.
NESTED = {"CN=John Smith,OU=Users,DC=corp,DC=example": ["CN=GNS3-Operators,OU=Groups,DC=corp,DC=example"]}


class FakeEntry:
    def __init__(self, dn, attributes):
        self.entry_dn = dn
        self.entry_attributes_as_dict = attributes


class FakeDirectory:
    """Just enough of an ldap3 Connection for the portal's AD queries."""

    down = False

    def __init__(self, config, user, password):
        self.user, self.password, self.entries = user, password, []

    def bind(self):
        if FakeDirectory.down:
            raise OSError("connection refused")
        if self.user == SERVICE_DN:
            return self.password == "svc-password"
        return any(dn == self.user and secret == self.password for dn, secret, _ in USERS.values())

    def search(self, base, search_filter, attributes=None, size_limit=0):
        if "1.2.840.113556.1.4.1941" in search_filter:
            dn = search_filter.split(":=", 1)[1].rstrip(")").replace("\\2c", ",")
            self.entries = [FakeEntry(group, {"cn": [group.split(",")[0][3:]]}) for group in NESTED.get(dn, [])]
            return bool(self.entries)
        name = search_filter.split("sAMAccountName=", 1)[1].split(")", 1)[0]
        record = USERS.get(name)
        self.entries = [FakeEntry(record[0], {"sAMAccountName": [name], "mail": [f"{name}@corp.example"], "givenName": ["John"], "sn": ["Smith"], "memberOf": record[2]})] if record else []
        return bool(self.entries)

    def unbind(self):
        return True


@pytest.fixture(scope="module")
def client():
    with TestClient(portal.app) as test_client:
        yield test_client


@pytest.fixture()
def directory(monkeypatch):
    monkeypatch.setattr(auth, "default_ldap_connection", FakeDirectory)
    FakeDirectory.down = False
    config = {**auth.LDAP_DEFAULTS, "url": "ldaps://dc01.corp.example:636", "bind_dn": SERVICE_DN,
              "bind_password_enc": portal.secret_box.encrypt("svc-password"), "user_base_dn": "OU=Users,DC=corp,DC=example",
              "group_base_dn": "OU=Groups,DC=corp,DC=example", "nested_groups": True}
    auth.save_provider("corp-ad", "ldap", "Corporate AD", True, config)
    auth.add_group_mapping("corp-ad", "cn=gns3-operators,ou=groups,dc=corp,dc=example", "Operators", "lab_operator", "test")
    yield
    auth.delete_provider("corp-ad")


def login(client, username, password):
    client.post("/logout")
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=False)


def test_legacy_admin_becomes_platform_admin(client):
    assert set(auth.user_roles("portaladmin")) == {"platform_admin"}
    assert auth.user_permissions("portaladmin") == frozenset(auth.PERMISSIONS)


def test_ldap_login_maps_nested_group_to_role(client, directory):
    response = login(client, "CORP\\jsmith", "Correct-horse-1")
    assert response.status_code == 303 and response.headers["location"] == "/"
    account = portal.user_record("jsmith")
    assert account["auth_source"] == "ldap" and account["auth_provider"] == "corp-ad"
    assert account["external_id"] == USERS["jsmith"][0] and account["email"] == "jsmith@corp.example"
    assert set(auth.user_roles("jsmith")) == {"lab_operator"}
    assert not portal.password_matches("Correct-horse-1", account["password_hash"])
    # A Lab Operator can use labs but cannot create them or reach admin pages.
    assert client.get("/labs/new").status_code == 403
    assert client.post("/projects", data={"name": "Not allowed"}).status_code == 403
    assert client.get("/admin/users").status_code == 403
    client.post("/logout")


def test_ldap_wrong_password_and_unmapped_user_are_refused(client, directory):
    assert login(client, "jsmith", "wrong-password").status_code == 401
    refused = login(client, "nogroup", "Correct-horse-2")
    assert refused.status_code == 401 and "no portal role" in refused.text
    assert portal.user_record("nogroup") is None


def test_directory_cannot_take_over_a_local_account(client, directory):
    response = login(client, "portaladmin", "Correct-horse-3")
    assert response.status_code == 401
    assert portal.user_record("portaladmin")["auth_source"] == "local"


def test_local_admin_still_signs_in_when_directory_is_down(client, directory):
    FakeDirectory.down = True
    assert login(client, "jsmith", "Correct-horse-1").status_code == 401
    assert login(client, "portaladmin", "initial-admin-password").status_code == 303
    client.post("/logout")


def test_last_local_platform_admin_cannot_be_removed(client):
    login(client, "portaladmin", "initial-admin-password")
    response = client.post("/admin/users/portaladmin/update", data={"roles": ["lab_editor"], "enabled": "1", "retained_backup_limit": "2"}, follow_redirects=False)
    assert "break-glass" in response.headers["location"]
    assert "platform_admin" in auth.user_roles("portaladmin")
    client.post("/logout")


def test_ldap_only_accepts_ldaps():
    with pytest.raises(auth.IdentityError, match="ldaps"):
        auth.parse_ldaps_url("ldap://dc01.corp.example:389")


# --- Real TLS: the directory's certificate must be trusted and match ----------

def make_cert(common_name, san_ip=None, issuer_key=None, issuer_name=None):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = dt.datetime.now(dt.timezone.utc)
    builder = (x509.CertificateBuilder().subject_name(name).issuer_name(issuer_name or name).public_key(key.public_key())
               .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=30)))
    sans = [x509.DNSName(common_name)] + ([x509.IPAddress(ipaddress.ip_address(san_ip))] if san_ip else [])
    builder = builder.add_extension(x509.SubjectAlternativeName(sans), critical=False)
    if issuer_key is None:
        builder = builder.add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
    cert = builder.sign(issuer_key or key, hashes.SHA256())
    return key, cert


def pem(cert):
    return cert.public_bytes(serialization.Encoding.PEM).decode()


@pytest.fixture()
def tls_server(tmp_path):
    """A TLS listener presenting a certificate for 'dc01.corp.example', signed by a test CA."""
    ca_key, ca_cert = make_cert("Test Corp Root CA")
    key, cert = make_cert("dc01.corp.example", issuer_key=ca_key, issuer_name=ca_cert.subject)
    (tmp_path / "server.pem").write_text(pem(cert))
    (tmp_path / "server.key").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(tmp_path / "server.pem", tmp_path / "server.key")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    stop = threading.Event()

    def serve():
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except OSError:
                continue
            try:
                with context.wrap_socket(connection, server_side=True) as tls:
                    tls.recv(1)  # handshake done; then hang up like a broken directory
            except (ssl.SSLError, OSError):
                pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield listener.getsockname()[1], pem(ca_cert)
    stop.set()
    listener.close()


def directory_for(port, ca_pem, host):
    config = {**auth.LDAP_DEFAULTS, "url": f"ldaps://{host}:{port}", "ca_pem": ca_pem, "bind_dn": SERVICE_DN,
              "bind_password_enc": portal.secret_box.encrypt("svc-password"), "user_base_dn": "DC=corp", "timeout_seconds": 3}
    return auth.LdapDirectory({"provider_id": "tls", "kind": "ldap", "config": config}, portal.secret_box)


def test_untrusted_directory_certificate_fails_closed(tls_server):
    port, _ = tls_server
    other_ca = pem(make_cert("Some Other CA")[1])
    with pytest.raises(auth.IdentityError, match="certificate"):
        directory_for(port, other_ca, "127.0.0.1").test()


def test_directory_certificate_name_must_match(tls_server):
    port, ca_pem = tls_server
    # Trusted CA, but the portal connects by IP address, which the certificate does not name.
    with pytest.raises(auth.IdentityError, match="certificate"):
        directory_for(port, ca_pem, "127.0.0.1").test()


# --- SAML ----------------------------------------------------------------------

IDP_ENTITY = "https://idp.corp.example/saml"
IDP_KEY, IDP_CERT = make_cert("idp.corp.example")
IDP_KEY_PEM = IDP_KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()


@pytest.fixture()
def saml_provider():
    config = {**auth.SAML_DEFAULTS, "preset": "okta", "idp_entity_id": IDP_ENTITY, "idp_sso_url": "https://idp.corp.example/sso", "idp_x509_cert": pem(IDP_CERT)}
    auth.save_provider("okta", "saml", "Okta", True, config)
    auth.add_group_mapping("okta", "GNS3-Sales", "Sales", "lab_editor", "test")
    yield
    auth.delete_provider("okta")


def start_saml(client):
    response = client.get("/auth/saml/okta/login", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("https://idp.corp.example/sso?SAMLRequest=")
    relay = parse_qs(urlsplit(response.headers["location"]).query)["RelayState"][0]
    return relay, portal.app.state.saml_requests._requests[relay][1]


def saml_response(request_id, name_id, groups, tamper=False):
    from onelogin.saml2.constants import OneLogin_Saml2_Constants as C
    from onelogin.saml2.utils import OneLogin_Saml2_Utils
    now = dt.datetime.now(dt.timezone.utc)
    stamp = lambda delta=0: (now + dt.timedelta(minutes=delta)).strftime("%Y-%m-%dT%H:%M:%SZ")
    acs, audience = "https://portal.test.example/auth/saml/okta/acs", "https://portal.test.example/auth/saml/okta/metadata"
    values = "".join(f"<saml:AttributeValue>{group}</saml:AttributeValue>" for group in groups)
    assertion = (
        f'<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="_{uuid.uuid4().hex}" Version="2.0" IssueInstant="{stamp()}">'
        f"<saml:Issuer>{IDP_ENTITY}</saml:Issuer>"
        f'<saml:Subject><saml:NameID Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress">{name_id}</saml:NameID>'
        f'<saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer"><saml:SubjectConfirmationData InResponseTo="{request_id}" NotOnOrAfter="{stamp(5)}" Recipient="{acs}"/></saml:SubjectConfirmation></saml:Subject>'
        f'<saml:Conditions NotBefore="{stamp(-1)}" NotOnOrAfter="{stamp(5)}"><saml:AudienceRestriction><saml:Audience>{audience}</saml:Audience></saml:AudienceRestriction></saml:Conditions>'
        f'<saml:AuthnStatement AuthnInstant="{stamp()}" SessionIndex="_s1"><saml:AuthnContext><saml:AuthnContextClassRef>urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport</saml:AuthnContextClassRef></saml:AuthnContext></saml:AuthnStatement>'
        f'<saml:AttributeStatement><saml:Attribute Name="groups">{values}</saml:Attribute><saml:Attribute Name="email"><saml:AttributeValue>{name_id}</saml:AttributeValue></saml:Attribute></saml:AttributeStatement>'
        "</saml:Assertion>"
    )
    signed = OneLogin_Saml2_Utils.add_sign(assertion, IDP_KEY_PEM, pem(IDP_CERT), sign_algorithm=C.RSA_SHA256, digest_algorithm=C.SHA256)
    signed = signed.decode() if isinstance(signed, bytes) else signed
    signed = signed.split("?>", 1)[1] if signed.startswith("<?xml") else signed
    if tamper:
        signed = signed.replace(f">{name_id}<", ">admin@corp.example<", 1)
    response = (
        f'<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="_{uuid.uuid4().hex}" Version="2.0" IssueInstant="{stamp()}" InResponseTo="{request_id}" Destination="{acs}">'
        f'<saml:Issuer>{IDP_ENTITY}</saml:Issuer><samlp:Status><samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></samlp:Status>{signed}</samlp:Response>'
    )
    return base64.b64encode(response.encode()).decode()


def test_saml_sign_in_creates_user_with_group_role(client, saml_provider):
    client.post("/logout")
    assert "Continue with Okta" in client.get("/login").text
    relay, request_id = start_saml(client)
    response = client.post("/auth/saml/okta/acs", data={"SAMLResponse": saml_response(request_id, "sam@corp.example", ["GNS3-Sales"]), "RelayState": relay}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/", response.text
    account = portal.user_record("sam@corp.example")
    assert account["auth_source"] == "saml" and account["auth_provider"] == "okta"
    assert set(auth.user_roles("sam@corp.example")) == {"lab_editor"}
    assert client.get("/labs/new").status_code != 403  # allowed; GNS3 itself is not running in tests
    # The SSO account cannot be used with a password.
    assert login(client, "sam@corp.example", "anything-at-all").status_code == 401
    client.post("/logout")


def test_saml_rejects_tampered_unsolicited_and_replayed_responses(client, saml_provider):
    relay, request_id = start_saml(client)
    tampered = client.post("/auth/saml/okta/acs", data={"SAMLResponse": saml_response(request_id, "eve@corp.example", ["GNS3-Sales"], tamper=True), "RelayState": relay})
    assert tampered.status_code == 401 and portal.user_record("admin@corp.example") is None
    unsolicited = client.post("/auth/saml/okta/acs", data={"SAMLResponse": saml_response("_unknown", "eve@corp.example", ["GNS3-Sales"]), "RelayState": "made-up"})
    assert unsolicited.status_code == 401
    relay, request_id = start_saml(client)
    payload = saml_response(request_id, "replay@corp.example", ["GNS3-Sales"])
    assert client.post("/auth/saml/okta/acs", data={"SAMLResponse": payload, "RelayState": relay}, follow_redirects=False).status_code == 303
    client.post("/logout")
    assert client.post("/auth/saml/okta/acs", data={"SAMLResponse": payload, "RelayState": relay}).status_code == 401


def test_saml_user_without_mapped_group_is_refused(client, saml_provider):
    relay, request_id = start_saml(client)
    response = client.post("/auth/saml/okta/acs", data={"SAMLResponse": saml_response(request_id, "visitor@corp.example", ["Everyone"]), "RelayState": relay})
    assert response.status_code == 401 and "no portal role" in response.text
    assert portal.user_record("visitor@corp.example") is None


def test_saml_cannot_take_over_a_local_account(client, saml_provider):
    relay, request_id = start_saml(client)
    response = client.post("/auth/saml/okta/acs", data={"SAMLResponse": saml_response(request_id, "portaladmin", ["GNS3-Sales"]), "RelayState": relay})
    assert response.status_code == 401
    assert portal.user_record("portaladmin")["auth_source"] == "local"


def test_sp_metadata_is_published(client, saml_provider):
    response = client.get("/auth/saml/okta/metadata")
    assert response.status_code == 200 and "https://portal.test.example/auth/saml/okta/acs" in response.text


def test_identity_admin_pages_render(client, directory, saml_provider):
    login(client, "portaladmin", "initial-admin-password")
    for path in ("/admin/identity", "/admin/identity/corp-ad", "/admin/identity/okta", "/admin/identity/new/ldap", "/admin/identity/new/saml?preset=entra", "/admin/roles", "/admin/users"):
        assert client.get(path).status_code == 200, path
    tested = client.post("/admin/identity/corp-ad/test", data={"sample_user": "jsmith"}, follow_redirects=False)
    assert "Found+CN%3DJohn+Smith" in tested.headers["location"]
    client.post("/logout")


def test_roles_removed_by_an_admin_stay_removed_after_restart(client):
    login(client, "portaladmin", "initial-admin-password")
    client.post("/admin/users", data={"username": "dana", "password": "dana-password-1", "roles": ["lab_operator"]})
    client.post("/admin/users/dana/update", data={"enabled": "1", "retained_backup_limit": "2"})
    assert auth.user_roles("dana") == {}
    portal.init_db()  # what a service restart does
    assert auth.user_roles("dana") == {}
    client.post("/logout")


def test_lost_group_membership_removes_group_roles(client, directory):
    assert login(client, "jsmith", "Correct-horse-1").status_code == 303
    assert set(auth.user_roles("jsmith")) == {"lab_operator"}
    auth.remove_group_mapping("corp-ad", "cn=gns3-operators,ou=groups,dc=corp,dc=example", "lab_operator")
    assert login(client, "jsmith", "Correct-horse-1").status_code == 401
    assert auth.user_roles("jsmith") == {}

"""Roles, permissions and external identity (LDAPS / Active Directory, SAML 2.0).

Local accounts stay available as the break-glass path: a local Platform Admin
can always sign in, even when every directory or SSO provider is down or
misconfigured.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlsplit

import portal_db as db

# --- Roles and permissions -------------------------------------------------

PERMISSIONS = {
    "users.manage": "Manage users, roles, groups and identity providers",
    "labs.all": "See and manage every lab, and assign lab owners",
    "labs.create": "Create labs",
    "labs.edit": "Rename, back up, share and delete own labs",
    "labs.operate": "Start and stop accessible labs",
    "templates.approve": "Approve and publish lab templates",
    "audit.all": "Read the full audit log",
    "servers.view": "See server status",
}

ROLES: dict[str, tuple[str, frozenset[str]]] = {
    "platform_admin": ("Platform Admin", frozenset(PERMISSIONS)),
    "lab_manager": ("Lab Manager", frozenset({"labs.all", "labs.create", "labs.edit", "labs.operate", "servers.view"})),
    "template_approver": ("Template Approver", frozenset({"templates.approve"})),
    "lab_editor": ("Lab Editor", frozenset({"labs.create", "labs.edit", "labs.operate"})),
    "lab_operator": ("Lab Operator", frozenset({"labs.operate"})),
    "viewer": ("Viewer", frozenset()),
    "auditor": ("Auditor", frozenset({"audit.all", "servers.view"})),
}
ROLE_ORDER = list(ROLES)
# v8.x accounts had a single admin/user role.
LEGACY_ROLE_MAP = {"admin": "platform_admin", "user": "lab_editor"}
# Stored as the password hash of directory and SSO users: it never verifies.
EXTERNAL_PASSWORD_MARKER = "!external"


def role_name(role_key: str) -> str:
    return ROLES.get(role_key, (role_key, frozenset()))[0]


def permissions_for_roles(role_keys: set[str]) -> frozenset[str]:
    granted: set[str] = set()
    for key in role_keys:
        granted |= ROLES.get(key, ("", frozenset()))[1]
    return frozenset(granted)


def user_roles(username: str) -> dict[str, set[str]]:
    """Return {role_key: {sources}} for a user; sources are 'direct' or 'group:<provider>'."""
    with db.connect() as connection:
        rows = connection.execute("SELECT role_key, source FROM user_roles WHERE username=?", (username,)).fetchall()
    roles: dict[str, set[str]] = {}
    for row in rows:
        roles.setdefault(str(row["role_key"]), set()).add(str(row["source"]))
    return roles


def user_permissions(username: str) -> frozenset[str]:
    """Permissions for a user, cached for the rest of the current request."""
    context = db.current_context()
    cache = context.setdefault("_permissions", {}) if isinstance(context, dict) else {}
    key = username.lower()
    if key not in cache:
        cache[key] = permissions_for_roles(set(user_roles(username)))
    return cache[key]


def set_direct_roles(username: str, role_keys: set[str]) -> None:
    role_keys = {key for key in role_keys if key in ROLES}
    with db.connect() as connection:
        connection.execute("DELETE FROM user_roles WHERE username=? AND source='direct'", (username,))
        for key in sorted(role_keys):
            connection.execute("INSERT INTO user_roles(username, role_key, source, assigned_at) VALUES(?,?,?,?)",
                               (username, key, "direct", now_iso()))
        sync_legacy_role(connection, username)
    db.current_context().pop("_permissions", None)


def set_group_roles(username: str, provider_id: str, role_keys: set[str]) -> None:
    """Replace the roles a user receives from one provider's group mappings."""
    source = f"group:{provider_id}"
    with db.connect() as connection:
        connection.execute("DELETE FROM user_roles WHERE username=? AND source=?", (username, source))
        for key in sorted(role_keys & set(ROLES)):
            connection.execute("INSERT INTO user_roles(username, role_key, source, assigned_at) VALUES(?,?,?,?)",
                               (username, key, source, now_iso()))
        sync_legacy_role(connection, username)
    db.current_context().pop("_permissions", None)


def sync_legacy_role(connection: db.Connection, username: str) -> None:
    """Keep portal_users.role ('admin'/'user') meaningful for older tooling and reports."""
    admin = connection.execute("SELECT 1 FROM user_roles WHERE username=? AND role_key='platform_admin' LIMIT 1", (username,)).fetchone()
    connection.execute("UPDATE portal_users SET role=? WHERE username=?", ("admin" if admin else "user", username))


def seed_roles_from_legacy() -> None:
    """One-time upgrade: give each v8.x account the role matching its admin/user value.

    Runs only while no roles exist at all, so an account whose roles an
    administrator later removes is never silently given a role again.
    """
    with db.connect() as connection:
        if connection.execute("SELECT 1 FROM user_roles LIMIT 1").fetchone():
            return
        rows = connection.execute("SELECT username, role FROM portal_users").fetchall()
        for row in rows:
            connection.execute("INSERT INTO user_roles(username, role_key, source, assigned_at) VALUES(?,?,?,?)",
                               (row["username"], LEGACY_ROLE_MAP.get(str(row["role"]), "lab_editor"), "direct", now_iso()))


def enabled_local_admin_count(excluding: str = "") -> int:
    """Local Platform Admins with a direct role: the break-glass accounts."""
    with db.connect() as connection:
        row = connection.execute(
            "SELECT COUNT(DISTINCT u.username) FROM portal_users u JOIN user_roles r ON r.username=u.username "
            "WHERE u.enabled=1 AND u.auth_source='local' AND r.role_key='platform_admin' AND r.source='direct' AND u.username<>?",
            (excluding,),
        ).fetchone()
    return int(row[0])


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- Secret encryption -----------------------------------------------------

class SecretBox:
    """Encrypt provider secrets (bind passwords) at rest with a key derived from PORTAL_SECRET_KEY."""

    def __init__(self, secret_key: str):
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF

        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"gns3-lab-portal/provider-secrets/v1").derive(secret_key.encode("utf-8"))
        self._fernet = Fernet(base64.urlsafe_b64encode(key))

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii") if value else ""

    def decrypt(self, token: str) -> str:
        from cryptography.fernet import InvalidToken
        if not token:
            return ""
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise IdentityError("A stored provider secret cannot be decrypted. Was PORTAL_SECRET_KEY changed? Re-enter the secret.") from exc


# --- Identity providers ----------------------------------------------------

class IdentityError(Exception):
    """A user-safe message: shown to administrators, never containing secrets."""


@dataclass
class ExternalIdentity:
    username: str
    external_id: str
    groups: list[str] = field(default_factory=list)
    email: str = ""
    first_name: str = ""
    last_name: str = ""


LDAP_DEFAULTS = {
    "url": "ldaps://dc01.example.internal:636",
    "ca_pem": "",
    "bind_dn": "",
    "bind_password_enc": "",
    "user_base_dn": "",
    "user_filter": "(&(objectClass=user)(|(sAMAccountName={username})(userPrincipalName={username})))",
    "username_attribute": "sAMAccountName",
    "group_base_dn": "",
    "group_filter": "(objectClass=group)",
    "nested_groups": True,
    "default_role": "",
    "timeout_seconds": 10,
}

# Attribute names each IdP sends by default. Administrators can override any of them.
SAML_PRESETS = {
    "entra": {
        "label": "Microsoft Entra ID",
        "username_attribute": "",
        "email_attribute": "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
        "first_name_attribute": "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/givenname",
        "last_name_attribute": "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/surname",
        "groups_attribute": "http://schemas.microsoft.com/ws/2008/06/identity/claims/groups",
    },
    "okta": {
        "label": "Okta",
        "username_attribute": "",
        "email_attribute": "email",
        "first_name_attribute": "firstName",
        "last_name_attribute": "lastName",
        "groups_attribute": "groups",
    },
    "adfs": {
        "label": "ADFS",
        "username_attribute": "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/upn",
        "email_attribute": "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
        "first_name_attribute": "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/givenname",
        "last_name_attribute": "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/surname",
        "groups_attribute": "http://schemas.xmlsoap.org/claims/Group",
    },
    "other": {
        "label": "Other SAML 2.0 provider",
        "username_attribute": "",
        "email_attribute": "email",
        "first_name_attribute": "givenName",
        "last_name_attribute": "sn",
        "groups_attribute": "groups",
    },
}

SAML_DEFAULTS = {
    "preset": "entra",
    "idp_entity_id": "",
    "idp_sso_url": "",
    "idp_x509_cert": "",
    "username_attribute": "",
    "email_attribute": "",
    "first_name_attribute": "",
    "last_name_attribute": "",
    "groups_attribute": "",
    "default_role": "",
}

PROVIDER_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,39}$")


def list_providers(kind: str | None = None, enabled_only: bool = False) -> list[dict[str, Any]]:
    query, values = "SELECT * FROM identity_providers", []
    clauses = []
    if kind:
        clauses.append("kind=?"); values.append(kind)
    if enabled_only:
        clauses.append("enabled=1")
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY kind, name"
    with db.connect() as connection:
        rows = connection.execute(query, tuple(values)).fetchall()
    return [_provider_row(row) for row in rows]


def get_provider(provider_id: str) -> dict[str, Any] | None:
    with db.connect() as connection:
        row = connection.execute("SELECT * FROM identity_providers WHERE provider_id=?", (provider_id,)).fetchone()
    return _provider_row(row) if row else None


def _provider_row(row: Any) -> dict[str, Any]:
    record = dict(row)
    defaults = LDAP_DEFAULTS if record["kind"] == "ldap" else SAML_DEFAULTS
    record["config"] = {**defaults, **json.loads(record.get("config") or "{}")}
    record["enabled"] = bool(record["enabled"])
    return record


def save_provider(provider_id: str, kind: str, name: str, enabled: bool, config: dict[str, Any]) -> None:
    if not PROVIDER_ID_RE.match(provider_id):
        raise IdentityError("Use a provider ID of 2–40 lowercase letters, digits or hyphens, starting with a letter.")
    if kind not in {"ldap", "saml"}:
        raise IdentityError("Unknown provider type.")
    name = name.strip()[:120] or provider_id
    stamp = now_iso()
    with db.connect() as connection:
        existing = connection.execute("SELECT kind FROM identity_providers WHERE provider_id=?", (provider_id,)).fetchone()
        if existing and existing["kind"] != kind:
            raise IdentityError("A provider with that ID already exists with a different type.")
        if existing:
            connection.execute("UPDATE identity_providers SET name=?, enabled=?, config=?, updated_at=? WHERE provider_id=?",
                               (name, 1 if enabled else 0, json.dumps(config), stamp, provider_id))
        else:
            connection.execute("INSERT INTO identity_providers(provider_id, kind, name, enabled, config, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                               (provider_id, kind, name, 1 if enabled else 0, json.dumps(config), stamp, stamp))


def delete_provider(provider_id: str) -> None:
    with db.connect() as connection:
        connection.execute("DELETE FROM group_role_mappings WHERE provider_id=?", (provider_id,))
        connection.execute("DELETE FROM user_roles WHERE source=?", (f"group:{provider_id}",))
        connection.execute("DELETE FROM identity_providers WHERE provider_id=?", (provider_id,))


def group_mappings(provider_id: str | None = None) -> list[dict[str, Any]]:
    query, values = "SELECT * FROM group_role_mappings", ()
    if provider_id:
        query += " WHERE provider_id=?"; values = (provider_id,)
    query += " ORDER BY provider_id, group_name, role_key"
    with db.connect() as connection:
        return [dict(row) for row in connection.execute(query, values).fetchall()]


def add_group_mapping(provider_id: str, group_id: str, group_name: str, role_key: str, actor: str) -> None:
    group_id = group_id.strip()
    if not group_id or len(group_id) > 512:
        raise IdentityError("Enter the group's distinguished name, object ID or name as the provider sends it.")
    if role_key not in ROLES:
        raise IdentityError("Choose a role.")
    with db.connect() as connection:
        connection.execute(
            "INSERT INTO group_role_mappings(provider_id, group_id, group_name, role_key, created_by, created_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(provider_id, group_id, role_key) DO UPDATE SET group_name=excluded.group_name",
            (provider_id, group_id, (group_name.strip() or group_id)[:255], role_key, actor, now_iso()),
        )


def remove_group_mapping(provider_id: str, group_id: str, role_key: str) -> None:
    with db.connect() as connection:
        connection.execute("DELETE FROM group_role_mappings WHERE provider_id=? AND group_id=? AND role_key=?", (provider_id, group_id, role_key))


def roles_for_groups(provider: dict[str, Any], groups: list[str]) -> set[str]:
    """Map the provider's group identifiers to roles; group matching is case-insensitive."""
    wanted = {group.strip().lower() for group in groups if group}
    roles = {row["role_key"] for row in group_mappings(provider["provider_id"]) if str(row["group_id"]).strip().lower() in wanted}
    default_role = str(provider["config"].get("default_role") or "")
    if not roles and default_role in ROLES:
        roles.add(default_role)
    return roles


# --- LDAPS / Active Directory ------------------------------------------------

def normalise_login_name(value: str) -> str:
    """Accept 'DOMAIN\\user', 'user@domain' or 'user' and return what the directory filter receives."""
    value = value.strip()
    if "\\" in value:
        value = value.split("\\", 1)[1]
    return value


def parse_ldaps_url(url: str) -> tuple[str, int]:
    parts = urlsplit(url.strip())
    if parts.scheme.lower() != "ldaps" or not parts.hostname:
        raise IdentityError("Only ldaps:// URLs are supported; plain LDAP would send passwords unencrypted.")
    return parts.hostname, parts.port or 636


def ldap_tls(ca_pem: str) -> Any:
    import ldap3
    if ca_pem.strip() and "BEGIN CERTIFICATE" not in ca_pem:
        raise IdentityError("The CA certificate must be in PEM format (-----BEGIN CERTIFICATE-----).")
    # CERT_REQUIRED makes ldap3 verify the chain and the server hostname; an
    # untrusted, expired or mismatched certificate fails closed.
    return ldap3.Tls(validate=ssl.CERT_REQUIRED, version=ssl.PROTOCOL_TLS_CLIENT,
                     ca_certs_data=ca_pem.strip() or None)


ConnectionFactory = Callable[[dict[str, Any], str, str], Any]


def default_ldap_connection(config: dict[str, Any], user: str, password: str) -> Any:
    import ldap3
    host, port = parse_ldaps_url(str(config["url"]))
    timeout = max(2, min(60, int(config.get("timeout_seconds") or 10)))
    server = ldap3.Server(host, port=port, use_ssl=True, tls=ldap_tls(str(config.get("ca_pem") or "")), get_info=ldap3.NONE, connect_timeout=timeout)
    return ldap3.Connection(server, user=user, password=password, receive_timeout=timeout, raise_exceptions=False, read_only=True)


class LdapDirectory:
    """Authenticate users and read their groups from Active Directory over LDAPS."""

    def __init__(self, provider: dict[str, Any], secrets_box: SecretBox, connect: ConnectionFactory | None = None):
        self.provider = provider
        self.config = provider["config"]
        self.secrets = secrets_box
        # Looked up at call time so tests can substitute a fake directory.
        self.connect = connect or (lambda config, user, password: default_ldap_connection(config, user, password))

    def _service_connection(self) -> Any:
        connection = self.connect(self.config, str(self.config.get("bind_dn") or ""), self.secrets.decrypt(str(self.config.get("bind_password_enc") or "")))
        if not self._bind(connection):
            raise IdentityError("The directory rejected the service account. Check the bind DN and password.")
        return connection

    @staticmethod
    def _bind(connection: Any) -> bool:
        try:
            return bool(connection.bind())
        except Exception as exc:  # ldap3 raises socket and TLS errors here
            if "certificate" in str(exc).lower() or isinstance(exc, ssl.SSLError):
                raise IdentityError("The directory's TLS certificate was not trusted or did not match the server name.") from exc
            raise IdentityError(f"Cannot connect to the directory: {type(exc).__name__}.") from exc

    def find_user(self, connection: Any, login: str) -> tuple[str, dict[str, Any]] | None:
        from ldap3.utils.conv import escape_filter_chars
        name_attribute = str(self.config.get("username_attribute") or "sAMAccountName")
        search_filter = str(self.config["user_filter"]).replace("{username}", escape_filter_chars(login))
        connection.search(str(self.config["user_base_dn"]), search_filter,
                          attributes=[name_attribute, "mail", "givenName", "sn", "memberOf", "userPrincipalName"], size_limit=2)
        entries = list(connection.entries)
        if len(entries) != 1:
            return None
        entry = entries[0]
        return str(entry.entry_dn), entry.entry_attributes_as_dict

    def groups_for(self, connection: Any, user_dn: str, attributes: dict[str, Any]) -> list[str]:
        from ldap3.utils.conv import escape_filter_chars
        groups = [str(value) for value in attributes.get("memberOf", [])]
        if self.config.get("nested_groups") and self.config.get("group_base_dn"):
            # LDAP_MATCHING_RULE_IN_CHAIN: Active Directory expands nested membership server-side.
            if connection.search(str(self.config["group_base_dn"]), f"(member:1.2.840.113556.1.4.1941:={escape_filter_chars(user_dn)})", attributes=["cn"]):
                groups.extend(str(entry.entry_dn) for entry in connection.entries)
        return sorted(set(groups), key=str.lower)

    def authenticate(self, login: str, password: str) -> ExternalIdentity | None:
        """Return the identity on success, None for wrong credentials; IdentityError for configuration faults."""
        if not password:
            return None  # An empty password would be an anonymous bind on many directories.
        login = normalise_login_name(login)
        service = self._service_connection()
        try:
            found = self.find_user(service, login)
            if not found:
                return None
            user_dn, attributes = found
            user_connection = self.connect(self.config, user_dn, password)
            if not self._bind(user_connection):
                return None
            try:
                user_connection.unbind()
            except Exception:
                pass
            name_attribute = str(self.config.get("username_attribute") or "sAMAccountName")
            username = _first(attributes.get(name_attribute)) or login
            return ExternalIdentity(
                username=username, external_id=user_dn, groups=self.groups_for(service, user_dn, attributes),
                email=_first(attributes.get("mail")), first_name=_first(attributes.get("givenName")), last_name=_first(attributes.get("sn")),
            )
        finally:
            try:
                service.unbind()
            except Exception:
                pass

    def test(self, sample_login: str = "") -> list[str]:
        """Check connection, service bind and optionally a user lookup; return readable results."""
        results = [f"Connected to {self.config['url']} and verified its certificate."]
        connection = self._service_connection()
        try:
            results.append("Service account bind succeeded.")
            if sample_login:
                found = self.find_user(connection, normalise_login_name(sample_login))
                if not found:
                    results.append(f"No single user matched '{sample_login}'.")
                else:
                    groups = self.groups_for(connection, found[0], found[1])
                    results.append(f"Found {found[0]} in {len(groups)} group(s).")
        finally:
            try:
                connection.unbind()
            except Exception:
                pass
        return results

    def search_groups(self, text: str, limit: int = 50) -> list[dict[str, str]]:
        from ldap3.utils.conv import escape_filter_chars
        if not self.config.get("group_base_dn"):
            raise IdentityError("Set the group search base first.")
        connection = self._service_connection()
        try:
            term = escape_filter_chars(text.strip()) if text.strip() else ""
            search_filter = f"(&{self.config['group_filter']}(cn=*{term}*))" if term else str(self.config["group_filter"])
            connection.search(str(self.config["group_base_dn"]), search_filter, attributes=["cn", "description"], size_limit=limit)
            return [{"group_id": str(entry.entry_dn), "name": _first(entry.entry_attributes_as_dict.get("cn")) or str(entry.entry_dn)} for entry in connection.entries]
        finally:
            try:
                connection.unbind()
            except Exception:
                pass


def _first(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return str(value or "").strip()


# --- SAML 2.0 -----------------------------------------------------------------

class SamlRequestStore:
    """Outstanding AuthnRequest IDs and consumed assertion IDs, kept in memory.

    The IdP posts the response cross-site, so the browser does not send the
    portal's SameSite=Lax session cookie; the request ID is looked up by the
    RelayState token instead. Entries expire after ten minutes.
    """

    TTL_SECONDS = 600
    MAX_PENDING = 10_000  # caps memory when the unauthenticated sign-in URL is flooded

    def __init__(self) -> None:
        self._requests: dict[str, tuple[str, str, float]] = {}
        self._assertions: dict[str, float] = {}

    def _expire(self) -> None:
        cutoff = time.monotonic() - self.TTL_SECONDS
        self._requests = {key: value for key, value in self._requests.items() if value[2] >= cutoff}
        self._assertions = {key: value for key, value in self._assertions.items() if value >= cutoff}

    @staticmethod
    def new_relay_token() -> str:
        return secrets.token_urlsafe(24)

    def put(self, relay: str, provider_id: str, request_id: str) -> None:
        self._expire()
        if len(self._requests) >= self.MAX_PENDING:
            raise IdentityError("Too many sign-ins are in progress. Try again in a few minutes.")
        self._requests[relay] = (provider_id, request_id, time.monotonic())

    def take(self, relay: str) -> tuple[str, str] | None:
        self._expire()
        value = self._requests.pop(relay, None)
        return (value[0], value[1]) if value else None

    def mark_assertion_used(self, assertion_id: str) -> bool:
        """Record an assertion ID; False when it was already used (a replay)."""
        self._expire()
        if not assertion_id or assertion_id in self._assertions:
            return False
        self._assertions[assertion_id] = time.monotonic()
        return True


def saml_settings(provider: dict[str, Any], public_url: str) -> dict[str, Any]:
    base = public_url.rstrip("/")
    pid = provider["provider_id"]
    config = provider["config"]
    return {
        "strict": True,
        "debug": False,
        "sp": {
            "entityId": f"{base}/auth/saml/{pid}/metadata",
            "assertionConsumerService": {"url": f"{base}/auth/saml/{pid}/acs", "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"},
            "NameIDFormat": "urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified",
        },
        "idp": {
            "entityId": str(config.get("idp_entity_id") or ""),
            "singleSignOnService": {"url": str(config.get("idp_sso_url") or ""), "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"},
            "x509cert": str(config.get("idp_x509_cert") or ""),
        },
        "security": {
            "authnRequestsSigned": False,
            "wantAssertionsSigned": True,
            "wantMessagesSigned": False,
            "wantNameId": True,
            "requestedAuthnContext": False,
            "rejectDeprecatedAlgorithm": True,
            "allowRepeatAttributeName": True,
        },
    }


def saml_request_data(public_url: str, path: str, get_data: dict[str, str], post_data: dict[str, str]) -> dict[str, Any]:
    """Describe the request as the public URL the IdP used, not the proxied local address."""
    parts = urlsplit(public_url)
    return {
        "https": "on" if parts.scheme == "https" else "off",
        "http_host": f"{parts.hostname or ''}:{parts.port}" if parts.port else (parts.hostname or ""),
        "script_name": path,
        "get_data": get_data,
        "post_data": post_data,
    }


def parse_idp_metadata(xml: str) -> dict[str, str]:
    from onelogin.saml2.idp_metadata_parser import OneLogin_Saml2_IdPMetadataParser
    try:
        parsed = OneLogin_Saml2_IdPMetadataParser.parse(xml.strip(), required_sso_binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect")
    except Exception as exc:
        raise IdentityError("The IdP metadata could not be read. Paste the federation metadata XML exactly as downloaded.") from exc
    idp = parsed.get("idp", {})
    cert = idp.get("x509cert") or ((idp.get("x509certMulti") or {}).get("signing") or [""])[0]
    if not idp.get("entityId") or not idp.get("singleSignOnService", {}).get("url") or not cert:
        raise IdentityError("The metadata does not include an entity ID, an HTTP-Redirect sign-in URL and a signing certificate.")
    return {"idp_entity_id": idp["entityId"], "idp_sso_url": idp["singleSignOnService"]["url"], "idp_x509_cert": cert}


def saml_identity(provider: dict[str, Any], name_id: str, attributes: dict[str, list[str]]) -> ExternalIdentity:
    config = provider["config"]
    preset = SAML_PRESETS.get(str(config.get("preset") or "other"), SAML_PRESETS["other"])

    def attribute(key: str) -> list[str]:
        name = str(config.get(key) or preset.get(key) or "")
        return [str(value) for value in attributes.get(name, [])] if name else []

    username = (attribute("username_attribute") or [name_id])[0].strip()
    if not username:
        raise IdentityError("The identity provider did not send a username.")
    return ExternalIdentity(
        username=username[:255], external_id=name_id[:512], groups=attribute("groups_attribute"),
        email=(attribute("email_attribute") or [""])[0][:254], first_name=(attribute("first_name_attribute") or [""])[0][:128],
        last_name=(attribute("last_name_attribute") or [""])[0][:128],
    )


def fingerprint(cert_pem: str) -> str:
    """SHA-256 fingerprint of a base64/PEM certificate, for display."""
    body = re.sub(r"-----[^-]+-----|\s", "", cert_pem or "")
    try:
        digest = hashlib.sha256(base64.b64decode(body)).hexdigest().upper()
    except (ValueError, TypeError):
        return ""
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))

"""Test configuration. The backend is chosen before the portal is imported.

SQLite runs by default. To run the same tests on MariaDB:

    PORTAL_TEST_MARIADB=1 PORTAL_DB_HOST=localhost PORTAL_DB_USER=root python -m pytest

The MariaDB run drops and recreates the ``gns3_portal_test`` database.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="gns3-portal-test-"))

os.environ.update({
    "PORTAL_AUTH_ENABLED": "true",
    "PORTAL_USERNAME": "portaladmin",
    "PORTAL_PASSWORD": "initial-admin-password",
    "PORTAL_SECRET_KEY": "test-secret",
    "GNS3_URL": "http://127.0.0.1:9",
    "AUDIT_DB": str(TMP / "audit.db"),
    "PORTAL_UPLOAD_DIR": str(TMP / "uploads"),
    "PORTAL_AVATAR_DIR": str(TMP / "avatars"),
    "RETAINED_BACKUP_DIR": str(TMP / "retained"),
    "LAB_TEMPLATE_DIR": str(TMP / "templates"),
    "PORTAL_LOG_DIR": str(TMP / "logs"),
    "PORTAL_PUBLIC_URL": "https://portal.test.example",
})
if os.getenv("PORTAL_TEST_MARIADB"):
    os.environ.update({"PORTAL_DB_BACKEND": "mariadb", "PORTAL_DB_NAME": "gns3_portal_test", "PORTAL_DB_AUTO_MIGRATE": "true"})
    import portal_db

    raw = portal_db._connect_mariadb(database=None)
    with raw.cursor() as cursor:
        cursor.execute("DROP DATABASE IF EXISTS gns3_portal_test")
        cursor.execute("CREATE DATABASE gns3_portal_test CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    raw.close()

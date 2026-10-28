-- Identity: roles, group-to-role mappings and identity providers (v9 phase 2).
--
-- portal_users gains its sign-in source (local, ldap or saml). Directory and
-- SSO users store a password hash that never verifies, so only their provider
-- can sign them in. Every table here is change-logged; identity provider
-- configuration is logged as a SHA-256 hash because it holds encrypted
-- secrets.

ALTER TABLE portal_users
  ADD COLUMN IF NOT EXISTS auth_source VARCHAR(16) NOT NULL DEFAULT 'local',
  ADD COLUMN IF NOT EXISTS auth_provider VARCHAR(40) NULL,
  ADD COLUMN IF NOT EXISTS external_id VARCHAR(512) NULL,
  ADD COLUMN IF NOT EXISTS last_login_at VARCHAR(40) NULL;

CREATE TABLE IF NOT EXISTS user_roles (
  username VARCHAR(255) NOT NULL,
  role_key VARCHAR(32) NOT NULL,
  source VARCHAR(64) NOT NULL,
  assigned_at VARCHAR(40) NOT NULL,
  PRIMARY KEY (username, role_key, source),
  KEY ix_user_roles_source (source)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS identity_providers (
  provider_id VARCHAR(40) NOT NULL,
  kind VARCHAR(8) NOT NULL,
  name VARCHAR(120) NOT NULL,
  enabled TINYINT(1) NOT NULL DEFAULT 0,
  config LONGTEXT NOT NULL,
  created_at VARCHAR(40) NOT NULL,
  updated_at VARCHAR(40) NOT NULL,
  PRIMARY KEY (provider_id),
  CONSTRAINT ck_identity_providers_kind CHECK (kind IN ('ldap', 'saml'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS group_role_mappings (
  provider_id VARCHAR(40) NOT NULL,
  group_id VARCHAR(512) NOT NULL,
  group_name VARCHAR(255) NOT NULL,
  role_key VARCHAR(32) NOT NULL,
  created_by VARCHAR(255) NOT NULL,
  created_at VARCHAR(40) NOT NULL,
  PRIMARY KEY (provider_id, group_id, role_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

DELIMITER $$

DROP TRIGGER IF EXISTS trg_user_roles_log_insert$$
CREATE TRIGGER trg_user_roles_log_insert AFTER INSERT ON user_roles FOR EACH ROW
  INSERT INTO db_change_log(table_name, operation, row_key, old_values, new_values, db_user, portal_actor, source_ip, request_id)
  VALUES('user_roles', 'INSERT', CONCAT_WS('|', NEW.username, NEW.role_key, NEW.source), NULL, JSON_OBJECT('username', NEW.username, 'role_key', NEW.role_key, 'source', NEW.source, 'assigned_at', NEW.assigned_at), USER(), NULLIF(@portal_actor, ''), NULLIF(@portal_source_ip, ''), NULLIF(@portal_request_id, ''))$$

DROP TRIGGER IF EXISTS trg_user_roles_log_update$$
CREATE TRIGGER trg_user_roles_log_update AFTER UPDATE ON user_roles FOR EACH ROW
  INSERT INTO db_change_log(table_name, operation, row_key, old_values, new_values, db_user, portal_actor, source_ip, request_id)
  VALUES('user_roles', 'UPDATE', CONCAT_WS('|', NEW.username, NEW.role_key, NEW.source), JSON_OBJECT('username', OLD.username, 'role_key', OLD.role_key, 'source', OLD.source, 'assigned_at', OLD.assigned_at), JSON_OBJECT('username', NEW.username, 'role_key', NEW.role_key, 'source', NEW.source, 'assigned_at', NEW.assigned_at), USER(), NULLIF(@portal_actor, ''), NULLIF(@portal_source_ip, ''), NULLIF(@portal_request_id, ''))$$

DROP TRIGGER IF EXISTS trg_user_roles_log_delete$$
CREATE TRIGGER trg_user_roles_log_delete AFTER DELETE ON user_roles FOR EACH ROW
  INSERT INTO db_change_log(table_name, operation, row_key, old_values, new_values, db_user, portal_actor, source_ip, request_id)
  VALUES('user_roles', 'DELETE', CONCAT_WS('|', OLD.username, OLD.role_key, OLD.source), JSON_OBJECT('username', OLD.username, 'role_key', OLD.role_key, 'source', OLD.source, 'assigned_at', OLD.assigned_at), NULL, USER(), NULLIF(@portal_actor, ''), NULLIF(@portal_source_ip, ''), NULLIF(@portal_request_id, ''))$$

DROP TRIGGER IF EXISTS trg_group_role_mappings_log_insert$$
CREATE TRIGGER trg_group_role_mappings_log_insert AFTER INSERT ON group_role_mappings FOR EACH ROW
  INSERT INTO db_change_log(table_name, operation, row_key, old_values, new_values, db_user, portal_actor, source_ip, request_id)
  VALUES('group_role_mappings', 'INSERT', CONCAT_WS('|', NEW.provider_id, NEW.group_id, NEW.role_key), NULL, JSON_OBJECT('provider_id', NEW.provider_id, 'group_id', NEW.group_id, 'group_name', NEW.group_name, 'role_key', NEW.role_key, 'created_by', NEW.created_by, 'created_at', NEW.created_at), USER(), NULLIF(@portal_actor, ''), NULLIF(@portal_source_ip, ''), NULLIF(@portal_request_id, ''))$$

DROP TRIGGER IF EXISTS trg_group_role_mappings_log_update$$
CREATE TRIGGER trg_group_role_mappings_log_update AFTER UPDATE ON group_role_mappings FOR EACH ROW
  INSERT INTO db_change_log(table_name, operation, row_key, old_values, new_values, db_user, portal_actor, source_ip, request_id)
  VALUES('group_role_mappings', 'UPDATE', CONCAT_WS('|', NEW.provider_id, NEW.group_id, NEW.role_key), JSON_OBJECT('provider_id', OLD.provider_id, 'group_id', OLD.group_id, 'group_name', OLD.group_name, 'role_key', OLD.role_key, 'created_by', OLD.created_by, 'created_at', OLD.created_at), JSON_OBJECT('provider_id', NEW.provider_id, 'group_id', NEW.group_id, 'group_name', NEW.group_name, 'role_key', NEW.role_key, 'created_by', NEW.created_by, 'created_at', NEW.created_at), USER(), NULLIF(@portal_actor, ''), NULLIF(@portal_source_ip, ''), NULLIF(@portal_request_id, ''))$$

DROP TRIGGER IF EXISTS trg_group_role_mappings_log_delete$$
CREATE TRIGGER trg_group_role_mappings_log_delete AFTER DELETE ON group_role_mappings FOR EACH ROW
  INSERT INTO db_change_log(table_name, operation, row_key, old_values, new_values, db_user, portal_actor, source_ip, request_id)
  VALUES('group_role_mappings', 'DELETE', CONCAT_WS('|', OLD.provider_id, OLD.group_id, OLD.role_key), JSON_OBJECT('provider_id', OLD.provider_id, 'group_id', OLD.group_id, 'group_name', OLD.group_name, 'role_key', OLD.role_key, 'created_by', OLD.created_by, 'created_at', OLD.created_at), NULL, USER(), NULLIF(@portal_actor, ''), NULLIF(@portal_source_ip, ''), NULLIF(@portal_request_id, ''))$$

DROP TRIGGER IF EXISTS trg_identity_providers_log_insert$$
CREATE TRIGGER trg_identity_providers_log_insert AFTER INSERT ON identity_providers FOR EACH ROW
  INSERT INTO db_change_log(table_name, operation, row_key, old_values, new_values, db_user, portal_actor, source_ip, request_id)
  VALUES('identity_providers', 'INSERT', CONCAT_WS('|', NEW.provider_id), NULL, JSON_OBJECT('provider_id', NEW.provider_id, 'kind', NEW.kind, 'name', NEW.name, 'enabled', NEW.enabled, 'created_at', NEW.created_at, 'updated_at', NEW.updated_at, 'config_sha256', SHA2(NEW.config, 256)), USER(), NULLIF(@portal_actor, ''), NULLIF(@portal_source_ip, ''), NULLIF(@portal_request_id, ''))$$

DROP TRIGGER IF EXISTS trg_identity_providers_log_update$$
CREATE TRIGGER trg_identity_providers_log_update AFTER UPDATE ON identity_providers FOR EACH ROW
  INSERT INTO db_change_log(table_name, operation, row_key, old_values, new_values, db_user, portal_actor, source_ip, request_id)
  VALUES('identity_providers', 'UPDATE', CONCAT_WS('|', NEW.provider_id), JSON_OBJECT('provider_id', OLD.provider_id, 'kind', OLD.kind, 'name', OLD.name, 'enabled', OLD.enabled, 'created_at', OLD.created_at, 'updated_at', OLD.updated_at, 'config_sha256', SHA2(OLD.config, 256)), JSON_OBJECT('provider_id', NEW.provider_id, 'kind', NEW.kind, 'name', NEW.name, 'enabled', NEW.enabled, 'created_at', NEW.created_at, 'updated_at', NEW.updated_at, 'config_sha256', SHA2(NEW.config, 256)), USER(), NULLIF(@portal_actor, ''), NULLIF(@portal_source_ip, ''), NULLIF(@portal_request_id, ''))$$

DROP TRIGGER IF EXISTS trg_identity_providers_log_delete$$
CREATE TRIGGER trg_identity_providers_log_delete AFTER DELETE ON identity_providers FOR EACH ROW
  INSERT INTO db_change_log(table_name, operation, row_key, old_values, new_values, db_user, portal_actor, source_ip, request_id)
  VALUES('identity_providers', 'DELETE', CONCAT_WS('|', OLD.provider_id), JSON_OBJECT('provider_id', OLD.provider_id, 'kind', OLD.kind, 'name', OLD.name, 'enabled', OLD.enabled, 'created_at', OLD.created_at, 'updated_at', OLD.updated_at, 'config_sha256', SHA2(OLD.config, 256)), NULL, USER(), NULLIF(@portal_actor, ''), NULLIF(@portal_source_ip, ''), NULLIF(@portal_request_id, ''))$$

DROP TRIGGER IF EXISTS trg_portal_users_log_insert$$
CREATE TRIGGER trg_portal_users_log_insert AFTER INSERT ON portal_users FOR EACH ROW
  INSERT INTO db_change_log(table_name, operation, row_key, old_values, new_values, db_user, portal_actor, source_ip, request_id)
  VALUES('portal_users', 'INSERT', NEW.username, NULL, JSON_OBJECT('username', NEW.username, 'role', NEW.role, 'enabled', NEW.enabled, 'created_at', NEW.created_at, 'email', NEW.email, 'first_name', NEW.first_name, 'last_name', NEW.last_name, 'avatar_filename', NEW.avatar_filename, 'retained_backup_limit', NEW.retained_backup_limit, 'auth_source', NEW.auth_source, 'auth_provider', NEW.auth_provider, 'external_id', NEW.external_id, 'last_login_at', NEW.last_login_at), USER(), NULLIF(@portal_actor, ''), NULLIF(@portal_source_ip, ''), NULLIF(@portal_request_id, ''))$$

DROP TRIGGER IF EXISTS trg_portal_users_log_update$$
CREATE TRIGGER trg_portal_users_log_update AFTER UPDATE ON portal_users FOR EACH ROW
  INSERT INTO db_change_log(table_name, operation, row_key, old_values, new_values, db_user, portal_actor, source_ip, request_id)
  VALUES('portal_users', 'UPDATE', NEW.username, JSON_OBJECT('username', OLD.username, 'role', OLD.role, 'enabled', OLD.enabled, 'created_at', OLD.created_at, 'email', OLD.email, 'first_name', OLD.first_name, 'last_name', OLD.last_name, 'avatar_filename', OLD.avatar_filename, 'retained_backup_limit', OLD.retained_backup_limit, 'auth_source', OLD.auth_source, 'auth_provider', OLD.auth_provider, 'external_id', OLD.external_id, 'last_login_at', OLD.last_login_at), JSON_SET(JSON_OBJECT('username', NEW.username, 'role', NEW.role, 'enabled', NEW.enabled, 'created_at', NEW.created_at, 'email', NEW.email, 'first_name', NEW.first_name, 'last_name', NEW.last_name, 'avatar_filename', NEW.avatar_filename, 'retained_backup_limit', NEW.retained_backup_limit, 'auth_source', NEW.auth_source, 'auth_provider', NEW.auth_provider, 'external_id', NEW.external_id, 'last_login_at', NEW.last_login_at), '$.password_changed', IF(OLD.password_hash <=> NEW.password_hash, FALSE, TRUE)), USER(), NULLIF(@portal_actor, ''), NULLIF(@portal_source_ip, ''), NULLIF(@portal_request_id, ''))$$

DROP TRIGGER IF EXISTS trg_portal_users_log_delete$$
CREATE TRIGGER trg_portal_users_log_delete AFTER DELETE ON portal_users FOR EACH ROW
  INSERT INTO db_change_log(table_name, operation, row_key, old_values, new_values, db_user, portal_actor, source_ip, request_id)
  VALUES('portal_users', 'DELETE', OLD.username, JSON_OBJECT('username', OLD.username, 'role', OLD.role, 'enabled', OLD.enabled, 'created_at', OLD.created_at, 'email', OLD.email, 'first_name', OLD.first_name, 'last_name', OLD.last_name, 'avatar_filename', OLD.avatar_filename, 'retained_backup_limit', OLD.retained_backup_limit, 'auth_source', OLD.auth_source, 'auth_provider', OLD.auth_provider, 'external_id', OLD.external_id, 'last_login_at', OLD.last_login_at), NULL, USER(), NULLIF(@portal_actor, ''), NULLIF(@portal_source_ip, ''), NULLIF(@portal_request_id, ''))$$

DELIMITER ;

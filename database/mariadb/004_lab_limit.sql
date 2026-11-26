-- v9.1: per-user lab limit (default 2, raised by an administrator on the Users page).
-- The portal_users change-log triggers are recreated so limit changes are recorded too.

ALTER TABLE portal_users ADD COLUMN IF NOT EXISTS lab_limit INT NOT NULL DEFAULT 2;

DELIMITER $$

DROP TRIGGER IF EXISTS trg_portal_users_log_insert$$
CREATE TRIGGER trg_portal_users_log_insert AFTER INSERT ON portal_users FOR EACH ROW
  INSERT INTO db_change_log(table_name, operation, row_key, old_values, new_values, db_user, portal_actor, source_ip, request_id)
  VALUES('portal_users', 'INSERT', NEW.username, NULL, JSON_OBJECT('username', NEW.username, 'role', NEW.role, 'enabled', NEW.enabled, 'created_at', NEW.created_at, 'email', NEW.email, 'first_name', NEW.first_name, 'last_name', NEW.last_name, 'avatar_filename', NEW.avatar_filename, 'retained_backup_limit', NEW.retained_backup_limit, 'lab_limit', NEW.lab_limit, 'auth_source', NEW.auth_source, 'auth_provider', NEW.auth_provider, 'external_id', NEW.external_id, 'last_login_at', NEW.last_login_at), USER(), NULLIF(@portal_actor, ''), NULLIF(@portal_source_ip, ''), NULLIF(@portal_request_id, ''))$$

DROP TRIGGER IF EXISTS trg_portal_users_log_update$$
CREATE TRIGGER trg_portal_users_log_update AFTER UPDATE ON portal_users FOR EACH ROW
  INSERT INTO db_change_log(table_name, operation, row_key, old_values, new_values, db_user, portal_actor, source_ip, request_id)
  VALUES('portal_users', 'UPDATE', NEW.username, JSON_OBJECT('username', OLD.username, 'role', OLD.role, 'enabled', OLD.enabled, 'created_at', OLD.created_at, 'email', OLD.email, 'first_name', OLD.first_name, 'last_name', OLD.last_name, 'avatar_filename', OLD.avatar_filename, 'retained_backup_limit', OLD.retained_backup_limit, 'lab_limit', OLD.lab_limit, 'auth_source', OLD.auth_source, 'auth_provider', OLD.auth_provider, 'external_id', OLD.external_id, 'last_login_at', OLD.last_login_at), JSON_SET(JSON_OBJECT('username', NEW.username, 'role', NEW.role, 'enabled', NEW.enabled, 'created_at', NEW.created_at, 'email', NEW.email, 'first_name', NEW.first_name, 'last_name', NEW.last_name, 'avatar_filename', NEW.avatar_filename, 'retained_backup_limit', NEW.retained_backup_limit, 'lab_limit', NEW.lab_limit, 'auth_source', NEW.auth_source, 'auth_provider', NEW.auth_provider, 'external_id', NEW.external_id, 'last_login_at', NEW.last_login_at), '$.password_changed', IF(OLD.password_hash <=> NEW.password_hash, FALSE, TRUE)), USER(), NULLIF(@portal_actor, ''), NULLIF(@portal_source_ip, ''), NULLIF(@portal_request_id, ''))$$

DROP TRIGGER IF EXISTS trg_portal_users_log_delete$$
CREATE TRIGGER trg_portal_users_log_delete AFTER DELETE ON portal_users FOR EACH ROW
  INSERT INTO db_change_log(table_name, operation, row_key, old_values, new_values, db_user, portal_actor, source_ip, request_id)
  VALUES('portal_users', 'DELETE', OLD.username, JSON_OBJECT('username', OLD.username, 'role', OLD.role, 'enabled', OLD.enabled, 'created_at', OLD.created_at, 'email', OLD.email, 'first_name', OLD.first_name, 'last_name', OLD.last_name, 'avatar_filename', OLD.avatar_filename, 'retained_backup_limit', OLD.retained_backup_limit, 'lab_limit', OLD.lab_limit, 'auth_source', OLD.auth_source, 'auth_provider', OLD.auth_provider, 'external_id', OLD.external_id, 'last_login_at', OLD.last_login_at), NULL, USER(), NULLIF(@portal_actor, ''), NULLIF(@portal_source_ip, ''), NULLIF(@portal_request_id, ''))$$

DELIMITER ;

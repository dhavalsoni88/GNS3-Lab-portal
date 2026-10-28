-- Audit layer.
--
-- 1. audit_log (portal events) gains request context and becomes append-only.
-- 2. db_change_log records every INSERT/UPDATE/DELETE on access-control and
--    lab-ownership tables, with old/new values, the MariaDB account, and the
--    portal user, source IP and request ID that the application sets on each
--    connection (@portal_actor, @portal_source_ip, @portal_request_id).
--    USER() is used, not CURRENT_USER(): inside a trigger CURRENT_USER() is the
--    trigger definer (the migration account), not the connected account.
--    Password hashes are never copied; only a password_changed flag is.
-- 3. Both tables reject UPDATE and DELETE for every account. Purging old
--    records requires a DBA to drop the trigger, which the server_audit
--    plugin records as DDL (scripts/setup-mariadb.sh enables it).
-- Sessions are deliberately not change-logged: they churn on every request
-- and logins/logouts are already portal audit events.

ALTER TABLE audit_log
  ADD COLUMN IF NOT EXISTS source_ip VARCHAR(45) NULL,
  ADD COLUMN IF NOT EXISTS user_agent VARCHAR(512) NULL,
  ADD COLUMN IF NOT EXISTS request_id VARCHAR(64) NULL,
  ADD COLUMN IF NOT EXISTS outcome VARCHAR(16) NOT NULL DEFAULT 'success',
  ADD COLUMN IF NOT EXISTS db_user VARCHAR(288) NULL;

CREATE TABLE IF NOT EXISTS db_change_log (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  changed_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  table_name VARCHAR(64) NOT NULL,
  operation ENUM('INSERT','UPDATE','DELETE') NOT NULL,
  row_key VARCHAR(1024) NOT NULL,
  old_values JSON NULL,
  new_values JSON NULL,
  db_user VARCHAR(288) NOT NULL,
  portal_actor VARCHAR(255) NULL,
  source_ip VARCHAR(45) NULL,
  request_id VARCHAR(64) NULL,
  PRIMARY KEY (id),
  KEY ix_db_change_log_changed_at (changed_at),
  KEY ix_db_change_log_table (table_name, changed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

DELIMITER $$

DROP TRIGGER IF EXISTS trg_audit_log_no_update$$
CREATE TRIGGER trg_audit_log_no_update BEFORE UPDATE ON audit_log FOR EACH ROW
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'audit_log is append-only'$$

DROP TRIGGER IF EXISTS trg_audit_log_no_delete$$
CREATE TRIGGER trg_audit_log_no_delete BEFORE DELETE ON audit_log FOR EACH ROW
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'audit_log is append-only'$$

DROP TRIGGER IF EXISTS trg_db_change_log_no_update$$
CREATE TRIGGER trg_db_change_log_no_update BEFORE UPDATE ON db_change_log FOR EACH ROW
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'db_change_log is append-only'$$

DROP TRIGGER IF EXISTS trg_db_change_log_no_delete$$
CREATE TRIGGER trg_db_change_log_no_delete BEFORE DELETE ON db_change_log FOR EACH ROW
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'db_change_log is append-only'$$

CREATE TRIGGER IF NOT EXISTS trg_audit_log_context BEFORE INSERT ON audit_log FOR EACH ROW
BEGIN
  SET NEW.db_user = USER();
  IF NEW.source_ip IS NULL THEN SET NEW.source_ip = NULLIF(@portal_source_ip, ''); END IF;
  IF NEW.request_id IS NULL THEN SET NEW.request_id = NULLIF(@portal_request_id, ''); END IF;
END$$

DROP TRIGGER IF EXISTS trg_portal_users_log_insert$$
CREATE TRIGGER trg_portal_users_log_insert AFTER INSERT ON portal_users FOR EACH ROW
  INSERT INTO db_change_log(table_name, operation, row_key, old_values, new_values, db_user, portal_actor, source_ip, request_id)
  VALUES('portal_users', 'INSERT', CONCAT_WS('|', NEW.username), NULL, JSON_OBJECT('username', NEW.username, 'role', NEW.role, 'enabled', NEW.enabled, 'created_at', NEW.created_at, 'email', NEW.email, 'first_name', NEW.first_name, 'last_name', NEW.last_name, 'avatar_filename', NEW.avatar_filename, 'retained_backup_limit', NEW.retained_backup_limit), USER(), NULLIF(@portal_actor, ''), NULLIF(@portal_source_ip, ''), NULLIF(@portal_request_id, ''))$$

DROP TRIGGER IF EXISTS trg_portal_users_log_update$$
CREATE TRIGGER trg_portal_users_log_update AFTER UPDATE ON portal_users FOR EACH ROW
  INSERT INTO db_change_log(table_name, operation, row_key, old_values, new_values, db_user, portal_actor, source_ip, request_id)
  VALUES('portal_users', 'UPDATE', CONCAT_WS('|', NEW.username), JSON_OBJECT('username', OLD.username, 'role', OLD.role, 'enabled', OLD.enabled, 'created_at', OLD.created_at, 'email', OLD.email, 'first_name', OLD.first_name, 'last_name', OLD.last_name, 'avatar_filename', OLD.avatar_filename, 'retained_backup_limit', OLD.retained_backup_limit), JSON_SET(JSON_OBJECT('username', NEW.username, 'role', NEW.role, 'enabled', NEW.enabled, 'created_at', NEW.created_at, 'email', NEW.email, 'first_name', NEW.first_name, 'last_name', NEW.last_name, 'avatar_filename', NEW.avatar_filename, 'retained_backup_limit', NEW.retained_backup_limit), '$.password_changed', IF(OLD.password_hash <=> NEW.password_hash, FALSE, TRUE)), USER(), NULLIF(@portal_actor, ''), NULLIF(@portal_source_ip, ''), NULLIF(@portal_request_id, ''))$$

DROP TRIGGER IF EXISTS trg_portal_users_log_delete$$
CREATE TRIGGER trg_portal_users_log_delete AFTER DELETE ON portal_users FOR EACH ROW
  INSERT INTO db_change_log(table_name, operation, row_key, old_values, new_values, db_user, portal_actor, source_ip, request_id)
  VALUES('portal_users', 'DELETE', CONCAT_WS('|', OLD.username), JSON_OBJECT('username', OLD.username, 'role', OLD.role, 'enabled', OLD.enabled, 'created_at', OLD.created_at, 'email', OLD.email, 'first_name', OLD.first_name, 'last_name', OLD.last_name, 'avatar_filename', OLD.avatar_filename, 'retained_backup_limit', OLD.retained_backup_limit), NULL, USER(), NULLIF(@portal_actor, ''), NULLIF(@portal_source_ip, ''), NULLIF(@portal_request_id, ''))$$

DROP TRIGGER IF EXISTS trg_lab_owners_log_insert$$
CREATE TRIGGER trg_lab_owners_log_insert AFTER INSERT ON lab_owners FOR EACH ROW
  INSERT INTO db_change_log(table_name, operation, row_key, old_values, new_values, db_user, portal_actor, source_ip, request_id)
  VALUES('lab_owners', 'INSERT', CONCAT_WS('|', NEW.project_id), NULL, JSON_OBJECT('project_id', NEW.project_id, 'username', NEW.username, 'assigned_at', NEW.assigned_at), USER(), NULLIF(@portal_actor, ''), NULLIF(@portal_source_ip, ''), NULLIF(@portal_request_id, ''))$$

DROP TRIGGER IF EXISTS trg_lab_owners_log_update$$
CREATE TRIGGER trg_lab_owners_log_update AFTER UPDATE ON lab_owners FOR EACH ROW
  INSERT INTO db_change_log(table_name, operation, row_key, old_values, new_values, db_user, portal_actor, source_ip, request_id)
  VALUES('lab_owners', 'UPDATE', CONCAT_WS('|', NEW.project_id), JSON_OBJECT('project_id', OLD.project_id, 'username', OLD.username, 'assigned_at', OLD.assigned_at), JSON_OBJECT('project_id', NEW.project_id, 'username', NEW.username, 'assigned_at', NEW.assigned_at), USER(), NULLIF(@portal_actor, ''), NULLIF(@portal_source_ip, ''), NULLIF(@portal_request_id, ''))$$

DROP TRIGGER IF EXISTS trg_lab_owners_log_delete$$
CREATE TRIGGER trg_lab_owners_log_delete AFTER DELETE ON lab_owners FOR EACH ROW
  INSERT INTO db_change_log(table_name, operation, row_key, old_values, new_values, db_user, portal_actor, source_ip, request_id)
  VALUES('lab_owners', 'DELETE', CONCAT_WS('|', OLD.project_id), JSON_OBJECT('project_id', OLD.project_id, 'username', OLD.username, 'assigned_at', OLD.assigned_at), NULL, USER(), NULLIF(@portal_actor, ''), NULLIF(@portal_source_ip, ''), NULLIF(@portal_request_id, ''))$$

DROP TRIGGER IF EXISTS trg_lab_shares_log_insert$$
CREATE TRIGGER trg_lab_shares_log_insert AFTER INSERT ON lab_shares FOR EACH ROW
  INSERT INTO db_change_log(table_name, operation, row_key, old_values, new_values, db_user, portal_actor, source_ip, request_id)
  VALUES('lab_shares', 'INSERT', CONCAT_WS('|', NEW.project_id, NEW.username), NULL, JSON_OBJECT('project_id', NEW.project_id, 'username', NEW.username, 'shared_by', NEW.shared_by, 'shared_at', NEW.shared_at), USER(), NULLIF(@portal_actor, ''), NULLIF(@portal_source_ip, ''), NULLIF(@portal_request_id, ''))$$

DROP TRIGGER IF EXISTS trg_lab_shares_log_update$$
CREATE TRIGGER trg_lab_shares_log_update AFTER UPDATE ON lab_shares FOR EACH ROW
  INSERT INTO db_change_log(table_name, operation, row_key, old_values, new_values, db_user, portal_actor, source_ip, request_id)
  VALUES('lab_shares', 'UPDATE', CONCAT_WS('|', NEW.project_id, NEW.username), JSON_OBJECT('project_id', OLD.project_id, 'username', OLD.username, 'shared_by', OLD.shared_by, 'shared_at', OLD.shared_at), JSON_OBJECT('project_id', NEW.project_id, 'username', NEW.username, 'shared_by', NEW.shared_by, 'shared_at', NEW.shared_at), USER(), NULLIF(@portal_actor, ''), NULLIF(@portal_source_ip, ''), NULLIF(@portal_request_id, ''))$$

DROP TRIGGER IF EXISTS trg_lab_shares_log_delete$$
CREATE TRIGGER trg_lab_shares_log_delete AFTER DELETE ON lab_shares FOR EACH ROW
  INSERT INTO db_change_log(table_name, operation, row_key, old_values, new_values, db_user, portal_actor, source_ip, request_id)
  VALUES('lab_shares', 'DELETE', CONCAT_WS('|', OLD.project_id, OLD.username), JSON_OBJECT('project_id', OLD.project_id, 'username', OLD.username, 'shared_by', OLD.shared_by, 'shared_at', OLD.shared_at), NULL, USER(), NULLIF(@portal_actor, ''), NULLIF(@portal_source_ip, ''), NULLIF(@portal_request_id, ''))$$

DROP TRIGGER IF EXISTS trg_backup_shares_log_insert$$
CREATE TRIGGER trg_backup_shares_log_insert AFTER INSERT ON backup_shares FOR EACH ROW
  INSERT INTO db_change_log(table_name, operation, row_key, old_values, new_values, db_user, portal_actor, source_ip, request_id)
  VALUES('backup_shares', 'INSERT', CONCAT_WS('|', NEW.project_id, NEW.snapshot_id, NEW.username), NULL, JSON_OBJECT('project_id', NEW.project_id, 'snapshot_id', NEW.snapshot_id, 'username', NEW.username, 'shared_by', NEW.shared_by, 'shared_at', NEW.shared_at), USER(), NULLIF(@portal_actor, ''), NULLIF(@portal_source_ip, ''), NULLIF(@portal_request_id, ''))$$

DROP TRIGGER IF EXISTS trg_backup_shares_log_update$$
CREATE TRIGGER trg_backup_shares_log_update AFTER UPDATE ON backup_shares FOR EACH ROW
  INSERT INTO db_change_log(table_name, operation, row_key, old_values, new_values, db_user, portal_actor, source_ip, request_id)
  VALUES('backup_shares', 'UPDATE', CONCAT_WS('|', NEW.project_id, NEW.snapshot_id, NEW.username), JSON_OBJECT('project_id', OLD.project_id, 'snapshot_id', OLD.snapshot_id, 'username', OLD.username, 'shared_by', OLD.shared_by, 'shared_at', OLD.shared_at), JSON_OBJECT('project_id', NEW.project_id, 'snapshot_id', NEW.snapshot_id, 'username', NEW.username, 'shared_by', NEW.shared_by, 'shared_at', NEW.shared_at), USER(), NULLIF(@portal_actor, ''), NULLIF(@portal_source_ip, ''), NULLIF(@portal_request_id, ''))$$

DROP TRIGGER IF EXISTS trg_backup_shares_log_delete$$
CREATE TRIGGER trg_backup_shares_log_delete AFTER DELETE ON backup_shares FOR EACH ROW
  INSERT INTO db_change_log(table_name, operation, row_key, old_values, new_values, db_user, portal_actor, source_ip, request_id)
  VALUES('backup_shares', 'DELETE', CONCAT_WS('|', OLD.project_id, OLD.snapshot_id, OLD.username), JSON_OBJECT('project_id', OLD.project_id, 'snapshot_id', OLD.snapshot_id, 'username', OLD.username, 'shared_by', OLD.shared_by, 'shared_at', OLD.shared_at), NULL, USER(), NULLIF(@portal_actor, ''), NULLIF(@portal_source_ip, ''), NULLIF(@portal_request_id, ''))$$

DROP TRIGGER IF EXISTS trg_retained_backups_log_insert$$
CREATE TRIGGER trg_retained_backups_log_insert AFTER INSERT ON retained_backups FOR EACH ROW
  INSERT INTO db_change_log(table_name, operation, row_key, old_values, new_values, db_user, portal_actor, source_ip, request_id)
  VALUES('retained_backups', 'INSERT', CONCAT_WS('|', NEW.backup_id), NULL, JSON_OBJECT('backup_id', NEW.backup_id, 'owner', NEW.owner, 'source_project_id', NEW.source_project_id, 'source_lab_name', NEW.source_lab_name, 'backup_name', NEW.backup_name, 'archive_path', NEW.archive_path, 'size_bytes', NEW.size_bytes, 'retained_at', NEW.retained_at), USER(), NULLIF(@portal_actor, ''), NULLIF(@portal_source_ip, ''), NULLIF(@portal_request_id, ''))$$

DROP TRIGGER IF EXISTS trg_retained_backups_log_update$$
CREATE TRIGGER trg_retained_backups_log_update AFTER UPDATE ON retained_backups FOR EACH ROW
  INSERT INTO db_change_log(table_name, operation, row_key, old_values, new_values, db_user, portal_actor, source_ip, request_id)
  VALUES('retained_backups', 'UPDATE', CONCAT_WS('|', NEW.backup_id), JSON_OBJECT('backup_id', OLD.backup_id, 'owner', OLD.owner, 'source_project_id', OLD.source_project_id, 'source_lab_name', OLD.source_lab_name, 'backup_name', OLD.backup_name, 'archive_path', OLD.archive_path, 'size_bytes', OLD.size_bytes, 'retained_at', OLD.retained_at), JSON_OBJECT('backup_id', NEW.backup_id, 'owner', NEW.owner, 'source_project_id', NEW.source_project_id, 'source_lab_name', NEW.source_lab_name, 'backup_name', NEW.backup_name, 'archive_path', NEW.archive_path, 'size_bytes', NEW.size_bytes, 'retained_at', NEW.retained_at), USER(), NULLIF(@portal_actor, ''), NULLIF(@portal_source_ip, ''), NULLIF(@portal_request_id, ''))$$

DROP TRIGGER IF EXISTS trg_retained_backups_log_delete$$
CREATE TRIGGER trg_retained_backups_log_delete AFTER DELETE ON retained_backups FOR EACH ROW
  INSERT INTO db_change_log(table_name, operation, row_key, old_values, new_values, db_user, portal_actor, source_ip, request_id)
  VALUES('retained_backups', 'DELETE', CONCAT_WS('|', OLD.backup_id), JSON_OBJECT('backup_id', OLD.backup_id, 'owner', OLD.owner, 'source_project_id', OLD.source_project_id, 'source_lab_name', OLD.source_lab_name, 'backup_name', OLD.backup_name, 'archive_path', OLD.archive_path, 'size_bytes', OLD.size_bytes, 'retained_at', OLD.retained_at), NULL, USER(), NULLIF(@portal_actor, ''), NULLIF(@portal_source_ip, ''), NULLIF(@portal_request_id, ''))$$

DROP TRIGGER IF EXISTS trg_lab_templates_log_insert$$
CREATE TRIGGER trg_lab_templates_log_insert AFTER INSERT ON lab_templates FOR EACH ROW
  INSERT INTO db_change_log(table_name, operation, row_key, old_values, new_values, db_user, portal_actor, source_ip, request_id)
  VALUES('lab_templates', 'INSERT', CONCAT_WS('|', NEW.template_id), NULL, JSON_OBJECT('template_id', NEW.template_id, 'owner', NEW.owner, 'source_project_id', NEW.source_project_id, 'snapshot_id', NEW.snapshot_id, 'name', NEW.name, 'description', NEW.description, 'archive_path', NEW.archive_path, 'size_bytes', NEW.size_bytes, 'created_at', NEW.created_at, 'approval_status', NEW.approval_status, 'approved_by', NEW.approved_by, 'approved_at', NEW.approved_at), USER(), NULLIF(@portal_actor, ''), NULLIF(@portal_source_ip, ''), NULLIF(@portal_request_id, ''))$$

DROP TRIGGER IF EXISTS trg_lab_templates_log_update$$
CREATE TRIGGER trg_lab_templates_log_update AFTER UPDATE ON lab_templates FOR EACH ROW
  INSERT INTO db_change_log(table_name, operation, row_key, old_values, new_values, db_user, portal_actor, source_ip, request_id)
  VALUES('lab_templates', 'UPDATE', CONCAT_WS('|', NEW.template_id), JSON_OBJECT('template_id', OLD.template_id, 'owner', OLD.owner, 'source_project_id', OLD.source_project_id, 'snapshot_id', OLD.snapshot_id, 'name', OLD.name, 'description', OLD.description, 'archive_path', OLD.archive_path, 'size_bytes', OLD.size_bytes, 'created_at', OLD.created_at, 'approval_status', OLD.approval_status, 'approved_by', OLD.approved_by, 'approved_at', OLD.approved_at), JSON_OBJECT('template_id', NEW.template_id, 'owner', NEW.owner, 'source_project_id', NEW.source_project_id, 'snapshot_id', NEW.snapshot_id, 'name', NEW.name, 'description', NEW.description, 'archive_path', NEW.archive_path, 'size_bytes', NEW.size_bytes, 'created_at', NEW.created_at, 'approval_status', NEW.approval_status, 'approved_by', NEW.approved_by, 'approved_at', NEW.approved_at), USER(), NULLIF(@portal_actor, ''), NULLIF(@portal_source_ip, ''), NULLIF(@portal_request_id, ''))$$

DROP TRIGGER IF EXISTS trg_lab_templates_log_delete$$
CREATE TRIGGER trg_lab_templates_log_delete AFTER DELETE ON lab_templates FOR EACH ROW
  INSERT INTO db_change_log(table_name, operation, row_key, old_values, new_values, db_user, portal_actor, source_ip, request_id)
  VALUES('lab_templates', 'DELETE', CONCAT_WS('|', OLD.template_id), JSON_OBJECT('template_id', OLD.template_id, 'owner', OLD.owner, 'source_project_id', OLD.source_project_id, 'snapshot_id', OLD.snapshot_id, 'name', OLD.name, 'description', OLD.description, 'archive_path', OLD.archive_path, 'size_bytes', OLD.size_bytes, 'created_at', OLD.created_at, 'approval_status', OLD.approval_status, 'approved_by', OLD.approved_by, 'approved_at', OLD.approved_at), NULL, USER(), NULLIF(@portal_actor, ''), NULLIF(@portal_source_ip, ''), NULLIF(@portal_request_id, ''))$$

DROP TRIGGER IF EXISTS trg_lab_template_access_log_insert$$
CREATE TRIGGER trg_lab_template_access_log_insert AFTER INSERT ON lab_template_access FOR EACH ROW
  INSERT INTO db_change_log(table_name, operation, row_key, old_values, new_values, db_user, portal_actor, source_ip, request_id)
  VALUES('lab_template_access', 'INSERT', CONCAT_WS('|', NEW.template_id, NEW.subject_type, NEW.subject_id), NULL, JSON_OBJECT('template_id', NEW.template_id, 'subject_type', NEW.subject_type, 'subject_id', NEW.subject_id, 'granted_by', NEW.granted_by, 'granted_at', NEW.granted_at), USER(), NULLIF(@portal_actor, ''), NULLIF(@portal_source_ip, ''), NULLIF(@portal_request_id, ''))$$

DROP TRIGGER IF EXISTS trg_lab_template_access_log_update$$
CREATE TRIGGER trg_lab_template_access_log_update AFTER UPDATE ON lab_template_access FOR EACH ROW
  INSERT INTO db_change_log(table_name, operation, row_key, old_values, new_values, db_user, portal_actor, source_ip, request_id)
  VALUES('lab_template_access', 'UPDATE', CONCAT_WS('|', NEW.template_id, NEW.subject_type, NEW.subject_id), JSON_OBJECT('template_id', OLD.template_id, 'subject_type', OLD.subject_type, 'subject_id', OLD.subject_id, 'granted_by', OLD.granted_by, 'granted_at', OLD.granted_at), JSON_OBJECT('template_id', NEW.template_id, 'subject_type', NEW.subject_type, 'subject_id', NEW.subject_id, 'granted_by', NEW.granted_by, 'granted_at', NEW.granted_at), USER(), NULLIF(@portal_actor, ''), NULLIF(@portal_source_ip, ''), NULLIF(@portal_request_id, ''))$$

DROP TRIGGER IF EXISTS trg_lab_template_access_log_delete$$
CREATE TRIGGER trg_lab_template_access_log_delete AFTER DELETE ON lab_template_access FOR EACH ROW
  INSERT INTO db_change_log(table_name, operation, row_key, old_values, new_values, db_user, portal_actor, source_ip, request_id)
  VALUES('lab_template_access', 'DELETE', CONCAT_WS('|', OLD.template_id, OLD.subject_type, OLD.subject_id), JSON_OBJECT('template_id', OLD.template_id, 'subject_type', OLD.subject_type, 'subject_id', OLD.subject_id, 'granted_by', OLD.granted_by, 'granted_at', OLD.granted_at), NULL, USER(), NULLIF(@portal_actor, ''), NULLIF(@portal_source_ip, ''), NULLIF(@portal_request_id, ''))$$

DELIMITER ;

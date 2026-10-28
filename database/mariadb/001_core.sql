-- GNS3 Lab Portal core schema for MariaDB 10.6+.
-- Mirrors the SQLite tables used by v8.x so data migrates one-to-one.
-- utf8mb4_unicode_ci is case-insensitive, matching SQLite's COLLATE NOCASE.
-- Timestamps stay ISO-8601 UTC strings, exactly as the application writes them.

CREATE TABLE IF NOT EXISTS portal_users (
  username VARCHAR(255) NOT NULL,
  password_hash VARCHAR(512) NOT NULL,
  role VARCHAR(32) NOT NULL,
  enabled TINYINT(1) NOT NULL DEFAULT 1,
  created_at VARCHAR(40) NOT NULL,
  email VARCHAR(254) NULL,
  first_name VARCHAR(128) NULL,
  last_name VARCHAR(128) NULL,
  avatar_filename VARCHAR(255) NULL,
  retained_backup_limit INT NOT NULL DEFAULT 2,
  PRIMARY KEY (username),
  CONSTRAINT ck_portal_users_role CHECK (role IN ('admin', 'user'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS portal_sessions (
  session_id_hash CHAR(64) NOT NULL,
  username VARCHAR(255) NOT NULL,
  issued_at VARCHAR(40) NOT NULL,
  last_seen_at VARCHAR(40) NOT NULL,
  expires_at VARCHAR(40) NOT NULL,
  server_epoch VARCHAR(64) NOT NULL,
  PRIMARY KEY (session_id_hash),
  KEY portal_sessions_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS audit_log (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  timestamp VARCHAR(40) NOT NULL,
  actor VARCHAR(255) NOT NULL,
  action VARCHAR(128) NOT NULL,
  target VARCHAR(512) NOT NULL,
  details LONGTEXT NULL,
  PRIMARY KEY (id),
  KEY ix_audit_log_timestamp (timestamp),
  KEY ix_audit_log_actor (actor),
  KEY ix_audit_log_action (action)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS lab_owners (
  project_id VARCHAR(64) NOT NULL,
  username VARCHAR(255) NOT NULL,
  assigned_at VARCHAR(40) NOT NULL,
  PRIMARY KEY (project_id),
  KEY ix_lab_owners_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS retained_backups (
  backup_id VARCHAR(64) NOT NULL,
  owner VARCHAR(255) NOT NULL,
  source_project_id VARCHAR(64) NOT NULL,
  source_lab_name VARCHAR(255) NOT NULL,
  backup_name VARCHAR(255) NOT NULL,
  archive_path VARCHAR(700) NOT NULL,
  size_bytes BIGINT NOT NULL,
  retained_at VARCHAR(40) NOT NULL,
  PRIMARY KEY (backup_id),
  UNIQUE KEY uq_retained_backups_path (archive_path),
  KEY ix_retained_backups_owner (owner)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS lab_shares (
  project_id VARCHAR(64) NOT NULL,
  username VARCHAR(255) NOT NULL,
  shared_by VARCHAR(255) NOT NULL,
  shared_at VARCHAR(40) NOT NULL,
  PRIMARY KEY (project_id, username),
  KEY ix_lab_shares_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS backup_shares (
  project_id VARCHAR(64) NOT NULL,
  snapshot_id VARCHAR(64) NOT NULL,
  username VARCHAR(255) NOT NULL,
  shared_by VARCHAR(255) NOT NULL,
  shared_at VARCHAR(40) NOT NULL,
  PRIMARY KEY (project_id, snapshot_id, username),
  KEY ix_backup_shares_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS lab_templates (
  template_id VARCHAR(64) NOT NULL,
  owner VARCHAR(255) NOT NULL,
  source_project_id VARCHAR(64) NOT NULL,
  snapshot_id VARCHAR(64) NOT NULL,
  name VARCHAR(255) NOT NULL,
  description TEXT NULL,
  archive_path VARCHAR(700) NULL,
  size_bytes BIGINT NULL,
  created_at VARCHAR(40) NOT NULL,
  approval_status VARCHAR(16) NOT NULL DEFAULT 'approved',
  approved_by VARCHAR(255) NULL,
  approved_at VARCHAR(40) NULL,
  PRIMARY KEY (template_id),
  UNIQUE KEY uq_lab_templates_source (source_project_id, snapshot_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS lab_template_access (
  template_id VARCHAR(64) NOT NULL,
  subject_type VARCHAR(32) NOT NULL,
  subject_id VARCHAR(255) NOT NULL,
  granted_by VARCHAR(255) NOT NULL,
  granted_at VARCHAR(40) NOT NULL,
  PRIMARY KEY (template_id, subject_type, subject_id),
  CONSTRAINT ck_template_access_subject CHECK (subject_type IN ('local_user', 'directory_group'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

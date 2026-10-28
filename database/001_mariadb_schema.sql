-- GNS3 Lab Portal v9.0 planning schema. Do not apply in production yet.
CREATE DATABASE IF NOT EXISTS gns3_lab_portal
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE gns3_lab_portal;

CREATE TABLE roles (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  role_key VARCHAR(64) NOT NULL,
  display_name VARCHAR(128) NOT NULL,
  PRIMARY KEY (id), UNIQUE KEY uq_roles_key (role_key)
) ENGINE=InnoDB;

CREATE TABLE permissions (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  permission_key VARCHAR(96) NOT NULL,
  PRIMARY KEY (id), UNIQUE KEY uq_permissions_key (permission_key)
) ENGINE=InnoDB;

CREATE TABLE role_permissions (
  role_id BIGINT UNSIGNED NOT NULL,
  permission_id BIGINT UNSIGNED NOT NULL,
  PRIMARY KEY (role_id, permission_id),
  CONSTRAINT fk_role_permissions_role FOREIGN KEY (role_id) REFERENCES roles(id) ON DELETE CASCADE,
  CONSTRAINT fk_role_permissions_permission FOREIGN KEY (permission_id) REFERENCES permissions(id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE users (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  username VARCHAR(255) NOT NULL,
  auth_source ENUM('local','ldap') NOT NULL,
  password_hash VARCHAR(512) NULL,
  email VARCHAR(254) NULL,
  first_name VARCHAR(128) NULL,
  last_name VARCHAR(128) NULL,
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  PRIMARY KEY (id), UNIQUE KEY uq_users_source_name (auth_source, username)
) ENGINE=InnoDB;

CREATE TABLE user_roles (
  user_id BIGINT UNSIGNED NOT NULL,
  role_id BIGINT UNSIGNED NOT NULL,
  PRIMARY KEY (user_id, role_id),
  CONSTRAINT fk_user_roles_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  CONSTRAINT fk_user_roles_role FOREIGN KEY (role_id) REFERENCES roles(id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE ldap_groups (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  distinguished_name VARCHAR(1024) NOT NULL,
  display_name VARCHAR(255) NOT NULL,
  imported_at DATETIME(6) NOT NULL,
  PRIMARY KEY (id), UNIQUE KEY uq_ldap_groups_dn (distinguished_name)
) ENGINE=InnoDB;

CREATE TABLE ldap_group_roles (
  ldap_group_id BIGINT UNSIGNED NOT NULL,
  role_id BIGINT UNSIGNED NOT NULL,
  PRIMARY KEY (ldap_group_id, role_id),
  CONSTRAINT fk_ldap_group_roles_group FOREIGN KEY (ldap_group_id) REFERENCES ldap_groups(id) ON DELETE CASCADE,
  CONSTRAINT fk_ldap_group_roles_role FOREIGN KEY (role_id) REFERENCES roles(id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE portal_sessions (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id BIGINT UNSIGNED NOT NULL,
  token_hash CHAR(64) NOT NULL,
  issued_at DATETIME(6) NOT NULL,
  last_seen_at DATETIME(6) NOT NULL,
  expires_at DATETIME(6) NOT NULL,
  revoked_at DATETIME(6) NULL,
  PRIMARY KEY (id), UNIQUE KEY uq_sessions_token_hash (token_hash),
  KEY ix_sessions_user (user_id),
  CONSTRAINT fk_sessions_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE audit_events (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  occurred_at DATETIME(6) NOT NULL,
  actor_user_id BIGINT UNSIGNED NULL,
  action VARCHAR(128) NOT NULL,
  target_type VARCHAR(64) NOT NULL,
  target_id VARCHAR(255) NULL,
  detail JSON NULL,
  source_ip VARCHAR(45) NULL,
  PRIMARY KEY (id), KEY ix_audit_occurred_at (occurred_at), KEY ix_audit_actor (actor_user_id),
  CONSTRAINT fk_audit_actor FOREIGN KEY (actor_user_id) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB;

INSERT IGNORE INTO roles (role_key, display_name) VALUES
  ('platform_admin', 'Platform Admin'), ('lab_manager', 'Lab Manager'),
  ('template_approver', 'Template Approver'), ('lab_editor', 'Lab Editor'),
  ('lab_operator', 'Lab Operator'), ('viewer', 'Viewer'), ('auditor', 'Auditor');

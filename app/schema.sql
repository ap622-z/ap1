-- 六张业务表 DDL（事实源：doc/constitution/database-schema.md）
-- 全部 IF NOT EXISTS，幂等可重跑。

CREATE TABLE IF NOT EXISTS users (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  nickname      VARCHAR(32)     NOT NULL,
  token_hash    CHAR(64)        NOT NULL,
  status        TINYINT         NOT NULL DEFAULT 1,
  created_at    DATETIME        NOT NULL,
  updated_at    DATETIME        NOT NULL,
  last_login_at DATETIME        NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_users_nickname (nickname),
  UNIQUE KEY uq_users_token_hash (token_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS sessions (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id        BIGINT UNSIGNED NOT NULL,
  status         TINYINT         NOT NULL DEFAULT 1,
  summary_text   MEDIUMTEXT      NULL,
  summary_tokens INT             NULL,
  summary_seq    BIGINT          NULL,
  last_active_at DATETIME        NULL,
  created_at     DATETIME        NOT NULL,
  updated_at     DATETIME        NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_sessions_user_id (user_id),
  CONSTRAINT fk_sessions_user FOREIGN KEY (user_id) REFERENCES users (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS session_messages (
  id                 BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  session_id         BIGINT UNSIGNED NOT NULL,
  seq                BIGINT          NOT NULL,
  type               VARCHAR(16)     NOT NULL,
  payload            JSON            NOT NULL,
  token_count        INT             NOT NULL DEFAULT 0,
  client_message_id  VARCHAR(64)     NULL,
  status             VARCHAR(16)     NULL,
  origin_message_id  BIGINT UNSIGNED NULL,
  created_at         DATETIME        NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_session_seq (session_id, seq),
  UNIQUE KEY uq_session_client_msg (session_id, client_message_id),
  KEY idx_session_origin (session_id, origin_message_id),
  CONSTRAINT fk_messages_session FOREIGN KEY (session_id) REFERENCES sessions (id),
  CONSTRAINT fk_messages_origin FOREIGN KEY (origin_message_id) REFERENCES session_messages (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS idol_infos (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  tag           VARCHAR(32)     NOT NULL,
  content       MEDIUMTEXT      NOT NULL,
  event_time    VARCHAR(32)     NULL,
  content_hash  CHAR(64)        NOT NULL,
  vector_synced TINYINT         NOT NULL DEFAULT 0,
  status        TINYINT         NOT NULL DEFAULT 1,
  created_at    DATETIME        NOT NULL,
  updated_at    DATETIME        NOT NULL,
  PRIMARY KEY (id),
  KEY idx_idol_infos_tag (tag)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS songs (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  song_title    VARCHAR(128)    NOT NULL,
  album         VARCHAR(128)    NULL,
  release_time  VARCHAR(32)     NULL,
  company       VARCHAR(128)    NULL,
  creators      VARCHAR(255)    NULL,
  collaboration VARCHAR(255)    NULL,
  intro         MEDIUMTEXT      NULL,
  content_hash  CHAR(64)        NOT NULL,
  vector_synced TINYINT         NOT NULL DEFAULT 0,
  status        TINYINT         NOT NULL DEFAULT 1,
  created_at    DATETIME        NOT NULL,
  updated_at    DATETIME        NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_songs_title (song_title)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS idol_lyrics (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  song_id       BIGINT UNSIGNED NOT NULL,
  seg_no        INT             NOT NULL,
  content       MEDIUMTEXT      NOT NULL,
  content_hash  CHAR(64)        NOT NULL,
  vector_synced TINYINT         NOT NULL DEFAULT 0,
  status        TINYINT         NOT NULL DEFAULT 1,
  created_at    DATETIME        NOT NULL,
  updated_at    DATETIME        NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_lyrics_song_seg (song_id, seg_no),
  KEY idx_lyrics_song (song_id),
  CONSTRAINT fk_lyrics_song FOREIGN KEY (song_id) REFERENCES songs (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

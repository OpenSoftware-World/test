"""SQLite veri katmanı: görevlerin kalıcı saklanması ve sorgulanması.

Flask uygulama bağlamı içinde (flask.g) bağlantı yönetir; dışarıya basit
fonksiyonlar sunar. Türkçe karakterlerin aranabilmesi için her görevin
arama metni ASCII tabanlı normalize edilerek saklanır.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from flask import current_app, g

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL,
    username_lower TEXT   NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    created_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT    NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    done        INTEGER NOT NULL DEFAULT 0,
    search_text TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL,
    completed_at TEXT,
    user_id     INTEGER REFERENCES users (id) ON DELETE CASCADE,
    due_at      TEXT,                 -- planlanan tarih/saat: 'YYYY-MM-DD HH:MM'
    priority    INTEGER NOT NULL DEFAULT 2, -- 1 düşük · 2 normal · 3 yüksek · 4 acil
    deleted_at  TEXT                  -- çöp kutusu taşıma anı (NULL = listede)
);
"""

# Türkçe harfleri ASCII karşılıklarıyla değiştiren harita (arama normalizasyonu)
_TR_MAP = str.maketrans("çÇğĞıİöÖşŞüÜâÂîÎûÛ", "cCgGiIoOsSuUaAiIuU")

_TIME_FMT = "%Y-%m-%d %H:%M:%S"


def normalize(text: str | None) -> str:
    """Arama için metni küçük harfe çevirir ve Türkçe harfleri ASCII'ye çevirir."""
    return (text or "").translate(_TR_MAP).lower()


def _escape_like(text: str) -> str:
    """LIKE joker karakterlerinin (%) kullanıcı aramasında etkisizleşmesini sağlar."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def get_db() -> sqlite3.Connection:
    """İstek bağlamına ait SQLite bağlantısını döndürür (yoksa oluşturur)."""
    if "db" not in g:
        db = sqlite3.connect(current_app.config["DATABASE"])
        db.row_factory = sqlite3.Row
        # Yabancı anahtar denetimi bağlantı bazındadır (şemadaki ON DELETE
        # CASCADE ancak böyle çalışır); busy_timeout yazma kilidi beklerken
        # "database is locked" hatasını yumuşatır
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA busy_timeout = 5000")
        g.db = db
    return g.db


def close_db(_exc: BaseException | None = None) -> None:
    """İstek bitiminde bağlantıyı kapatır."""
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_app(app) -> None:
    """Bağlantı kapanışını kaydeder, şemayı oluşturur ve sürüm yükseltmesi yapar."""
    app.teardown_appcontext(close_db)
    with app.app_context():
        db = get_db()
        db.executescript(_SCHEMA)
        # Hesap desteğinden önceki veritabanlarına user_id sütunu eklenir
        columns = {row[1] for row in db.execute("PRAGMA table_info(tasks)")}
        if "user_id" not in columns:
            db.execute("ALTER TABLE tasks ADD COLUMN user_id INTEGER")
        # Tarih/saat ve önem derecesi sonradan eklendi
        if "due_at" not in columns:
            db.execute("ALTER TABLE tasks ADD COLUMN due_at TEXT")
        if "priority" not in columns:
            db.execute(
                "ALTER TABLE tasks ADD COLUMN priority INTEGER NOT NULL DEFAULT 2"
            )
        if "deleted_at" not in columns:
            db.execute("ALTER TABLE tasks ADD COLUMN deleted_at TEXT")
        db.commit()
        # Eşzamanlı okuma/yazma için WAL günlüğü (kalıcı ayar, dosyaya yazılır)
        db.execute("PRAGMA journal_mode = WAL")
        # Sık kullanılan sorgular için indeks: kullanıcı filtresi + çöp kutusu
        # + takvim tarih aralığı (EXPLAIN: SCAN -> SEARCH)
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_user_active "
            "ON tasks (user_id, deleted_at, due_at)"
        )
        db.commit()


# --------------------------------------------------------------------------- #
# Kullanıcılar
# --------------------------------------------------------------------------- #
def create_user(username: str, password_hash: str) -> dict:
    """Yeni kullanıcı açar; ilk kullanıcı, sahipsiz (misafir) görevleri devralır."""
    db = get_db()
    now = datetime.now().strftime(_TIME_FMT)
    is_first = db.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 0
    cursor = db.execute(
        """
        INSERT INTO users (username, username_lower, password_hash, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (username, username.lower(), password_hash, now),
    )
    user_id = cursor.lastrowid
    if is_first:
        # Uygulama hesapsız kullanımda üretilmiş görevleri ilk hesaba devret
        db.execute("UPDATE tasks SET user_id = ? WHERE user_id IS NULL", (user_id,))
    db.commit()
    return get_user(user_id)


def get_user(user_id: int) -> dict | None:
    """Kullanıcıyı kimliğine göre getirir."""
    row = get_db().execute(
        "SELECT * FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    return dict(row) if row else None


def get_user_by_username(username: str) -> dict | None:
    """Kullanıcı adı (büyük/küçük harf duyarsız) ile kullanıcı getirir."""
    row = get_db().execute(
        "SELECT * FROM users WHERE username_lower = ?", (username.lower(),)
    ).fetchone()
    return dict(row) if row else None


def username_taken(username: str) -> bool:
    """Kullanıcı adı kullanımda mı?"""
    return get_user_by_username(username) is not None


def create_task(user_id: int, title: str, description: str = "",
                due_at: str | None = None, priority: int = 2) -> dict:
    """Kullanıcıya yeni görev oluşturur ve kaydı döndürür."""
    db = get_db()
    now = datetime.now().strftime(_TIME_FMT)
    cursor = db.execute(
        """
        INSERT INTO tasks (title, description, search_text, created_at, user_id,
                           due_at, priority)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (title, description, normalize(f"{title} {description}"), now, user_id,
         due_at, priority),
    )
    db.commit()
    return get_task(cursor.lastrowid, user_id)


def get_task(task_id: int, user_id: int) -> dict | None:
    """Kullanıcının kendi (çöp kutusunda olmayan) görevini getirir."""
    row = get_db().execute(
        "SELECT * FROM tasks WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
        (task_id, user_id),
    ).fetchone()
    return dict(row) if row else None


def list_tasks(user_id: int, query: str = "") -> list[dict]:
    """Kullanıcının görevlerini listeler; sorgu verilirse başlık/açıklamada arar.

    Sıralama: bekleyenler önce; tarihi olanlar en yakın tarihe göre
    (tarih(siz) en sona), sonra en yeniler.
    """
    normalized = normalize((query or "").strip())
    # due_at IS NULL: tarihliler 0 (ön), tarihsizler 1 (son) → NULL'lar en alta
    order = "ORDER BY done ASC, due_at IS NULL, due_at ASC, created_at DESC, id DESC"
    if normalized:
        pattern = f"%{_escape_like(normalized)}%"
        rows = get_db().execute(
            f"SELECT * FROM tasks WHERE user_id = ? AND deleted_at IS NULL "
            f"AND search_text LIKE ? ESCAPE '\\' {order}",
            (user_id, pattern),
        ).fetchall()
    else:
        rows = get_db().execute(
            f"SELECT * FROM tasks WHERE user_id = ? AND deleted_at IS NULL {order}",
            (user_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_tasks_between(user_id: int, start: str, end: str) -> list[dict]:
    """Takvim için: başlangıcı dahil, sonu hariç aralıktaki tarihli görevler.

    `start`/`end` 'YYYY-MM-DD HH:MM' biçimindedir; due_at metin olarak
    karşılaştırıldığı için sıralama da doğal olarak saat sırasına göre olur.
    Çöp kutusundaki görevler dahil edilmez.
    """
    rows = get_db().execute(
        "SELECT * FROM tasks WHERE user_id = ? AND deleted_at IS NULL "
        "AND due_at IS NOT NULL AND due_at >= ? AND due_at < ? "
        "ORDER BY due_at ASC, priority DESC",
        (user_id, start, end),
    ).fetchall()
    return [dict(row) for row in rows]


def update_task(user_id: int, task_id: int, title: str, description: str,
                due_at: str | None, priority: int) -> dict | None:
    """Kullanıcının görevinin alanlarını günceller; görev yoksa None döner."""
    task = get_task(task_id, user_id)
    if task is None:
        return None
    db = get_db()
    db.execute(
        """
        UPDATE tasks
           SET title = ?, description = ?, search_text = ?, due_at = ?, priority = ?
         WHERE id = ? AND user_id = ?
        """,
        (title, description, normalize(f"{title} {description}"), due_at, priority,
         task_id, user_id),
    )
    db.commit()
    return get_task(task_id, user_id)


def toggle_task(user_id: int, task_id: int) -> dict | None:
    """Kullanıcının görevinin tamamlanma durumunu tersine çevirir; yoksa None."""
    task = get_task(task_id, user_id)
    if task is None:
        return None
    db = get_db()
    new_done = 0 if task["done"] else 1
    db.execute(
        "UPDATE tasks SET done = ?, completed_at = ? WHERE id = ? AND user_id = ?",
        (new_done, datetime.now().strftime(_TIME_FMT) if new_done else None,
         task_id, user_id),
    )
    db.commit()
    return get_task(task_id, user_id)


def soft_delete(user_id: int, task_id: int) -> bool:
    """Görevi çöp kutusuna taşır (kayıt silinmez; deleted_at işaretlenir)."""
    db = get_db()
    cursor = db.execute(
        "UPDATE tasks SET deleted_at = ? "
        "WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
        (datetime.now().strftime(_TIME_FMT), task_id, user_id),
    )
    db.commit()
    return cursor.rowcount > 0


def restore_task(user_id: int, task_id: int) -> bool:
    """Çöp kutusundaki görevi listeye geri alır."""
    db = get_db()
    cursor = db.execute(
        "UPDATE tasks SET deleted_at = NULL "
        "WHERE id = ? AND user_id = ? AND deleted_at IS NOT NULL",
        (task_id, user_id),
    )
    db.commit()
    return cursor.rowcount > 0


def purge_task(user_id: int, task_id: int) -> bool:
    """Görevi kalıcı olarak yok eder (geri alınamaz); yalnız sahibi silebilir."""
    db = get_db()
    cursor = db.execute(
        "DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id)
    )
    db.commit()
    return cursor.rowcount > 0


def list_deleted(user_id: int) -> list[dict]:
    """Çöp kutusundaki görevleri (en son silinen önce) listeler."""
    rows = get_db().execute(
        "SELECT * FROM tasks WHERE user_id = ? AND deleted_at IS NOT NULL "
        "ORDER BY deleted_at DESC, id DESC",
        (user_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def count_deleted(user_id: int) -> int:
    """Çöp kutusundaki görev sayısı."""
    return get_db().execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE user_id = ? AND deleted_at IS NOT NULL",
        (user_id,),
    ).fetchone()["n"]


def stats(user_id: int) -> dict:
    """Kullanıcının toplam / bekleyen / tamamlanan görev sayılarını döndürür.

    Çöp kutusundaki görevler istatistiklere dahil edilmez.
    """
    row = get_db().execute(
        "SELECT COUNT(*) AS total, COALESCE(SUM(done), 0) AS done "
        "FROM tasks WHERE user_id = ? AND deleted_at IS NULL",
        (user_id,),
    ).fetchone()
    total = row["total"]
    done = row["done"]
    return {
        "total": total,
        "done": done,
        "active": total - done,
        "percent": round(done / total * 100) if total else 0,
    }

"""Kullanıcı hesapları: oluşturma, doğrulama ve oturum yönetimi.

Parolalar werkzeug ile `scrypt` özeti olarak saklanır (düz metin yok).
Doğrulama hataları İngilizce anahtar döndürür; arayüz metnine çevirerek
`app.py` içindeki rotalar çevirir (çok dilli destekle uyumlu).
"""
from __future__ import annotations

import re
import sqlite3
import time
from collections import deque

from flask import g, session
from werkzeug.security import check_password_hash, generate_password_hash

import database

# Kullanıcı adı: 3-20 karakter; harf, rakam, nokta, tire, alt çizgi
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,20}$")
MIN_PASSWORD_LEN = 6
MAX_PASSWORD_LEN = 128  # aşırı uzun girdilerle özet hesaplatmayı sınırlar

SESSION_KEY = "user_id"

# Kaba kuvvet koruması: (ip, kullanıcı) başına kayan pencere
MAX_FAILED_LOGINS = 5
LOCK_WINDOW_SECONDS = 15 * 60  # 15 dakika
_MAX_TRACKED_KEYS = 5000  # bellek sınırı: aşırı birikimde tablo sıfırlanır

# (ip, kullanıcı_adı_küçük) -> başarısız deneme zaman damgaları
_failures: dict[tuple[str, str], deque] = {}


def hash_password(password: str) -> str:
    """Parolayı kriptografik özete çevirir."""
    return generate_password_hash(password)


def check_password(password_hash: str, password: str) -> bool:
    """Parola özetini doğrular."""
    return check_password_hash(password_hash, password)


def validate_registration(username: str, password: str, confirm: str) -> dict[str, str]:
    """Kayıt girdilerini doğrular; {alan: hata_anahtarı} döndürür."""
    errors: dict[str, str] = {}
    if not username:
        errors["username"] = "auth.err_username_required"
    elif not USERNAME_RE.match(username):
        errors["username"] = "auth.err_username_invalid"
    elif database.username_taken(username):
        errors["username"] = "auth.err_username_taken"

    if not password:
        errors["password"] = "auth.err_password_required"
    elif len(password) < MIN_PASSWORD_LEN:
        errors["password"] = "auth.err_password_short"
    elif len(password) > MAX_PASSWORD_LEN:
        errors["password"] = "auth.err_password_short"

    if confirm != password:
        errors["confirm"] = "auth.err_password_mismatch"
    return errors


# Zamanlama eşitleme için sabit sahte özet: bir kez üretilir, isteklerde yalnızca
# doğrulanır (var olmayan kullanıcıda da tam bir scrypt doğrulaması çalışsın diye)
_DUMMY_HASH = generate_password_hash("zamanlama-esitleme")


def register(username: str, password: str, confirm: str) -> tuple[dict | None, dict[str, str]]:
    """Yeni hesap açar; hata varsa (None, {alan: anahtar}) döndürür."""
    errors = validate_registration(username, password, confirm)
    if errors:
        return None, errors
    try:
        return database.create_user(username, hash_password(password)), {}
    except sqlite3.IntegrityError:
        # Yarış durumu: doğrulamadan sonra kullanıcı adı eşzamanlı olarak başkası
        # tarafından alınmış olabilir -> "zaten alınmış" gibi karşıla (500 değil)
        return None, {"username": "auth.err_username_taken"}


def login(username: str, password: str) -> dict | None:
    """Kimlik doğrular; başarısızsa None döndürür (aynı hata mesajı verilir)."""
    user = database.get_user_by_username(username) if username else None
    if user is None:
        # Zamanlama eşitleme: var olmayan kullanıcıda da TEK scrypt doğrulaması
        # çalışır. Üret+doğrula (2 scrypt) yapsaydı geçerli kullanıcı adı yanıt
        # süresiyle ayırt edilebilirdi. Özet bir kez üretilir, istekte doğrulanır.
        check_password(_DUMMY_HASH, password)
        return None
    if not check_password(user["password_hash"], password):
        return None
    return user


def login_user(user: dict, remember: bool = False) -> None:
    """Oturumu temizleyip kullanıcıyı oturuma bağlar (sabitleme koruması).

    remember=True ise "beni hatırla" için çerez kalıcı olur (süre
    PERMANENT_SESSION_LIFETIME); değilse tarayıcı kapanınca oturum düşer.
    """
    session.clear()
    session.permanent = remember
    session[SESSION_KEY] = user["id"]
    g.pop("auth_user", None)  # istek içi kullanıcı önbelleğini tazele


def logout_user() -> None:
    """Oturumu kapatır."""
    session.clear()
    g.pop("auth_user", None)


def current_user_id() -> int | None:
    """Oturumdaki kullanıcı kimliği."""
    return session.get(SESSION_KEY)


def current_user() -> dict | None:
    """Oturumdaki kullanıcı kaydı; istek başına tek sorgu (g ile önbelleklenir)."""
    if "auth_user" not in g:
        user_id = current_user_id()
        g.auth_user = database.get_user(user_id) if user_id else None
    return g.auth_user


# --------------------------------------------------------------------------- #
# Kaba kuvvet (brute-force) koruması
# --------------------------------------------------------------------------- #
def _prune(fails: deque, now: float) -> None:
    """Pencere dışındaki eski denemeleri atar."""
    while fails and now - fails[0] > LOCK_WINDOW_SECONDS:
        fails.popleft()


def is_locked_out(ip: str, username: str) -> bool:
    """Son LOCK_WINDOW_SECONDS içinde MAX_FAILED_LOGINS deneme aşıldıysa True."""
    fails = _failures.get((ip, username.lower()))
    if not fails:
        return False
    now = time.monotonic()
    _prune(fails, now)
    return len(fails) >= MAX_FAILED_LOGINS


def register_failure(ip: str, username: str) -> None:
    """Başarısız bir giriş denemesini kaydeder."""
    now = time.monotonic()
    key = (ip, username.lower())
    if key not in _failures:
        if len(_failures) >= _MAX_TRACKED_KEYS:
            _failures.clear()  # aşırı birikimi savuştur (yalnızca sınır amaçlı)
        _failures[key] = deque()
    fails = _failures[key]
    _prune(fails, now)
    fails.append(now)


def clear_failures(ip: str, username: str) -> None:
    """Başarılı girişte deneme kayıtlarını siler."""
    _failures.pop((ip, username.lower()), None)


def reset_lockout_state() -> None:
    """Kilit verisini sıfırlar (testler ve bakım için)."""
    _failures.clear()

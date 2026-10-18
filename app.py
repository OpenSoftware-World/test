"""Görev Merkezi - Flask tabanlı görev yönetimi uygulaması.

Katmanlar:
  * app.py        -> HTTP rotaları, doğrulama, CSRF koruması
  * database.py   -> SQLite veri erişimi
  * i18n.py       -> Çok dilli destek (langs/ altındaki .ini dil paketleri)
  * templates/    -> Jinja2 arayüzü (Bootstrap 5)
  * static/       -> Özgün CSS/JS ve yerel Bootstrap varlıkları
"""
from __future__ import annotations

import calendar
import hmac
import os
import secrets
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (
    Flask,
    abort,
    current_app,
    flash,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

import auth
import database
import i18n

MAX_TITLE_LEN = 200
MAX_DESC_LEN = 2000

# JS'in doğrudan kullanacağı çeviri anahtarları (base.html'de window.I18N'e basılır)
_CLIENT_KEYS = (
    "js.added",
    "js.completed",
    "js.undone",
    "js.deleted",
    "js.trashed",
    "js.restored",
    "js.purged",
    "js.saved",
    "js.list_failed",
    "js.now",
    "error.generic",
    "error.title_required",
    "toast.success",
    "toast.danger",
    "toast.info",
    "toast.warning",
    "theme.to_dark",
    "theme.to_light",
)


# --------------------------------------------------------------------------- #
# Yardımcılar
# --------------------------------------------------------------------------- #
def _load_secret_key(instance_path: str) -> str:
    """Oturum anahtarını yükler; yoksa üretip instance/secret_key dosyasına yazar."""
    path = os.path.join(instance_path, "secret_key")
    try:
        with open(path, encoding="utf-8") as handle:
            key = handle.read().strip()
        if key:
            return key
    except OSError:  # dosya yok ya da okunamadı -> yeni anahtar üret
        pass
    key = secrets.token_hex(32)
    os.makedirs(instance_path, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(key)
    os.chmod(path, 0o600)  # yalnızca dosya sahibi okuyabilsin
    return key


def _wants_json() -> bool:
    """İstek tarayıcı yerine JS tarafından (fetch) gönderildi mi?"""
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _t(key: str, **kwargs) -> str:
    """İstek bağlamındaki dil için çeviriyi döndürür."""
    return current_app.extensions["i18n"].gettext(i18n.get_locale(), key, **kwargs)


def _csrf_token() -> str:
    """Oturumda yoksa yeni bir CSRF anahtarı üretir ve döndürür."""
    token = session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf"] = token
    return token


def _parse_due(raw: str | None) -> tuple[str | None, bool]:
    """datetime-local girdisini ('2026-10-20T14:30') saklanan biçime çevirir.

    Boş girdi (None, False); geçersiz biçim (None, True) döner.
    """
    value = (raw or "").strip().replace("T", " ")
    if not value:
        return None, False
    try:
        datetime.strptime(value, "%Y-%m-%d %H:%M")
    except ValueError:
        return None, True
    return value, False


def _validate_task(title: str, description: str, due_raw, priority_raw):
    """Alanları doğrular; (hatalar, due_at, priority) üçlüsünü döndürür."""
    errors: dict[str, str] = {}
    if not title:
        errors["title"] = _t("error.title_required")
    elif len(title) > MAX_TITLE_LEN:
        errors["title"] = _t("error.title_too_long", max=MAX_TITLE_LEN)
    if len(description) > MAX_DESC_LEN:
        errors["description"] = _t("error.desc_too_long", max=MAX_DESC_LEN)

    due_at, due_invalid = _parse_due(due_raw)
    if due_invalid:
        errors["due_at"] = _t("error.due_invalid")

    if priority_raw in (None, ""):
        priority = 2  # alan gelmediyse varsayılan önem
    else:
        try:
            priority = int(priority_raw)
        except (TypeError, ValueError):
            priority = 0
        if not 1 <= priority <= 4:
            errors["priority"] = _t("error.priority_invalid")
            priority = 2
    return errors, due_at, priority


def _not_found():
    """Bilinmeyen görev için JSON ya da HTML 404 yanıtı üretir."""
    if _wants_json():
        return jsonify(ok=False, error=_t("error.not_found")), 404
    abort(404)


# Takvimde kabul edilen tarih aralığı: uç yıllarda ay/hafta kaydırma tarih
# taşmalarına (OverflowError -> 500) yol açıyordu; aralık dışına bugüne düşülür
_MIN_CAL_DATE = date(1900, 1, 1)
_MAX_CAL_DATE = date(9998, 12, 31)


def _parse_date(value: str | None) -> date | None:
    """'YYYY-MM-DD' biçimini date'e çevirir; boş/geçersiz/dışarıdaysa None döner."""
    try:
        parsed = datetime.strptime((value or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None
    return parsed if _MIN_CAL_DATE <= parsed <= _MAX_CAL_DATE else None


def _shift_month(day: date, delta: int) -> date:
    """Ay kaydırır; hedef ayda o gün yoksa ayın son gününe sabitler."""
    total = day.month - 1 + delta
    year = day.year + total // 12
    month = total % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _day_cell(day: date, by_day: dict, today_: date, selected: date,
              muted: bool = False) -> dict:
    """Takvim hücresi verisi: gün numarası, görev rozetleri ve durum bayrakları."""
    tasks = [] if muted else by_day.get(day.isoformat(), [])
    return {
        "iso": day.isoformat(),
        "num": day.day,
        "muted": muted,
        "is_today": day == today_,
        "is_selected": day == selected and not muted,
        "tasks": tasks,
        "more": max(0, len(tasks) - 3),  # 3'ten fazlası "+N" olarak gösterilir
    }


def _month_cells(selected: date, by_day: dict, today_: date) -> list[dict]:
    """Pazartesiyle başlayan aylık ızgara (ihtiyaç kadar 5-6 satır)."""
    first = selected.replace(day=1)
    next_month = _shift_month(first, 1)
    days_in_month = (next_month - first).days
    lead = first.weekday()  # pazartesi = 0
    rows = -(-(lead + days_in_month) // 7)
    start = first - timedelta(days=lead)
    return [
        _day_cell(start + timedelta(days=i), by_day, today_, selected,
                  muted=(start + timedelta(days=i)).month != selected.month)
        for i in range(rows * 7)
    ]


def _safe_next(default=None) -> str | None:
    """Formdaki `next` alanını yalnızca aynı sunucuya göreliyse döndürür."""
    nxt = (request.form.get("next") or "").strip()
    if nxt.startswith("/") and not nxt.startswith("//"):
        return nxt
    return default


def _csp_nonce() -> str:
    """İstek başına bir CSP nonce üretir; başlık ve şablon aynı değeri paylaşır."""
    if not getattr(g, "csp_nonce", ""):
        g.csp_nonce = secrets.token_urlsafe(16)
    return g.csp_nonce


def _register_security_headers(app: Flask) -> None:
    """Güvenlik başlıkları: tıklama hırsızlığı, içerik türü ve CSP koruması.

    CSP nonce'lu uygulanır: satır içi scriptler `csp_nonce()` ile işaretlenir,
    kaynaklar 'self' ile sınırlandırılır; böylece enjekte edilen uzak betik
    çalışamaz.
    """

    @app.after_request
    def _security_headers(response):
        if response.mimetype == "text/html":
            nonce = _csp_nonce()
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; "
                f"script-src 'self' 'nonce-{nonce}'; "
                "style-src 'self' 'unsafe-inline'; "  # satır içi style= alanları için
                "img-src 'self' data:; "
                "font-src 'self' data:; "
                "connect-src 'self'; "
                "object-src 'none'; base-uri 'self'; "
                "form-action 'self'; frame-ancestors 'none'",
            )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response


# --------------------------------------------------------------------------- #
# Uygulama fabrikası
# --------------------------------------------------------------------------- #
def create_app(test_config: dict | None = None) -> Flask:
    """Uygulamayı oluşturur; testler için yapılandırma enjeksiyonu desteklenir."""
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        # .env'den verilmezse instance/secret_key dosyasında saklanan anahtar
        # kullanılır; böylece yeniden başlatmada oturumlar (dil tercihi, CSRF koruması)
        # korunur.
        SECRET_KEY=os.environ.get("SECRET_KEY") or _load_secret_key(app.instance_path),
        DATABASE=os.path.join(app.instance_path, "tasks.db"),
        LANGS_DIR=os.path.join(app.root_path, "langs"),
        MAX_CONTENT_LENGTH=64 * 1024,  # 64 KB: form için fazlasıyla yeterli
        # "Beni hatırla" işaretlenirse oturum çerezi bu kadar süre kalıcı olur
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        # Çerez bayrakları: SameSite (CSRF ek katmanı) + HttpOnly (JS'ten erişilemez);
        # HTTPS ile yayında `COOKIE_SECURE=1` verilerek Secure da açılır
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "") == "1",
        JSON_SORT_KEYS=False,
    )
    if test_config:
        app.config.update(test_config)

    os.makedirs(app.instance_path, exist_ok=True)
    database.init_app(app)
    i18n.init_app(app)

    app.jinja_env.globals["csrf_token"] = _csrf_token
    app.jinja_env.globals["csp_nonce"] = _csp_nonce
    # Geçmiş tarih kontrolü (gecikmiş görev) için şablonda "şimdi" değeri
    app.jinja_env.globals["now"] = lambda: datetime.now().strftime("%Y-%m-%d %H:%M")
    app.jinja_env.filters["format_date"] = _format_date

    _register_i18n(app)
    _register_csrf(app)
    _register_security_headers(app)
    _register_auth(app)
    _register_routes(app)
    return app


def _register_i18n(app: Flask) -> None:
    """Şablonlara çeviri fonksiyonunu ve JS'in kullanacağı sözlükleri enjekte eder."""

    @app.context_processor
    def _inject_translations():
        engine = app.extensions["i18n"]
        locale = i18n.get_locale()
        return {
            "t": lambda key, **kwargs: engine.gettext(locale, key, **kwargs),
            "locale": locale,
            "locales": engine.labels(locale),
            "client_strings": {key: engine.gettext(locale, key) for key in _CLIENT_KEYS},
            "client_months": engine.months(locale),
            "current_user": auth.current_user(),
        }


def _format_date(value: str) -> str:
    """'2026-10-18 16:12:00' / '2026-10-20 14:30' -> '18 Eki 2026 · 16:12'

    Ay adı seçili dile göre döner; biçimi tanınmayan değer olduğu gibi döner.
    """
    if not value:
        return ""
    dt = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(value, fmt)
            break
        except (TypeError, ValueError):
            continue
    if dt is None:
        return value
    months = current_app.extensions["i18n"].months(i18n.get_locale())
    return f"{dt.day} {months[dt.month - 1]} {dt.year} · {dt:%H:%M}"


def _register_auth(app: Flask) -> None:
    """Kayıt / giriş / çıkış rotalarını ve giriş korumasını tanımlar."""

    def login_required(view):
        """Giriş yapılmamış istekleri giriş sayfasına (XHR ise 401'e) yönlendirir."""

        @wraps(view)
        def wrapped(*args, **kwargs):
            user = auth.current_user()
            if user is None:
                if _wants_json():
                    return jsonify(ok=False, error=_t("error.login_required")), 401
                return redirect(url_for("login"))
            return view(*args, **kwargs)

        return wrapped

    app.login_required = login_required  # diğer modüllerin/şablonların erişimi için

    @app.route("/giris", methods=["GET", "POST"], endpoint="login")
    def _login():
        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            remember = request.form.get("remember") == "on"

            # Kaba kuvvet koruması: pencere içinde deneme limiti aşılırsa
            # kullanıcı adından bağımsız olarak kilit uygulanır
            ip = request.remote_addr or "?"
            if auth.is_locked_out(ip, username):
                message = _t("auth.err_locked", minutes=auth.LOCK_WINDOW_SECONDS // 60)
                if _wants_json():
                    return jsonify(ok=False, errors={"username": message}), 429
                flash(message, "danger")
                response = make_response(render_template("login.html", username=username), 429)
                response.headers["Retry-After"] = str(auth.LOCK_WINDOW_SECONDS)
                return response

            user = auth.login(username, password)
            if user is None:
                auth.register_failure(ip, username)
                message = _t("auth.err_wrong_credentials")
                if _wants_json():
                    return jsonify(ok=False, errors={"username": message}), 401
                flash(message, "danger")
                return render_template("login.html", username=username)
            auth.clear_failures(ip, username)
            auth.login_user(user, remember=remember)
            flash(_t("auth.flash_logged_in", name=user["username"]), "success")
            return redirect(url_for("index"))
        return render_template("login.html")

    @app.route("/kayit", methods=["GET", "POST"], endpoint="register")
    def _register():
        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            confirm = request.form.get("confirm") or ""
            user, error_keys = auth.register(username, password, confirm)
            if user is None:
                if _wants_json():
                    return jsonify(
                        ok=False, errors={k: _t(v) for k, v in error_keys.items()}
                    ), 422
                for key in error_keys.values():
                    flash(_t(key), "danger")
                return render_template("register.html", username=username)
            auth.login_user(user)
            flash(_t("auth.flash_registered"), "success")
            return redirect(url_for("index"))
        return render_template("register.html")

    @app.post("/cikis", endpoint="logout")
    def _logout():
        auth.logout_user()
        if not _wants_json():
            flash(_t("auth.flash_logged_out"), "info")
        return redirect(url_for("login"))


def _register_csrf(app: Flask) -> None:
    """Tüm POST isteklerinde oturum CSRF anahtarını doğrular."""

    @app.before_request
    def _check_csrf() -> None:
        if request.method != "POST":
            return
        sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token") or ""
        expected = session.get("_csrf") or ""
        if not sent or not hmac.compare_digest(sent, expected):
            if _wants_json():
                return jsonify(ok=False, error=_t("error.csrf")), 400
            abort(400, description=_t("error.csrf"))


def _register_routes(app: Flask) -> None:
    @app.get("/")
    @app.login_required
    def index():
        """Görev listesi; fragment=1 ile yalnızca liste parçası döner (JS arama)."""
        user = auth.current_user()
        query = (request.args.get("q") or "").strip()
        context = {
            "query": query,
            "tasks": database.list_tasks(user["id"], query),
            "stats": database.stats(user["id"]),
            "trash_count": database.count_deleted(user["id"]),
        }
        if request.args.get("fragment") == "1":
            return render_template("task_list.html", **context)
        return render_template("index.html", **context)

    @app.post("/tasks")
    @app.login_required
    def add_task():
        """Yeni görev ekler."""
        user = auth.current_user()
        title = (request.form.get("title") or "").strip()
        description = (request.form.get("description") or "").strip()
        query = (request.form.get("q") or "").strip()

        # Takvim formu ayrı `date`+`time` alanı gönderir; due_at oradan kurulur
        due_raw = request.form.get("due_at")
        if not (due_raw or "").strip() and request.form.get("date"):
            due_raw = f"{request.form.get('date')} {request.form.get('time') or '09:00'}"

        errors, due_at, priority = _validate_task(
            title, description, due_raw, request.form.get("priority")
        )
        if errors:
            if _wants_json():
                return jsonify(ok=False, errors=errors), 422
            for message in errors.values():
                flash(message, "danger")
            return redirect(_safe_next() or url_for("index", q=query))

        task = database.create_task(user["id"], title, description, due_at, priority)
        if _wants_json():
            return jsonify(ok=True, task=task, stats=database.stats(user["id"])), 201
        flash(_t("flash.added"), "success")
        return redirect(_safe_next() or url_for("index", q=query))

    @app.post("/lang/<code>")
    def set_lang(code: str):
        """Dil tercihini oturuma ve düz çereze yazar, bulunduğu sayfaya döner."""
        if not i18n.set_locale(code):
            return _not_found()
        query = (request.form.get("q") or "").strip()
        next_url = request.form.get("next") or ""
        # Yalnızca aynı sunucuya ait göreli yollara yönlendir (açık yönlendirme koruması)
        target = url_for("index", q=query)
        if next_url.startswith("/") and not next_url.startswith("//"):
            target = next_url
        response = redirect(target)
        response.set_cookie(
            i18n.COOKIE_KEY, code, max_age=60 * 60 * 24 * 365,
            samesite="Lax", secure=current_app.config["SESSION_COOKIE_SECURE"],
        )
        return response

    @app.post("/tasks/<int:task_id>/toggle")
    @app.login_required
    def toggle_task(task_id: int):
        """Görevin tamamlanma durumunu değiştirir."""
        user = auth.current_user()
        task = database.toggle_task(user["id"], task_id)
        if task is None:
            return _not_found()
        if _wants_json():
            return jsonify(ok=True, task=task, stats=database.stats(user["id"]))
        flash(_t("flash.updated"), "info")
        return redirect(url_for("index", q=(request.args.get("q") or "").strip()))

    @app.post("/tasks/<int:task_id>/edit")
    @app.login_required
    def edit_task(task_id: int):
        """Görevin alanlarını günceller (başlık, açıklama, tarih/saat, önem)."""
        user = auth.current_user()
        title = (request.form.get("title") or "").strip()
        description = (request.form.get("description") or "").strip()
        query = (request.form.get("q") or "").strip()

        errors, due_at, priority = _validate_task(
            title, description, request.form.get("due_at"), request.form.get("priority")
        )
        if errors:
            if _wants_json():
                return jsonify(ok=False, errors=errors), 422
            for message in errors.values():
                flash(message, "danger")
            return redirect(url_for("index", q=query))

        task = database.update_task(
            user["id"], task_id, title, description, due_at, priority
        )
        if task is None:
            return _not_found()
        if _wants_json():
            return jsonify(ok=True, task=task, stats=database.stats(user["id"]))
        flash(_t("flash.edited"), "success")
        return redirect(url_for("index", q=query))

    @app.post("/tasks/<int:task_id>/delete")
    @app.login_required
    def delete_task(task_id: int):
        """Görevi çöp kutusuna taşır (kalıcı silme ayrı `/purge` rotasıdır)."""
        user = auth.current_user()
        if not database.soft_delete(user["id"], task_id):
            return _not_found()
        if _wants_json():
            return jsonify(
                ok=True,
                stats=database.stats(user["id"]),
                trash_count=database.count_deleted(user["id"]),
            )
        flash(_t("flash.trashed"), "warning")
        return redirect(url_for("index", q=(request.args.get("q") or "").strip()))

    @app.post("/tasks/<int:task_id>/restore")
    @app.login_required
    def restore_task(task_id: int):
        """Çöp kutusundaki görevi listeye geri alır."""
        user = auth.current_user()
        if not database.restore_task(user["id"], task_id):
            return _not_found()
        if _wants_json():
            return jsonify(
                ok=True,
                stats=database.stats(user["id"]),
                trash_count=database.count_deleted(user["id"]),
            )
        flash(_t("flash.restored"), "success")
        return redirect(url_for("trash"))

    @app.post("/tasks/<int:task_id>/purge")
    @app.login_required
    def purge_task(task_id: int):
        """Görevi kalıcı olarak siler; adının birebir yazılması gerekir."""
        user = auth.current_user()
        task = database.get_task(user["id"], task_id)  # listede olmayan çöp görevleri için
        if task is None:
            # Çöp kutusundaki kayıt olabilir; sahiplik kontrolüyle çek
            task = next(
                (t for t in database.list_deleted(user["id"]) if t["id"] == task_id),
                None,
            )
        if task is None:
            return _not_found()

        written = (request.form.get("confirm_title") or "").strip()
        if written != task["title"]:
            message = _t("error.purge_mismatch")
            if _wants_json():
                return jsonify(ok=False, error=message), 422
            flash(message, "danger")
            return redirect(url_for("trash"))

        if not database.purge_task(user["id"], task_id):
            return _not_found()
        if _wants_json():
            return jsonify(
                ok=True,
                stats=database.stats(user["id"]),
                trash_count=database.count_deleted(user["id"]),
            )
        flash(_t("flash.purged"), "warning")
        return redirect(url_for("index"))

    @app.get("/cop", endpoint="trash")
    @app.login_required
    def trash_page():
        """Çöp kutusu: silinen görevler, geri alma ve kalıcı silme."""
        user = auth.current_user()
        return render_template(
            "trash.html", deleted=database.list_deleted(user["id"])
        )

    @app.get("/takvim", endpoint="calendar")
    @app.login_required
    def calendar_page():
        """Takvim: aylık ızgara / haftalık şerit + seçili gün paneli.

        Adres yalnızca iki alan taşır: `d` (seçili gün) ve `view` (month|week);
        dönem bunlardan türetilir, böylece bağlantılar paylaşılabilir kalır.
        """
        user = auth.current_user()
        view = request.args.get("view", "month")
        if view not in ("month", "week"):
            view = "month"
        today_ = date.today()
        selected = _parse_date(request.args.get("d")) or today_

        # Dönem + ok bağlantılarının hedef günleri
        if view == "month":
            period_start = selected.replace(day=1)
            period_end = _shift_month(period_start, 1)
            prev_d = _shift_month(selected, -1).isoformat()
            next_d = _shift_month(selected, 1).isoformat()
        else:
            period_start = selected - timedelta(days=selected.weekday())
            period_end = period_start + timedelta(days=7)
            prev_d = (selected - timedelta(days=7)).isoformat()
            next_d = (selected + timedelta(days=7)).isoformat()

        tasks = database.list_tasks_between(
            user["id"],
            period_start.isoformat() + " 00:00",
            period_end.isoformat() + " 00:00",
        )
        by_day: dict[str, list[dict]] = {}
        for task in tasks:
            by_day.setdefault(task["due_at"][:10], []).append(task)

        if view == "month":
            cells = _month_cells(selected, by_day, today_)
        else:
            cells = [
                _day_cell(period_start + timedelta(days=i), by_day, today_, selected)
                for i in range(7)
            ]

        # Dönem başlığı (takvimde tam ay adı kullanılır)
        engine = current_app.extensions["i18n"]
        months_full = engine.months_full(i18n.get_locale())
        if view == "month":
            period_label = f"{months_full[selected.month - 1]} {selected.year}"
        else:
            last = period_end - timedelta(days=1)
            if (last.month, last.year) == (period_start.month, period_start.year):
                period_label = (
                    f"{period_start.day} – {last.day} "
                    f"{months_full[period_start.month - 1]} {period_start.year}"
                )
            else:
                period_label = (
                    f"{period_start.day} {months_full[period_start.month - 1]} – "
                    f"{last.day} {months_full[last.month - 1]} {last.year}"
                )

        return render_template(
            "calendar.html",
            view=view,
            cells=cells,
            selected=selected,
            today_=today_,
            selected_tasks=by_day.get(selected.isoformat(), []),
            selected_label=f"{selected.day} {months_full[selected.month - 1]} {selected.year}",
            period_label=period_label,
            prev_d=prev_d,
            next_d=next_d,
            weekdays=[_t(f"weekdays.{i}") for i in range(1, 8)],
        )

    @app.errorhandler(404)
    def _not_found_page(_error):
        if _wants_json():
            return jsonify(ok=False, error=_t("error.page_not_found")), 404
        return render_template("404.html"), 404


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)

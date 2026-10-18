"""Görev Merkezi için uçtan uca testler (Flask test istemcisi + unittest)."""
from __future__ import annotations

import os
import pathlib
import re
import sqlite3
import sys
import tempfile
import unittest

# `python -m unittest` ile farklı dizinlerden çalıştırılsın diye kök dizini yola ekle
ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import auth  # noqa: E402
import database  # noqa: E402
from app import create_app  # noqa: E402


class TaskAppTestCase(unittest.TestCase):
    USERNAME = "tester"
    PASSWORD = "sifre123"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.app = self._make_app(self._tmp.name)
        auth.reset_lockout_state()  # kilit verisi modül genelinde; her test sıfır başlasın
        self.client, self.csrf = self._register_client(self.app, self.USERNAME)
        with self.app.app_context():
            self.user_id = database.get_user_by_username(self.USERNAME)["id"]

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ------------------------------------------------------------- yardımcılar
    @staticmethod
    def _make_app(directory: str):
        return create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret",
                "DATABASE": os.path.join(directory, "test.db"),
            }
        )

    @staticmethod
    def _extract_csrf(response) -> str:
        match = re.search(r'name="csrf-token" content="([^"]+)"', response.get_data(as_text=True))
        assert match, "Sayfada CSRF anahtarı bulunamadı"
        return match.group(1)

    def _register_client(self, app, username: str, password: str | None = None):
        """Yeni istemci açar, hesap oluşturup giriş yapar; (istemci, csrf) döner."""
        password = password or self.PASSWORD
        client = app.test_client()
        csrf = self._extract_csrf(client.get("/giris"))
        client.post(
            "/kayit",
            data={"username": username, "password": password, "confirm": password,
                  "_csrf": csrf},
            follow_redirects=True,
        )
        # Girişten sonra oturum yenilenir; güncel CSRF anahtarını sayfadan al
        return client, self._extract_csrf(client.get("/"))

    def _post(self, url: str, data: dict | None = None, xhr: bool = False):
        """POST gönderir; xhr=True ise JSON, değilse yönlendirme akışını kullanır."""
        payload = dict(data or {})
        if xhr:
            headers = {"X-Requested-With": "XMLHttpRequest", "X-CSRF-Token": self.csrf}
        else:
            payload["_csrf"] = self.csrf
            headers = {}
        return self.client.post(url, data=payload, headers=headers, follow_redirects=not xhr)

    def _login_attempt(self, password: str):
        """Giriş sayfasından taze CSRF ile tek bir giriş denemesi yapar."""
        csrf = self._extract_csrf(self.client.get("/giris"))
        return self.client.post(
            "/giris",
            data={"username": self.USERNAME, "password": password, "_csrf": csrf},
        )

    def _create(self, title: str, description: str = "", due_at: str | None = None,
                priority: int = 2) -> int:
        with self.app.app_context():
            return database.create_task(
                self.user_id, title, description, due_at, priority
            )["id"]

    # ------------------------------------------------------------------ tests
    def test_index_renders_empty_state(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Görev Merkezi", response.get_data(as_text=True))
        self.assertIn("Listen henüz boş", response.get_data(as_text=True))

    def test_add_task_without_js_redirects_and_persists(self) -> None:
        response = self._post("/tasks", {"title": "Sunum taslağını hazırla", "description": "10 slayt"})
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn("Sunum taslağını hazırla", text)
        self.assertIn("Görev listeye eklendi.", text)

    def test_add_task_requires_title(self) -> None:
        response = self._post("/tasks", {"title": "   "})
        self.assertIn("Görev başlığı boş olamaz.", response.get_data(as_text=True))

    def test_add_task_json_validation_returns_422(self) -> None:
        response = self._post("/tasks", {"title": ""}, xhr=True)
        self.assertEqual(response.status_code, 422)
        payload = response.get_json()
        self.assertFalse(payload["ok"])
        self.assertIn("title", payload["errors"])

    def test_toggle_task_updates_done_and_completed_at(self) -> None:
        task_id = self._create("Test görevi")

        first = self._post(f"/tasks/{task_id}/toggle", xhr=True).get_json()
        self.assertTrue(first["ok"])
        self.assertEqual(first["task"]["done"], 1)
        self.assertIsNotNone(first["task"]["completed_at"])
        self.assertEqual(first["stats"]["done"], 1)
        self.assertEqual(first["stats"]["active"], 0)

        second = self._post(f"/tasks/{task_id}/toggle", xhr=True).get_json()
        self.assertEqual(second["task"]["done"], 0)
        self.assertIsNone(second["task"]["completed_at"])
        self.assertEqual(second["stats"]["percent"], 0)

    def test_delete_moves_task_to_trash(self) -> None:
        task_id = self._create("Çöpe gidecek görev")

        deleted = self._post(f"/tasks/{task_id}/delete", xhr=True).get_json()
        self.assertTrue(deleted["ok"])
        self.assertEqual(deleted["stats"]["total"], 0)   # aktif listeden düştü
        self.assertEqual(deleted["trash_count"], 1)      # çöp kutusuna gitti

        # Kayıt silinmedi: yalnızca deleted_at işaretlendi
        with self.app.app_context():
            row = database.get_db().execute(
                "SELECT deleted_at FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        self.assertIsNotNone(row["deleted_at"])

        # Ana listede görünmez, çöp kutusunda görünür
        self.assertNotIn("Çöpe gidecek görev", self.client.get("/").get_data(as_text=True))
        self.assertIn("Çöpe gidecek görev", self.client.get("/cop").get_data(as_text=True))

        # Aynı göreve ikinci silme denemesi 404 (zaten çöp kutusunda)
        again = self._post(f"/tasks/{task_id}/delete", xhr=True)
        self.assertEqual(again.status_code, 404)

    def test_restore_brings_task_back(self) -> None:
        task_id = self._create("Geri alınacak görev")
        self._post(f"/tasks/{task_id}/delete", xhr=True)

        restored = self._post(f"/tasks/{task_id}/restore", xhr=True).get_json()
        self.assertTrue(restored["ok"])
        self.assertEqual(restored["trash_count"], 0)
        self.assertIn("Geri alınacak görev", self.client.get("/").get_data(as_text=True))
        self.assertNotIn("Geri alınacak görev",
                         self.client.get("/cop").get_data(as_text=True))

    def test_purge_requires_exact_task_name(self) -> None:
        task_id = self._create("Kalıcı silinecek görev")
        self._post(f"/tasks/{task_id}/delete", xhr=True)  # önce çöp kutusuna

        wrong = self._post(f"/tasks/{task_id}/purge",
                           {"confirm_title": "yanlis isim"}, xhr=True)
        self.assertEqual(wrong.status_code, 422)
        self.assertIn("eşleşmiyor", wrong.get_json()["error"])

        right = self._post(f"/tasks/{task_id}/purge",
                           {"confirm_title": "Kalıcı silinecek görev"}, xhr=True)
        self.assertTrue(right.get_json()["ok"])

        # Kayıt tamamen yok oldu (geri alınamaz)
        with self.app.app_context():
            row = database.get_db().execute(
                "SELECT id FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        self.assertIsNone(row)

    def test_purge_other_users_task_returns_404(self) -> None:
        task_id = self._create("Kisisel gorev")
        client_b, csrf_b = self._register_client(self.app, "buse")
        response = client_b.post(
            f"/tasks/{task_id}/purge",
            data={"confirm_title": "Kisisel gorev", "_csrf": csrf_b},
        )
        self.assertEqual(response.status_code, 404)

    def test_trash_page_requires_login(self) -> None:
        response = self.app.test_client().get("/cop")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/giris", response.headers["Location"])

    # ------------------------------------------------------------------ takvim
    def test_calendar_requires_login(self) -> None:
        response = self.app.test_client().get("/takvim")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/giris", response.headers["Location"])

    def test_calendar_month_view_places_task_on_its_date(self) -> None:
        self._create("Takvimdeki gorev", due_at="2026-11-15 10:30")
        self._create("Tarihsiz gorev")  # tarihsiz görev takvimde görünmez
        gone = self._create("Copataki gorev", due_at="2026-11-15 11:00")
        self._post(f"/tasks/{gone}/delete", xhr=True)  # çöp kutusundakiler de yok

        page = self.client.get("/takvim?d=2026-11-15").get_data(as_text=True)
        self.assertGreaterEqual(page.count("Takvimdeki gorev"), 2)  # çip + gün paneli
        self.assertNotIn("Tarihsiz gorev", page)
        self.assertNotIn("Copataki gorev", page)
        self.assertIn("Kasım 2026", page)   # dönem başlığı tam ay adıdır
        self.assertIn("data-href", page)    # hücreye tıklayınca gün seçilir

    def test_calendar_week_view_navigates_by_seven_days(self) -> None:
        self._create("Haftalik is", due_at="2026-11-17 09:00")

        page = self.client.get("/takvim?view=week&d=2026-11-17").get_data(as_text=True)
        self.assertIn("Haftalik is", page)
        self.assertIn("d=2026-11-10", page)   # bir önceki hafta oku
        self.assertIn("d=2026-11-24", page)   # sonraki hafta oku

        # geçersiz view değeri aylık görünüme düşer
        fallback = self.client.get(
            "/takvim?view=yok-boyle&d=2026-11-17"
        ).get_data(as_text=True)
        self.assertIn("Kasım 2026", fallback)

    def test_calendar_add_form_sets_date_and_returns_to_calendar(self) -> None:
        response = self.client.post(
            "/tasks",
            data={
                "title": "Takvimden eklenen",
                "date": "2026-11-20",
                "time": "14:45",
                "priority": "3",
                "next": "/takvim?view=month&d=2026-11-20",
                "_csrf": self.csrf,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/takvim?view=month&d=2026-11-20", response.headers["Location"])

        with self.app.app_context():
            row = database.get_db().execute(
                "SELECT due_at, priority FROM tasks WHERE title = ?",
                ("Takvimden eklenen",),
            ).fetchone()
        self.assertEqual(row["due_at"], "2026-11-20 14:45")
        self.assertEqual(row["priority"], 3)
        self.assertIn(
            "Takvimden eklenen",
            self.client.get("/takvim?d=2026-11-20").get_data(as_text=True),
        )

    def test_calendar_add_rejects_external_next(self) -> None:
        """Açık yönlendirme: `next` alanı yalnızca aynı sunucuya göreli olabilir."""
        response = self.client.post(
            "/tasks",
            data={
                "title": "Guvenlik kontrolu",
                "date": "2026-11-21",
                "next": "https://evil.example/",
                "_csrf": self.csrf,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("evil.example", response.headers["Location"])

    # ------------------------------------------------- güvenlik ve dayanıklılık
    def test_calendar_extreme_dates_do_not_crash(self) -> None:
        """Uç yıllar ay/hafta kaydırmada tarih taşmasına (500) yol açmamalı."""
        for value in ("0001-01-15", "9999-12-01", "1899-05-05", "boyle-bir-tarih"):
            response = self.client.get(f"/takvim?d={value}")
            self.assertEqual(response.status_code, 200, value)

    def test_login_lockout_after_five_failures(self) -> None:
        for _ in range(5):
            self.assertEqual(self._login_attempt("yanlis-sifre").status_code, 200)

        locked = self._login_attempt("yanlis-sifre-2")
        self.assertEqual(locked.status_code, 429)
        self.assertIn("Retry-After", locked.headers)

        # kilitliyken doğru şifre de reddedilir ve kilit mesajı görünür
        blocked = self._login_attempt(self.PASSWORD)
        self.assertEqual(blocked.status_code, 429)
        self.assertIn("başarısız giriş", blocked.get_data(as_text=True))

    def test_successful_login_resets_failure_counter(self) -> None:
        for _ in range(3):
            self._login_attempt("yanlis-sifre")
        self.assertEqual(self._login_attempt(self.PASSWORD).status_code, 302)
        # başarılı giriş sayacı sıfırladı: kalan deneme hakları yenilendi
        self.assertEqual(self._login_attempt("yanlis-sifre").status_code, 200)
        self.assertEqual(self._login_attempt(self.PASSWORD).status_code, 302)

    def test_unknown_user_gets_same_login_error(self) -> None:
        """Var olmayan kullanıcı, var olana aynı mesajla reddedilir (enumerasyon)."""
        csrf = self._extract_csrf(self.client.get("/giris"))
        response = self.client.post(
            "/giris",
            data={"username": "boyle-birisi", "password": "herhangi-bir", "_csrf": csrf},
        )
        text = response.get_data(as_text=True)
        self.assertIn("Kullanıcı adı veya şifre hatalı", text)
        self.assertNotIn("bulunamadı", text)

    def test_security_headers_and_csp_nonce(self) -> None:
        """Güvenlik başlıkları ve nonce'lu CSP her HTML yanıtta bulunur."""
        response = self.client.get("/giris")
        headers = response.headers
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Referrer-Policy"], "same-origin")

        csp = headers["Content-Security-Policy"]
        self.assertIn("frame-ancestors 'none'", csp)
        match = re.search(r"script-src 'self' 'nonce-([^']+)'", csp)
        self.assertTrue(match, csp)
        html = response.get_data(as_text=True)
        self.assertIn(f'<script nonce="{match.group(1)}">', html)
        self.assertNotIn("<script>", html)  # nonce'suz satır içi script kalmamalı

        # oturumlu sayfa da aynı korumayı taşır
        authed = self.client.get("/")
        self.assertEqual(authed.headers["X-Content-Type-Options"], "nosniff")
        authed_html = authed.get_data(as_text=True)
        authed_nonce = re.search(r"nonce-([^']+)", authed.headers["Content-Security-Policy"]).group(1)
        self.assertIn(f'<script nonce="{authed_nonce}">', authed_html)
        self.assertNotIn("<script>", authed_html)

    def test_registration_race_reports_username_taken(self) -> None:
        """Eşzamanlı aynı-kullanıcı-adı yarışı 500 değil 'zaten kullanımda' döner."""
        original = database.create_user

        def _racing(*args, **kwargs):
            raise sqlite3.IntegrityError("UNIQUE users.username_lower")

        database.create_user = _racing
        try:
            response = self.client.post(
                "/kayit",
                data={"username": "yaris", "password": "sifre123",
                      "confirm": "sifre123", "_csrf": self.csrf},
                follow_redirects=True,
            )
        finally:
            database.create_user = original
        self.assertIn("zaten kullanımda", response.get_data(as_text=True))

    def test_unknown_task_toggle_returns_404(self) -> None:
        response = self._post("/tasks/9999/toggle", xhr=True)
        self.assertEqual(response.status_code, 404)

    def test_search_filters_by_title(self) -> None:
        keep = self._create("Haftalık raporu yaz")
        self._create("Marka görsellerini yükle")

        response = self.client.get("/?q=rapor")
        text = response.get_data(as_text=True)
        self.assertIn("Haftalık raporu yaz", text)
        self.assertNotIn("Marka görsellerini yükle", text)
        self.assertIn(f'data-id="{keep}"', text)

        empty = self.client.get("/?q=yokboylegorev")
        self.assertIn("Sonuç yok", empty.get_data(as_text=True))

    def test_search_normalizes_turkish_characters(self) -> None:
        self._create("Şirket sunumunu gönder")
        self._create("Günlük notları güncelle")

        # Türkçe büyük/küçük harf farkı ve ASCII karşılığı ile arama
        for query in ("Şirket", "sirket", "SIRKET"):
            text = self.client.get(f"/?q={query}").get_data(as_text=True)
            self.assertIn("Şirket sunumunu gönder", text, f"sorgu: {query}")
            self.assertNotIn("Günlük notları güncelle", text, f"sorgu: {query}")

    def test_search_ignores_like_wildcards(self) -> None:
        self._create("Tek görev")
        text = self.client.get("/?q=%25").get_data(as_text=True)  # %
        self.assertNotIn("Tek görev", text)
        self.assertIn("Sonuç yok", text)

    def test_search_fragment_returns_list_only(self) -> None:
        self._create("Fragment görevi")
        response = self.client.get("/?q=fragment&fragment=1")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn("Fragment görevi", text)
        self.assertNotIn("<html", text)  # tam sayfa değil, yalnızca liste parçası

    def test_post_without_csrf_token_is_rejected(self) -> None:
        response = self.client.post("/tasks", data={"title": "Güvensiz"})
        self.assertEqual(response.status_code, 400)

    def test_post_with_wrong_csrf_token_is_rejected(self) -> None:
        response = self.client.post(
            "/tasks", data={"title": "Güvensiz", "_csrf": "yanlis-token"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        self.assertEqual(response.status_code, 400)

    def test_unknown_page_returns_404(self) -> None:
        response = self.client.get("/olarak-bir-sayfa")
        self.assertEqual(response.status_code, 404)
        self.assertIn("Sayfa bulunamadı", response.get_data(as_text=True))

    def test_title_length_limit(self) -> None:
        response = self._post("/tasks", {"title": "x" * 250}, xhr=True)
        self.assertEqual(response.status_code, 422)
        self.assertIn("200 karakter", response.get_json()["errors"]["title"])

    # ------------------------------------- tarih/saat, önem, düzenleme
    def test_add_task_with_due_date_and_priority(self) -> None:
        response = self._post(
            "/tasks",
            {"title": "Sunuma hazırlan", "due_at": "2026-12-31T18:30", "priority": "4"},
            xhr=True,
        )
        self.assertEqual(response.status_code, 201)
        task = response.get_json()["task"]
        self.assertEqual(task["due_at"], "2026-12-31 18:30")
        self.assertEqual(task["priority"], 4)

    def test_add_task_rejects_invalid_due_and_priority(self) -> None:
        bad_due = self._post("/tasks", {"title": "X", "due_at": "bozuk"}, xhr=True)
        self.assertEqual(bad_due.status_code, 422)
        self.assertIn("due_at", bad_due.get_json()["errors"])

        bad_prio = self._post("/tasks", {"title": "X", "priority": "9"}, xhr=True)
        self.assertEqual(bad_prio.status_code, 422)
        self.assertIn("priority", bad_prio.get_json()["errors"])

    def test_edit_task_updates_fields(self) -> None:
        task_id = self._create("Eski başlık")
        response = self._post(
            f"/tasks/{task_id}/edit",
            {"title": "Yeni başlık", "description": "Yeni açıklama",
             "due_at": "2026-11-01T09:15", "priority": "3"},
            xhr=True,
        )
        self.assertEqual(response.status_code, 200)
        task = response.get_json()["task"]
        self.assertEqual(task["title"], "Yeni başlık")
        self.assertEqual(task["description"], "Yeni açıklama")
        self.assertEqual(task["due_at"], "2026-11-01 09:15")
        self.assertEqual(task["priority"], 3)

        # Düzenleme sonrası arama metni de güncellenir
        text = self.client.get("/?q=yeni").get_data(as_text=True)
        self.assertIn("Yeni başlık", text)

    def test_edit_other_users_task_returns_404(self) -> None:
        task_id = self._create("Kisisel gorev")
        client_b, csrf_b = self._register_client(self.app, "buse")
        response = client_b.post(
            f"/tasks/{task_id}/edit",
            data={"title": "Ele gecirildi", "_csrf": csrf_b},
        )
        self.assertEqual(response.status_code, 404)

    def test_tasks_with_due_dates_sort_first(self) -> None:
        no_date = self._create("Tarihsiz gorev")
        later = self._create("Ileri tarihli", due_at="2027-01-01 10:00")
        sooner = self._create("Yakin tarihli", due_at="2026-01-01 10:00")

        text = self.client.get("/").get_data(as_text=True)
        i_sooner = text.index(f'data-id="{sooner}"')
        i_later = text.index(f'data-id="{later}"')
        i_none = text.index(f'data-id="{no_date}"')
        self.assertLess(i_sooner, i_later)       # en yakın tarih önce
        self.assertLess(i_later, i_none)         # tarihli, tarihsizden önce

    # -------------------------------------------------------------- hesaplar
    def test_index_requires_login(self) -> None:
        anonymous = self.app.test_client()
        response = anonymous.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/giris", response.headers["Location"])
        page = anonymous.get("/giris")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Giriş yap", page.get_data(as_text=True))

    def test_register_logs_user_in_and_shows_username(self) -> None:
        client, _ = self._register_client(self.app, "elif")
        text = client.get("/").get_data(as_text=True)
        self.assertIn("elif", text)
        self.assertIn("Çıkış yap", text)

    def test_register_rejects_duplicate_username_case_insensitive(self) -> None:
        client = self.app.test_client()
        csrf = self._extract_csrf(client.get("/kayit"))
        response = client.post(
            "/kayit",
            data={"username": "TESTER", "password": "baska123", "confirm": "baska123",
                  "_csrf": csrf},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("zaten kullanımda", response.get_data(as_text=True))

    def test_register_validates_password_and_username(self) -> None:
        client = self.app.test_client()
        csrf = self._extract_csrf(client.get("/kayit"))
        response = client.post(
            "/kayit",
            data={"username": "ab", "password": "kisa", "confirm": "farkli", "_csrf": csrf},
        )
        text = response.get_data(as_text=True)
        self.assertIn("3-20 karakter", text)      # kullanıcı adı kuralı
        self.assertIn("en az 6 karakter", text)   # kısa şifre
        self.assertIn("eşleşmiyor", text)         # şifre tekrarı

    def test_login_with_wrong_password_shows_single_error(self) -> None:
        client = self.app.test_client()
        csrf = self._extract_csrf(client.get("/giris"))
        response = client.post(
            "/giris",
            data={"username": self.USERNAME, "password": "yanlis", "_csrf": csrf},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Kullanıcı adı veya şifre hatalı.", response.get_data(as_text=True))

    def test_login_remember_makes_session_permanent(self) -> None:
        # "Beni hatırla" işaretlenirse oturum kalıcı olur
        client = self.app.test_client()
        csrf = self._extract_csrf(client.get("/giris"))
        client.post("/giris", data={"username": self.USERNAME, "password": self.PASSWORD,
                                    "remember": "on", "_csrf": csrf},
                    follow_redirects=True)
        with client.session_transaction() as sess:
            self.assertTrue(sess.permanent)

        # İşaretlenmezse oturum yalnız tarayıcıyı kadar sürer
        plain = self.app.test_client()
        csrf = self._extract_csrf(plain.get("/giris"))
        plain.post("/giris", data={"username": self.USERNAME, "password": self.PASSWORD,
                                   "_csrf": csrf}, follow_redirects=True)
        with plain.session_transaction() as sess:
            self.assertFalse(sess.permanent)

    def test_auth_pages_have_theme_toggle_and_meter(self) -> None:
        client = self.app.test_client()
        login_page = client.get("/giris").get_data(as_text=True)
        self.assertIn('id="theme-toggle"', login_page)   # tema düğmesi
        self.assertIn('name="remember"', login_page)     # beni hatırla
        register_page = client.get("/kayit").get_data(as_text=True)
        self.assertIn('id="theme-toggle"', register_page)
        self.assertIn('strength-meter', register_page)   # şifre gücü göstergesi

    def test_tasks_are_isolated_between_users(self) -> None:
        # Kesme işareti HTML'de &#39; olabileceği için düz başlık kullan
        task_id = self._create("Ahmetin gizli görevi")
        client_b, csrf_b = self._register_client(self.app, "buse")

        # B kullanıcısı A'nın görevini listede görmez
        self.assertNotIn("Ahmetin gizli görevi", client_b.get("/").get_data(as_text=True))

        # B, A'nın görevine işlem yapamaz (404)
        response = client_b.post(
            f"/tasks/{task_id}/toggle",
            headers={"X-Requested-With": "XMLHttpRequest", "X-CSRF-Token": csrf_b},
        )
        self.assertEqual(response.status_code, 404)

        # A'nın görevi yerinde durur
        self.assertIn("Ahmetin gizli görevi", self.client.get("/").get_data(as_text=True))

    def test_first_account_inherits_guest_tasks(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        app = self._make_app(tmp.name)
        # Hesap öncesi dönemde eklenmiş (sahipsiz) görevi taklit et
        with app.app_context():
            db = database.get_db()
            db.execute(
                "INSERT INTO tasks (title, created_at, user_id) VALUES (?, ?, NULL)",
                ("Misafir görevi", "2026-01-01 08:00:00"),
            )
            db.commit()

        client, _ = self._register_client(app, "miras")
        self.assertIn("Misafir görevi", client.get("/").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)

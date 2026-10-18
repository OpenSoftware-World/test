"""Çok dilli destek (i18n).

`langs/` altındaki her `.ini` dosyası bir dil paketidir (ör. `tr.ini`, `en.ini`).
Anahtarlar `bölüm.anahtar` biçiminde oluşturulur; yer tutucular `{ad}` şeklindedir.

Seçim sırası: oturumdaki tercih -> Accept-Language başlığı -> varsayılan dil (tr).
Hedef dilde eksik bir anahtar için varsayılan dilin karşılığı kullanılır, o da
yoksa anahtarın kendisi döner (eksik çeviri kolayca tespit edilebilsin diye).

Yeni dil eklemek için yalnızca `langs/<kod>.ini` dosyası oluşturmak yeterlidir.
"""
from __future__ import annotations

import configparser
from pathlib import Path

from flask import current_app, request, session

DEFAULT_LOCALE = "tr"
SESSION_KEY = "locale"
COOKIE_KEY = "locale"  # imzasız düz çerez: anahtar değişse de tercih kaybolmasın


def _read_catalog(path: Path) -> dict[str, str]:
    """Bir .ini dil dosyasını {bölüm.anahtar: metin} sözlüğüne çevirir."""
    parser = configparser.ConfigParser(interpolation=None)  # metinde {tutucu} kullanılıyor
    parser.read(path, encoding="utf-8")
    catalog: dict[str, str] = {}
    for section in parser.sections():
        for key, value in parser.items(section):
            catalog[f"{section}.{key}"] = value.strip()
    return catalog


class I18n:
    """Dil paketlerini tutan ve çeviri üreten nesne (app.extensions['i18n'])."""

    def __init__(self, langs_dir: str | Path, default_locale: str = DEFAULT_LOCALE):
        self.langs_dir = Path(langs_dir)
        self.default_locale = default_locale
        self._catalogs = {
            path.stem: _read_catalog(path)
            for path in sorted(self.langs_dir.glob("*.ini"))
        }
        if not self._catalogs:
            raise RuntimeError(f"Hiç dil dosyası bulunamadı: {self.langs_dir}")
        if self.default_locale not in self._catalogs:
            raise RuntimeError(
                f"Varsayılan dil dosyası eksik: "
                f"{self.langs_dir / (self.default_locale + '.ini')}"
            )

    @property
    def locales(self) -> list[str]:
        """Desteklenen dil kodları (dosya adları)."""
        return list(self._catalogs)

    def label(self, locale: str, code: str) -> str:
        """Dil menüsünde görünecek dil adı (ör. 'Deutsch')."""
        return self.gettext(locale, f"lang.{code}") or code

    def labels(self, locale: str) -> dict[str, str]:
        """Menü için {dil_kodu: dil_adı} sözlüğü."""
        return {code: self.label(locale, code) for code in self.locales}

    def months(self, locale: str) -> list[str]:
        """1-12 arası ay kısaltmaları (tarih biçimi için)."""
        return self._month_names(locale, "months")

    def months_full(self, locale: str) -> list[str]:
        """1-12 arası tam ay adları (takvim başlıkları için).

        `months_full` bölümü yoksa ay kısaltmalarına düşer.
        """
        names = self._month_names(locale, "months_full")
        return names if any(names) else self.months(locale)

    def _month_names(self, locale: str, section: str) -> list[str]:
        """Verilen bölümdeki 1-12 ay adlarını dil tercihiyle döndürür."""
        catalog = self._catalogs.get(locale) or self._catalogs[self.default_locale]
        fallback = self._catalogs[self.default_locale]
        return [
            catalog.get(f"{section}.{i}") or fallback.get(f"{section}.{i}", "")
            for i in range(1, 13)
        ]

    def gettext(self, locale: str, key: str, **kwargs) -> str:
        """Anahtarın çevirisini döndürür; yer tutucular doldurulur."""
        catalog = self._catalogs.get(locale) or self._catalogs[self.default_locale]
        template = catalog.get(key) or self._catalogs[self.default_locale].get(key)
        if template is None:
            return key
        if not kwargs:
            return template
        try:
            return template.format(**kwargs)
        except (KeyError, IndexError, ValueError):  # bozuk yer tutucuya karşı dayanıklılık
            return template


def init_app(app) -> None:
    """Dil paketlerini yükler ve uygulamanın uzantı olarak saklar."""
    app.extensions["i18n"] = I18n(app.config["LANGS_DIR"], DEFAULT_LOCALE)


def _negotiate(supported: list[str]) -> str | None:
    """Accept-Language başlığındaki en yüksek öncelikli desteklenen dile göre seçim."""
    header = request.headers.get("Accept-Language", "")
    candidates: list[tuple[str, float]] = []
    for part in header.split(","):
        tag, _, params = part.strip().partition(";")
        tag = tag.strip().lower()
        if not tag:
            continue
        quality = 1.0
        for param in params.split(";"):
            param = param.strip()
            if param.startswith("q="):
                try:
                    quality = float(param[2:])
                except ValueError:
                    quality = 0.0
        candidates.append((tag, quality))
    for tag, quality in sorted(candidates, key=lambda item: -item[1]):
        if quality <= 0:
            continue
        base = tag.split("-")[0]
        if base in supported:
            return base
    return None


def get_locale() -> str:
    """Kullanılacak dili belirler: oturum -> düz çerez -> tarayıcı tercihi -> varsayılan."""
    engine: I18n = current_app.extensions["i18n"]
    locale = session.get(SESSION_KEY)
    if locale in engine.locales:
        return locale
    # Kullanıcı daha önce seçim yaptıysa (düz çerez) ona göre açılır
    locale = request.cookies.get(COOKIE_KEY)
    if locale in engine.locales:
        session[SESSION_KEY] = locale
        return locale
    locale = _negotiate(engine.locales) or engine.default_locale
    session[SESSION_KEY] = locale
    return locale


def set_locale(code: str) -> bool:
    """Dil tercihini oturuma yazar; dil destekleniyorsa True döner."""
    engine: I18n = current_app.extensions["i18n"]
    if code not in engine.locales:
        return False
    session[SESSION_KEY] = code
    return True

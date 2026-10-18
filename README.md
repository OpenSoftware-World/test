# Görev Merkezi

Flask + Bootstrap 5 ile geliştirilmiş, modern ve profesyonel görünümlü görev
yönetim uygulaması.

## Özellikler

- **Görev ekleme** — başlık (zorunlu) ve açıklama (isteğe bağlı) ile
- **Görev silme → çöp kutusu** — silme penceresinde varsayılan işlem görevi
  çöp kutusuna taşımaktır (geri alınabilir). Kalıcı silme için onay kutusu
  işaretlenir, ekrandaki uyarı "geri alınamaz" der ve **görevin tam adının
  yazılması** istenir; eşleşmeyince buton açılmaz
- **Çöp kutusu** — liste başlığındaki bağlantıdan erişilir; silinen görevler
  listelenir, tek tıkla **geri alınabilir** veya adı yazılarak kalıcı silinir
- **Tamamlama** — tıklama ile işaretleme/geri alma, ilerleme çubuğu ve canlı sayaçlar
- **Tarih/saat** — görev için planlanan tarih-saat seçilir (isteğe bağlı);
  tarihi olan görevler en yakın tarihe göre sıralanır, süresi geçen görev
  kırmızı "Gecikti" etiketiyle işaretlenir
- **Önem derecesi** — Düşük (yeşilimsi) · Normal (koyu yeşil) · Yüksek (sarı) ·
  Acil (kırmızı); kartın sol şeridi ve rozet renkle gösterilir
- **Düzenleme** — karttaki kalem düğmesiyle açılan pencerede başlık, açıklama,
  tarih/saat ve önem derecesi değiştirilir
- **Takvim** (`/takvim`) — liste başlığındaki Takvim bağlantısıyla açılır; aylık
  ızgara ve haftalık şerit arasında geçiş yapılır. Tarihli görevler önem rengine
  göre çip olarak görünür, çipe tıklayınca aynı düzenleme penceresi açılır.
  Güne tıklayınca altındaki **gün paneli** açılır: o günün görevleri ve
  tarih-saati hazır gelen hızlı ekleme formu (gelecek haftalar planlamak için)
- **Arama** — yazarken (260 ms gecikmeli) başlık ve açıklamada arama; Türkçe
  karakterlerde `Şirket` / `sirket` / `SIRKET` eşleşir (`/` kısayolu ile aramaya odaklan)
- **Kullanıcı hesapları** — açık kayıt; herkes kendi hesabını açar, görevler
  yalnızca hesap sahibine görünür. Şifreler `scrypt` özetiyle saklanır (düz metin
  yok). İlk açılan hesap, hesapsız dönemde üretilmiş görevleri devralır.
- **Çok dilli arayüz** — Türkçe / English / Deutsch; tarayıcı diline göre otomatik
  açılır, paneldeki menüden değiştirilir, seçim çerezle kalıcıdır (giriş ve kayıt
  sayfalarında da dil seçilebilir)
- **Karanlık tema** — marka yanındaki ay düğmesiyle tek tık; sistem tercihi
  (`prefers-color-scheme`) algılanır, seçim tarayıcıda saklanır. Açık morumsu
  tema varsayılan olarak aynen korunur.
- Kalıcı veri saklama (SQLite, `instance/tasks.db`), ek bağımlılık yok
- CSRF korumalı formlar, sunucu tarafı girdi doğrulaması (başlık ≤ 200, açıklama ≤ 2000)
- **Kaba kuvvet koruması** — aynı IP+kullanıcı için 5 başarısız girişden sonra
  15 dakika kilit (yanıt `Retry-After` başlığı taşır); başarılı giriş sayacı sıfırlar
- **Güvenlik başlıkları** — nonce'lu `Content-Security-Policy` (kaynaklar 'self',
  satır içi betikler nonce'lu, `frame-ancestors 'none'`), `X-Content-Type-Options`,
  `X-Frame-Options`, `Referrer-Policy`; oturum çerezi HttpOnly + SameSite=Lax
  (HTTPS yayınında `COOKIE_SECURE=1` ile Secure açılır)
- **Zamanlama eşitlemeli giriş** — var olmayan kullanıcı adı, yanıt süresiyle
  ayırt edilemez (sabit sahte özetle tek scrypt doğrulaması)
- **SQLite iyileştirmeleri** — WAL günlüğü, kullanıcı+çöp+tarih indeksi,
  yabancı anahtar denetimi ve `busy_timeout` (eşzamanlı erişimde "locked" yok)
- JS kapalıyken de çalışan yönlendirme tabanlı yedek akış
- Duyarlı (responsive) tasarım, özgün koyu/açık panel teması, animasyonlu geri bildirimler

## Kurulum ve çalıştırma

```bash
pip install -r requirements.txt   # yalnızca Flask gerekir
python3 app.py                    # http://127.0.0.1:7000
```

`PORT` değişkeni ile port, `SECRET_KEY` değişkeni ile oturum anahtarı
değiştirilebilir (üretilmezse `instance/secret_key` dosyasında saklanır; böylece
yeniden başlatmada oturumlar korunur):

```bash
PORT=8080 SECRET_KEY="gizli-anahtar" python3 app.py
```

## Test

```bash
python3 -m unittest discover -s tests -t . -v
```

44 test; ekleme, çöp kutusuna taşıma, geri alma, ad onaylı kalıcı silme,
tarih/saat, önem derecesi, sıralama, düzenleme, arama, takvim (ay/hafta, gün
paneli, takvimden ekleme, uç tarihler), doğrulama, CSRF, kilitlenme (brute-force),
güvenlik başlıkları + CSP nonce, 404, kayıt/giriş ve hesaplar arası izolasyon
senaryolarını kapsar.

## Proje yapısı

```
app.py               # Flask uygulama fabrikası, rotalar, doğrulama, CSRF
auth.py              # Kullanıcı hesapları (kayıt, giriş, oturum, şifre özeti)
database.py          # SQLite veri katmanı (CRUD + arama normalizasyonu)
i18n.py              # Çok dilli destek (dil paketi yükleyici + seçim)
langs/               # tr.ini, en.ini, de.ini - bir dosya = bir dil
templates/           # base, index, task_list (fragment), calendar, trash,
                     # login, register, 404 + ortak parça/modal dosyaları
static/css/style.css # özgün tema, animasyonlar, karanlık tema, dil menüsü
static/js/app.js     # ekleme/silme/tamamlama/arama etkileşimleri
static/vendor/       # yerel Bootstrap 5.3.3 + Bootstrap Icons (çevrimdışı çalışır)
tests/test_app.py    # unittest ile uçtan uca testler
instance/tasks.db    # veritabanı (ilk çalıştırmada otomatik oluşur)
```

## Çok dilli destek

- Her dil `langs/<kod>.ini` dosyasıdır; yeni dil eklemek için dosya oluşturmak
  yeterlidir (`langs/fr.ini` → uygulama onu menüde otomatik görür).
- Seçim sırası: oturumdaki tercih → `locale` çerezi (1 yıl) → `Accept-Language`
  başlığı → varsayılan (`tr`). Dil menüsü sol paneldeki `TR/EN/DE` düğmesindedir;
  giriş/kayıt sayfalarında ise kartın altında küçük dil satırı vardır.
- Değiştirilebilir metinlerin tamamı (arayüz, doğrulama/hata mesajları, toast
  bildirimleri, ay kısaltmaları) dil dosyalarındadır.
- Anahtarlar `bölüm.anahtar` biçiminde, yer tutucular `{ad}` şeklindedir.
  Hedef dilde eksik anahtar için Türkçe karşılık, o da yoksa anahtarın kendisi
  döner (eksik çeviri kolay fark edilir).

## HTTP uç noktaları

| Metot    | Yol                   | Açıklama                                          |                 |
| -------- | --------------------- | ------------------------------------------------- | --------------- |
| GET/POST | `/giris`              | Giriş sayfası                                     |                 |
| GET/POST | `/kayit`              | Kayıt sayfası (ilk hesap görevleri devralır)      |                 |
| POST     | `/cikis`              | Oturumu kapat                                     |                 |
| GET      | `/`                   | Liste (`?q=` arama, `fragment=1`) — giriş zorunlu |                 |
| GET      | `/cop`                | Çöp kutusu (silinen görevler)                     |                 |
| GET      | `/takvim`             | Takvim (`view=month                               | week`, `d=gün`) |
| POST     | `/tasks`              | Görev ekle (tarih/saat + önem dahil)              |                 |
| POST     | `/tasks/<id>/edit`    | Görevi düzenle                                    |                 |
| POST     | `/tasks/<id>/toggle`  | Tamamlama durumunu değiştir                       |                 |
| POST     | `/tasks/<id>/delete`  | Görevi çöp kutusuna taşı                          |                 |
| POST     | `/tasks/<id>/restore` | Çöp kutusundan geri al                            |                 |
| POST     | `/tasks/<id>/purge`   | Kalıcı sil (`confirm_title` adı birebir istemeli) |                 |
| POST     | `/lang/<code>`        | Dil tercihini değiştir (`tr`/`en`/`de`)           |                 |

JSON yanıt isteklerde `X-Requested-With: XMLHttpRequest` başlığı gönderilir.
Giriş gerektiren sayfalara girişsiz istekler `/giris`'e yönlendirilir (XHR ise
`401` JSON döner). Görev kayıtları kullanıcıya bağlıdır; başka bir hesabın
görevine işlem yapılamaz (`404`).

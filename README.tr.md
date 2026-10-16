<div align="center">

<img src="docs/assets/logo.png" width="112" alt="CIRANA logosu">

# CIRANA

### Her pencere, yerinde.

CIRANA, uygulamalarınızı başlatır ve gerçek pencerelerini Windows'ta çalışma alanlarına ve döşemeli (tiled) düzenlere yerleştirir.<br>
Bir şey açın, yerine otomatik oturur. Gömme yok, görünüm değişikliği yok, hiçbir veri bilgisayarınızdan çıkmaz.

[Başlarken](#başlarken) · [Çalışırken görün](#çalışırken-görün) · [Kısayollar](docs/shortcuts.md) · [Mimari](docs/architecture.md)

[English](README.md) · **Türkçe**

</div>

<!--
  SLOT hero  ->  docs/assets/hero.png  (1600 px genişlik; bkz. docs/assets/README.md)
  Göstermek için yorum işaretlerini silin:

  <p align="center"><img src="docs/assets/hero.png" alt="CIRANA ana ekranı ve döşenmiş uygulama pencereleri" width="960"></p>
-->

<br>

## Neden CIRANA?

**Tek tıkla çalışır bir düzen.** Bir uygulama grubunu çalışma alanı olarak kaydedin; örneğin bir editör, bir tarayıcı ve
bir terminalden oluşan *Development*. Çalışma alanını etkinleştirince açık pencereler yeniden kullanılır, eksik olanlar
başlatılır, hepsi yan yana ya da ızgara düzeninde yerleşir ve öne getirilir.

**Pencereler kendini düzenler.** Dinamik döşemede yeni pencere kendi karesini alır, diğerleri ona yer açar. Kapanan pencere
alanını geri verir. Düzen motoru, pencere sayısına, monitörün şekline ve uygulamaların ısrar ettiği en küçük boyutlara
uyan yerleşimi seçer.

**Uygulamalarınız size ait kalır.** CIRANA başka bir uygulamanın penceresini asla gömmez, başka bir pencereye bağlamaz,
görünümünü değiştirmez. Her uygulama kendi başlık çubuğunu, görev çubuğu düğmesini ve Alt+Tab girişini korur; CIRANA
kapansa bile çalışmaya devam eder. Tamamen çevrimdışıdır: hesap, sunucu ya da telemetri yoktur. Veriler
`%LOCALAPPDATA%\CIRANA\` altında durur.

<br>

## Özellikler

### Her şeyi bulun ve başlatın

Ana sayfa, Uygulamalar ve Favoriler; Başlat menüsünde, paketlenmiş uygulamalarda ve App Paths kayıtlarında bulunan her şeyi,
yeniden adlandırabileceğiniz kategorilere ayrılmış olarak gösterir. Pencerenin içinde her yerden `Ctrl+K` ile uygulama,
kategori ve çalışma alanı arayabilirsiniz.

<!--
  SLOT dashboard  ->  docs/assets/dashboard.png
  <p align="center"><img src="docs/assets/dashboard.png" alt="Çalışma alanı kartları ve uygulama kategorileriyle CIRANA ana sayfası" width="900"></p>
-->

### Çalışma alanları

Çalışma alanı; kendi düzeni, monitörü, boşluğu ve ana panel genişliği olan, adlandırılmış bir uygulama veya açık pencere
grubudur. Girdileri sürükleyip bırakarak sıralayın, aynı programın birden çok penceresi arasından seçim yapın, uygulamadan
önce düzenin canlı önizlemesini görün. Çalışma alanını kapatmak yalnızca pencerelerini yönetmeyi bırakır; uygulamalar
çalışmaya devam eder.

<!--
  SLOT workspaces  ->  docs/assets/workspaces.png
  <p align="center"><img src="docs/assets/workspaces.png" alt="Düzen önizlemesiyle Çalışma alanları sayfası" width="900"></p>
-->

### Uyarlanabilir döşeme

*Automatic (adaptive)*; BSP, ana + yan panel, ızgara, tek sütun ve yan yana düzenlerini dener, her birini puanlar ve en
iyisini tutar. Monitörün şeklini hesaba katar (üç pencere ultrawide'da yan yana, dikey monitörde tek sütun olur), ince
şeritlerden ve minik karelerden kaçınır, düzeninizi bir yerleşimden diğerine sıçratmadan sabit tutar. En küçük boyuttan
daha fazla küçülmeyi reddeden uygulamalara yer açılır ya da kesilmek yerine yüzer hale getirilir.

<!--
  SLOT adaptive-layout  ->  docs/assets/adaptive-layout.png
  <p align="center"><img src="docs/assets/adaptive-layout.png" alt="Birden çok döşenmiş pencereyle uyarlanabilir düzen" width="900"></p>
-->

### Uygulamalar yan yana

Yönetilen her pencere ince bir kenarlık alır: odaktaki yeşil, diğerleri gri. Bir pencereyi başka birinin üstüne sürükleyip
yer değiştirin; yarı saydam önizleme nereye oturacağını gösterir. Bir kenarı sürükleyerek yeniden boyutlandırın, komşular
uyum sağlar.

<!--
  SLOT side-by-side  ->  docs/assets/side-by-side.png
  <p align="center"><img src="docs/assets/side-by-side.png" alt="Odak kenarlığıyla yan yana döşenmiş birkaç uygulama" width="900"></p>
-->

### BSP, yığınlar ve monocle

BSP düzeninde yeni pencere, odaktaki kareyi böler. Birkaç pencereyi tek bir kareye yığıp aralarında gezinebilir ya da
**monocle** ile odaktaki pencereye tüm ekranı verip kapattığınızda her şeyi tam eski yerine koyabilirsiniz.

<!--
  SLOT monocle  ->  docs/assets/monocle.png
  <p align="center"><img src="docs/assets/monocle.png" alt="Çalışma alanını dolduran monocle modunda bir pencere" width="900"></p>
-->

### Klavye kontrolü

Odağı taşıyın, kareleri değiştirin, boyutlandırın, yüzdürün, yığın yapın ve pencereleri başka bir çalışma alanına gönderin.
Küresel kısayollar fare gerektirmez ve her uygulamada çalışır. Bkz. [Klavye kısayolları](#klavye-kısayolları).

### Akıcı hareket

Pencereler yeni yerlerine süzülür, odak kenarlıkları solar, bırakma önizlemesi hedefler arasında kayar. Bu tamamen
görseldir: son konumlar animasyon olsa da olmasa da birebir aynıdır ve isterseniz kapatabilirsiniz.

<br>

## Çalışırken görün

Kısa bir demo planlanıyor. Şunları gösterecek:

- bir pencerenin açılıp düzene oturması
- iki pencerenin yer değiştirmesi
- bir pencere kapanınca animasyonlu yeniden döşeme
- bir pencereyi monocle'a alıp geri döndürme

<!--
  SLOT demo  ->  docs/assets/demo.gif  (1280 px genişlik, 5 MB altı)
  <p align="center"><img src="docs/assets/demo.gif" alt="CIRANA'nın döşeme, yer değiştirme ve monocle işlemleri" width="900"></p>
-->

<br>

## Başlarken

CIRANA şimdilik kaynak koddan çalıştırılır. Henüz yayınlanmış bir yükleyici veya sürüm yoktur.

**Ön koşullar:** Windows 11 ve Python 3.11 veya üzeri (`py -0p` kurulu sürümleri listeler).

```powershell
git clone https://github.com/cirana/Cirana-Playground.git
cd Cirana-Playground
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m cirana
```

**İlk çalıştırma**

1. CIRANA uygulamalarınızı bir kez tarar ve listeyi önbelleğe alır. Yenilemek için Ayarlar'daki *Scan now* düğmesini kullanın.
2. **Yeni bir kurulumda tüm pencerelerin döşenmesi açıktır** ve düzen *Automatic (adaptive)*'dir. Açık uygulama
   pencereleriniz hemen düzenlenir. Kapatmak için Windows sayfasında *Tile all windows* işaretini kaldırın.
3. **Workspaces** sayfasını açın, bir çalışma alanı oluşturun, uygulama ekleyin (*Add applications…*) veya zaten açık
   pencereleri ekleyin (*Add open windows…*), bir düzen seçin ve *Activate*'e basın.
4. Aramak ve başlatmak için `Ctrl+K`'ye basın, işleri taşımak için aşağıdaki kısayolları kullanın.

Önceki bir sürümü kullandıysanız ayarlarınıza dokunulmaz. Seçtiğiniz düzenler seçili kalır; *Automatic (adaptive)*
düzen listesine eklenen bir seçenektir.

**Çalıştırılabilir dosya oluşturma.** `pip install pyinstaller` ve `pyinstaller cirana.spec --noconfirm`,
`dist\CIRANA\` altında tek klasörlü bir derleme üretir.

<br>

## Klavye kısayolları

Varsayılanlar odaktaki pencere üzerinde çalışır.

| Kısayol | İşlem |
|---|---|
| `Ctrl+Alt+` ok tuşları | Komşu pencereye odaklan |
| `Ctrl+Alt+Shift+` ok tuşları | Komşu kareyle yer değiştir; kenarda sonraki monitöre taşı |
| `Ctrl+Alt+Win+` ok tuşları | Kareyi genişlet, daralt, uzat veya kısalt |
| `Ctrl+Alt+Enter` | Monocle aç / kapat |
| `Ctrl+Alt+Space` | Pencereyi yüzdür / yeniden döşe |
| `Ctrl+Alt+PageDown` / `PageUp` | Yığındaki sonraki / önceki pencere |
| `Ctrl+Alt+Backspace` | Pencereyi yığından çıkar |
| `Ctrl+Alt+Shift+PageDown` / `PageUp` | Pencereyi sonraki / önceki çalışma alanına taşı |
| `Ctrl+K` | Ara ve başlat (CIRANA içinde) |

Her kısayol **Settings → Keyboard and focus → Edit shortcuts…** bölümünden değiştirilebilir. Varsayılan kısayolu olmayan
işlemlerle birlikte tam liste [docs/shortcuts.md](docs/shortcuts.md) dosyasındadır (İngilizce).

<br>

## Özelleştirme

**Düzen.** Windows sayfasında tüm pencereleri döşeme düzenini seçin: *Automatic (adaptive)* (yeni kurulumlarda
varsayılan), *By window count*, *Side by side*, *Main + side panel*, *Grid 2×2*, *Dynamic grid* veya *BSP*; ayrıca boşluk ve
ana panel genişliği. Düzeni elle seçmek, uyarlanabilir motoru o kapsam için kapatır. *Automatic (adaptive)*'i yeniden
seçmek açar.

**Yüzen pencereler.** Odaktaki pencereyi yüzdürmek için `Ctrl+Alt+Space`'e basın ya da Windows sayfasında satırındaki ☰
menüsünü kullanın. *Settings → Window rules* altında bir programı her zaman yüzen, her zaman yok sayılan ya da bir
çalışma alanına ait yapabilirsiniz. Boyutlandırma kenarlığı olmayan pencereler (sabit diyaloglar, tam ekran oyunlar) hiç
döşenmez.

**Çalışma alanları.** Her çalışma alanının kendi düzeni, monitörü, boşluğu ve ana panel genişliği vardır; ayrıca
pencereleri açılıp kapandıkça düzeni takip ettiren bir *Dynamic tiling* anahtarı bulunur.

**Animasyon.** *Settings → Animations* pencere hareketini (varsayılan olarak açık, 180 ms, en çok 350 ms; 0 animasyonsuz),
odak kenarlığının solmasını ve bırakma önizlemesinin kaymasını denetler. Windows sayfasındaki *Borders* anahtarı odak
kenarlıklarını gizler.

**Fareyi izleyen odak.** Varsayılan olarak kapalıdır. Açıkken Windows'un kendi ayarını tüm pencereler için kullanır;
CIRANA kapanınca sizin değeriniz geri gelir.

<br>

## Gereksinimler

- **Windows 11.** CIRANA Windows 11'de geliştirildi ve test edildi. Kodda sürüm denetimi yok, ancak Windows 10
  doğrulanmadı.
- Kaynaktan çalıştırmak için **Python 3.11+**; PySide6 6.6+, pywin32 ve psutil (hepsi `requirements.txt` içinde).
- Birden çok monitör ve monitör başına DPI desteklenir.
- Windows, normal yetkili bir programın yükseltilmiş (yönetici) uygulamanın penceresini taşımasına izin vermez. CIRANA
  bunu bildirir ve o pencereye dokunmaz. Diğer sanal masaüstlerindeki pencereler yönetilmez. Ayrıntılar
  [docs/limitations.md](docs/limitations.md) dosyasında (İngilizce).

<br>

## Geliştirme

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
```

Normal test paketi pencere yöneticisini simüle edilmiş bir masaüstüne karşı çalıştırır ve gerçek bir pencereye asla
dokunmaz. İsteğe bağlı canlı testler (`CIRANA_LIVE=1`) gerçek Win32 pencerelerini dener. Ayrı bir veri klasörü için
`CIRANA_HOME` ayarlayın.

| Doküman (İngilizce) | İçerik |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Modüller, pencere izleme, düzenler, döşeme, kısayollar, veri |
| [docs/testing.md](docs/testing.md) | Test stratejisi, canlı testler, kapsam dışı olanlar |
| [docs/limitations.md](docs/limitations.md) | Bilinen sınırlamalar |
| [docs/shortcuts.md](docs/shortcuts.md) | Tüm klavye kısayolları |
| [docs/assets/README.md](docs/assets/README.md) | Ekran görüntüleri ve demo nasıl çekilir |

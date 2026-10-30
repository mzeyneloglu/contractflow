# Kurulum rehberi

Bu rehber ContractFlow'u Windows, macOS veya Linux üzerinde, Docker kullanmadan yerel olarak kurar. Uygulamanın test edildiği ortam: Windows 11 x64, Python 3.14.6, Ollama 0.40.11, yalnızca CPU (Intel i7-8565U, 32 GB RAM).

## 1. Önkoşullar

| Bileşen | Sürüm | Not |
|---|---|---|
| Python | 3.12+ (3.14.6 ile test edildi) | `python --version` |
| Ollama | 0.40+ | https://ollama.com/download |
| Oracle | 12.1+ | python-oracledb Thin mode; Oracle Client kurulumu gerekmez |
| Hedef servis | Çalışan bir HTTP API ve OpenAPI/Swagger JSON/YAML dosyası | Spring Boot için `/v3/api-docs` çıktısı yeterlidir |

İnternet yalnızca ilk kurulumda (pip paketleri ve Ollama modelleri) gerekir. Kurulumdan sonra doğrulama akışı tamamen yerel çalışır; bulut LLM, bulut vektör veritabanı veya CDN kullanılmaz.

## 2. Bağımlılıklar

```bash
cd contractflow
python -m pip install -r requirements.lock
```

`requirements.lock` test edilen sürümleri sabitler. İzole bir ortam tercih ederseniz önce `python -m venv .venv` ile sanal ortam oluşturup etkinleştirebilirsiniz.

## 3. Ollama modelleri

```bash
ollama pull gemma3:4b
ollama pull qwen3-embedding:0.6b
```

## 4. Ortam kontrolü

```bash
python scripts/doctor.py
```

Tüm satırlar `[OK]` olmalıdır.

## 5. Yapılandırma (opsiyonel)

`.env.example` dosyasını `.env` olarak kopyalayıp port, Ollama adresi veya sorgu limitlerini değiştirebilirsiniz. **Parola ve token'lar `.env`'e yazılmaz**; bunlar arayüzden girilir ve işletim sisteminin gizli bilgi deposunda (Windows Credential Manager / macOS Keychain / Secret Service) saklanır. Keyring kullanılamayan ortamlarda `data/secrets/` altında Fernet ile şifrelenmiş dosyalar kullanılır; anahtar dosyası (`master.key`) aynı makinede durduğu için bu yedek yöntem yalnızca diske düz metin yazılmasını engeller, makineye erişimi olan birine karşı koruma sağlamaz.

## 6. Başlatma

```bash
python -m app.main
```

Uygulama yalnızca `http://127.0.0.1:8000` adresine bind olur.

## 7. Proje kurulumu (arayüz)

1. **Projeler** ekranında proje adı ve ortamla taslak proje oluşturun.
2. **Kurulum → 1 · Proje ve Oracle:** host, port, service name, kullanıcı, parola ve izinli şemaları girin. **Bağlantıyı Test Et** gerçek `oracle_mcp` sürecini başlatır, Oracle sürümünü ve izinli tablo sayısını gösterir.
3. **2 · API ve OpenAPI:** base URL, kimlik doğrulama tipi ve bilgisini girin; **Servisi Kontrol Et**. OpenAPI/Swagger dosyasını yükleyin; endpoint önizlemesi ve algılanan sayfalama profili görünür.
4. **3 · Belgeler ve model:** PDF/Markdown iş kuralı belgelerini toplu yükleyin; durumlar kuyrukta → ayrıştırılıyor → embed ediliyor → hazır şeklinde ilerler. **Yetenek Testi** gerçek modelle yapılandırılmış aksiyon ve embedding boyutunu doğrular.

### Oracle hesabı hakkında güvenlik notu

İdeal olan, yalnızca izinli nesnelerde SELECT yetkisi olan ayrı bir okuma hesabıdır. Ayrı hesap açılamıyorsa mevcut hesapla çalışılabilir; bu durumda bile `oracle_mcp` her sorguyu sunucu içinde yeniden doğrular, yalnızca SELECT'e izin verir ve her yürütmeyi `SET TRANSACTION READ ONLY` ile başlatır. SYS/SYSTEM gibi yönetici hesaplarını kullanmayın.

## 8. Servisin iş mantığına erişim (önerilir)

ContractFlow'un servisin gerçekte hangi SQL'i çalıştırdığını görebilmesi için aşağıdakilerden en az birini yapılandırın (Kurulum → *Kod ve SQL izi* ve *Oracle* adımları):

| Yol | Gerekenler |
|---|---|
| Kaynak kod | Servisin repo klasör(ler)i. Derleme gerekmez. |
| V$SQL yakalama | Bağlantı kullanıcısına `V$SQL`, `V$SESSION` (ve isteğe bağlı `V$SQL_BIND_CAPTURE`) okuma yetkisi: `GRANT SELECT ON V_$SQL TO <kullanıcı>;` vb. Kurulumda "Çalışma anı SQL yakalama" işaretlenir; servis farklı bir DB kullanıcısıyla bağlanıyorsa o kullanıcı da yazılır. Yetkiyi DBeaver'da `SELECT COUNT(*) FROM V$SESSION WHERE ROWNUM = 1` ile kontrol edebilirsiniz. |
| SQL logu | Servis tarafında datasource-proxy veya p6spy ile SQL loglama ve `X-ContractFlow-Trace` başlığının MDC'ye konması; log dosyası yolu kurulumda girilir. |
| OpenTelemetry | Servisi `-javaagent:opentelemetry-javaagent.jar` ile, `OTEL_EXPORTER_OTLP_PROTOCOL=http/json` ve `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://127.0.0.1:8000/v1/traces` ortam değişkenleriyle başlatın. |

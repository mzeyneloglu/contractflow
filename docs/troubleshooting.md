# Sorun giderme

| Belirti | Durum / kod | Neden ve çözüm |
|---|---|---|
| "Servise bağlanılamadı. Backend projesini başlatıp tekrar kontrol edin" | `WAITING_FOR_SERVICE` / `CONNECTION_REFUSED` | Hedef servis kapalı veya port yanlış. Servisi başlatın, doğrulama ekranında **Tekrar Kontrol Et**'e basın; çalışma checkpoint'ten devam eder, Oracle sonucu yeniden çalıştırılmaz. |
| Yetkilendirme hatası (HTTP 401/403) | `AUTH_REQUIRED` | Token süresi dolmuş veya yanlış. Kurulum → API'de kimlik bilgisini güncelleyip **Tekrar dene**. Bu durum "servis kapalı" olarak raporlanmaz. |
| Endpoint bulunamadı (HTTP 404) | `ERROR` / `NOT_FOUND` | Base URL'de context path eksik olabilir (ör. `/api`). Servis kapalı sayılmaz. |
| Zaman aşımı, DNS, TLS | `WAITING_FOR_SERVICE` | Ayrı hata sınıflarıdır; doğrulama sonuçlandırılmaz. Kendi imzalı sertifikada "TLS sertifikasını doğrula" seçeneğini yalnızca güvendiğiniz yerel ortamda kapatın. |
| Ollama'ya ulaşılamadı / model kurulu değil | `WAITING_FOR_OLLAMA` | `ollama serve` ve `ollama pull gemma3:4b`, `ollama pull qwen3-embedding:0.6b`. Sonra **Tekrar Kontrol Et**. |
| Hazır belge yok | `WAITING_FOR_DOCUMENTS` | Belge yükleyin veya **Belgesiz devam et** (rapor iş kuralı kapsamının eksik olduğunu belirtir). |
| PDF `OCR gerekli` | `needs_ocr` | Taranmış PDF'te metin katmanı yok. Metin içeren PDF yükleyin veya harici OCR uygulayın; ilk sürümde yerel OCR yoktur. |
| `MODEL_OUTPUT_INVALID` | `ERROR` | Model iki denemede şemaya uygun çıktı üretemedi. Temperature'ı düşürün veya talebi sadeleştirin. Sistem serbest metni regex ile çalıştırmaz. |
| `STEP_LIMIT` | `ERROR` | Ajan adım veya çalışma başına model çağrısı limitine ulaştı. Kurulum → model ayarlarından limitleri artırabilirsiniz. |
| `ORA-01017` | Oracle `AUTH_REQUIRED` | Kullanıcı adı/parola hatalı. |
| `DPY-3010` / `DPY-3015` | `CONFIGURATION_ERROR` | Oracle sürümü Thin mode tarafından desteklenmiyor (12.1 öncesi) veya parola doğrulayıcı tipi eski. Thick mode ilk sürümde desteklenmez. |
| `POLICY_VIOLATION` | Oracle aracı | Sorgu SELECT-only politikasını ihlal etti (DML, DB link, izin dışı şema/fonksiyon). Bu bir güvenlik özelliğidir. |
| `UNKNOWN_OBJECT` | Oracle aracı | Tablo/kolon izinli şemalarda bulunamadı. Şema allowlist'ini kontrol edin. |
| Çalışma çok yavaş | — | `gemma3:4b` yalnızca CPU'da çalışıyorsa ajan başına 30 saniye–3 dakika sürebilir. GPU'lu makinede belirgin şekilde hızlanır. `ollama ps` ile işlemciyi görebilirsiniz. |
| Yanlış tablo/kolon eşleştirmesi | `NEEDS_CLARIFICATION` | İlk çalışmada eşleştirme ve SQL onayınıza sunulur. **Reddet** ve not yazın; ajan geri bildirimle yeniden önerir. Kayıtlı eşleştirmeler Kurulum → Gelişmiş'ten silinebilir. |

Günlükler: uygulama konsolu ve `data/oracle_mcp.log` (MCP sunucusunun stderr'i; parola içermez).

# Değerlendirme ve test sonuçları

Bu belge hangi kontrollerin **gerçek** bağımlılıklarla, hangilerinin **simülasyon** veya birim testiyle çalıştırıldığını ayrı ayrı gösterir. Mock veya simülasyon sonuçları gerçek entegrasyon olarak sunulmaz.

Ortam: Windows 11 x64, Python 3.14.6, Ollama 0.40.11, `gemma3:4b` ve `qwen3-embedding:0.6b`, yalnızca CPU (Intel i7-8565U, 32 GB RAM).

## Özet

| Alan | Tür | Sonuç |
|---|---|---|
| Karşılaştırma motoru | Birim | 13/13 — 4 temel adet kombinasyonu, eksik sayfa, kesilmiş DB, bağlantı hatası, beklentisiz, tekrar eden ID, ID'siz kayıt, büyük Decimal, NULL, çok satır |
| SELECT-only politika | Birim | 20 olumlu yapı (INNER/LEFT/RIGHT/FULL/CROSS JOIN, çoklu JOIN, AND/OR/parantez, NULL, LIKE, IN, EXISTS, alt sorgu, CTE, DISTINCT, COUNT/SUM/AVG/MIN/MAX, GROUP BY/HAVING, ORDER BY, UNION/UNION ALL, FETCH FIRST) ve 23 olumsuz örnek (DML, DDL, MERGE, TRUNCATE, GRANT, çoklu ifade, FOR UPDATE, SELECT INTO, DB link, WITH FUNCTION, PL/SQL, `DBMS_*` paket çağrısı, şema fonksiyonu, UDF, izin dışı şema, şemasız tablo, bilinmeyen tablo/kolon, isimsiz bind) + CTE gölgeleme, UNION dalı, CTE içinde FOR UPDATE, bind eşleşmesi |
| SQL derleyici | Birim | Bind zorunluluğu, injection denemeleri, OR grupları, kolonsuz agregasyon |
| oracle_mcp stdio | Entegrasyon (gerçek süreç) | Araç keşfi, sunucu tarafı politika (DB'ye bağlanmadan DELETE reddi), eksik secret → `CONFIGURATION_ERROR`, erişilemeyen DB → `CONNECTION_REFUSED`, parola argüman/ortam/mesajda yok |
| OpenAPI | Birim | 3.0 local `$ref`, path-level + operation parametre birleştirme, path/query/header/cookie, `explode`, security override, OAuth2 desteklenmiyor notu, uzak `$ref` uyarısı, Swagger 2.0 `collectionFormat`, YAML, bozuk JSON, desteklenmeyen sürüm |
| HTTP / curl | Birim | URL kodlama, serialization stilleri, curl maskeleme ve quoting, Basic/Bearer/API key, 3 sayfa toplama, sayfalar arası tekrar eden ID, sayfa limiti, 401/403/404/500, bağlantı reddi, zaman aşımı, şemasız yanıtta ilk sayfadan profil algılama |
| RAG | Gerçek Ollama | Aşağıdaki tablo |
| Web uygulaması | Entegrasyon | Türkçe sayfalar, taslak profil, gizli bilgilerin yanıtta/DB'de olmaması, OpenAPI yükleme, servis kapalı, desteklenmeyen dosya |
| Kod analizi | Birim + gerçek stdio MCP | Route/sabit, interface→impl, native/JPQL/MyBatis/derived, entity; native sorgunun COUNT'a sarılıp SELECT-only politikadan geçerek çalışması (sonuç 12) |
| Çalışma anı yakalama | Birim + gerçek stdio MCP (simüle V$SQL) | V$SQL penceresi, bind capture, Hibernate sayfalı SQL dönüşümü, OTLP JSON alıcısı, korelasyonlu log; üçgen teşhisi (API 15 / servis 15 / iş kuralı 12 → iş mantığı farkı) |
| Uçtan uca ajan akışı | **SİMÜLASYON** (gerçek gemma3:4b + gerçek stdio MCP + gerçek HTTP; Oracle yerine SQLite) | Aşağıdaki bölüm |
| Gerçek Oracle + gerçek hedef servis | Gerçek (kurum içi) | Oracle 19.30 bağlantısı, 97 izinli tablo/view, MCP araç keşfi, lazy şema keşfi; gerçek servis çağrısı (`get-gm-units`, 3 sayfa, 265 benzersiz ID) ve tutarsızlığın (yanlış tablo eşleşmesi: 40.463) tespiti. Bu bulgu kod analizi ve üç kaynaklı doğrulamanın geliştirilmesine yol açtı |

## RAG retrieval değerlendirmesi (gerçek `qwen3-embedding:0.6b`)

`tests/evaluation/corpus/` altındaki iki sentetik Türkçe belge üzerinde 14 soru; her soru için doğru dosyada beklenen ifadeyi içeren parçanın ilk 4 sonuçta bulunması aranır. Ham sonuç: `docs/evaluation_results/retrieval.json`.

| Metrik | Değer |
|---|---|
| Soru sayısı | 14 |
| hit@4 | 1.00 |
| MRR | 0.893 |

Ayrıca gerçek Ollama ile doğrulanan davranışlar: chunk'lar gerçek token sayısıyla ≤ 700 token; embedding modeli/digest metadata'sı; aynı dosyanın yeni sürümü eski sürümü aktif aramadan çıkarır; projeler arası izolasyon; metin içeren PDF sayfa referansı; taranmış PDF `needs_ocr`.

Tokenizer doğrulaması: Ollama'daki modelin BPE sözlüğünden kurulan yerel tokenizer, üç Türkçe/SQL/kod içeren örnekte Ollama'nın `prompt_eval_count` değeriyle birebir aynı sayıyı verdi (EOS dahil).

## Uçtan uca simülasyon

`tests/e2e/test_simulated_flow.py` — **SİMÜLASYON** etiketi taşır. Ajanlar, RAG, LangGraph, checkpoint, MCP stdio protokolü, SELECT-only politika ve HTTP sayfalama gerçektir; yalnızca veritabanı (SQLite, Oracle SQL'i SQLGlot ile çevrilerek) ve hedef servis (yerel test servisi) simüledir. Test, kullanıcının rolünü üstlenir: açıklama sorularını yanıtlar ve SQL beklenen kapsam filtrelerini içermiyorsa eşleştirmeyi gerekçeli olarak reddeder.

Bu test çalıştırıldığında ham sonucu `docs/evaluation_results/simulation_e2e.json` dosyasına yazar. Kayıtlı en son uçtan uca sonuçlar aşağıdaki demo tablosundadır.

### Demo ortamında gerçek ajanlarla iki senaryo (DEMO verisi)

| Senaryo | Sonuç | Beklenti | API (C) | Servis sorgusu (B) | İş kuralı sorgusu (A) | B kaynağı | Süre |
|---|---|---:|---:|---:|---:|---|---|
| orders | PASS | 12 | 12 | 12 | 12 | vsql | 6 dk 37 sn |
| legacy | FAIL | 12 | 15 | 15 | 12 | vsql | 7 dk 24 sn |

Demo projesi, iki uç için daha önce onaylanmış iş kuralı eşleştirmesiyle (proje hafızası) başlatıldı; bu nedenle Oracle ajanı atlandı. Planlama, İş Kuralı ve Analiz ajanları ile `legacy` için LLM'siz API seçimi gerçek `gemma3:4b` ile çalıştı. `orders` ucu kataloğda `orders/legacy` ile belirsiz olduğundan API ajanı LLM ile seçim yaptı.


### Simülasyonun ortaya çıkardığı ve giderilen sorunlar

| Gözlem | Düzeltme |
|---|---|
| Planlama ajanı açık bir talepte gereksiz soru sordu | Katalogdaki bilinen kaynaklar bağlama eklendi, prompt netleştirildi |
| İş kuralı ajanının JSON çıktısı 1024 token sınırında kesildi | `num_predict` 2048; şemaya `maxLength`/`maxItems` sınırları |
| Oracle ajanı kapsam filtrelerini atladı; DB 18, API 12 → sistem doğru şekilde **FAIL** verdi ve sayıları zorla eşitlemedi | Her kural için zorunlu `rule_decisions`; "filtre" kararı verilen kuralın filtresi yoksa kapı reddeder ve geri bildirimle yeniden dener |
| Ön arama "aktif kayıt" kuralını getirmedi | Kavram bazlı ön arama sorguları (aktiflik, silinme, sayma birimi, istisna, kapsam) |
| "Silinmişse sayılmaz ama geri alınmışsa sayılır" ifade edilemiyordu | Predicate `or_group` desteği: `(DELETED IS NULL OR RESTORED IS NOT NULL)` |

## Gerçek sistem doğrulaması

<!-- REAL_RESULTS -->

Bu bölüm kullanıcının kendi Spring Boot servisi, Swagger dosyası, analist belgeleri ve on-prem Oracle'ı ile yapılacak çalıştırmanın sonuçlarıyla doldurulur. O çalıştırma yapılmadan aşağıdaki kontroller **BLOCKED** durumundadır:

- `tests/integration/test_oracle_mcp_stdio.py::test_real_oracle_ping_and_metadata` (`CONTRACTFLOW_TEST_PROJECT_ID` gerektirir)
- `scripts/smoke.py <proje_id>` — gerçek Oracle `SELECT 1 FROM DUAL`, izinli tablolar, politika reddi, servis erişimi, model yetenek testi
- Gerçek talep ile PASS, beklenti-FAIL (kasıtlı yanlış beklenti), servis kapalı → bekleme → devam
- API–Oracle FAIL senaryosu yalnızca gerçek veride tutarsızlık varsa gözlenebilir; yapay hata modu eklenmemiştir

## Testleri çalıştırma

```bash
python -m pytest tests/unit tests/integration -q
python -m pytest tests/e2e -s -q
```

Ollama yoksa ilgili testler `BLOCKED: …` gerekçesiyle **skip** edilir; geçmiş gibi raporlanmaz.

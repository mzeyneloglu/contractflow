# Kullanım rehberi

## Bir doğrulamanın yaşam döngüsü

1. **Talep:** Doğrulama ekranına beklentinizi yazın. İyi bir talep; servis/kaynak adını, filtre parametresini ve değerini, beklenen sayıyı içerir.
   - `orders servisinde customerId'si 7 olan siparişlerin sayısı 12 olmalı. İş kurallarına göre API ile Oracle'ı karşılaştır.`
   - Beklenti vermezseniz yalnızca API–Oracle tutarlılığı ölçülür.
2. **Ön kontrol:** Ollama modelleri, hazır belge, OpenAPI kataloğu ve Oracle (MCP `ping`) kontrol edilir. Eksik varsa çalışma ilgili bekleme durumuna geçer.
3. **Planlama → İş Kuralı → API → Oracle ajanları:** Zaman çizelgesinde her ajanın görevini, gerçek araç çağrılarını (araç adı + doğrulanmış argümanlar) ve kısa gerekçesini görürsünüz. İç düşünce zinciri gösterilmez veya kaydedilmez.
4. **Eşleştirme onayı (ilk kez):** Endpoint–tablo eşleştirmesi, parametre–kolon eşlemesi, iş kuralından gelen kapsam filtreleri (kaynak dosya/bölümüyle) ve çalıştırılacak SQL size gösterilir.
   - **Onayla ve kaydet:** eşleştirme proje hafızasına yazılır; aynı operasyon için sonraki çalışmalarda yeniden sorulmaz.
   - **Reddet:** kısa bir not yazın (ör. "silinmiş kayıtlar hariç tutulmalı"); Oracle ajanı notla yeniden önerir.
5. **Yürütme:** API'nin bütün sayfaları ve Oracle sorgusu paralel çalışır.
6. **Karşılaştırma ve rapor:** Üç bağımsız assertion, kapsam notları, etiketli hipotezler.

## Bekleme durumları ve devam

| Durum | Ne yapmalı |
|---|---|
| Servis bekleniyor | Backend'i başlatın → **Tekrar Kontrol Et**. Tamamlanmış Oracle gözlemi yeniden çalıştırılmaz. |
| Yetki gerekli | Kurulum'da token/parolayı güncelleyin → **Tekrar dene** |
| Ollama bekleniyor | Gösterilen `ollama pull …` komutlarını çalıştırın → **Tekrar Kontrol Et** |
| Belge bekleniyor | Belge yükleyin veya **Belgesiz devam et** |
| Açıklama bekleniyor | Ajanın sorusunu yanıtlayın; yanıt sonraki ajanlara bağlam olarak geçer |

Çalışmayı her an **İptal Et** ile durdurabilirsiniz. Uygulama kapanıp açılırsa yarım kalan çalışmalar son checkpoint'ten sürdürülür.

## Rapor

- **JSON** ve **Markdown** olarak indirilebilir.
- Maskeli curl komutu kopyalanıp yerelde çalıştırılabilir: `export CONTRACTFLOW_TOKEN=…` ile kendi token'ınızı verin.
- **Aynı kontrolü tekrar çalıştır** yeni bir çalışma başlatır; önceki çalışmanın değerleri yeni çalışmanın gerçeği olarak kullanılmaz.

## Birden fazla servis/repo

Her servis için ayrı proje oluşturun. Projeler; bağlantılar, gizli bilgiler, belgeler, RAG indeksi, eşleştirme hafızası ve geçmiş açısından birbirinden yalıtılmıştır.

## Gerçek bağlantı smoke testi

Arayüzde projeyi kurduktan sonra proje kimliğiyle (`/projects/<id>/setup` adresindeki `<id>`):

```bash
python scripts/smoke.py <proje_id>
```

Gerçek Oracle'da `SELECT 1 FROM DUAL`, izinli tablo listesi, SELECT-only politika reddi, hedef servis erişimi ve gerçek model yetenek testi raporlanır.

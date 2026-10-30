# Genel Veri Politikası (SENTETİK TEST BELGESİ)

Bu belge yalnızca ContractFlow retrieval testleri için hazırlanmış sentetik bir proje geneli politika dokümanıdır.

## Tenant Ayrımı

Tüm servisler `TENANT_ID` kolonu ile çalışır. API çağrısı hangi tenant için yapıldıysa veritabanı karşılaştırması da aynı tenant ile filtrelenmelidir.

## Tarih ve Saat Dilimi

Tarih filtreleri Europe/Istanbul saat dilimine göre uygulanır. Gün sonu 23:59:59 olarak kabul edilir.

## Arşiv Tabloları

`_ARCH` ile biten tablolar arşiv tablolarıdır ve canlı adet hesaplarında kullanılmaz.

## Kaynak Önceliği

Servise özel analist dokümanı ile bu genel politika çelişirse servise özel doküman önceliklidir.

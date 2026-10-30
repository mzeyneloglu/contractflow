# Sipariş Servisi İş Kuralları (SENTETİK TEST BELGESİ)

Bu belge yalnızca ContractFlow retrieval testleri için hazırlanmış sentetik bir analist dokümanıdır. Gerçek bir sisteme ait değildir.

## Kapsam

Kurallar `GET /orders` endpoint'i ve ORDERS tablosu için geçerlidir. Endpoint `customerId` parametresiyle müşteri bazında filtreleme yapar.

## Aktif Sipariş Tanımı

Bir sipariş yalnızca `STATUS` kolonu `A` olduğunda aktif kabul edilir. `P` (beklemede) ve `C` (iptal) durumundaki siparişler aktif sayılmaz.

## Silinmiş Kayıtlar

Silme işlemi soft-delete olarak yapılır. `DELETED_AT` kolonu dolu olan kayıtlar silinmiş kabul edilir ve hiçbir adet hesabına dahil edilmez.

### İstisna: Arşivden Geri Alınanlar

`RESTORED_AT` alanı dolu olan kayıtlar, `DELETED_AT` dolu olsa bile geri alınmış kabul edilir ve aktif sayıma dahil edilir.

## Sayma Birimi

Sipariş adedi `orderId` üzerinden benzersiz olarak sayılır. Bir siparişin birden fazla sipariş satırı olması adedi artırmaz; satır tablosuyla yapılan JOIN sonrası tekrar eden kayıtlar tekilleştirilmelidir.

## Test Müşterileri

`CUSTOMER_TYPE` değeri `TEST` olan müşterilere ait siparişler raporlama ve adet kontrollerinde hariç tutulur.

## Sayfalama

Endpoint Spring Page yanıtı döner. `page` sıfırdan başlar, `size` en fazla 100 olabilir. Toplam kayıt sayısı `totalElements` alanında bildirilir; ilk sayfadaki kayıt sayısı toplam adet değildir.

```json
{"content": [{"orderId": 1}], "totalElements": 1, "number": 0, "size": 100}
```

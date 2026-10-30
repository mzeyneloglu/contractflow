from dataclasses import dataclass


@dataclass(frozen=True)
class RetrievalCase:
    question: str
    required_text: str
    required_file: str


RETRIEVAL_CASES = [
    RetrievalCase("Hangi siparişler aktif sayılır?", "STATUS", "siparis_servisi_kurallari.md"),
    RetrievalCase("Silinmiş kayıtlar adede dahil mi?", "DELETED_AT", "siparis_servisi_kurallari.md"),
    RetrievalCase("Soft delete edilmiş ama geri alınmış sipariş sayılır mı?", "RESTORED_AT", "siparis_servisi_kurallari.md"),
    RetrievalCase("Sipariş adedi hangi alan üzerinden benzersiz sayılmalı?", "orderId", "siparis_servisi_kurallari.md"),
    RetrievalCase("JOIN sonrası tekrar eden satırlar nasıl ele alınır?", "tekilleştirilmelidir", "siparis_servisi_kurallari.md"),
    RetrievalCase("Test müşterilerinin siparişleri hariç mi?", "CUSTOMER_TYPE", "siparis_servisi_kurallari.md"),
    RetrievalCase("Toplam kayıt sayısı yanıtta hangi alanda?", "totalElements", "siparis_servisi_kurallari.md"),
    RetrievalCase("page parametresi kaçtan başlıyor?", "sıfırdan", "siparis_servisi_kurallari.md"),
    RetrievalCase("customerId filtresi hangi endpoint için geçerli?", "customerId", "siparis_servisi_kurallari.md"),
    RetrievalCase("Tenant filtresi karşılaştırmada nasıl uygulanmalı?", "TENANT_ID", "genel_veri_politikasi.md"),
    RetrievalCase("Arşiv tabloları sayımda kullanılır mı?", "_ARCH", "genel_veri_politikasi.md"),
    RetrievalCase("Dokümanlar çelişirse hangisi öncelikli?", "önceliklidir", "genel_veri_politikasi.md"),
    RetrievalCase("Tarih filtresinde hangi saat dilimi kullanılır?", "Europe/Istanbul", "genel_veri_politikasi.md"),
    RetrievalCase("İptal durumundaki kayıtlar aktif mi?", "iptal", "siparis_servisi_kurallari.md"),
]

# «Texnologiyalar va usullar» maydoni uchun matn (UZ)

Prototip — Python’da yozilgan Telegram-bot. U 500 000 bitim reyestri bo‘yicha
o‘zbek va rus tillarida savollarga javob beradi. CSV ma’lumotlari SQLite’ga
yuklanadi; yuklashda fizik hajm (lot soni × lot), bitim sanasidagi dollar kursi
bo‘yicha so‘m va dollar summalari, holat bayroqlari, yil/oy/chorak va indekslar
hisoblanadi. Gemini Flash (3.5 Flash Lite, avtomatik ravishda boshqa mavjud
Flash modeliga o‘tish bilan) faqat so‘rov rejalashtiruvchisi sifatida
ishlatiladi: u intent va SQL’ni JSON ko‘rinishida qaytaradi, barcha raqamlar,
kompaniya nomlari va kodlar SQL natijasidan olinib, deterministik ikki tilli
shablonlarga qo‘yiladi. Shu sababli model raqam yoki kompaniyani o‘ylab topa
olmaydi.

SQL bajarilishidan oldin qat’iy tekshiruvdan o‘tadi: faqat SELECT/WITH, faqat
olti jadval, read-only ulanish, authorizer, 8 soniyalik taymaut va qatorlar
limiti. Mahsulot, hudud, holat, kompaniya, broker va birlik nomlari bazadagi
haqiqiy qiymatlarga o‘zbek, rus va ingliz aliaslari orqali moslashtiriladi;
noaniq nomlar bo‘yicha foydalanuvchidan aniqlashtirish so‘raladi. Ma’lumotda
javob bo‘lmasa (prognoz, siyosat, ob-havo, foyda, telefon, mavjud bo‘lmagan
kompaniya), bot buni ochiq aytadi. Har bir javob ostida «Manba» bloki bor: SQL
so‘rov, qatorlar soni, davr va holatlar qamrovi — tekshiruvchi natijani qayta
hosil qilishi mumkin.

Standart holatda reyestrning barcha qatorlari, shu jumladan bekor qilinganlar
hisobga olinadi (bu manbada ko‘rsatilgan); «bajarilgan» deb so‘ralsa
is_completed=1, «bekor qilingan» deb so‘ralsa is_canceled=1 filtrlanadi. Javob
tili savoldan aniqlanadi; o‘zbek lotin, o‘zbek kirill va rus tillari
qo‘llanadi. Bot systemd ostida doim ishlaydigan VM’da joylashtirilgan, havola
orqali o‘rnatish va ro‘yxatdan o‘tishsiz ochiladi; 55 ta oflayn test, jonli
sinov va alohida xavfsizlik testi (SQL-funksiyalar allowlisti, HTML-in’ektsiya,
so‘rov limiti) javoblarni mustaqil SQL so‘rovlar bilan tekshiradi.

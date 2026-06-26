# Sisyphus Rules — Kalıcı Bellek

Bu dosya Sisyphus güncellense bile sistemimizin unutmaması gereken kuralları içerir.

---

## KURAL 1: Git Commit'lerde Yasaklı İfadeler

Aşağıdaki ifadeler **hiçbir commit mesajında, commit body'sinde, author/committer bilgisinde veya tag'de** bulunamaz:

- `Sisyphus`
- `sisyphus-dev-ai`
- `clio-agent@sisyphuslabs`
- `Co-authored-by: Sisyphus`
- `Ultraworked with`

### Author Bilgisi

Tüm commit'lerde author ve committer şu şekilde olmalıdır:

```
Author: SERDAR-AKIN <26321769+SERDAR-AKIN@users.noreply.github.com>
```

### Git Hook

`.git/hooks/commit-msg` bu kuralı otomatik denetler. Hook'u tüm repolara kopyalamak için:

```bash
cp .git/hooks/commit-msg ~/.git-templates/hooks/commit-msg
git config --global init.templateDir ~/.git-templates
```

---

## KURAL 2: Python 3.12 Kullanımı

Python 3.14, bleak ile uyumlu değildir. Tüm komutlar `python3.12` ile çalıştırılmalıdır.

---

## KURAL 3: BLE Bağlantı Stratejisi

- DL24 yalnızca bir BLE bağlantısını kabul eder
- Bağlanmadan önce `_kick_other_connections()` ile mevcut bağlantıyı kes
- `write_gatt_char(CHAR, b'\x01\x00')` ile notification etkinleştir (CCCD descriptor değil)
- NotificationReassembler akümülasyon buffer'ı kullanır (sabit 23+19 değil)

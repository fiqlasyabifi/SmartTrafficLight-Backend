# Smart Traffic Light Rawamangun — Backend

**Kelompok 12**  
Backend lokal untuk prototipe simulasi Smart Traffic Light Rawamangun.

## Fungsi saat ini

- Mendengarkan perintah pada Firebase Realtime Database `/commands`.
- Mengirim perintah JSONL melalui USB-Serial ke ESP32.
- Menyimpan balasan ESP32 pada `/responses`.
- Memperbarui status ESP32 pada `/esp32/status`.
- Memperbarui status backend pada `/backend/status`.

Perintah yang diizinkan pada tahap ini hanya `PING` dan `STATUS`. Firmware ESP32 tetap menjalankan siklus lampu otomatis secara mandiri. Belum ada kendali prioritas ambulans atau pengubahan fase lampu dari backend.

## Persyaratan

- Windows
- Python 3.14 (atau versi kompatibel yang telah diuji)
- ESP32 DevKit V1 terhubung melalui USB-Serial
- Firebase Project dan Realtime Database
- Service Account Firebase Admin SDK
- Arduino Serial Monitor/Serial Plotter ditutup saat backend berjalan

## Instalasi lokal

Buat dan aktifkan virtual environment:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

Pasang dependensi:

```powershell
python -m pip install -r requirements.txt
```

## Kredensial Firebase

Unduh Service Account JSON dari Firebase Console → Project settings → Service accounts.

Simpan file secara lokal di:

```text
C:\STL-credentials\firebase-admin-key.json
```

Jangan commit, unggah, atau membagikan file Service Account JSON. File tersebut memberi akses administratif ke Firebase.

## Konfigurasi

Periksa nilai konfigurasi di bagian atas `backend.py`:

- `SERVICE_ACCOUNT_FILE`
- `DATABASE_URL`
- `SERIAL_PORT` (default `COM3`)
- `SERIAL_BAUD_RATE` (default `115200`)

Sesuaikan port COM dengan yang muncul di Windows Device Manager bila berbeda.

## Menjalankan backend

```powershell
python backend.py
```

Hentikan dengan `Ctrl+C`.

## Struktur Realtime Database

```text
/commands/{request_id}
/responses/{request_id}
/esp32/status
/backend/status
```

Perintah awal yang didukung:

- `PING`
- `STATUS`

## Catatan teknis dan batas penggunaan

- Firebase Admin SDK `listen()` pada Python bersifat eksperimental; untuk penggunaan lebih lanjut, siapkan mekanisme pemulihan atau polling HTTPS sebagai fallback.
- Membuka port serial dapat me-reset ESP32 melalui rangkaian USB-to-Serial.
- Proyek ini merupakan prototipe simulasi akademik di meja kerja, bukan sistem untuk mengendalikan APILL atau lalu lintas jalan nyata.
- Jangan pernah menaruh private key, password, token, atau file kredensial di repository.

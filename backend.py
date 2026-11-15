"""Backend lokal Smart Traffic Light Rawamangun - Kelompok 12.

Alur:
Firebase /commands/{request_id} -> serial JSONL -> ESP32
ESP32 ACK/NAK -> Firebase /responses/{request_id}
Status ESP32 -> Firebase /esp32/status

Perintah yang diizinkan pada tahap ini hanya PING dan STATUS.
Firmware ESP32 tetap menjalankan siklus lampu otomatis secara mandiri.
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import firebase_admin
import serial
from firebase_admin import credentials, db


# ============================================================
# KONFIGURASI PROYEK
# ============================================================
SERVICE_ACCOUNT_FILE = Path(r"C:\STL-credentials\firebase-admin-key.json")
DATABASE_URL = (
    "https://smart-traffic-light-rawa-d711e-default-rtdb."
    "asia-southeast1.firebasedatabase.app/"
)

SERIAL_PORT = "COM3"
SERIAL_BAUD_RATE = 115200
SERIAL_READ_TIMEOUT = 0.15
SERIAL_RESPONSE_TIMEOUT = 4.0

# Firmware saat ini menampilkan splash 3 detik, lalu startup all-red 3 detik.
# Membuka port serial dapat me-reset ESP32, jadi beri waktu untuk dua tahap itu.
ESP32_BOOT_WAIT_SECONDS = 6.5

# Backend meminta STATUS berkala agar Firebase menyimpan status ESP32 terbaru.
STATUS_POLL_INTERVAL_SECONDS = 3.0
BACKEND_HEARTBEAT_INTERVAL_SECONDS = 10.0

ALLOWED_COMMANDS = {"PING", "STATUS"}
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


# ============================================================
# ANTRIAN PERINTAH DARI FIREBASE
# ============================================================
command_queue: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
queued_request_ids: set[str] = set()
queued_ids_lock = threading.Lock()


# ============================================================
# UTILITAS
# ============================================================
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(message: str) -> None:
    print(message, flush=True)


def enqueue_command(request_id: str, payload: Any) -> None:
    """Masukkan command pending ke antrean, satu kali per request ID."""
    if not REQUEST_ID_PATTERN.fullmatch(request_id):
        log(f"[FIREBASE] Abaikan request_id tidak valid: {request_id!r}")
        return

    if not isinstance(payload, dict):
        return

    if str(payload.get("status", "")).lower() != "pending":
        return

    with queued_ids_lock:
        if request_id in queued_request_ids:
            return
        queued_request_ids.add(request_id)

    command_queue.put((request_id, payload.copy()))
    log(f"[FIREBASE] Perintah baru masuk antrean: {request_id}")


def firebase_commands_callback(event: Any) -> None:
    """Callback listen() hanya memvalidasi dan mengantrikan data.

    Callback Firebase berjalan pada background thread. Akses serial sengaja
    tetap dilakukan oleh thread utama agar pembacaan/penulisan COM3 teratur.
    """
    try:
        event_path = getattr(event, "path", "/") or "/"
        event_data = getattr(event, "data", None)

        if not isinstance(event_data, dict):
            return

        normalized_path = event_path.strip("/")

        # Snapshot awal /commands atau perubahan pada satu anak langsung.
        if not normalized_path:
            for request_id, payload in event_data.items():
                enqueue_command(str(request_id), payload)
        elif "/" not in normalized_path:
            enqueue_command(normalized_path, event_data)
        # Perubahan lebih dalam, misalnya /id/status, tidak perlu diproses.

    except Exception as error:  # Jangan biarkan callback listener berhenti diam-diam.
        log(f"[FIREBASE] Kesalahan callback listener: {error}")


def write_backend_status(status_ref: Any, **fields: Any) -> None:
    data = {"last_seen": utc_now_iso(), **fields}
    status_ref.update(data)


def publish_esp32_status(status_ref: Any, response: dict[str, Any]) -> None:
    """Simpan informasi status dari jawaban STATUS ESP32."""
    status_ref.update(
        {
            "online": True,
            "state": response.get("state", "UNKNOWN"),
            "phase": response.get("phase", "UNKNOWN"),
            "condition": response.get("condition", "UNKNOWN"),
            "remaining_seconds": response.get("remaining_seconds"),
            "updated_at": utc_now_iso(),
        }
    )


# ============================================================
# KOMUNIKASI SERIAL JSONL
# ============================================================
def read_serial_line(connection: serial.Serial) -> str | None:
    raw_line = connection.readline()
    if not raw_line:
        return None

    text = raw_line.decode("utf-8", errors="replace").strip()
    if text:
        log(f"[ESP32] {text}")
    return text or None


def send_serial_command(
    connection: serial.Serial,
    command: str,
    request_id: str,
) -> dict[str, Any]:
    """Kirim satu command dan tunggu ACK/NAK dengan ID yang sama."""
    payload = {"cmd": command, "id": request_id}
    message = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")

    connection.write(message)
    connection.flush()
    log(f"[SERIAL -> ESP32] {payload}")

    deadline = time.monotonic() + SERIAL_RESPONSE_TIMEOUT
    while time.monotonic() < deadline:
        text = read_serial_line(connection)
        if not text or not text.startswith("{"):
            continue

        try:
            response = json.loads(text)
        except json.JSONDecodeError:
            continue

        if isinstance(response, dict) and response.get("id") == request_id:
            log(f"[ESP32 -> SERIAL] Balasan terverifikasi: {response}")
            return response

    raise TimeoutError(
        f"Tidak menerima ACK/NAK untuk {command} (request_id={request_id})."
    )


# ============================================================
# PEMROSESAN PERINTAH FIREBASE
# ============================================================
def process_firebase_command(
    connection: serial.Serial,
    commands_ref: Any,
    responses_ref: Any,
    esp32_status_ref: Any,
    request_id: str,
    command_data: dict[str, Any],
) -> None:
    command_ref = commands_ref.child(request_id)
    response_ref = responses_ref.child(request_id)

    command = str(command_data.get("cmd", "")).strip().upper()

    command_ref.update(
        {
            "status": "processing",
            "processing_at": utc_now_iso(),
        }
    )

    if command not in ALLOWED_COMMANDS:
        response = {
            "type": "NAK",
            "id": request_id,
            "cmd": command or None,
            "message": "UNSUPPORTED_COMMAND",
            "received_at": utc_now_iso(),
        }
        response_ref.set(response)
        command_ref.update(
            {"status": "rejected", "completed_at": utc_now_iso()}
        )
        log(f"[BACKEND] Perintah {request_id} ditolak: {command!r}")
        return

    try:
        response = send_serial_command(connection, command, request_id)
        response_with_time = {**response, "received_at": utc_now_iso()}
        response_ref.set(response_with_time)

        if response.get("type") == "ACK":
            command_ref.update(
                {"status": "completed", "completed_at": utc_now_iso()}
            )
            if command == "STATUS":
                publish_esp32_status(esp32_status_ref, response)
            log(f"[BACKEND] Perintah {request_id} selesai.")
        else:
            command_ref.update(
                {"status": "rejected", "completed_at": utc_now_iso()}
            )
            log(f"[BACKEND] ESP32 menolak perintah {request_id}.")

    except TimeoutError as error:
        failure = {
            "type": "TIMEOUT",
            "id": request_id,
            "cmd": command,
            "message": str(error),
            "received_at": utc_now_iso(),
        }
        response_ref.set(failure)
        command_ref.update(
            {"status": "failed", "completed_at": utc_now_iso()}
        )
        log(f"[BACKEND] {error}")


# ============================================================
# PROGRAM UTAMA
# ============================================================
def main() -> None:
    listener = None
    connection: serial.Serial | None = None
    backend_status_ref = None
    firebase_ready = False

    log("=" * 58)
    log("SMART TRAFFIC LIGHT RAWAMANGUN - KELOMPOK 12")
    log("PYTHON BACKEND: FIREBASE <-> ESP32")
    log("Perintah aktif tahap ini: PING dan STATUS")
    log("=" * 58)

    try:
        if not SERVICE_ACCOUNT_FILE.is_file():
            raise FileNotFoundError(
                "Service Account tidak ditemukan: "
                f"{SERVICE_ACCOUNT_FILE}\n"
                "Periksa lokasi file firebase-admin-key.json."
            )

        credential = credentials.Certificate(str(SERVICE_ACCOUNT_FILE))
        firebase_admin.initialize_app(
            credential,
            {"databaseURL": DATABASE_URL},
        )
        firebase_ready = True
        log("[FIREBASE] Firebase Admin SDK berhasil diinisialisasi.")

        commands_ref = db.reference("commands")
        responses_ref = db.reference("responses")
        esp32_status_ref = db.reference("esp32/status")
        backend_status_ref = db.reference("backend/status")

        log(f"[SERIAL] Membuka {SERIAL_PORT} pada {SERIAL_BAUD_RATE} bps...")
        connection = serial.Serial(
            SERIAL_PORT,
            SERIAL_BAUD_RATE,
            timeout=SERIAL_READ_TIMEOUT,
            write_timeout=1.0,
        )

        # Membuka COM3 bisa me-reset ESP32. Beri waktu splash dan startup.
        log(
            "[SERIAL] Menunggu ESP32 menyelesaikan splash dan startup "
            f"({ESP32_BOOT_WAIT_SECONDS:.1f} detik)..."
        )
        time.sleep(ESP32_BOOT_WAIT_SECONDS)
        connection.reset_input_buffer()
        log("[SERIAL] Port siap digunakan.")

        # Pasang listener hanya pada node commands, bukan root database.
        listener = commands_ref.listen(firebase_commands_callback)
        log("[FIREBASE] Listener aktif pada /commands.")

        write_backend_status(
            backend_status_ref,
            online=True,
            serial_connected=True,
            serial_port=SERIAL_PORT,
            message="Backend aktif dan menunggu perintah.",
        )

        next_status_poll = 0.0
        next_heartbeat = time.monotonic() + BACKEND_HEARTBEAT_INTERVAL_SECONDS

        log("\nBACKEND AKTIF. Tekan CTRL+C untuk menghentikan.\n")

        while True:
            # Tampilkan log fase/countdown yang dikirim ESP32 secara berkala.
            read_serial_line(connection)

            # Dahulukan perintah yang datang dari Firebase.
            try:
                request_id, command_data = command_queue.get_nowait()
            except queue.Empty:
                request_id = ""
                command_data = {}

            if request_id:
                process_firebase_command(
                    connection,
                    commands_ref,
                    responses_ref,
                    esp32_status_ref,
                    request_id,
                    command_data,
                )
                command_queue.task_done()
                continue

            # Polling STATUS menjaga node /esp32/status tetap diperbarui.
            now = time.monotonic()
            if now >= next_status_poll:
                poll_id = f"poll-{uuid.uuid4().hex[:12]}"
                try:
                    response = send_serial_command(connection, "STATUS", poll_id)
                    if response.get("type") == "ACK":
                        publish_esp32_status(esp32_status_ref, response)
                    else:
                        log(f"[BACKEND] Poll STATUS menerima NAK: {response}")
                except TimeoutError as error:
                    log(f"[BACKEND] Gagal memperbarui status ESP32: {error}")
                next_status_poll = time.monotonic() + STATUS_POLL_INTERVAL_SECONDS
                continue

            if now >= next_heartbeat:
                write_backend_status(
                    backend_status_ref,
                    online=True,
                    serial_connected=connection.is_open,
                    serial_port=SERIAL_PORT,
                    message="Backend aktif dan listener berjalan.",
                )
                next_heartbeat = time.monotonic() + BACKEND_HEARTBEAT_INTERVAL_SECONDS

    except KeyboardInterrupt:
        log("\n[BACKEND] Penghentian diminta melalui CTRL+C.")
    except serial.SerialException as error:
        log(f"[SERIAL] Gagal menggunakan {SERIAL_PORT}: {error}")
        log("Pastikan ESP32 terhubung dan Serial Monitor Arduino IDE ditutup.")
    except Exception as error:
        log(f"[BACKEND] Terjadi kesalahan: {error}")
    finally:
        if listener is not None:
            try:
                listener.close()
                log("[FIREBASE] Listener dihentikan.")
            except Exception as error:
                log(f"[FIREBASE] Gagal menutup listener: {error}")

        if connection is not None and connection.is_open:
            try:
                connection.close()
                log("[SERIAL] Koneksi COM ditutup.")
            except Exception as error:
                log(f"[SERIAL] Gagal menutup port: {error}")

        if firebase_ready and backend_status_ref is not None:
            try:
                write_backend_status(
                    backend_status_ref,
                    online=False,
                    serial_connected=False,
                    message="Backend dihentikan.",
                )
            except Exception as error:
                log(f"[FIREBASE] Gagal memperbarui status shutdown: {error}")

        log("Backend selesai.")


if __name__ == "__main__":
    main()

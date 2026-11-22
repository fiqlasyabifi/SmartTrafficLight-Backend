"""Backend lokal Smart Traffic Light Rawamangun - Kelompok 12.

Alur:
    Android Virtual GPS
        -> Firebase /ambulance/sessions/{uid}
        -> Python Backend memantau sesi dan umur GPS
        -> Firebase /commands/{request_id}
        -> Serial JSONL
        -> ESP32

PRIORITY dibuat ketika:
    - simulation.status == running
    - live.status == MOVING
    - timestamp GPS valid dan segar
    - distance_remaining_m <= 150 meter

RELEASE dibuat ketika sesi yang sudah meminta PRIORITY:
    - live.status == ARRIVED
    - sesi dibatalkan
    - sesi selesai/dihentikan secara manual
    - GPS stale
    - timestamp GPS tidak valid (terlalu jauh di masa depan)

Umur GPS ditampilkan sebagai selisih bertanda (bisa negatif).
Selisih kecil akibat perbedaan jam ditoleransi untuk keputusan prioritas,
namun tidak diubah menjadi nol pada log agar dapat diamati.

Firmware ESP32 memiliki failsafe GREEN HOLD maksimum 90 detik.
Prototipe ini untuk miniatur, bukan pengendali jalan umum.
"""

from __future__ import annotations

import json
import math
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

SERVICE_ACCOUNT_FILE = Path(
    r"C:\STL-credentials\firebase-admin-key.json"
)

DATABASE_URL = (
    "https://smart-traffic-light-rawa-d711e-default-rtdb."
    "asia-southeast1.firebasedatabase.app/"
)

SERIAL_PORT = "COM3"
SERIAL_BAUD_RATE = 115200
SERIAL_READ_TIMEOUT = 0.15
SERIAL_RESPONSE_TIMEOUT = 4.0
ESP32_BOOT_WAIT_SECONDS = 6.5

STATUS_POLL_INTERVAL_SECONDS = 3.0
BACKEND_HEARTBEAT_INTERVAL_SECONDS = 10.0

# Pemantauan GPS.
AMBULANCE_SCAN_INTERVAL_SECONDS = 1.0
MAX_GPS_DATA_AGE_SECONDS = 5.0
PRIORITY_TRIGGER_DISTANCE_METERS = 150.0

# Toleransi perbedaan jam Windows terhadap waktu server Firebase.
# Umur antara -5 dan 0 detik tetap ditampilkan sebagai angka negatif,
# tetapi masih dianggap dapat diterima untuk pemantauan.
# Timestamp lebih maju dari batas ini dianggap tidak valid.
MAX_FUTURE_CLOCK_SKEW_SECONDS = 5.0

# Pemetaan traffic light ke kelompok lampu ESP32.
TRAFFIC_LIGHT_TO_GROUP = {
    "TL1": "A",
    "TL2": "A",
    "TL3": "C",
    "TL4": "C",
    "TL5": "B",
    "TL6": "B",
}

ALLOWED_COMMANDS = {
    "PING",
    "STATUS",
    "PRIORITY",
    "RELEASE",
}
VALID_PRIORITY_GROUPS = {"A", "B", "C"}
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

RELEASE_LOG_TITLES = {
    "AMBULANCE_ARRIVED": "AMBULANS TIBA DI TUJUAN",
    "SESSION_CANCELLED": "SIMULASI AMBULANS DIBATALKAN",
    "SESSION_ENDED": "SESI SIMULASI BERAKHIR",
    "GPS_STALE": "GPS TIDAK SEGAR",
    "GPS_TIMESTAMP_INVALID": "TIMESTAMP GPS TIDAK VALID",
}


# ============================================================
# ANTRIAN PERINTAH FIREBASE
# ============================================================

command_queue: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
queued_request_ids: set[str] = set()
queued_ids_lock = threading.Lock()


def utc_now_iso() -> str:
    """Waktu UTC untuk metadata/log Firebase yang berbentuk ISO-8601."""
    return datetime.now(timezone.utc).isoformat()


def log(message: str) -> None:
    print(message, flush=True)


def enqueue_command(request_id: str, payload: Any) -> None:
    """Masukkan perintah pending ke antrean lokal, satu kali per ID."""
    if not REQUEST_ID_PATTERN.fullmatch(request_id):
        log(f"[FIREBASE] Abaikan request_id tidak valid: {request_id!r}")
        return

    if not isinstance(payload, dict):
        return

    if str(payload.get("status", "")).lower() != "pending":
        return

    # Callback Firebase berjalan di background; hanya main thread memakai serial.
    with queued_ids_lock:
        if request_id in queued_request_ids:
            return
        queued_request_ids.add(request_id)

    command_queue.put((request_id, payload.copy()))
    log(f"[FIREBASE] Perintah baru masuk antrean: {request_id}")


def firebase_commands_callback(event: Any) -> None:
    """Terima perubahan pada Firebase /commands."""
    try:
        event_path = getattr(event, "path", "/") or "/"
        event_data = getattr(event, "data", None)

        if not isinstance(event_data, dict):
            return

        normalized_path = event_path.strip("/")

        if not normalized_path:
            for request_id, payload in event_data.items():
                enqueue_command(str(request_id), payload)
        elif "/" not in normalized_path:
            enqueue_command(normalized_path, event_data)

    except Exception as error:
        log(f"[FIREBASE] Kesalahan callback listener: {error}")


def write_backend_status(status_ref: Any, **fields: Any) -> None:
    status_ref.update({"last_seen": utc_now_iso(), **fields})


def publish_esp32_status(
    status_ref: Any,
    response: dict[str, Any],
) -> None:
    """Simpan status ESP32, termasuk status prioritas bila tersedia."""
    status_ref.update(
        {
            "online": True,
            "state": response.get("state", "UNKNOWN"),
            "phase": response.get("phase", "UNKNOWN"),
            "condition": response.get("condition", "UNKNOWN"),
            "remaining_seconds": response.get("remaining_seconds"),
            "priority_active": response.get("priority_active", False),
            "priority_group": response.get("priority_group", "-"),
            "priority_hold": response.get("priority_hold", False),
            "updated_at": utc_now_iso(),
        }
    )


def update_session_simulation(
    sessions_ref: Any,
    uid: Any,
    **fields: Any,
) -> None:
    """Update metadata prioritas pada /ambulance/sessions/{uid}/simulation."""
    if not uid:
        return

    try:
        sessions_ref.child(str(uid)).child("simulation").update(fields)
    except Exception as error:
        log(f"[GPS] Gagal memperbarui sesi {uid}: {error}")


def parse_gps_age_seconds(
    live: dict[str, Any],
    now_ms: float,
) -> float | None:
    """Hitung dan kembalikan umur GPS aktual dalam detik.

    Firebase ServerValue.TIMESTAMP menghasilkan epoch-milliseconds.
    Umur negatif tidak dinormalisasi menjadi 0 karena berguna untuk
    melihat perbedaan jam komputer terhadap waktu server Firebase.
    Nilai negatif yang lebih besar daripada toleransi masa depan akan
    ditolak oleh monitor_ambulance_sessions().
    """
    try:
        raw_updated_at = live.get("updated_at")
        if isinstance(raw_updated_at, bool):
            return None
        updated_at_ms = float(raw_updated_at)
    except (TypeError, ValueError):
        return None

    if not math.isfinite(updated_at_ms):
        return None

    age_seconds = (now_ms - updated_at_ms) / 1000.0
    if not math.isfinite(age_seconds):
        return None

    return age_seconds


def create_release_command(
    uid: str,
    simulation: dict[str, Any],
    sessions_ref: Any,
    commands_ref: Any,
    group: str,
    reason: str,
) -> None:
    """Buat satu perintah RELEASE untuk sesi yang sudah meminta PRIORITY."""
    started_at = simulation.get("started_at")
    if started_at is None:
        return

    already_released_for = simulation.get("priority_release_for_started_at")
    if (
        already_released_for is not None
        and str(already_released_for) == str(started_at)
    ):
        return

    release_request_id = f"rel-{uuid.uuid4().hex[:12]}"
    release_data = {
        "cmd": "RELEASE",
        "group": group,
        "status": "pending",
        "source": "ambulance_gps_release",
        "ambulance_uid": uid,
        "simulation_started_at": started_at,
        "release_reason": reason,
        "created_at": utc_now_iso(),
    }

    try:
        # Buat command, lalu tandai started_at agar tidak dibuat duplikat.
        commands_ref.child(release_request_id).set(release_data)
        update_session_simulation(
            sessions_ref,
            uid,
            priority_release_for_started_at=started_at,
            priority_release_request_id=release_request_id,
            priority_release_status="submitted",
            priority_release_reason=reason,
            priority_release_requested_at=utc_now_iso(),
        )

        # Enqueue lokal; event listener akan terdeduplikasi berdasarkan ID.
        enqueue_command(release_request_id, release_data)

        title = RELEASE_LOG_TITLES.get(reason, "RELEASE DIPERLUKAN")
        log("=" * 58)
        log(f"[GPS] {title}")
        log(f"[GPS] UID                : {uid}")
        log(f"[GPS] Kelompok           : {group}")
        log(f"[GPS] Alasan RELEASE     : {reason}")
        log(f"[GPS] Command ID         : {release_request_id}")
        log("[GPS] Perintah RELEASE dibuat di Firebase.")
        log("=" * 58)

    except Exception as error:
        log(f"[GPS] Gagal membuat RELEASE untuk UID {uid}: {error}")


def monitor_ambulance_sessions(
    sessions_ref: Any,
    commands_ref: Any,
) -> None:
    """Pantau sesi ambulans dan buat PRIORITY/RELEASE sesuai status.

    PRIORITY:
        simulation.status=running
        live.status=MOVING
        GPS segar dan timestamp tidak terlalu maju
        distance_remaining_m <= 150 meter

    RELEASE, jika sesi sudah meminta PRIORITY:
        live.status=ARRIVED
        atau sesi dibatalkan
        atau sesi diselesaikan/dihentikan manual
        atau GPS stale
        atau timestamp GPS terlalu jauh di masa depan
    """
    try:
        sessions = sessions_ref.get()
    except Exception as error:
        log(f"[GPS] Gagal membaca sesi ambulans: {error}")
        return

    if not isinstance(sessions, dict):
        return

    now_ms = time.time() * 1000.0

    for uid_value, session_data in sessions.items():
        uid = str(uid_value)

        if not isinstance(session_data, dict):
            continue

        simulation = session_data.get("simulation") or {}
        live = session_data.get("live") or {}

        if not isinstance(simulation, dict) or not isinstance(live, dict):
            continue

        simulation_status = str(
            simulation.get("status", "")
        ).strip().lower()
        gps_status = str(live.get("status", "")).strip().upper()
        started_at = simulation.get("started_at")

        if started_at is None:
            continue

        age_seconds = parse_gps_age_seconds(live, now_ms)
        request_marker = simulation.get("priority_requested_for_started_at")
        request_matches_session = (
            request_marker is not None
            and str(request_marker) == str(started_at)
        )

        # ----------------------------------------------------
        # 1. Sesi ini sudah meminta PRIORITY: evaluasi RELEASE.
        # ----------------------------------------------------
        if request_matches_session:
            # Hanya sesi yang dibuat oleh versi GREEN HOLD ini yang boleh
            # memicu RELEASE otomatis. Ini menghindari sesi lama dari tes
            # PRIORITY sebelumnya melepas prioritas sesi baru.
            if simulation.get("priority_hold_version") != 1:
                continue

            priority_status = str(
                simulation.get("priority_request_status", "submitted")
            ).strip().lower()
            group = str(
                simulation.get("priority_group", "")
            ).strip().upper()

            if group not in VALID_PRIORITY_GROUPS:
                continue

            # Tidak perlu RELEASE jika PRIORITY ditolak/gagal.
            if priority_status in {"rejected", "failed"}:
                continue

            reason: str | None = None

            # Urutan ini penting: pembatalan harus dideteksi sebelum status
            # sesi selesai umum. ARRIVED hanya jika live benar-benar ARRIVED.
            if (
                gps_status in {"CANCELLED", "CANCELED"}
                or simulation_status in {"cancelled", "canceled"}
            ):
                reason = "SESSION_CANCELLED"

            elif gps_status == "ARRIVED":
                reason = "AMBULANCE_ARRIVED"

            elif (
                gps_status in {"FINISHED", "FAILED", "STOPPED"}
                or simulation_status in {
                    "completed",
                    "complete",
                    "finished",
                    "failed",
                    "stopped",
                }
            ):
                # Status simulation=completed tanpa live=ARRIVED bukan
                # bukti bahwa ambulans benar-benar sudah tiba.
                reason = "SESSION_ENDED"

            elif age_seconds is None:
                reason = "GPS_STALE"

            elif age_seconds > MAX_GPS_DATA_AGE_SECONDS:
                reason = "GPS_STALE"

            elif age_seconds < -MAX_FUTURE_CLOCK_SKEW_SECONDS:
                reason = "GPS_TIMESTAMP_INVALID"

            if reason is not None:
                create_release_command(
                    uid=uid,
                    simulation=simulation,
                    sessions_ref=sessions_ref,
                    commands_ref=commands_ref,
                    group=group,
                    reason=reason,
                )

            # Sesi ini sudah punya request PRIORITY; jangan trigger lagi.
            continue

        # ----------------------------------------------------
        # 2. Untuk sesi baru, hanya proses status RUNNING.
        # ----------------------------------------------------
        if simulation_status != "running":
            continue

        if gps_status != "MOVING":
            continue

        # Data GPS harus mempunyai timestamp, segar, dan tidak terlalu maju.
        if age_seconds is None:
            continue
        if age_seconds < -MAX_FUTURE_CLOCK_SKEW_SECONDS:
            continue
        if age_seconds > MAX_GPS_DATA_AGE_SECONDS:
            continue

        # Validasi koordinat dan jarak.
        try:
            latitude = float(live.get("latitude"))
            longitude = float(live.get("longitude"))
            distance_remaining = float(live.get("distance_remaining_m"))
        except (TypeError, ValueError):
            continue

        if (
            not math.isfinite(latitude)
            or not math.isfinite(longitude)
            or not -90.0 <= latitude <= 90.0
            or not -180.0 <= longitude <= 180.0
            or not math.isfinite(distance_remaining)
            or distance_remaining < 0.0
            or distance_remaining > PRIORITY_TRIGGER_DISTANCE_METERS
        ):
            continue

        traffic_light = str(
            live.get("target_traffic_light")
            or simulation.get("target_traffic_light")
            or ""
        ).strip().upper()
        group = TRAFFIC_LIGHT_TO_GROUP.get(traffic_light)

        if group is None:
            log(
                f"[GPS] Traffic light tujuan tidak dikenal: "
                f"{traffic_light!r} (UID {uid})"
            )
            continue

        request_id = f"amb-{uuid.uuid4().hex[:12]}"
        command_data = {
            "cmd": "PRIORITY",
            "group": group,
            "status": "pending",
            "source": "ambulance_gps",
            "ambulance_uid": uid,
            "simulation_started_at": started_at,
            "target_traffic_light": traffic_light,
            "latitude": latitude,
            "longitude": longitude,
            "distance_remaining_m": distance_remaining,
            "created_at": utc_now_iso(),
        }

        try:
            commands_ref.child(request_id).set(command_data)

            update_session_simulation(
                sessions_ref,
                uid,
                priority_requested_for_started_at=started_at,
                priority_request_id=request_id,
                priority_group=group,
                priority_requested_at=utc_now_iso(),
                priority_request_status="submitted",
                priority_hold_version=1,
            )

            # Listener Firebase juga melihat node ini; ID yang sama tidak
            # akan masuk dua kali ke antrean lokal.
            enqueue_command(request_id, command_data)

            log("=" * 58)
            log("[GPS] AMBULANS MEMENUHI SYARAT PRIORITAS")
            log(f"[GPS] UID                : {uid}")
            log(f"[GPS] Traffic Light      : {traffic_light}")
            log(f"[GPS] Kelompok           : {group}")
            log(f"[GPS] Latitude           : {latitude:.7f}")
            log(f"[GPS] Longitude          : {longitude:.7f}")
            log(f"[GPS] Jarak tersisa      : {distance_remaining:.2f} m")
            # Tanda + / - dan tiga desimal memperlihatkan selisih waktu asli.
            log(f"[GPS] Umur data          : {age_seconds:+.3f} detik")
            log(f"[GPS] Command ID         : {request_id}")
            log("[GPS] Perintah PRIORITY dibuat di Firebase.")
            log("=" * 58)

        except Exception as error:
            log(f"[GPS] Gagal membuat PRIORITY untuk UID {uid}: {error}")


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
    group: str | None = None,
) -> dict[str, Any]:
    """Kirim satu perintah dan tunggu ACK/NAK dengan ID yang sama."""
    payload: dict[str, Any] = {
        "cmd": command,
        "id": request_id,
    }

    if command in {"PRIORITY", "RELEASE"}:
        if group not in VALID_PRIORITY_GROUPS:
            raise ValueError("Kelompok harus A, B, atau C.")
        payload["group"] = group

    message = (
        json.dumps(payload, separators=(",", ":")) + "\n"
    ).encode("utf-8")

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

        if not isinstance(response, dict):
            continue
        if response.get("id") != request_id:
            continue

        response_command = response.get("cmd")
        if response_command is not None and response_command != command:
            continue

        log(f"[ESP32 -> SERIAL] Balasan terverifikasi: {response}")
        return response

    raise TimeoutError(
        f"Tidak menerima ACK/NAK untuk {command} "
        f"(request_id={request_id})."
    )


# ============================================================
# PEMROSESAN PERINTAH FIREBASE
# ============================================================


def reject_firebase_command(
    command_ref: Any,
    response_ref: Any,
    request_id: str,
    command: str,
    message: str,
) -> None:
    response_ref.set(
        {
            "type": "NAK",
            "id": request_id,
            "cmd": command or None,
            "message": message,
            "received_at": utc_now_iso(),
        }
    )
    command_ref.update(
        {
            "status": "rejected",
            "completed_at": utc_now_iso(),
        }
    )
    log(f"[BACKEND] Perintah {request_id} ditolak: {message}")


def process_firebase_command(
    connection: serial.Serial,
    commands_ref: Any,
    responses_ref: Any,
    esp32_status_ref: Any,
    ambulance_sessions_ref: Any,
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
        reject_firebase_command(
            command_ref,
            response_ref,
            request_id,
            command,
            "UNSUPPORTED_COMMAND",
        )
        return

    group: str | None = None
    if command in {"PRIORITY", "RELEASE"}:
        group = str(command_data.get("group", "")).strip().upper()
        if group not in VALID_PRIORITY_GROUPS:
            reject_firebase_command(
                command_ref,
                response_ref,
                request_id,
                command,
                "INVALID_OR_MISSING_GROUP",
            )
            update_session_simulation(
                ambulance_sessions_ref,
                command_data.get("ambulance_uid"),
                priority_request_status="rejected",
                priority_error="INVALID_OR_MISSING_GROUP",
            )
            return

    try:
        response = send_serial_command(
            connection=connection,
            command=command,
            request_id=request_id,
            group=group,
        )

        response_with_time = {
            **response,
            "received_at": utc_now_iso(),
        }
        response_ref.set(response_with_time)

        if response.get("type") == "ACK":
            command_ref.update(
                {
                    "status": "completed",
                    "completed_at": utc_now_iso(),
                }
            )

            if command == "STATUS":
                publish_esp32_status(esp32_status_ref, response)

            elif command == "PRIORITY":
                command_ref.update({"accepted_group": group})
                update_session_simulation(
                    ambulance_sessions_ref,
                    command_data.get("ambulance_uid"),
                    priority_request_status="queued",
                    priority_ack_at=utc_now_iso(),
                    priority_ack_group=group,
                )
                log(f"[BACKEND] PRIORITY kelompok {group} diterima ESP32.")

            elif command == "RELEASE":
                update_session_simulation(
                    ambulance_sessions_ref,
                    command_data.get("ambulance_uid"),
                    priority_release_status="queued",
                    priority_release_ack_at=utc_now_iso(),
                    priority_release_esp32_status=response.get(
                        "status", "ACK"
                    ),
                )
                log(
                    f"[BACKEND] RELEASE kelompok {group} diterima ESP32: "
                    f"{response.get('status', 'ACK')}"
                )

            log(f"[BACKEND] Perintah {request_id} berhasil diproses.")

        else:
            command_ref.update(
                {
                    "status": "rejected",
                    "completed_at": utc_now_iso(),
                }
            )

            if command == "PRIORITY":
                update_session_simulation(
                    ambulance_sessions_ref,
                    command_data.get("ambulance_uid"),
                    priority_request_status="rejected",
                    priority_error=response.get("message", "ESP32_NAK"),
                )
            elif command == "RELEASE":
                update_session_simulation(
                    ambulance_sessions_ref,
                    command_data.get("ambulance_uid"),
                    priority_release_status="rejected",
                    priority_release_error=response.get("message", "ESP32_NAK"),
                )

            log(f"[BACKEND] ESP32 menolak perintah {request_id}: {response}")

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

        if command == "PRIORITY":
            update_session_simulation(
                ambulance_sessions_ref,
                command_data.get("ambulance_uid"),
                priority_request_status="failed",
                priority_error="SERIAL_TIMEOUT",
            )
        elif command == "RELEASE":
            update_session_simulation(
                ambulance_sessions_ref,
                command_data.get("ambulance_uid"),
                priority_release_status="failed",
                priority_release_error="SERIAL_TIMEOUT",
            )

        log(f"[BACKEND] {error}")

    except serial.SerialException as error:
        failure = {
            "type": "SERIAL_ERROR",
            "id": request_id,
            "cmd": command,
            "message": str(error),
            "received_at": utc_now_iso(),
        }
        response_ref.set(failure)
        command_ref.update(
            {"status": "failed", "completed_at": utc_now_iso()}
        )

        if command == "PRIORITY":
            update_session_simulation(
                ambulance_sessions_ref,
                command_data.get("ambulance_uid"),
                priority_request_status="failed",
                priority_error="SERIAL_ERROR",
            )
        elif command == "RELEASE":
            update_session_simulation(
                ambulance_sessions_ref,
                command_data.get("ambulance_uid"),
                priority_release_status="failed",
                priority_release_error="SERIAL_ERROR",
            )

        log(f"[SERIAL] Kesalahan komunikasi: {error}")

    except Exception as error:
        failure = {
            "type": "BACKEND_ERROR",
            "id": request_id,
            "cmd": command,
            "message": str(error),
            "received_at": utc_now_iso(),
        }
        try:
            response_ref.set(failure)
            command_ref.update(
                {"status": "failed", "completed_at": utc_now_iso()}
            )
        except Exception as write_error:
            log(f"[FIREBASE] Gagal menyimpan error: {write_error}")

        log(f"[BACKEND] Kesalahan pemrosesan perintah: {error}")


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
    log("Perintah aktif: PING, STATUS, PRIORITY, RELEASE")
    log("Pemantauan otomatis: AMBULANCE GPS + GREEN HOLD")
    log("=" * 58)

    try:
        if not SERVICE_ACCOUNT_FILE.is_file():
            raise FileNotFoundError(
                "Service Account tidak ditemukan: "
                f"{SERVICE_ACCOUNT_FILE}\n"
                "Periksa lokasi firebase-admin-key.json."
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
        ambulance_sessions_ref = db.reference("ambulance/sessions")
        esp32_status_ref = db.reference("esp32/status")
        backend_status_ref = db.reference("backend/status")

        log(
            f"[SERIAL] Membuka {SERIAL_PORT} "
            f"pada {SERIAL_BAUD_RATE} bps..."
        )
        connection = serial.Serial(
            SERIAL_PORT,
            SERIAL_BAUD_RATE,
            timeout=SERIAL_READ_TIMEOUT,
            write_timeout=1.0,
        )

        log(
            "[SERIAL] Menunggu ESP32 menyelesaikan splash dan startup "
            f"({ESP32_BOOT_WAIT_SECONDS:.1f} detik)..."
        )
        time.sleep(ESP32_BOOT_WAIT_SECONDS)
        connection.reset_input_buffer()
        log("[SERIAL] Port siap digunakan.")

        listener = commands_ref.listen(firebase_commands_callback)
        log("[FIREBASE] Listener aktif pada /commands.")

        write_backend_status(
            backend_status_ref,
            online=True,
            serial_connected=True,
            serial_port=SERIAL_PORT,
            serial_baud_rate=SERIAL_BAUD_RATE,
            allowed_commands=["PING", "STATUS", "PRIORITY", "RELEASE"],
            ambulance_monitoring=True,
            green_hold_enabled=True,
            ambulance_scan_interval_seconds=AMBULANCE_SCAN_INTERVAL_SECONDS,
            priority_trigger_distance_m=PRIORITY_TRIGGER_DISTANCE_METERS,
            max_gps_age_seconds=MAX_GPS_DATA_AGE_SECONDS,
            max_future_clock_skew_seconds=MAX_FUTURE_CLOCK_SKEW_SECONDS,
            message=(
                "Backend aktif, memantau GPS ambulans, "
                "dan mengelola PRIORITY/RELEASE."
            ),
        )

        next_status_poll = 0.0
        next_ambulance_scan = 0.0
        next_heartbeat = (
            time.monotonic() + BACKEND_HEARTBEAT_INTERVAL_SECONDS
        )

        log("")
        log("BACKEND AKTIF.")
        log("Pemantauan GPS ambulans aktif.")
        log(
            "GREEN HOLD akan dilepas saat ARRIVED / pembatalan / "
            "sesi selesai / GPS stale."
        )
        log("Umur GPS ditampilkan apa adanya (termasuk nilai negatif).")
        log("Menunggu perintah dari Firebase.")
        log("Tekan CTRL+C untuk menghentikan.")
        log("")

        while True:
            read_serial_line(connection)
            now = time.monotonic()

            # Periksa GPS dan status sesi setiap detik.
            if now >= next_ambulance_scan:
                monitor_ambulance_sessions(
                    ambulance_sessions_ref,
                    commands_ref,
                )
                next_ambulance_scan = (
                    time.monotonic() + AMBULANCE_SCAN_INTERVAL_SECONDS
                )

            # Dahulukan perintah Firebase / hasil pemantauan GPS.
            try:
                request_id, command_data = command_queue.get_nowait()
            except queue.Empty:
                request_id = ""
                command_data = {}

            if request_id:
                try:
                    process_firebase_command(
                        connection=connection,
                        commands_ref=commands_ref,
                        responses_ref=responses_ref,
                        esp32_status_ref=esp32_status_ref,
                        ambulance_sessions_ref=ambulance_sessions_ref,
                        request_id=request_id,
                        command_data=command_data,
                    )
                finally:
                    command_queue.task_done()
                continue

            now = time.monotonic()

            # Poll STATUS ESP32 secara berkala.
            if now >= next_status_poll:
                poll_id = f"poll-{uuid.uuid4().hex[:12]}"
                try:
                    response = send_serial_command(
                        connection=connection,
                        command="STATUS",
                        request_id=poll_id,
                    )
                    if response.get("type") == "ACK":
                        publish_esp32_status(esp32_status_ref, response)
                    else:
                        log(
                            "[BACKEND] Poll STATUS menerima NAK: "
                            f"{response}"
                        )
                except TimeoutError as error:
                    log(
                        "[BACKEND] Gagal memperbarui status ESP32: "
                        f"{error}"
                    )

                next_status_poll = (
                    time.monotonic() + STATUS_POLL_INTERVAL_SECONDS
                )
                continue

            # Heartbeat backend.
            if now >= next_heartbeat:
                write_backend_status(
                    backend_status_ref,
                    online=True,
                    serial_connected=connection.is_open,
                    serial_port=SERIAL_PORT,
                    message=(
                        "Backend aktif, listener berjalan, "
                        "GPS monitoring dan GREEN HOLD aktif."
                    ),
                )
                next_heartbeat = (
                    time.monotonic() + BACKEND_HEARTBEAT_INTERVAL_SECONDS
                )

    except KeyboardInterrupt:
        log("\n[BACKEND] Penghentian diminta melalui CTRL+C.")

    except serial.SerialException as error:
        log(f"[SERIAL] Gagal menggunakan {SERIAL_PORT}: {error}")
        log(
            "Pastikan ESP32 terhubung dan Serial Monitor Arduino IDE ditutup."
        )

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
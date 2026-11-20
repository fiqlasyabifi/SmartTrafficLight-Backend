
"""
GPS Monitor - Smart Traffic Light Rawamangun

Fungsi:
- Membaca posisi ambulans dari Firebase.
- Membaca konfigurasi traffic light.
- Menghitung jarak geografis ambulans ke tujuan.
- Memantau status dan waktu pembaruan GPS.

Mode:
MONITORING ONLY.
Tidak mengirim perintah ke ESP32.
"""

import time
import math
from pathlib import Path
from datetime import datetime, timezone

import firebase_admin
from firebase_admin import credentials, db


# =====================================================
# KONFIGURASI FIREBASE
# Mengikuti konfigurasi backend.py
# =====================================================

SERVICE_ACCOUNT_FILE = Path(
    r"C:\STL-credentials\firebase-admin-key.json"
)

DATABASE_URL = (
    "https://smart-traffic-light-rawa-d711e-default-rtdb."
    "asia-southeast1.firebasedatabase.app/"
)

POLL_INTERVAL_SECONDS = 1
GPS_STALE_SECONDS = 5

EARTH_RADIUS_METERS = 6_371_000.0


# =====================================================
# INISIALISASI FIREBASE
# =====================================================

def initialize_firebase():

    if not SERVICE_ACCOUNT_FILE.is_file():
        raise FileNotFoundError(
            f"Service Account tidak ditemukan: "
            f"{SERVICE_ACCOUNT_FILE}"
        )

    credential = credentials.Certificate(
        str(SERVICE_ACCOUNT_FILE)
    )

    firebase_admin.initialize_app(
        credential,
        {"databaseURL": DATABASE_URL}
    )

    print("[FIREBASE] Berhasil terhubung.")
    return db.reference("")


# =====================================================
# PERHITUNGAN JARAK HAVERSINE
# =====================================================

def calculate_distance(
    latitude1,
    longitude1,
    latitude2,
    longitude2
):
    lat1 = math.radians(latitude1)
    lat2 = math.radians(latitude2)

    delta_lat = math.radians(latitude2 - latitude1)
    delta_lon = math.radians(longitude2 - longitude1)

    a = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1)
        * math.cos(lat2)
        * math.sin(delta_lon / 2) ** 2
    )

    central_angle = 2 * math.asin(
        math.sqrt(max(0.0, min(1.0, a)))
    )

    return EARTH_RADIUS_METERS * central_angle


# =====================================================
# FORMAT ANGKA
# =====================================================

def format_number(value, decimal=2):

    try:
        return f"{float(value):.{decimal}f}"
    except (TypeError, ValueError):
        return "Tidak tersedia"


# =====================================================
# MEMBACA DATA GPS AMBULANS
# =====================================================

def monitor_ambulance(root):

    sessions = (
        root.child("ambulance/sessions").get()
        or {}
    )

    map_config = root.child("map_config").get() or {}

    traffic_lights = map_config.get(
        "traffic_lights", {}
    )

    approach_points = map_config.get(
        "approach_reference_points", {}
    )

    if not sessions:
        print("[GPS] Belum ada sesi ambulans.")
        return

    for uid, session in sessions.items():

        simulation = session.get("simulation") or {}
        live = session.get("live") or {}

        if not live:
            continue

        target_id = (
            live.get("target_traffic_light")
            or simulation.get("target_traffic_light")
        )

        latitude = live.get("latitude")
        longitude = live.get("longitude")

        if latitude is None or longitude is None:
            print(f"[GPS] Koordinat tidak tersedia untuk {uid}.")
            continue

        latitude = float(latitude)
        longitude = float(longitude)

        light = traffic_lights.get(target_id, {})
        approach = light.get("approach")

        destination = approach_points.get(
            approach, {}
        )

        target_lat = destination.get("latitude")
        target_lon = destination.get("longitude")

        # Jarak garis lurus dari koordinat GPS ke tujuan.
        geographic_distance = None

        if target_lat is not None and target_lon is not None:

            geographic_distance = calculate_distance(
                latitude,
                longitude,
                float(target_lat),
                float(target_lon)
            )

        # Jarak yang dihitung oleh Android.
        reported_distance = live.get(
            "distance_remaining_m"
        )

        # Memeriksa umur data GPS.
        updated_at = live.get("updated_at")
        data_age = None

        if isinstance(updated_at, (int, float)):
            data_age = max(
                0.0,
                time.time() - (updated_at / 1000.0)
            )

        simulation_status = simulation.get(
            "status", "unknown"
        )

        gps_status = live.get(
            "status", "UNKNOWN"
        )

        if (
            simulation_status == "running"
            and (
                data_age is None
                or data_age > GPS_STALE_SECONDS
            )
        ):
            display_status = "STALE - GPS TIDAK DIPERBARUI"

        elif simulation_status == "completed":
            display_status = f"{gps_status} - SIMULASI SELESAI"

        else:
            display_status = gps_status

        # Menampilkan hasil.
        print("\n" + "=" * 55)
        print("SMART TRAFFIC LIGHT - GPS MONITOR")
        print("=" * 55)

        print(f"Firebase UID      : {uid}")
        print(f"Traffic Light     : {target_id}")
        print(f"Kelompok MCU      : {light.get('group', '-')}")
        print(f"Arah pendekatan   : {approach}")

        print("-" * 55)

        print(f"Latitude          : {latitude:.7f}")
        print(f"Longitude         : {longitude:.7f}")

        print("-" * 55)

        print(
            "Jarak Android     : "
            f"{format_number(reported_distance)} meter"
        )

        print(
            "Jarak Haversine   : "
            f"{format_number(geographic_distance)} meter"
        )

        print(
            "Jarak ditempuh    : "
            f"{format_number(live.get('distance_travelled_m'))} meter"
        )

        print(
            "Progress          : "
            f"{format_number(float(live.get('progress', 0)) * 100)}%"
        )

        print("-" * 55)

        print(
            f"Status GPS        : {display_status}"
        )

        print(
            f"Status simulasi   : {simulation_status}"
        )

        if data_age is not None:
            print(
                f"Umur data         : {data_age:.1f} detik"
            )

        print("=" * 55)


# =====================================================
# PROGRAM UTAMA
# =====================================================

def main():

    print("=" * 55)
    print("SMART TRAFFIC LIGHT RAWAMANGUN")
    print("VIRTUAL GPS MONITOR")
    print("MODE: MONITORING ONLY")
    print("=" * 55)

    root = initialize_firebase()

    print("\n[GPS] Pemantauan dimulai.")
    print("[GPS] Tekan CTRL+C untuk menghentikan.\n")

    try:

        while True:

            monitor_ambulance(root)

            time.sleep(POLL_INTERVAL_SECONDS)

    except KeyboardInterrupt:

        print("\n[GPS] Pemantauan dihentikan.")

    except Exception as error:

        print(f"\n[ERROR] {error}")


if __name__ == "__main__":
    main()
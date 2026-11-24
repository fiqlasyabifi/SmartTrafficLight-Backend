import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

# Configure FFmpeg before importing OpenCV. This is useful for HLS streams.
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "rw_timeout;10000000|user_agent;Mozilla/5.0",
)

import cv2
import numpy as np
import firebase_admin
from firebase_admin import credentials, db

from services.webcam_detector import WebcamDetector


ROOT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = ROOT_DIR / "config" / "webcam_roi.json"
FIREBASE_NODE = "/webcam_analysis/latest"
WINDOW_NAME = "Smart Traffic Light - Video Analysis"

SOURCE_OPTIONS = {
    "W": {
        "type": "webcam",
        "name": "Webcam Laptop",
        "roi_key": "roi_webcam",
    },
    "C": {
        "type": "cctv",
        "name": "CCTV Gerbang Pemuda C03",
        "roi_key": "roi_cctv",
    },
}


def current_time():
    return datetime.now(timezone.utc).isoformat()


def load_config():
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"File konfigurasi tidak ditemukan: {CONFIG_PATH}"
        )

    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        config = json.load(file)

    # Separate ROI storage for each video source.
    config.setdefault("roi_webcam", [])
    config.setdefault("roi_cctv", [])
    return config


def save_config(config):
    """Save configuration, including the currently selected source ROI."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG_PATH.open("w", encoding="utf-8") as file:
        json.dump(config, file, indent=4, ensure_ascii=False)


def resolve_path(path_value):
    path = Path(path_value)
    return path if path.is_absolute() else ROOT_DIR / path


def choose_source():
    """Ask the user to choose W (webcam) or C (public CCTV)."""
    print("\n==============================================")
    print("       SMART TRAFFIC LIGHT RAWAMANGUN")
    print("==============================================")
    print("PILIH SUMBER VIDEO:")
    print("  [W] Webcam Laptop")
    print("  [C] CCTV Publik - Gerbang Pemuda C03")
    print("  [Q] Keluar")

    while True:
        choice = input("Pilih sumber [W/C/Q]: ").strip().upper()
        if choice in SOURCE_OPTIONS:
            return SOURCE_OPTIONS[choice]
        if choice == "Q":
            return None
        print("Pilihan tidak valid. Masukkan W, C, atau Q.")


def initialize_firebase(config):
    credential_path = resolve_path(config["firebase_service_account"])
    if not credential_path.exists():
        raise FileNotFoundError(
            f"Kredensial Firebase tidak ditemukan: {credential_path}"
        )

    try:
        firebase_admin.get_app()
    except ValueError:
        certificate = credentials.Certificate(str(credential_path))
        firebase_admin.initialize_app(
            certificate,
            {"databaseURL": config["firebase_database_url"]},
        )

    reference = db.reference(FIREBASE_NODE)
    print(f"Firebase terhubung. Node: {FIREBASE_NODE}")
    return reference


def publish_status(reference, status, online, source_type, source_name, extra=None):
    payload = {
        "source": "laptop_webcam" if source_type == "webcam" else "public_cctv",
        "source_type": source_type,
        "source_name": source_name,
        "status": status,
        "online": online,
        "updated_at": current_time(),
        "timestamp": int(time.time()),
    }
    if extra:
        payload.update(extra)
    reference.update(payload)


def open_video_source(config, source_type):
    if source_type == "webcam":
        camera_index = int(config.get("camera_index", 0))
        print(f"Membuka webcam laptop (index {camera_index})...")
        if os.name == "nt":
            camera = cv2.VideoCapture(camera_index, cv2.CAP_DSHOW)
        else:
            camera = cv2.VideoCapture(camera_index)

        camera.set(
            cv2.CAP_PROP_FRAME_WIDTH,
            int(config.get("capture_width", 1280)),
        )
        camera.set(
            cv2.CAP_PROP_FRAME_HEIGHT,
            int(config.get("capture_height", 720)),
        )
    else:
        stream_url = config.get("stream_url", "").strip()
        if not stream_url:
            raise ValueError("stream_url CCTV belum diisi di webcam_roi.json")
        print("Membuka CCTV Gerbang Pemuda C03 melalui HLS...")
        camera = cv2.VideoCapture(stream_url, cv2.CAP_FFMPEG)

    if not camera.isOpened():
        camera.release()
        raise RuntimeError(f"Gagal membuka sumber video: {source_type}")

    print("Sumber video berhasil dibuka.")
    return camera


def empty_analysis():
    return {
        "counts": {
            "car": 0,
            "motorcycle": 0,
            "bus": 0,
            "truck": 0,
        },
        "total_vehicles": 0,
        "detections": [],
        "roi_polygon": None,
    }


def validate_roi(points):
    if len(points) < 3:
        return False, "ROI harus memiliki minimal 3 titik."

    for point in points:
        if len(point) != 2:
            return False, "Format titik ROI tidak valid."
        x, y = point
        if not (0.0 <= float(x) <= 1.0 and 0.0 <= float(y) <= 1.0):
            return False, "Koordinat ROI harus berada pada rentang 0 sampai 1."

    polygon = np.array(
        [[float(x), float(y)] for x, y in points],
        dtype=np.float32,
    )
    if abs(float(cv2.contourArea(polygon))) < 0.001:
        return False, "Bentuk ROI terlalu kecil atau titik-titiknya segaris."

    return True, "OK"


def draw_roi_editor(display, points, width, height):
    """Draw the user's in-progress polygon in magenta, with no yellow overlay."""
    pixel_points = [
        (
            int(round(float(x) * (width - 1))),
            int(round(float(y) * (height - 1))),
        )
        for x, y in points
    ]

    magenta = (255, 0, 255)  # OpenCV BGR

    if len(pixel_points) >= 2:
        polygon = np.array(pixel_points, dtype=np.int32).reshape((-1, 1, 2))
        cv2.polylines(
            display,
            [polygon],
            isClosed=len(pixel_points) >= 3,
            color=magenta,
            thickness=2,
            lineType=cv2.LINE_AA,
        )

    for index, point in enumerate(pixel_points, start=1):
        cv2.circle(display, point, 6, magenta, -1, cv2.LINE_AA)
        cv2.putText(
            display,
            str(index),
            (point[0] + 8, point[1] - 8),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            magenta,
            2,
            cv2.LINE_AA,
        )

    instructions = [
        "EDIT ROI - click kiri: tambah titik",
        "Klik kanan: hapus titik terakhir",
        "S: simpan | R: reset | C: batal",
        f"Jumlah titik: {len(points)} (minimal 3)",
    ]
    for index, text in enumerate(instructions):
        cv2.putText(
            display,
            text,
            (15, 25 + index * 25),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.58,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )


def main():
    selected = choose_source()
    if selected is None:
        print("Program dibatalkan.")
        return

    source_type = selected["type"]
    source_name = selected["name"]
    roi_key = selected["roi_key"]

    camera = None
    firebase_ref = None
    process_status = "stopped"

    # Shared state used by the OpenCV mouse callback.
    roi_editor = {
        "active": False,
        "points": [],
        "frame_width": 0,
        "frame_height": 0,
    }

    try:
        config = load_config()
        active_roi = [point[:] for point in config.get(roi_key, [])]

        print(f"\nSumber dipilih: {source_name}")
        print(f"ROI tersimpan untuk sumber ini: {len(active_roi)} titik")

        firebase_ref = initialize_firebase(config)
        publish_status(
            firebase_ref,
            "starting",
            True,
            source_type,
            source_name,
        )

        model_path = resolve_path(config["model_path"])
        if not model_path.exists():
            raise FileNotFoundError(f"Model YOLO tidak ditemukan: {model_path}")

        print("Memuat model YOLO...")
        detector = WebcamDetector(
            model_path=str(model_path),
            roi_points=active_roi,
            confidence_threshold=float(config.get("confidence_threshold", 0.25)),
            iou_threshold=float(config.get("iou_threshold", 0.45)),
            image_size=int(config.get("image_size", 640)),
            device=config.get("device"),
        )

        camera = open_video_source(config, source_type)
        show_preview = bool(config.get("show_preview", True))

        if not show_preview:
            raise RuntimeError(
                "show_preview harus true agar editor ROI dengan mouse dapat digunakan."
            )

        cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_AUTOSIZE)

        def mouse_callback(event, x, y, flags, parameter):
            if not roi_editor["active"]:
                return

            width = roi_editor["frame_width"]
            height = roi_editor["frame_height"]
            if width <= 1 or height <= 1:
                return

            if event == cv2.EVENT_LBUTTONDOWN:
                normalized_x = min(max(x / (width - 1), 0.0), 1.0)
                normalized_y = min(max(y / (height - 1), 0.0), 1.0)
                roi_editor["points"].append([normalized_x, normalized_y])
                print(f"Titik ROI ditambahkan: {len(roi_editor['points'])}")

            elif event == cv2.EVENT_RBUTTONDOWN and roi_editor["points"]:
                roi_editor["points"].pop()
                print("Titik ROI terakhir dihapus.")

        cv2.setMouseCallback(WINDOW_NAME, mouse_callback)

        publish_interval = max(
            0.2,
            float(config.get("publish_interval_seconds", 1.0)),
        )
        last_publish = 0.0
        failed_reads = 0

        print("\nVideo siap.")
        print("Tekan E untuk menggambar ulang ROI.")
        print("Klik kiri tambah titik; klik kanan hapus titik terakhir.")
        print("Tekan S untuk menyimpan, R untuk reset, C untuk batal, Q untuk keluar.")

        publish_status(
            firebase_ref,
            "running",
            True,
            source_type,
            source_name,
            {"roi_configured": len(active_roi) >= 3},
        )

        while True:
            success, frame = camera.read()
            if not success or frame is None:
                failed_reads += 1
                if failed_reads % 5 == 0:
                    print(f"Belum menerima frame video ({failed_reads}/30)...")
                if failed_reads >= 30:
                    raise RuntimeError(
                        "30 kali gagal membaca frame. Periksa koneksi webcam/CCTV."
                    )
                time.sleep(0.1)
                key = cv2.waitKey(1) & 0xFF
                if key in (ord("q"), ord("Q")):
                    break
                continue

            failed_reads = 0
            height, width = frame.shape[:2]
            roi_editor["frame_width"] = width
            roi_editor["frame_height"] = height

            if roi_editor["active"]:
                # Show the clean camera image while editing; hide the saved ROI.
                analysis = empty_analysis()
                display = frame.copy()
                draw_roi_editor(
                    display,
                    roi_editor["points"],
                    width,
                    height,
                )
            elif len(detector.roi_points) >= 3:
                analysis = detector.detect(frame)
                display = detector.draw(frame, analysis)
                cv2.putText(
                    display,
                    "E: edit ROI | Q: keluar",
                    (15, display.shape[0] - 20),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.6,
                    (255, 255, 255),
                    2,
                    cv2.LINE_AA,
                )
            else:
                analysis = empty_analysis()
                display = frame.copy()
                cv2.putText(
                    display,
                    "ROI belum dibuat - tekan E untuk menggambar ROI",
                    (15, 35),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.62,
                    (255, 255, 255),
                    2,
                    cv2.LINE_AA,
                )
                cv2.putText(
                    display,
                    "Q: keluar",
                    (15, display.shape[0] - 20),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.6,
                    (255, 255, 255),
                    2,
                    cv2.LINE_AA,
                )

            now = time.monotonic()
            if not roi_editor["active"] and now - last_publish >= publish_interval:
                counts = analysis["counts"]
                roi_configured = len(detector.roi_points) >= 3
                payload = {
                    "source": "laptop_webcam" if source_type == "webcam" else "public_cctv",
                    "source_type": source_type,
                    "source_name": source_name,
                    "status": "running",
                    "online": True,
                    "updated_at": current_time(),
                    "timestamp": int(time.time()),
                    "roi_configured": roi_configured,
                    "counts": counts,
                    "total_vehicles": analysis["total_vehicles"],
                    "frame": {
                        "width": int(width),
                        "height": int(height),
                    },
                    "roi_points": detector.roi_points,
                    "counting_method": "bottom_center_inside_polygon_roi",
                }

                try:
                    firebase_ref.set(payload)
                    print(
                        f"[{source_type.upper()}] Firebase updated | "
                        f"Mobil={counts['car']} | "
                        f"Motor={counts['motorcycle']} | "
                        f"Bus={counts['bus']} | "
                        f"Truk={counts['truck']} | "
                        f"Total={analysis['total_vehicles']}"
                    )
                except Exception as firebase_error:
                    print("Gagal mengirim data Firebase:", firebase_error)

                last_publish = now

            cv2.imshow(WINDOW_NAME, display)
            key = cv2.waitKey(1) & 0xFF

            # Start a fresh polygon. The saved ROI remains active until S is pressed.
            if key in (ord("e"), ord("E")) and not roi_editor["active"]:
                roi_editor["active"] = True
                roi_editor["points"] = []
                print("\nMODE EDIT ROI. Gambar polygon baru; S untuk mengganti ROI tersimpan.")

            elif key in (ord("s"), ord("S")) and roi_editor["active"]:
                new_points = [point[:] for point in roi_editor["points"]]
                valid, message = validate_roi(new_points)
                if not valid:
                    print("ROI belum disimpan:", message)
                else:
                    # Update only the chosen source's ROI.
                    config[roi_key] = new_points
                    save_config(config)
                    detector.roi_points = [point[:] for point in new_points]
                    active_roi = [point[:] for point in new_points]
                    roi_editor["active"] = False
                    roi_editor["points"] = []
                    last_publish = 0.0
                    print(f"ROI {source_name} berhasil disimpan ({len(new_points)} titik).")
                    print(f"File konfigurasi: {CONFIG_PATH}")

            elif key in (ord("r"), ord("R")) and roi_editor["active"]:
                roi_editor["points"] = []
                print("Titik ROI yang sedang digambar direset.")

            elif key in (ord("c"), ord("C")) and roi_editor["active"]:
                roi_editor["active"] = False
                roi_editor["points"] = []
                print("Edit dibatalkan. ROI tersimpan sebelumnya tetap digunakan.")

            elif key in (ord("q"), ord("Q")):
                print("Program video dihentikan oleh pengguna.")
                break

    except KeyboardInterrupt:
        print("\nProgram dihentikan oleh pengguna.")

    except Exception as error:
        process_status = "error"
        print("\nTerjadi kesalahan:", error)
        if firebase_ref is not None:
            try:
                publish_status(
                    firebase_ref,
                    "error",
                    False,
                    source_type,
                    source_name,
                    {"error": str(error)},
                )
            except Exception:
                pass

    finally:
        if camera is not None:
            camera.release()
        cv2.destroyAllWindows()

        if firebase_ref is not None:
            try:
                publish_status(
                    firebase_ref,
                    "stopped" if process_status != "error" else "error",
                    False,
                    source_type,
                    source_name,
                )
            except Exception as error:
                print("Gagal memperbarui status Firebase:", error)

        print("Proses selesai.")


if __name__ == "__main__":
    main()
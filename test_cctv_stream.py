import os
import time

# Atur timeout FFmpeg sebelum mengimpor OpenCV.
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
    "rw_timeout;10000000|user_agent;Mozilla/5.0"
)

import cv2


STREAM_URL = (
    "https://dki-jkt.balitower.co.id:7028/"
    "502160_JKP_POLDA_JL.-GERBANG-PEMUDA-C03_CCTV-02/"
    "index.m3u8"
)


def main():
    print("====================================", flush=True)
    print("TES CCTV GERBANG PEMUDA C03", flush=True)
    print("====================================", flush=True)

    print("OpenCV:", cv2.__version__, flush=True)

    print("\nMenghubungkan ke streaming CCTV...", flush=True)

    camera = None

    try:
        params = [
            cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 15000,
            cv2.CAP_PROP_READ_TIMEOUT_MSEC, 10000
        ]

        camera = cv2.VideoCapture(
            STREAM_URL,
            cv2.CAP_FFMPEG,
            params
        )

    except (TypeError, cv2.error) as error:
        print("Mode timeout tidak tersedia:", error, flush=True)
        print("Mencoba metode koneksi biasa...", flush=True)

        camera = cv2.VideoCapture(
            STREAM_URL,
            cv2.CAP_FFMPEG
        )

    if not camera.isOpened():
        print("\nGAGAL membuka stream CCTV.", flush=True)
        return

    print("Stream berhasil dibuka.", flush=True)

    try:
        print(
            "Backend:",
            camera.getBackendName(),
            flush=True
        )
    except cv2.error:
        print("Backend tidak dapat diidentifikasi.", flush=True)

    print("\nMenunggu frame CCTV...", flush=True)

    frame_count = 0
    failed_count = 0

    cv2.namedWindow(
        "CCTV GERBANG PEMUDA C03",
        cv2.WINDOW_NORMAL
    )

    while True:
        success, frame = camera.read()

        if not success or frame is None:
            failed_count += 1

            print(
                f"Frame gagal diterima: {failed_count}/10",
                flush=True
            )

            if failed_count >= 10:
                print(
                    "\nTidak berhasil menerima frame CCTV.",
                    flush=True
                )
                break

            time.sleep(0.3)
            continue

        failed_count = 0
        frame_count += 1

        if frame_count == 1:
            print("\nBERHASIL menerima gambar CCTV!", flush=True)
            print(
                "Resolusi:",
                frame.shape[1],
                "x",
                frame.shape[0],
                flush=True
            )
            print(
                "Kecerahan rata-rata:",
                round(float(frame.mean()), 2),
                flush=True
            )

        cv2.imshow(
            "CCTV GERBANG PEMUDA C03",
            frame
        )

        key = cv2.waitKey(20) & 0xFF

        if key == ord("q"):
            print("Pengujian dihentikan.", flush=True)
            break

    camera.release()
    cv2.destroyAllWindows()

    print("\nTotal frame diterima:", frame_count, flush=True)
    print("Tes selesai.", flush=True)


if __name__ == "__main__":
    main()
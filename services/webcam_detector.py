import cv2
import numpy as np
from ultralytics import YOLO


VEHICLE_CLASSES = {
    "car": "car",
    "mobil": "car",
    "motorcycle": "motorcycle",
    "motorbike": "motorcycle",
    "motor": "motorcycle",
    "sepeda motor": "motorcycle",
    "bus": "bus",
    "truck": "truck",
    "truk": "truck",
}


class WebcamDetector:
    def __init__(
        self,
        model_path,
        roi_points=None,
        confidence_threshold=0.25,
        iou_threshold=0.45,
        image_size=640,
        device=None,
    ):
        self.model = YOLO(model_path)
        self.roi_points = roi_points or []
        self.confidence_threshold = confidence_threshold
        self.iou_threshold = iou_threshold
        self.image_size = image_size
        self.device = device

        print("Model YOLO berhasil dimuat.")
        print(f"Jumlah titik ROI aktif: {len(self.roi_points)}")

    @staticmethod
    def canonical_class(class_name):
        return VEHICLE_CLASSES.get(str(class_name).strip().lower())

    def get_class_name(self, class_id):
        names = self.model.names
        if isinstance(names, dict):
            return str(names.get(class_id, class_id))
        if 0 <= class_id < len(names):
            return str(names[class_id])
        return str(class_id)

    def get_roi_polygon(self, width, height):
        if len(self.roi_points) < 3:
            return None

        points = []
        for x, y in self.roi_points:
            px = int(round(float(x) * (width - 1)))
            py = int(round(float(y) * (height - 1)))
            points.append([px, py])

        return np.array(points, dtype=np.int32)

    def detect(self, frame):
        height, width = frame.shape[:2]
        roi_polygon = self.get_roi_polygon(width, height)

        counts = {
            "car": 0,
            "motorcycle": 0,
            "bus": 0,
            "truck": 0,
        }
        detections = []

        options = {
            "conf": self.confidence_threshold,
            "iou": self.iou_threshold,
            "imgsz": self.image_size,
            "verbose": False,
        }
        if self.device is not None:
            options["device"] = self.device

        results = self.model.predict(source=frame, **options)
        if not results or results[0].boxes is None:
            return {
                "counts": counts,
                "total_vehicles": 0,
                "detections": detections,
                "roi_polygon": roi_polygon,
            }

        for box in results[0].boxes:
            coordinates = box.xyxy[0].cpu().numpy()
            x1, y1, x2, y2 = [int(round(float(value))) for value in coordinates]
            class_id = int(box.cls[0].item())
            confidence = float(box.conf[0].item())
            class_name = self.get_class_name(class_id)
            vehicle_type = self.canonical_class(class_name)

            if vehicle_type is None:
                continue

            # Bottom Center = tengah sisi bawah bounding box.
            bottom_x = int(round((x1 + x2) / 2))
            bottom_y = y2
            inside_roi = False

            if roi_polygon is not None:
                inside_roi = (
                    cv2.pointPolygonTest(
                        roi_polygon,
                        (float(bottom_x), float(bottom_y)),
                        False,
                    ) >= 0
                )

            detections.append({
                "class_name": class_name,
                "vehicle_type": vehicle_type,
                "confidence": confidence,
                "box": [x1, y1, x2, y2],
                "bottom_center": [bottom_x, bottom_y],
                "inside_roi": bool(inside_roi),
            })

            if inside_roi:
                counts[vehicle_type] += 1

        return {
            "counts": counts,
            "total_vehicles": sum(counts.values()),
            "detections": detections,
            "roi_polygon": roi_polygon,
        }

    def draw(self, frame, analysis):
        output = frame.copy()
        roi_polygon = analysis["roi_polygon"]

        # Only draw the saved user-defined polygon; no default yellow box/fill.
        if roi_polygon is not None:
            cv2.polylines(
                output,
                [roi_polygon],
                True,
                (0, 255, 0),
                2,
                cv2.LINE_AA,
            )

        # Show bounding boxes only for detections counted inside the ROI.
        for detection in analysis["detections"]:
            if not detection["inside_roi"]:
                continue

            x1, y1, x2, y2 = detection["box"]
            bottom_x, bottom_y = detection["bottom_center"]
            color = (0, 255, 0)

            cv2.rectangle(output, (x1, y1), (x2, y2), color, 2)
            cv2.circle(output, (bottom_x, bottom_y), 5, (0, 0, 255), -1)
            cv2.putText(
                output,
                f'{detection["class_name"]} {detection["confidence"]:.2f}',
                (x1, max(20, y1 - 8)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                color,
                2,
            )

        counts = analysis["counts"]
        lines = [
            f'Mobil: {counts["car"]}',
            f'Motor: {counts["motorcycle"]}',
            f'Bus: {counts["bus"]}',
            f'Truk: {counts["truck"]}',
            f'Total: {analysis["total_vehicles"]}',
        ]
        for index, line in enumerate(lines):
            cv2.putText(
                output,
                line,
                (15, 30 + index * 25),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.65,
                (255, 255, 255),
                2,
            )

        return output
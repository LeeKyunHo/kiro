import os
import glob
import re
import argparse
import numpy as np
import cv2
from PIL import Image, ImageDraw

# OpenCV 윈도우 한글/특수문자 경로 안전 디코딩
_original_imread = cv2.imread
def safe_imread(filename, flags=cv2.IMREAD_COLOR):
    try:
        data = np.fromfile(filename, dtype=np.uint8)
        img = cv2.imdecode(data, flags)
        if img is not None:
            return img
    except Exception:
        pass
    return _original_imread(filename, flags)
cv2.imread = safe_imread

from nudenet import NudeDetector

# 포즈별 사타구니/고환 코어 안전 구역 [x_min, y_min, x_max, y_max] (비율)
POSE_SAFE_ANCHORS = {
    "missionary": [0.34, 0.58, 0.66, 0.84],   # 하단으로 충분히 길게 확보 (고환 차단)
    "doggystyle": [0.35, 0.52, 0.65, 0.78],
    "cowgirl":    [0.34, 0.60, 0.66, 0.86],
    "fellatio":   [0.38, 0.45, 0.64, 0.70],
    "side":       [0.35, 0.55, 0.65, 0.82],
    "default_h":  [0.34, 0.56, 0.66, 0.84]
}

def get_pose_category(code):
    if code in [40, 41, 49, 50, 140, 141, 149, 150, 164]:
        return "missionary"
    elif code in [43, 44, 74, 143, 144]:
        return "doggystyle"
    elif code in [45, 46, 47, 48, 62, 63, 69, 70, 145, 146, 147, 148, 162, 163, 165, 169, 170]:
        return "cowgirl"
    elif code in [58, 59, 155, 156, 157, 158, 159]:
        return "fellatio"
    elif code in [53, 54, 153, 154]:
        return "side"
    elif (40 <= code <= 75) or (140 <= code <= 170):
        return "default_h"
    return None

def merge_boxes(boxes):
    """인접하거나 겹치는 박스들을 하나로 합쳐 깔끔한 직사각형 생성"""
    if not boxes:
        return []
    
    # [x0, y0, x1, y1] 전체를 포함하는 경계 박스 계산
    merged = []
    boxes = sorted(boxes, key=lambda b: b[1])
    
    current = boxes[0]
    for nxt in boxes[1:]:
        # 수직 또는 수평으로 인접/중첩되는 경우 병합
        if (nxt[1] <= current[3] + 20 and 
            not (nxt[2] < current[0] - 20 or nxt[0] > current[2] + 20)):
            current = [
                min(current[0], nxt[0]),
                min(current[1], nxt[1]),
                max(current[2], nxt[2]),
                max(current[3], nxt[3])
            ]
        else:
            merged.append(current)
            current = nxt
    merged.append(current)
    return merged

def run_absolute_censor(folder_path, censor_style="black_bar"):
    folder_path = os.path.abspath(folder_path)
    if not os.path.exists(folder_path):
        print(f"[오류] 경로를 찾을 수 없습니다: {folder_path}")
        return

    print("[엔진 초기화] NudeNet 절대 방어 고환 차단 엔진 로딩 중...")
    detector = NudeDetector()

    extensions = ["*.webp", "*.png", "*.jpg"]
    image_files = []
    for ext in extensions:
        image_files.extend(glob.glob(os.path.join(folder_path, "**", ext), recursive=True))

    total = len(image_files)
    print(f"[스캔 시작] 총 {total}장의 에셋을 정밀 검사합니다.")

    processed_count = 0
    for idx, file_path in enumerate(image_files, 1):
        filename = os.path.basename(file_path)
        match = re.search(r"_(\d{3})\.", filename)
        pose_code = int(match.group(1)) if match else -1

        is_h_scene = (40 <= pose_code <= 75) or (140 <= pose_code <= 170)
        if not is_h_scene:
            continue

        try:
            raw_boxes = []
            detections = detector.detect(file_path)

            with Image.open(file_path) as img:
                img_w, img_h = img.size

                # 1. AI 감지 (초민감도 0.08 적용)
                for d in detections:
                    c_name = d["class"]
                    score = d["score"]

                    if c_name in ["FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED", "ANUS_EXPOSED"]:
                        if score >= 0.08:
                            x, y, w, h = d["box"]
                            
                            # 상반신 30% 영역 내 얼굴/목 오탐지 제외
                            if y < img_h * 0.30 and (y + h) < img_h * 0.42:
                                continue

                            # [핵심] 고환(Testicles) 차단: 아래 방향으로 높이의 35% 강제 확장
                            x0 = max(0, x - int(w * 0.08))
                            y0 = max(0, y - int(h * 0.05))
                            x1 = min(img_w, x + w + int(w * 0.08))
                            y1 = min(img_h, y + h + int(h * 0.35)) # 하단 고환/음낭 완벽 덮기

                            raw_boxes.append([x0, y0, x1, y1])

                # 2. AI가 고환이나 성기를 전혀 못 잡았을 경우의 안전망 (Safe Anchor)
                if not raw_boxes:
                    cat = get_pose_category(pose_code)
                    if cat:
                        r = POSE_SAFE_ANCHORS[cat]
                        raw_boxes.append([
                            int(img_w * r[0]),
                            int(img_h * r[1]),
                            int(img_w * r[2]),
                            int(img_h * r[3])
                        ])

                # 3. 쪼개진 박스들을 깔끔하게 병합
                final_boxes = merge_boxes(raw_boxes)

                # 4. 이미지 렌더링
                img = img.convert("RGBA")
                draw = ImageDraw.Draw(img)

                for (x0, y0, x1, y1) in final_boxes:
                    if censor_style == "black_bar":
                        draw.rectangle([x0, y0, x1, y1], fill=(0, 0, 0, 255))
                    elif censor_style == "mosaic":
                        crop_area = img.crop((x0, y0, x1, y1))
                        mw = max(1, (x1 - x0) // 10)
                        mh = max(1, (y1 - y0) // 10)
                        small = crop_area.resize((mw, mh), Image.NEAREST)
                        mosaic = small.resize((x1 - x0, y1 - y0), Image.NEAREST)
                        img.paste(mosaic, (x0, y0))

                img.convert("RGB").save(file_path, quality=95)
                processed_count += 1
                print(f"[{idx}/{total}] 고환/성기 차단 완료: {filename}")

        except Exception as e:
            print(f"[에러 발생] {file_path}: {e}")

    print(f"\n[완료] 총 {processed_count}장의 에셋을 빈틈없이 차단했습니다.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="고환 및 미세 돌기 절대 차단 스크립트")
    parser.add_argument("-d", "--dir", type=str, required=True, help="검열할 이미지 폴더 경로")
    parser.add_argument("-m", "--mode", type=str, default="black_bar", choices=["black_bar", "mosaic"])
    args = parser.parse_args()
    run_absolute_censor(args.dir, args.mode)
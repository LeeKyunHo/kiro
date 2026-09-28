"""censor_studio.py — 고품질 블랭크(솔리드 블랙 바) 원클릭 검열 웹 스튜디오.

특징:
- 100% 완전 불투명 솔리드 블랙 바(Blank) 지원 (투과/비침 원천 차단)
- 인체 실루엣과 포즈 흐름을 살리는 슬림 사선 회전 바 (각도/두께 실시간 조절)
- 마우스 휠 각도 회전 + 직전 각도/크기 자동 기억 (1클릭 1초 컷)
- Space / Enter 키 원터치 고화질 덮어쓰기 저장 및 다음 이미지 자동 이동
- H-씬(040~075, 140~170) 자동 필터링 기능
"""

import os
import sys
import glob
import re
import math
import argparse
import webbrowser
from pathlib import Path

# 윈도우 콘솔 UTF-8 출력 보장
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from flask import Flask, request, jsonify, send_file, render_template_string
from PIL import Image, ImageDraw

app = Flask(__name__)

# 전역 작업 디렉토리
TARGET_DIR = ""
BACKUP_ENABLED = True

import urllib.parse

def get_image_list(target_dir, filter_h=True):
    """지정 디렉토리에서 이미지 파일 목록 반환 (한글 및 대소문자 안전)"""
    if not os.path.exists(target_dir):
        return []
    
    valid_exts = {".webp", ".png", ".jpg", ".jpeg"}
    all_files = []
    
    for root, _, filenames in os.walk(target_dir):
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext in valid_exts and not fname.endswith(".orig"):
                full_p = os.path.abspath(os.path.join(root, fname))
                all_files.append(full_p)
                
    unique_files = sorted(list(set(all_files)))
    
    h_files = []
    other_files = []
    
    for f in unique_files:
        filename = os.path.basename(f)
        match = re.search(r"_(\d{2,4})\.", filename)
        code = int(match.group(1)) if match else -1
        # 40번 이상 (H-씬, 오토코노코 씬, 200번대 이벤트 씬 등 모든 검열 대상)
        is_h = (code >= 40)
        
        item = {
            "path": f,
            "filename": filename,
            "code": code,
            "is_h": is_h
        }
        
        if is_h:
            h_files.append(item)
        else:
            other_files.append(item)
            
    # filter_h가 켜져 있을 때:
    # 1. 40번 이후 검열 대상 파일이 있으면 그것만 반환 (실전 모드)
    # 2. 만약 테스트 이미지처럼 40번 이후 파일이 하나도 없으면 일반 파일 반환 (스마트 폴백)
    if filter_h:
        if h_files:
            return h_files
        else:
            return other_files
            
    # 전체 보기 시 40번 이후 먼저, 그 다음 일반 씬
    return h_files + other_files

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Kiro Censor Studio — 블랭크 검열 도구</title>
    <style>
        :root {
            --bg-color: #121316;
            --panel-bg: #1c1e24;
            --accent: #4f46e5;
            --accent-hover: #6366f1;
            --text-main: #f3f4f6;
            --text-sub: #9ca3af;
            --border: #2d3139;
            --success: #10b981;
            --warning: #f59e0b;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; user-select: none; }
        body {
            background-color: var(--bg-color);
            color: var(--text-main);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            display: flex;
            flex-direction: column;
            height: 100vh;
            overflow: hidden;
        }
        header {
            background: var(--panel-bg);
            border-bottom: 1px solid var(--border);
            padding: 10px 20px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            height: 56px;
        }
        .header-title {
            display: flex;
            align-items: center;
            gap: 12px;
            font-weight: 700;
            font-size: 1.05rem;
        }
        .badge {
            background: var(--accent);
            color: #fff;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 0.75rem;
            font-weight: 600;
        }
        .nav-controls {
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .btn {
            background: #2b2f3a;
            color: var(--text-main);
            border: 1px solid var(--border);
            padding: 6px 14px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 0.85rem;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 6px;
            transition: all 0.15s;
        }
        .btn:hover { background: #373c49; }
        .btn-primary {
            background: var(--accent);
            border-color: var(--accent);
            color: #fff;
        }
        .btn-primary:hover { background: var(--accent-hover); }
        .btn-success {
            background: var(--success);
            border-color: var(--success);
            color: #fff;
        }
        .main-container {
            display: flex;
            flex: 1;
            height: calc(100vh - 56px);
        }
        .canvas-area {
            flex: 1;
            position: relative;
            background: #090a0c;
            display: flex;
            align-items: center;
            justify-content: center;
            overflow: hidden;
        }
        #viewport {
            max-width: 95%;
            max-height: 95%;
            box-shadow: 0 10px 30px rgba(0,0,0,0.7);
            border: 1px solid #262930;
            cursor: crosshair;
        }
        .sidebar {
            width: 320px;
            background: var(--panel-bg);
            border-left: 1px solid var(--border);
            padding: 16px;
            display: flex;
            flex-direction: column;
            gap: 16px;
            overflow-y: auto;
        }
        .section-title {
            font-size: 0.8rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: var(--text-sub);
            margin-bottom: 8px;
            font-weight: 700;
        }
        .control-group {
            background: #14161b;
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 12px;
            display: flex;
            flex-direction: column;
            gap: 10px;
        }
        .control-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            font-size: 0.85rem;
        }
        .control-row label { color: var(--text-sub); }
        input[type="range"] {
            flex: 1;
            margin-left: 12px;
            accent-color: var(--accent);
        }
        .help-box {
            font-size: 0.8rem;
            color: var(--text-sub);
            line-height: 1.5;
            background: #14161b;
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 12px;
        }
        .help-box kbd {
            background: #262a34;
            color: #e5e7eb;
            padding: 2px 6px;
            border-radius: 4px;
            border: 1px solid #3c4250;
            font-family: monospace;
            font-size: 0.75rem;
        }
        .toast {
            position: absolute;
            bottom: 24px;
            left: 50%;
            transform: translateX(-50%);
            background: rgba(16, 185, 129, 0.95);
            color: #fff;
            padding: 8px 18px;
            border-radius: 20px;
            font-size: 0.9rem;
            font-weight: 600;
            pointer-events: none;
            opacity: 0;
            transition: opacity 0.25s ease-in-out;
            box-shadow: 0 4px 14px rgba(0,0,0,0.4);
            z-index: 100;
        }
        .toast.show { opacity: 1; }
    </style>
</head>
<body>

    <header>
        <div class="header-title">
            <span>KIRO CENSOR STUDIO</span>
            <span class="badge" id="poseBadge">포즈 ---</span>
            <span id="fileIndex" style="color: var(--text-sub); font-size: 0.85rem;">0 / 0</span>
        </div>
        <div class="nav-controls">
            <label style="display: flex; align-items: center; gap: 6px; font-size: 0.85rem; margin-right: 15px; cursor: pointer;">
                <input type="checkbox" id="filterH" checked> 40번 이후 검열 대상만 보기 (040+)
            </label>
            <button class="btn" id="btnPrev" title="이전 (A, ←)">◀ 이전</button>
            <button class="btn" id="btnSkip" title="저장 없이 건너뛰기 (D, →)">건너뛰기 ▶</button>
            <button class="btn btn-primary" id="btnSave" title="저장 후 다음 (Space, Enter)">💾 저장 & 다음 (Space)</button>
        </div>
    </header>

    <div class="main-container">
        <div class="canvas-area">
            <canvas id="viewport"></canvas>
            <div id="toast" class="toast">저장 완료!</div>
        </div>

        <div class="sidebar">
            <div>
                <div class="section-title">현재 파일 정보</div>
                <div style="font-weight: 600; font-size: 0.9rem; word-break: break-all;" id="currentFilename">-</div>
            </div>

            <div>
                <div class="section-title">블랭크 바 속성 (실시간 조절)</div>
                <div class="control-group">
                    <div class="control-row">
                        <label>회전 각도</label>
                        <input type="range" id="angleSlider" min="-90" max="90" value="0">
                        <span id="angleVal" style="width: 38px; text-align: right; font-family: monospace;">0°</span>
                    </div>
                    <div class="control-row">
                        <label>길이(가로)</label>
                        <input type="range" id="widthSlider" min="20" max="300" value="100">
                        <span id="widthVal" style="width: 38px; text-align: right; font-family: monospace;">100</span>
                    </div>
                    <div class="control-row">
                        <label>두께(세로)</label>
                        <input type="range" id="heightSlider" min="10" max="150" value="30">
                        <span id="heightVal" style="width: 38px; text-align: right; font-family: monospace;">30</span>
                    </div>
                    <div class="control-row" style="margin-top: 4px;">
                        <button class="btn" id="btnResetBar" style="flex: 1; justify-content: center;">바 모두 지우기 (C)</button>
                        <button class="btn" id="btnUndoBar" style="flex: 1; justify-content: center; margin-left: 6px;">실행 취소 (Z)</button>
                    </div>
                </div>
            </div>

            <div>
                <div class="section-title">단축키 가이드 (1초 컷 워크플로우)</div>
                <div class="help-box">
                    • <b>마우스 클릭</b>: 클릭 위치에 100% 솔리드 블랙 바 생성<br>
                    • <b>마우스 휠</b>: 바 <b>각도 회전</b> (-90° ~ +90°)<br>
                    • <b>Shift + 휠</b>: 바 <b>길이 확대/축소</b><br>
                    • <b>Ctrl + 휠</b>: 바 <b>두께 확대/축소</b><br>
                    • <b>바 드래그</b>: 위치 정밀 이동<br>
                    • <kbd>Space</kbd> / <kbd>Enter</kbd>: <b>저장 후 다음 이미지</b><br>
                    • <kbd>→</kbd> / <kbd>D</kbd>: 저장 안 하고 다음 이미지<br>
                    • <kbd>←</kbd> / <kbd>A</kbd>: 이전 이미지<br>
                    • <kbd>Ctrl+Z</kbd> / <kbd>Z</kbd>: 마지막 바 삭제<br>
                    • <kbd>C</kbd>: 모든 바 초기화
                </div>
            </div>

            <div style="margin-top: auto; font-size: 0.75rem; color: var(--text-sub); text-align: center;">
                Kiro Automation Pipeline • 100% Solid Blank Engine
            </div>
        </div>
    </div>

    <script>
        const canvas = document.getElementById('viewport');
        const ctx = canvas.getContext('2d');
        const toast = document.getElementById('toast');

        // 상태 변수
        let images = [];
        let currentIndex = 0;
        let currentImgElement = null;
        let scaleRatio = 1.0; // 캔버스 표시 크기 / 원본 이미지 크기
        
        // 현재 이미지에 놓인 검열 바 목록 [{cx, cy, width, height, angle}] (원본 픽셀 기준)
        let bars = [];
        let selectedBarIndex = -1;
        let isDragging = false;
        let dragOffset = { x: 0, y: 0 };

        // 직전 사용 파라미터 (다음 씬에서도 유지하여 1클릭 1초 컷 지원)
        let lastAngle = -15; // 기본 살짝 사선
        let lastWidth = 110;
        let lastHeight = 32;

        // UI 요소
        const angleSlider = document.getElementById('angleSlider');
        const widthSlider = document.getElementById('widthSlider');
        const heightSlider = document.getElementById('heightSlider');
        const angleVal = document.getElementById('angleVal');
        const widthVal = document.getElementById('widthVal');
        const heightVal = document.getElementById('heightVal');
        const filterH = document.getElementById('filterH');

        // 로드 함수
        async function fetchImages() {
            const res = await fetch(`/api/images?filter_h=${filterH.checked}`);
            images = await res.json();
            currentIndex = 0;
            if (images.length > 0) {
                loadImage(0);
            } else {
                alert('해당 폴더에서 이미지를 찾을 수 없습니다.\\n- H-씬 필터가 켜져 있다면 해제해보세요.\\n- 폴더 내에 webp, png, jpg 파일이 있는지 확인해주세요.');
            }
        }

        function showToast(msg) {
            toast.innerText = msg;
            toast.classList.add('show');
            setTimeout(() => toast.classList.remove('show'), 1200);
        }

        async function loadImage(index) {
            if (index < 0 || index >= images.length) return;
            currentIndex = index;
            const item = images[index];

            document.getElementById('fileIndex').innerText = `${index + 1} / ${images.length}`;
            document.getElementById('currentFilename').innerText = item.filename;
            document.getElementById('poseBadge').innerText = item.code >= 0 ? `포즈 #${item.code}` : '일반 씬';

            bars = [];
            selectedBarIndex = -1;

            const img = new Image();
            img.src = `/api/image?path=${encodeURIComponent(item.path)}&t=${new Date().getTime()}`;
            img.onload = () => {
                currentImgElement = img;
                setupCanvas(img);
                render();
            };
        }

        function setupCanvas(img) {
            const container = canvas.parentElement;
            const maxW = container.clientWidth * 0.94;
            const maxH = container.clientHeight * 0.94;

            const aspect = img.width / img.height;
            let displayW = maxW;
            let displayH = maxW / aspect;

            if (displayH > maxH) {
                displayH = maxH;
                displayW = maxH * aspect;
            }

            canvas.width = img.width;
            canvas.height = img.height;
            canvas.style.width = `${displayW}px`;
            canvas.style.height = `${displayH}px`;

            scaleRatio = displayW / img.width;
        }

        function getOriginalCoords(e) {
            const rect = canvas.getBoundingClientRect();
            const clientX = e.clientX - rect.left;
            const clientY = e.clientY - rect.top;
            return {
                x: clientX / (rect.width / canvas.width),
                y: clientY / (rect.height / canvas.height)
            };
        }

        // 회전 사각형 그리기 (100% 완전 불투명 솔리드 블랙)
        function drawRotatedBar(ctx, bar, isSelected) {
            ctx.save();
            ctx.translate(bar.cx, bar.cy);
            ctx.rotate(bar.angle * Math.PI / 180);

            // 100% 완전 불투명 솔리드 블랙
            ctx.fillStyle = '#000000';
            ctx.fillRect(-bar.width / 2, -bar.height / 2, bar.width, bar.height);

            // 선택된 상태일 때만 은은한 테두리 표시 (저장 시에는 제외됨)
            if (isSelected) {
                ctx.lineWidth = Math.max(2, canvas.width / 400);
                ctx.strokeStyle = '#4f46e5';
                ctx.strokeRect(-bar.width / 2, -bar.height / 2, bar.width, bar.height);
            }

            ctx.restore();
        }

        function render() {
            if (!currentImgElement) return;
            ctx.clearRect(0, 0, canvas.width, canvas.height);
            ctx.drawImage(currentImgElement, 0, 0);

            bars.forEach((bar, idx) => {
                drawRotatedBar(ctx, bar, idx === selectedBarIndex);
            });
        }

        function isPointInBar(x, y, bar) {
            // 점 (x, y)를 바의 로컬 좌표계로 역회전 변환
            const rad = -bar.angle * Math.PI / 180;
            const dx = x - bar.cx;
            const dy = y - bar.cy;
            const localX = dx * Math.cos(rad) - dy * Math.sin(rad);
            const localY = dx * Math.sin(rad) + dy * Math.cos(rad);

            return Math.abs(localX) <= bar.width / 2 && Math.abs(localY) <= bar.height / 2;
        }

        // 이벤트 리스너들
        canvas.addEventListener('mousedown', (e) => {
            if (e.button !== 0) return;
            const pos = getOriginalCoords(e);

            // 기존 바 클릭 여부 확인 (역순으로 위쪽 바부터)
            let clickedIndex = -1;
            for (let i = bars.length - 1; i >= 0; i--) {
                if (isPointInBar(pos.x, pos.y, bars[i])) {
                    clickedIndex = i;
                    break;
                }
            }

            if (clickedIndex !== -1) {
                selectedBarIndex = clickedIndex;
                isDragging = true;
                dragOffset.x = pos.x - bars[clickedIndex].cx;
                dragOffset.y = pos.y - bars[clickedIndex].cy;
                syncControls();
            } else {
                // 새 바 생성 (직전 각도/크기 기억)
                const newBar = {
                    cx: pos.x,
                    cy: pos.y,
                    width: lastWidth,
                    height: lastHeight,
                    angle: lastAngle
                };
                bars.push(newBar);
                selectedBarIndex = bars.length - 1;
                isDragging = true;
                dragOffset.x = 0;
                dragOffset.y = 0;
                syncControls();
            }
            render();
        });

        window.addEventListener('mousemove', (e) => {
            if (!isDragging || selectedBarIndex === -1) return;
            const pos = getOriginalCoords(e);
            bars[selectedBarIndex].cx = pos.x - dragOffset.x;
            bars[selectedBarIndex].cy = pos.y - dragOffset.y;
            render();
        });

        window.addEventListener('mouseup', () => {
            isDragging = false;
        });

        // 마우스 휠로 각도 / 크기 조절
        canvas.addEventListener('wheel', (e) => {
            e.preventDefault();
            if (selectedBarIndex === -1 && bars.length > 0) {
                selectedBarIndex = bars.length - 1;
            }
            if (selectedBarIndex === -1) return;

            const bar = bars[selectedBarIndex];

            if (e.shiftKey) {
                // 길이(가로) 조절
                bar.width = Math.max(20, Math.min(600, bar.width + (e.deltaY < 0 ? 8 : -8)));
                lastWidth = bar.width;
            } else if (e.ctrlKey) {
                // 두께(세로) 조절
                bar.height = Math.max(10, Math.min(300, bar.height + (e.deltaY < 0 ? 4 : -4)));
                lastHeight = bar.height;
            } else {
                // 각도 조절 (5도 단위)
                const deltaAngle = e.deltaY < 0 ? 5 : -5;
                bar.angle = ((bar.angle + deltaAngle + 90) % 180) - 90;
                lastAngle = bar.angle;
            }

            syncControls();
            render();
        }, { passive: false });

        function syncControls() {
            if (selectedBarIndex === -1) return;
            const bar = bars[selectedBarIndex];
            angleSlider.value = bar.angle;
            angleVal.innerText = `${bar.angle}°`;
            widthSlider.value = bar.width;
            widthVal.innerText = bar.width;
            heightSlider.value = bar.height;
            heightVal.innerText = bar.height;
        }

        // 슬라이더 이벤트
        angleSlider.addEventListener('input', (e) => {
            if (selectedBarIndex === -1) return;
            bars[selectedBarIndex].angle = parseInt(e.target.value);
            angleVal.innerText = `${bars[selectedBarIndex].angle}°`;
            lastAngle = bars[selectedBarIndex].angle;
            render();
        });
        widthSlider.addEventListener('input', (e) => {
            if (selectedBarIndex === -1) return;
            bars[selectedBarIndex].width = parseInt(e.target.value);
            widthVal.innerText = bars[selectedBarIndex].width;
            lastWidth = bars[selectedBarIndex].width;
            render();
        });
        heightSlider.addEventListener('input', (e) => {
            if (selectedBarIndex === -1) return;
            bars[selectedBarIndex].height = parseInt(e.target.value);
            heightVal.innerText = bars[selectedBarIndex].height;
            lastHeight = bars[selectedBarIndex].height;
            render();
        });

        document.getElementById('btnResetBar').addEventListener('click', () => {
            bars = [];
            selectedBarIndex = -1;
            render();
        });

        document.getElementById('btnUndoBar').addEventListener('click', () => {
            if (bars.length > 0) {
                bars.pop();
                selectedBarIndex = bars.length - 1;
                syncControls();
                render();
            }
        });

        filterH.addEventListener('change', fetchImages);

        // 저장 함수
        async function saveAndNext() {
            if (bars.length === 0) {
                // 바가 없으면 그냥 다음으로 패스
                nextImage();
                return;
            }

            const current = images[currentIndex];
            try {
                const res = await fetch('/api/save', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        path: current.path,
                        bars: bars
                    })
                });
                const data = await res.json();
                if (data.success) {
                    showToast('저장 완료!');
                    nextImage();
                } else {
                    alert('저장 실패: ' + data.error);
                }
            } catch (err) {
                alert('통신 오류: ' + err);
            }
        }

        function nextImage() {
            if (currentIndex < images.length - 1) {
                loadImage(currentIndex + 1);
            } else {
                alert('모든 이미지 검열 및 확인이 완료되었습니다!');
            }
        }

        function prevImage() {
            if (currentIndex > 0) {
                loadImage(currentIndex - 1);
            }
        }

        document.getElementById('btnSave').addEventListener('click', saveAndNext);
        document.getElementById('btnSkip').addEventListener('click', nextImage);
        document.getElementById('btnPrev').addEventListener('click', prevImage);

        // 키보드 단축키
        window.addEventListener('keydown', (e) => {
            if (e.target.tagName === 'INPUT') return;

            if (e.code === 'Space' || e.code === 'Enter') {
                e.preventDefault();
                saveAndNext();
            } else if (e.code === 'ArrowRight' || e.code === 'KeyD') {
                e.preventDefault();
                nextImage();
            } else if (e.code === 'ArrowLeft' || e.code === 'KeyA') {
                e.preventDefault();
                prevImage();
            } else if ((e.ctrlKey && e.code === 'KeyZ') || e.code === 'KeyZ') {
                e.preventDefault();
                if (bars.length > 0) {
                    bars.pop();
                    selectedBarIndex = bars.length - 1;
                    syncControls();
                    render();
                }
            } else if (e.code === 'KeyC') {
                bars = [];
                selectedBarIndex = -1;
                render();
            }
        });

        // 초기 시작
        fetchImages();
    </script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route("/api/images")
def api_images():
    filter_h = request.args.get("filter_h", "true").lower() == "true"
    imgs = get_image_list(TARGET_DIR, filter_h=filter_h)
    return jsonify(imgs)

@app.route("/api/image")
def api_image():
    raw_path = request.args.get("path")
    if not raw_path:
        return "Image path required", 400
    path = os.path.abspath(urllib.parse.unquote(raw_path))
    if not os.path.exists(path):
        return f"Image not found: {path}", 404
    return send_file(path)

@app.route("/api/save", methods=["POST"])
def api_save():
    try:
        data = request.json
        raw_path = data.get("path")
        file_path = os.path.abspath(urllib.parse.unquote(raw_path)) if raw_path else None
        bars = data.get("bars", [])
        
        if not file_path or not os.path.exists(file_path):
            return jsonify({"success": False, "error": "파일을 찾을 수 없습니다."}), 404

        # 이미지 로드
        img = Image.open(file_path).convert("RGB")
        draw = ImageDraw.Draw(img)

        # 100% 완전 불투명 솔리드 블랙 폴리곤 렌더링
        for b in bars:
            cx = float(b["cx"])
            cy = float(b["cy"])
            w = float(b["width"])
            h = float(b["height"])
            angle_deg = float(b.get("angle", 0))
            rad = angle_deg * math.pi / 180.0

            # 로컬 4개 꼭짓점
            corners = [
                (-w / 2.0, -h / 2.0),
                (w / 2.0, -h / 2.0),
                (w / 2.0, h / 2.0),
                (-w / 2.0, h / 2.0)
            ]

            # 회전 변환
            world_corners = []
            for lx, ly in corners:
                rx = lx * math.cos(rad) - ly * math.sin(rad) + cx
                ry = lx * math.sin(rad) + ly * math.cos(rad) + cy
                world_corners.append((rx, ry))

            # 완전 차폐 솔리드 블랙 칠하기
            draw.polygon(world_corners, fill=(0, 0, 0))

        # 백업 생성 (최초 1회: 상위 폴더의 assets_original 에 원본 파일명 그대로 보관)
        if BACKUP_ENABLED and TARGET_DIR:
            parent_dir = os.path.dirname(TARGET_DIR)
            dir_name = os.path.basename(TARGET_DIR)
            backup_root = os.path.join(parent_dir, f"{dir_name}_original")
            
            # 상대 경로 유지
            rel_path = os.path.relpath(file_path, TARGET_DIR)
            backup_path = os.path.join(backup_root, rel_path)
            
            os.makedirs(os.path.dirname(backup_path), exist_ok=True)
            if not os.path.exists(backup_path):
                import shutil
                shutil.copy2(file_path, backup_path)

        # 원본 포맷으로 최고화질(quality=95) 저장
        if file_path.lower().endswith(".webp"):
            img.save(file_path, format="WEBP", quality=95)
        elif file_path.lower().endswith(".png"):
            img.save(file_path, format="PNG")
        else:
            img.save(file_path, quality=95)

        return jsonify({"success": True})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

def main():
    global TARGET_DIR, BACKUP_ENABLED
    parser = argparse.ArgumentParser(description="Kiro Censor Studio - 원클릭 블랭크 검열 전용 도구")
    parser.add_argument("-d", "--dir", type=str, required=True, help="에셋 폴더 경로 (예: projects/don/assets)")
    parser.add_argument("-p", "--port", type=int, default=5050, help="웹 서버 포트 (기본: 5050)")
    parser.add_argument("--no-backup", action="store_true", help="수정 전 원본 백업 비활성화")
    args = parser.parse_args()

    TARGET_DIR = os.path.abspath(args.dir)
    BACKUP_ENABLED = not args.no_backup

    if not os.path.exists(TARGET_DIR):
        print(f"[오류] 지정한 경로가 존재하지 않습니다: {TARGET_DIR}")
        sys.exit(1)

    url = f"http://127.0.0.1:{args.port}"
    backup_info = os.path.join(os.path.dirname(TARGET_DIR), f"{os.path.basename(TARGET_DIR)}_original") if BACKUP_ENABLED else "비활성화"
    print("=" * 60)
    print("  Kiro Censor Studio (블랭크 검열 스튜디오)")
    print(f"  - 대상 경로: {TARGET_DIR}")
    print(f"  - 원본 보관: {backup_info}")
    print(f"  - 웹 주소: {url}")
    print("  - 단축키: 클릭=바 생성, 휠=각도회전, Space=저장&다음, →=건너뛰기")
    print("=" * 60)

    # 브라우저 자동 오픈
    webbrowser.open(url)
    app.run(host="127.0.0.1", port=args.port, debug=False)

if __name__ == "__main__":
    main()

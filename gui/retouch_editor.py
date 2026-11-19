"""
gui/retouch_editor.py
PC 갤러리(/gallery)와 모바일 검수 뷰어(/inspect)에서 공용으로 사용하는
HTML5 Canvas 인라인 리터치·수동 검열 에디터 컴포넌트.

지원 기능:
- 마우스 + 모바일 터치(Pointer Events / Touch fallback) 드래그 선분 보간
- 화이트 브러시 / 블랙 브러시 / 지우개(원본 복원 브러시) / 브러시 크기 조절
- Undo 스택(최대 20단계), 초기 상태 리셋, 무검열 원본(_origin) 전체 로드
- 확대/축소(Zoom 100%~300%) 및 패닝(Pan) 모드
- 저장 시 POST /api/retouch/save 로 Base64 PNG 전송 (KIRO_API_TOKEN 헤더 자동 첨부)
  및 캐시 버스팅(?t=...) 콜백으로 썸네일·라이트박스 즉시 갱신
"""

from __future__ import annotations

RETOUCH_EDITOR_CSS: str = """
<style id="kiro-retouch-css">
    .kiro-retouch-modal {
        display: none;
        position: fixed;
        inset: 0;
        z-index: 9999;
        background: rgba(2, 6, 23, 0.96);
        backdrop-filter: blur(10px);
        -webkit-backdrop-filter: blur(10px);
        flex-direction: column;
        overflow: hidden;
        user-select: none;
        -webkit-user-select: none;
    }
    .kiro-retouch-modal.active {
        display: flex;
    }
    .kiro-retouch-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 10px;
        padding: 10px 16px;
        padding-top: max(10px, env(safe-area-inset-top));
        background: #0f172a;
        border-bottom: 1px solid #334155;
        color: #f8fafc;
        flex-shrink: 0;
    }
    .kiro-retouch-title-wrap {
        display: flex;
        flex-direction: column;
        min-width: 0;
    }
    .kiro-retouch-title {
        font-size: 14px;
        font-weight: 700;
        color: #f8fafc;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    .kiro-retouch-sub {
        font-size: 11px;
        color: #94a3b8;
    }
    .kiro-retouch-close {
        background: #334155;
        color: #f8fafc;
        border: 1px solid #475569;
        border-radius: 8px;
        padding: 6px 12px;
        font-size: 12px;
        font-weight: 600;
        cursor: pointer;
        flex-shrink: 0;
    }
    .kiro-retouch-stage {
        flex: 1;
        display: flex;
        align-items: center;
        justify-content: center;
        overflow: hidden;
        padding: 12px;
        position: relative;
        touch-action: none;
    }
    #kiroEditorCanvas {
        max-width: 100%;
        max-height: 100%;
        width: auto;
        height: auto;
        object-fit: contain;
        border-radius: 8px;
        box-shadow: 0 20px 50px rgba(0, 0, 0, 0.85);
        cursor: crosshair;
        touch-action: none;
        background: #0b1120;
        transform-origin: center center;
    }
    #kiroEditorCanvas.pan-mode {
        cursor: grab;
    }
    .kiro-retouch-toolbar {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        justify-content: center;
        gap: 8px;
        padding: 10px 14px;
        padding-bottom: max(12px, env(safe-area-inset-bottom));
        background: #0f172a;
        border-top: 1px solid #334155;
        flex-shrink: 0;
    }
    .kiro-retouch-group {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        flex-wrap: wrap;
    }
    .kiro-tool-btn {
        background: #1e293b;
        color: #e2e8f0;
        border: 1px solid #475569;
        border-radius: 8px;
        padding: 7px 11px;
        font-size: 12px;
        font-weight: 600;
        cursor: pointer;
        transition: all 0.12s ease;
        white-space: nowrap;
    }
    .kiro-tool-btn:hover:not(:disabled) {
        background: #334155;
    }
    .kiro-tool-btn:disabled {
        opacity: 0.42;
        cursor: not-allowed;
    }
    .kiro-tool-btn.active {
        background: #2563eb;
        border-color: #60a5fa;
        color: #ffffff;
        box-shadow: 0 0 10px rgba(37, 99, 235, 0.55);
    }
    .kiro-tool-btn.warn {
        background: rgba(217, 119, 6, 0.22);
        border-color: rgba(251, 191, 36, 0.65);
        color: #fde68a;
    }
    .kiro-save-btn {
        background: #10b981;
        color: #022c22;
        border: 1px solid #34d399;
        border-radius: 8px;
        padding: 8px 16px;
        font-size: 13px;
        font-weight: 800;
        cursor: pointer;
        white-space: nowrap;
    }
    .kiro-save-btn:hover:not(:disabled) {
        background: #34d399;
    }
    .kiro-save-btn:disabled {
        opacity: 0.6;
        cursor: wait;
    }
</style>
"""

RETOUCH_EDITOR_HTML: str = """
<div id="kiroRetouchModal" class="kiro-retouch-modal" aria-hidden="true">
    <div class="kiro-retouch-header">
        <div class="kiro-retouch-title-wrap">
            <div class="kiro-retouch-title" id="kiroRetouchTitle">✏️ 인라인 리터치 에디터</div>
            <div class="kiro-retouch-sub" id="kiroRetouchSubtitle">저장 시 /api/retouch/save 로 원자 저장되며 최초 1회 _origin 백업이 자동 보존됩니다</div>
        </div>
        <button type="button" class="kiro-retouch-close" onclick="closeKiroRetouchEditor()">닫기 ✕</button>
    </div>
    <div class="kiro-retouch-stage" id="kiroRetouchStage">
        <canvas id="kiroEditorCanvas"></canvas>
    </div>
    <div class="kiro-retouch-toolbar">
        <div class="kiro-retouch-group">
            <button type="button" class="kiro-tool-btn warn" id="kiroBtnLoadOrigin" onclick="loadOriginToKiroCanvas()" title="무검열 원본(_origin)으로 캔버스를 초기화합니다">
                🧹 원본 로드
            </button>
            <button type="button" class="kiro-tool-btn active" id="kiroBtnWhiteBrush" onclick="setKiroRetouchTool('white_brush')">
                🖌️ 화이트
            </button>
            <button type="button" class="kiro-tool-btn" id="kiroBtnBlackBrush" onclick="setKiroRetouchTool('black_brush')">
                🖤 블랙
            </button>
            <button type="button" class="kiro-tool-btn" id="kiroBtnEraser" onclick="setKiroRetouchTool('eraser')" title="드래그한 영역만 무검열 원본 픽셀로 복원합니다">
                🩹 지우개
            </button>
            <button type="button" class="kiro-tool-btn" id="kiroBtnPan" onclick="setKiroRetouchTool('pan')" title="확대 상태에서 화면을 드래그 이동합니다">
                ✋ 이동
            </button>
            <button type="button" class="kiro-tool-btn" onclick="undoKiroRetouchStroke()">↩️ 취소</button>
            <button type="button" class="kiro-tool-btn" onclick="resetKiroRetouchCanvas()">🔄 리셋</button>
        </div>
        <div class="kiro-retouch-group">
            <span style="font-size:12px;color:#94a3b8;">브러시:</span>
            <input type="range" id="kiroBrushSize" min="6" max="80" value="28" style="width:84px;" oninput="updateKiroBrushSize(this.value)" />
            <span id="kiroBrushSizeVal" style="font-size:12px;color:#38bdf8;font-weight:700;min-width:24px;text-align:right;">28</span>
            <button type="button" class="kiro-tool-btn" onclick="adjustKiroRetouchZoom(-0.25)">➖</button>
            <span id="kiroZoomVal" style="font-size:11px;color:#cbd5e1;min-width:38px;text-align:center;">100%</span>
            <button type="button" class="kiro-tool-btn" onclick="adjustKiroRetouchZoom(0.25)">➕</button>
            <button type="button" class="kiro-tool-btn" onclick="resetKiroRetouchZoom()">1:1</button>
        </div>
        <button type="button" class="kiro-save-btn" id="kiroBtnRetouchSave" onclick="saveKiroRetouchEdit()">
            💾 저장 (/api/retouch/save)
        </button>
    </div>
</div>
"""


def build_retouch_editor_script(
    on_saved_js: str = "",
    on_notify_js: str = "console.log(msg);",
) -> str:
    """PC 갤러리(/gallery) 및 모바일 뷰어(/inspect) 공용 HTML5 Canvas 리터치 에디터 JS 블록을 생성한다."""
    return f"""
<script id="kiro-retouch-script">
(function() {{
    const state = {{
        isOpen: false,
        relPath: '',
        imageUrl: '',
        originUrl: '',
        initialImgObj: null,
        originImgObj: null,
        originReady: false,
        canvas: null,
        ctx: null,
        isDrawing: false,
        isPanning: false,
        lastPos: null,
        panStart: null,
        tool: 'white_brush',
        brushRadius: 28,
        zoom: 1.0,
        panX: 0,
        panY: 0,
        undoStack: [],
        saving: false
    }};

    function notifyUser(msg, isError) {{
        try {{
            {on_notify_js}
        }} catch (e) {{
            console.log(msg);
        }}
    }}

    function buildKiroAuthHeaders(extra) {{
        const headers = Object.assign({{}}, extra || {{}});
        let tok = '';
        try {{
            tok = sessionStorage.getItem('kiro_api_token') || '';
        }} catch (e) {{}}
        if (!tok) {{
            tok = new URLSearchParams(window.location.search).get('token') || '';
        }}
        if (tok) headers['X-Kiro-Token'] = tok;
        return headers;
    }}

    function withCacheBust(url) {{
        if (!url) return '';
        const sep = url.includes('?') ? '&' : '?';
        const cleaned = url.replace(/([?&])t=\\d+/g, '').replace(/[?&]$/, '');
        return cleaned + (cleaned.includes('?') ? '&' : '?') + 't=' + Date.now();
    }}

    function applyCanvasTransform() {{
        if (!state.canvas) return;
        state.canvas.style.transform = `translate(${{state.panX}}px, ${{state.panY}}px) scale(${{state.zoom}})`;
        const zLbl = document.getElementById('kiroZoomVal');
        if (zLbl) zLbl.textContent = Math.round(state.zoom * 100) + '%';
    }}

    function pushUndoState() {{
        if (!state.ctx || !state.canvas) return;
        if (state.undoStack.length >= 20) state.undoStack.shift();
        state.undoStack.push(
            state.ctx.getImageData(0, 0, state.canvas.width, state.canvas.height)
        );
    }}

    function getEraserSource() {{
        if (state.originReady && state.originImgObj) return state.originImgObj;
        return state.initialImgObj;
    }}

    function drawCircleAt(x, y) {{
        const ctx = state.ctx;
        const r = state.brushRadius;
        if (!ctx) return;

        ctx.save();
        if (state.tool === 'white_brush' || state.tool === 'black_brush') {{
            const rgb = state.tool === 'white_brush' ? '255, 255, 255' : '12, 12, 16';
            const grad = ctx.createRadialGradient(x, y, r * 0.4, x, y, r);
            grad.addColorStop(0, `rgba(${{rgb}}, 1.0)`);
            grad.addColorStop(0.85, `rgba(${{rgb}}, 0.96)`);
            grad.addColorStop(1, `rgba(${{rgb}}, 0.0)`);
            ctx.fillStyle = grad;
            ctx.beginPath();
            ctx.arc(x, y, r, 0, Math.PI * 2);
            ctx.fill();
        }} else if (state.tool === 'eraser') {{
            const srcImg = getEraserSource();
            if (srcImg && state.canvas) {{
                ctx.beginPath();
                ctx.arc(x, y, r, 0, Math.PI * 2);
                ctx.clip();
                ctx.drawImage(srcImg, 0, 0, state.canvas.width, state.canvas.height);
            }}
        }}
        ctx.restore();
    }}

    function drawSegment(p1, p2) {{
        if (!p1) {{
            drawCircleAt(p2.x, p2.y);
            return;
        }}
        const dx = p2.x - p1.x;
        const dy = p2.y - p1.y;
        const dist = Math.hypot(dx, dy);
        const step = Math.max(2, state.brushRadius * 0.22);
        const steps = Math.max(1, Math.ceil(dist / step));
        for (let i = 1; i <= steps; i++) {{
            const t = i / steps;
            drawCircleAt(p1.x + dx * t, p1.y + dy * t);
        }}
    }}

    function getClientXY(e) {{
        if (e.touches && e.touches.length > 0) {{
            return {{ x: e.touches[0].clientX, y: e.touches[0].clientY }};
        }}
        return {{ x: e.clientX, y: e.clientY }};
    }}

    function getCanvasCoords(e) {{
        const cv = state.canvas;
        const rect = cv.getBoundingClientRect();
        const pt = getClientXY(e);
        return {{
            x: (pt.x - rect.left) * (cv.width / Math.max(1, rect.width)),
            y: (pt.y - rect.top) * (cv.height / Math.max(1, rect.height))
        }};
    }}

    function onPointerStart(e) {{
        if (!state.isOpen || state.saving) return;
        e.preventDefault();
        if (state.tool === 'pan') {{
            state.isPanning = true;
            const pt = getClientXY(e);
            state.panStart = {{ x: pt.x - state.panX, y: pt.y - state.panY }};
            return;
        }}
        pushUndoState();
        state.isDrawing = true;
        const pos = getCanvasCoords(e);
        state.lastPos = pos;
        drawCircleAt(pos.x, pos.y);
    }}

    function onPointerMove(e) {{
        if (!state.isOpen || state.saving) return;
        if (state.isPanning && state.panStart) {{
            e.preventDefault();
            const pt = getClientXY(e);
            state.panX = pt.x - state.panStart.x;
            state.panY = pt.y - state.panStart.y;
            applyCanvasTransform();
            return;
        }}
        if (!state.isDrawing) return;
        e.preventDefault();
        const pos = getCanvasCoords(e);
        drawSegment(state.lastPos, pos);
        state.lastPos = pos;
    }}

    function onPointerEnd() {{
        state.isDrawing = false;
        state.isPanning = false;
        state.lastPos = null;
        state.panStart = null;
    }}

    function bindCanvasEvents() {{
        const cv = document.getElementById('kiroEditorCanvas');
        if (!cv || cv.dataset.kiroBound === '1') return;
        cv.dataset.kiroBound = '1';
        state.canvas = cv;
        state.ctx = cv.getContext('2d', {{ willReadFrequently: true }});

        cv.addEventListener('pointerdown', onPointerStart);
        window.addEventListener('pointermove', onPointerMove);
        window.addEventListener('pointerup', onPointerEnd);
        window.addEventListener('pointercancel', onPointerEnd);

        cv.addEventListener('touchstart', onPointerStart, {{ passive: false }});
        cv.addEventListener('touchmove', onPointerMove, {{ passive: false }});
        cv.addEventListener('touchend', onPointerEnd);
        cv.addEventListener('wheel', function(e) {{
            if (!state.isOpen) return;
            e.preventDefault();
            window.adjustKiroRetouchZoom(e.deltaY < 0 ? 0.2 : -0.2);
        }}, {{ passive: false }});
    }}

    window.setKiroRetouchTool = function(tool) {{
        state.tool = tool;
        const map = {{
            white_brush: 'kiroBtnWhiteBrush',
            black_brush: 'kiroBtnBlackBrush',
            eraser: 'kiroBtnEraser',
            pan: 'kiroBtnPan'
        }};
        Object.keys(map).forEach(k => {{
            const el = document.getElementById(map[k]);
            if (el) el.classList.toggle('active', k === tool);
        }});
        if (state.canvas) state.canvas.classList.toggle('pan-mode', tool === 'pan');
    }};

    window.updateKiroBrushSize = function(val) {{
        state.brushRadius = Math.max(4, parseInt(val, 10) || 28);
        const lbl = document.getElementById('kiroBrushSizeVal');
        if (lbl) lbl.textContent = String(state.brushRadius);
    }};

    window.adjustKiroRetouchZoom = function(delta) {{
        state.zoom = Math.min(3.5, Math.max(0.75, Math.round((state.zoom + delta) * 100) / 100));
        applyCanvasTransform();
    }};

    window.resetKiroRetouchZoom = function() {{
        state.zoom = 1.0;
        state.panX = 0;
        state.panY = 0;
        applyCanvasTransform();
    }};

    window.undoKiroRetouchStroke = function() {{
        if (!state.ctx || state.undoStack.length === 0) return;
        const prev = state.undoStack.pop();
        state.ctx.putImageData(prev, 0, 0);
    }};

    window.resetKiroRetouchCanvas = function() {{
        if (!state.ctx || !state.canvas || !state.initialImgObj) return;
        pushUndoState();
        state.ctx.drawImage(state.initialImgObj, 0, 0, state.canvas.width, state.canvas.height);
    }};

    window.loadOriginToKiroCanvas = function() {{
        if (!state.ctx || !state.canvas) return;
        if (!state.originReady || !state.originImgObj) {{
            notifyUser('무검열 원본(_origin)이 없는 에셋입니다.', true);
            return;
        }}
        pushUndoState();
        state.ctx.drawImage(state.originImgObj, 0, 0, state.canvas.width, state.canvas.height);
        notifyUser('무검열 원본(_origin)을 캔버스에 로드했습니다.', false);
    }};

    window.openKiroRetouchEditor = function(relPath, title, hasOrigin) {{
        bindCanvasEvents();
        if (!state.canvas || !state.ctx) return;
        state.relPath = relPath;
        state.imageUrl = '/api/image?path=' + encodeURIComponent(relPath);
        state.originUrl = '/api/image?path=' + encodeURIComponent(relPath) + '&origin=1';
        state.undoStack = [];
        state.originReady = false;
        state.originImgObj = null;
        window.resetKiroRetouchZoom();
        window.setKiroRetouchTool('white_brush');

        const titleEl = document.getElementById('kiroRetouchTitle');
        if (titleEl) titleEl.textContent = '✏️ 리터치 · ' + (title || relPath);
        const origBtn = document.getElementById('kiroBtnLoadOrigin');
        if (origBtn) origBtn.disabled = !hasOrigin;

        const modal = document.getElementById('kiroRetouchModal');
        if (modal) {{
            modal.classList.add('active');
            modal.setAttribute('aria-hidden', 'false');
        }}
        state.isOpen = true;

        const img = new Image();
        img.crossOrigin = 'anonymous';
        img.onload = function() {{
            state.initialImgObj = img;
            state.canvas.width = img.naturalWidth || img.width;
            state.canvas.height = img.naturalHeight || img.height;
            state.ctx.clearRect(0, 0, state.canvas.width, state.canvas.height);
            state.ctx.drawImage(img, 0, 0);
        }};
        img.onerror = function() {{
            notifyUser('이미지를 캔버스에 불러오지 못했습니다.', true);
        }};
        img.src = withCacheBust(state.imageUrl);

        if (hasOrigin) {{
            const oImg = new Image();
            oImg.crossOrigin = 'anonymous';
            oImg.onload = function() {{
                state.originImgObj = oImg;
                state.originReady = true;
                if (origBtn) origBtn.disabled = false;
            }};
            oImg.src = withCacheBust(state.originUrl);
        }}
    }};

    window.closeKiroRetouchEditor = function() {{
        const modal = document.getElementById('kiroRetouchModal');
        if (modal) {{
            modal.classList.remove('active');
            modal.setAttribute('aria-hidden', 'true');
        }}
        state.isOpen = false;
    }};

    window.saveKiroRetouchEdit = async function() {{
        if (!state.canvas || !state.relPath || state.saving) return;
        state.saving = true;
        const btn = document.getElementById('kiroBtnRetouchSave');
        if (btn) btn.disabled = true;
        try {{
            const dataUrl = state.canvas.toDataURL('image/png');
            const resp = await fetch('/api/retouch/save', {{
                method: 'POST',
                headers: buildKiroAuthHeaders({{ 'Content-Type': 'application/json' }}),
                body: JSON.stringify({{ path: state.relPath, image_base64: dataUrl, quality: 95 }})
            }});
            const data = await resp.json();
            if (!resp.ok || !data.success) {{
                throw new Error(data.detail || '리터치 저장 실패');
            }}
            const savedPayload = {{
                rel_path: state.relPath,
                timestamp: Date.now(),
                origin_backed_up: !!data.origin_backed_up
            }};
            window.closeKiroRetouchEditor();
            notifyUser('✅ 리터치 저장 완료 (_origin 백업 보존)', false);
            {on_saved_js}
        }} catch (err) {{
            notifyUser('❌ ' + (err && err.message ? err.message : String(err)), true);
        }} finally {{
            state.saving = false;
            if (btn) btn.disabled = false;
        }}
    }};
}})();
</script>
"""


__all__ = [
    "RETOUCH_EDITOR_CSS",
    "RETOUCH_EDITOR_HTML",
    "build_retouch_editor_script",
]

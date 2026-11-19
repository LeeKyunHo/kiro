"""
gui/mobile_viewer.py
스마트폰 브라우저용 경량 원격 검수 뷰어 (/inspect).

- Wave P4-A REST API(/api/rosters, /api/gallery/assets, /api/image, /api/recensor,
  /api/restore_origin, /api/retouch/save, /api/asset/delete, /api/asset/restore, /api/reroll)만 호출.
- 세로 스크롤 카드형 검수 리스트, 원본 토글(origin=1), 공용 터치 리터치 캔버스(retouch_editor.py),
  원클릭 재검열·원본복원·리롤·삭제/Undo 지원.
- 원격 노출 환경에서 KIRO_API_TOKEN 을 sessionStorage 에 안전하게 입력/전달(X-Kiro-Token) 지원.
"""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from fastapi.responses import HTMLResponse

from generator.option_specs import get_option_spec
from gui.retouch_editor import (
    RETOUCH_EDITOR_CSS,
    RETOUCH_EDITOR_HTML,
    build_retouch_editor_script,
)


def generate_mobile_viewer_html(default_roster: str = "") -> str:
    """/inspect 경량 모바일 원격 검수 뷰어 HTML 문서를 생성한다."""
    censor_spec = get_option_spec("censor_style")
    styles = (
        list(censor_spec.choices)
        if censor_spec and censor_spec.choices
        else ["white_silhouette", "black_silhouette", "manga_bar", "manga_glow", "mosaic", "wide_bar"]
    )
    style_options_html = "\n".join(
        f'<option value="{html.escape(s, quote=True)}">{html.escape(s)}</option>'
        for s in styles
    )

    retouch_script = build_retouch_editor_script(
        on_saved_js="if (typeof window.onKiroMobileRetouchSaved === 'function') { window.onKiroMobileRetouchSaved(savedPayload); }",
        on_notify_js="if (typeof window.showKiroMobileToast === 'function') { window.showKiroMobileToast(msg, !!isError); }",
    )

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
    <title>📱 Kiro 모바일 검수 (/inspect)</title>
    <style>
        * {{ box-sizing: border-box; -webkit-tap-highlight-color: transparent; }}
        body {{
            margin: 0; padding: 0; background: #020617; color: #f8fafc;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        }}
        .topbar {{
            position: sticky; top: 0; z-index: 40;
            background: rgba(15, 23, 42, 0.96); backdrop-filter: blur(10px);
            border-bottom: 1px solid #1e293b; padding: 10px 12px;
            padding-top: max(10px, env(safe-area-inset-top));
        }}
        .top-row {{ display: flex; align-items: center; justify-content: space-between; gap: 8px; flex-wrap: wrap; }}
        .brand {{ font-size: 15px; font-weight: 800; color: #a5b4fc; text-decoration: none; }}
        .nav-links {{ display: flex; gap: 6px; align-items: center; }}
        .nav-pill {{
            background: #1e293b; color: #cbd5e1; border: 1px solid #334155;
            border-radius: 999px; padding: 4px 10px; font-size: 11px; text-decoration: none; cursor: pointer;
        }}
        .controls {{ display: flex; gap: 6px; margin-top: 8px; flex-wrap: wrap; }}
        .ctrl-select {{
            flex: 1; min-width: 110px; background: #0f172a; color: #f8fafc;
            border: 1px solid #334155; border-radius: 8px; padding: 7px 8px; font-size: 12px;
        }}
        .token-bar {{
            display: none; margin-top: 8px; gap: 6px; align-items: center;
        }}
        .token-bar.open {{ display: flex; }}
        .token-input {{
            flex: 1; background: #020617; color: #e2e8f0; border: 1px solid #475569;
            border-radius: 8px; padding: 6px 10px; font-size: 12px;
        }}
        .feed {{
            max-width: 680px; margin: 0 auto; padding: 12px 12px 96px;
            display: flex; flex-direction: column; gap: 14px;
        }}
        .asset-card {{
            background: #0f172a; border: 1px solid #1e293b; border-radius: 14px;
            overflow: hidden; box-shadow: 0 10px 24px rgba(0, 0, 0, 0.45);
        }}
        .card-head {{
            display: flex; justify-content: space-between; align-items: center;
            padding: 9px 12px; border-bottom: 1px solid #1e293b; font-size: 12px;
        }}
        .card-title {{ font-weight: 700; color: #e2e8f0; }}
        .card-badge {{
            font-size: 10px; font-weight: 700; padding: 2px 7px; border-radius: 999px;
            background: #1e293b; color: #94a3b8;
        }}
        .card-badge.origin {{ background: rgba(217, 119, 6, 0.25); color: #fcd34d; }}
        .card-img-wrap {{
            background: #020617; display: flex; justify-content: center; align-items: center;
            min-height: 220px; position: relative;
        }}
        .card-img {{
            width: 100%; max-height: 72vh; object-fit: contain; display: block;
        }}
        .card-actions {{
            display: flex; flex-wrap: wrap; gap: 6px; padding: 10px 10px;
            border-top: 1px solid #1e293b;
        }}
        .act-btn {{
            flex: 1 1 auto; min-width: 72px; padding: 8px 8px; border-radius: 9px;
            font-size: 12px; font-weight: 700; cursor: pointer; border: 1px solid #334155;
            background: #1e293b; color: #e2e8f0; text-align: center;
        }}
        .act-btn.origin {{ background: rgba(217,119,6,0.22); border-color: rgba(251,191,36,0.55); color: #fde68a; }}
        .act-btn.retouch {{ background: rgba(16,185,129,0.22); border-color: rgba(52,211,153,0.55); color: #6ee7b7; }}
        .act-btn.recensor {{ background: rgba(79,70,229,0.25); border-color: rgba(129,140,248,0.55); color: #c7d2fe; }}
        .act-btn.reroll {{ background: rgba(139,92,246,0.22); border-color: rgba(167,139,250,0.55); color: #ddd6fe; }}
        .act-btn.delete {{ background: rgba(225,29,72,0.22); border-color: rgba(244,63,94,0.55); color: #fda4af; }}
        .act-btn:disabled {{ opacity: 0.4; cursor: not-allowed; }}
        .toast-bar {{
            position: fixed; left: 12px; right: 12px; bottom: max(14px, env(safe-area-inset-bottom));
            z-index: 60; display: none; align-items: center; justify-content: space-between; gap: 10px;
            background: #1e293b; border: 1px solid #475569; border-radius: 12px; padding: 10px 14px;
            box-shadow: 0 12px 30px rgba(0,0,0,0.7); font-size: 12px;
        }}
        .toast-bar.visible {{ display: flex; }}
        .toast-undo {{
            background: #f59e0b; color: #020617; border: none; border-radius: 8px;
            padding: 6px 10px; font-weight: 800; font-size: 12px; cursor: pointer;
        }}
    </style>
    {RETOUCH_EDITOR_CSS}
</head>
<body>
    <header class="topbar">
        <div class="top-row">
            <a class="brand" href="/inspect">📱 Kiro 모바일 검수</a>
            <div class="nav-links">
                <a class="nav-pill" href="/">스튜디오</a>
                <a class="nav-pill" href="/gallery">갤러리</a>
                <button type="button" class="nav-pill" onclick="toggleKiroTokenBar()">🔑 토큰</button>
            </div>
        </div>
        <div class="controls">
            <select id="rosterSelect" class="ctrl-select" onchange="onRosterChange()"></select>
            <select id="charSelect" class="ctrl-select" onchange="renderCards()"></select>
            <select id="styleSelect" class="ctrl-select">{style_options_html}</select>
        </div>
        <div id="tokenBar" class="token-bar">
            <input id="tokenInput" type="password" class="token-input" placeholder="KIRO_API_TOKEN (원격 인증 설정 시 입력)" />
            <button type="button" class="nav-pill" onclick="saveKiroToken()">저장</button>
        </div>
    </header>

    <main id="assetFeed" class="feed">
        <div style="text-align:center;color:#64748b;padding:40px 0;">에셋 목록을 불러오는 중...</div>
    </main>

    <div id="toastBar" class="toast-bar">
        <span id="toastMsg"></span>
        <button id="toastUndoBtn" type="button" class="toast-undo" style="display:none;" onclick="undoLastDelete()">↩️ Undo</button>
    </div>

    {RETOUCH_EDITOR_HTML}
    {retouch_script}

    <script>
    (function() {{
        const DEFAULT_ROSTER = {html.escape(repr(default_roster), quote=False) if default_roster else "''"};
        const MOBILE_CHUNK_SIZE = 20;
        let currentMatrix = null;
        let lastTrash = null;
        let toastTimer = null;
        let renderSeq = 0;

        function getAuthHeaders(extra) {{
            const headers = Object.assign({{}}, extra || {{}});
            let tok = '';
            try {{ tok = sessionStorage.getItem('kiro_api_token') || ''; }} catch (e) {{}}
            if (!tok) tok = new URLSearchParams(window.location.search).get('token') || '';
            if (tok) headers['X-Kiro-Token'] = tok;
            return headers;
        }}

        window.toggleKiroTokenBar = function() {{
            const bar = document.getElementById('tokenBar');
            if (!bar) return;
            bar.classList.toggle('open');
            try {{
                document.getElementById('tokenInput').value = sessionStorage.getItem('kiro_api_token') || '';
            }} catch (e) {{}}
        }};

        window.saveKiroToken = function() {{
            const val = (document.getElementById('tokenInput').value || '').trim();
            try {{
                if (val) sessionStorage.setItem('kiro_api_token', val);
                else sessionStorage.removeItem('kiro_api_token');
            }} catch (e) {{}}
            document.getElementById('tokenBar').classList.remove('open');
            window.showKiroMobileToast(val ? 'API 토큰이 세션에 저장되었습니다.' : 'API 토큰이 해제되었습니다.', false);
        }};

        window.showKiroMobileToast = function(msg, isError, showUndo) {{
            const bar = document.getElementById('toastBar');
            const msgEl = document.getElementById('toastMsg');
            const undoBtn = document.getElementById('toastUndoBtn');
            if (!bar || !msgEl) return;
            msgEl.textContent = msg;
            bar.style.borderColor = isError ? '#f43f5e' : '#38bdf8';
            undoBtn.style.display = showUndo ? 'inline-block' : 'none';
            bar.classList.add('visible');
            if (toastTimer) clearTimeout(toastTimer);
            toastTimer = setTimeout(() => bar.classList.remove('visible'), showUndo ? 7000 : 3200);
        }};

        async function apiPost(url, body) {{
            const resp = await fetch(url, {{
                method: 'POST',
                headers: getAuthHeaders({{ 'Content-Type': 'application/json' }}),
                body: JSON.stringify(body)
            }});
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.detail || ('HTTP ' + resp.status));
            return data;
        }}

        function imgApiUrl(relPath, origin, ts) {{
            let u = '/api/image?path=' + encodeURIComponent(relPath);
            if (origin) u += '&origin=1';
            u += '&t=' + (ts || Date.now());
            return u;
        }}

        async function loadRosters() {{
            const resp = await fetch('/api/rosters');
            const data = await resp.json();
            const sel = document.getElementById('rosterSelect');
            sel.innerHTML = '';
            (data.rosters || []).forEach(r => {{
                const opt = document.createElement('option');
                opt.value = r;
                opt.textContent = r;
                sel.appendChild(opt);
            }});
            if (DEFAULT_ROSTER && (data.rosters || []).includes(DEFAULT_ROSTER)) {{
                sel.value = DEFAULT_ROSTER;
            }}
            await onRosterChange();
        }}

        window.onRosterChange = async function() {{
            const roster = document.getElementById('rosterSelect').value;
            if (!roster) return;
            const resp = await fetch('/api/gallery/assets?roster=' + encodeURIComponent(roster));
            currentMatrix = await resp.json();
            const cSel = document.getElementById('charSelect');
            const prevChar = cSel.value;
            cSel.innerHTML = '';
            const chars = currentMatrix.characters || [];
            let defaultChar = 'all';
            chars.forEach(ch => {{
                const opt = document.createElement('option');
                opt.value = ch.prefix;
                opt.textContent = ch.name + ' (' + ch.total_assets + '장)';
                cSel.appendChild(opt);
                if (defaultChar === 'all' && (ch.total_assets || 0) > 0) {{
                    defaultChar = ch.prefix;
                }}
            }});
            if (defaultChar === 'all' && chars.length > 0 && chars[0].prefix) {{
                defaultChar = chars[0].prefix;
            }}
            const allOpt = document.createElement('option');
            allOpt.value = 'all';
            allOpt.textContent = '전체 캐릭터 (' + (currentMatrix.total_assets || 0) + '장)';
            cSel.appendChild(allOpt);
            if (prevChar && Array.from(cSel.options).some(o => o.value === prevChar) && prevChar !== 'all') {{
                cSel.value = prevChar;
            }} else {{
                cSel.value = defaultChar;
            }}
            window.renderCards();
        }};

        function buildAssetCard(item, idx) {{
            const card = document.createElement('article');
            card.className = 'asset-card';
            card.dataset.rel = item.rel_path;
            const imgId = 'mimg_' + idx;
            const badgeId = 'mbadge_' + idx;
            card.innerHTML = `
                <div class="card-head">
                    <span class="card-title">[${{item.prefix}}] #${{item.code_str || '?'}} ${{item.pose_label}}</span>
                    <span id="${{badgeId}}" class="card-badge ${{item.has_origin ? 'origin' : ''}}">${{item.has_origin ? '검열본 (원본있음)' : '일반'}}</span>
                </div>
                <div class="card-img-wrap">
                    <img id="${{imgId}}" class="card-img" loading="lazy" decoding="async" data-origin="0" src="${{imgApiUrl(item.rel_path, false, Math.round((item.mtime || 0)*1000))}}" alt="${{item.filename}}" />
                </div>
                <div class="card-actions">
                    <button type="button" class="act-btn origin" ${{item.has_origin ? '' : 'disabled'}} data-act="toggle">👁️ 원본</button>
                    <button type="button" class="act-btn retouch" data-act="retouch">✏️ 리터치</button>
                    <button type="button" class="act-btn recensor" data-act="recensor">🛡️ 재검열</button>
                    <button type="button" class="act-btn origin" ${{item.has_origin ? '' : 'disabled'}} data-act="restore_origin">🔄 복원</button>
                    <button type="button" class="act-btn reroll" data-act="reroll">🎲 리롤</button>
                    <button type="button" class="act-btn delete" data-act="delete">🗑️ 삭제</button>
                </div>
            `;
            card.querySelector('[data-act="toggle"]').onclick = () => {{
                const imgEl = document.getElementById(imgId);
                const bEl = document.getElementById(badgeId);
                const nextOrig = imgEl.dataset.origin !== '1';
                imgEl.dataset.origin = nextOrig ? '1' : '0';
                imgEl.src = imgApiUrl(item.rel_path, nextOrig, Date.now());
                bEl.textContent = nextOrig ? '무검열 원본 (_origin)' : '검열본 (원본있음)';
            }};
            card.querySelector('[data-act="retouch"]').onclick = () => {{
                window.openKiroRetouchEditor(item.rel_path, item.prefix + ' #' + (item.code_str || '') + ' ' + item.pose_label, !!item.has_origin);
            }};
            card.querySelector('[data-act="recensor"]').onclick = async () => {{
                try {{
                    const style = document.getElementById('styleSelect').value;
                    await apiPost('/api/recensor', {{ path: item.rel_path, style }});
                    const imgEl = document.getElementById(imgId);
                    imgEl.dataset.origin = '0';
                    imgEl.src = imgApiUrl(item.rel_path, false, Date.now());
                    window.showKiroMobileToast('✅ 재검열 완료 (' + style + ')', false);
                }} catch (e) {{ window.showKiroMobileToast('❌ ' + e.message, true); }}
            }};
            card.querySelector('[data-act="restore_origin"]').onclick = async () => {{
                try {{
                    await apiPost('/api/restore_origin', {{ path: item.rel_path }});
                    const imgEl = document.getElementById(imgId);
                    imgEl.dataset.origin = '0';
                    imgEl.src = imgApiUrl(item.rel_path, false, Date.now());
                    window.showKiroMobileToast('✅ 무검열 원본 복원 완료', false);
                }} catch (e) {{ window.showKiroMobileToast('❌ ' + e.message, true); }}
            }};
            card.querySelector('[data-act="reroll"]').onclick = async () => {{
                try {{
                    const style = document.getElementById('styleSelect').value;
                    const r = await apiPost('/api/reroll', {{
                        roster: item.roster, char: item.char_name, code: item.code_str, censor_style: style
                    }});
                    window.showKiroMobileToast('🎲 리롤 큐 등록됨 (' + (r.job ? r.job.job_id : '') + ')', false);
                }} catch (e) {{ window.showKiroMobileToast('❌ ' + e.message, true); }}
            }};
            card.querySelector('[data-act="delete"]').onclick = async () => {{
                try {{
                    const r = await apiPost('/api/asset/delete', {{ path: item.rel_path }});
                    lastTrash = {{ trash_id: r.trash_id, roster: r.roster, filename: r.filename }};
                    card.remove();
                    window.showKiroMobileToast('🗑️ 휴지통 이동됨: ' + r.filename, false, true);
                }} catch (e) {{ window.showKiroMobileToast('❌ ' + e.message, true); }}
            }};
            return card;
        }}

        window.renderCards = function() {{
            const feed = document.getElementById('assetFeed');
            if (!currentMatrix) return;
            const mySeq = ++renderSeq;
            const selectedChar = document.getElementById('charSelect').value || 'all';
            const poseMap = {{}};
            (currentMatrix.poses || []).forEach(p => {{ poseMap[p.code_str] = p; }});

            const items = [];
            (currentMatrix.characters || []).forEach(ch => {{
                if (selectedChar !== 'all' && ch.prefix !== selectedChar) return;
                (ch.assets || []).forEach(a => {{
                    const pm = poseMap[a.code_str] || {{}};
                    items.push(Object.assign({{}}, a, {{
                        roster: currentMatrix.roster,
                        char_name: ch.name,
                        prefix: ch.prefix,
                        pose_label: pm.label || a.label || a.filename
                    }}));
                }});
            }});

            if (items.length === 0) {{
                feed.innerHTML = '<div style="text-align:center;color:#64748b;padding:40px 0;">표시할 에셋이 없습니다.</div>';
                return;
            }}

            feed.innerHTML = '';
            let cursor = 0;
            function appendNextChunk() {{
                if (mySeq !== renderSeq) return;
                const frag = document.createDocumentFragment();
                const end = Math.min(cursor + MOBILE_CHUNK_SIZE, items.length);
                for (let idx = cursor; idx < end; idx++) {{
                    frag.appendChild(buildAssetCard(items[idx], idx));
                }}
                feed.appendChild(frag);
                cursor = end;
                if (cursor < items.length) {{
                    window.requestAnimationFrame(appendNextChunk);
                }}
            }}
            appendNextChunk();
        }};

        window.undoLastDelete = async function() {{
            if (!lastTrash) return;
            try {{
                await apiPost('/api/asset/restore', {{ trash_id: lastTrash.trash_id, roster: lastTrash.roster }});
                const fname = lastTrash.filename;
                lastTrash = null;
                window.showKiroMobileToast('↩️ 복원 완료: ' + fname, false);
                await window.onRosterChange();
            }} catch (e) {{ window.showKiroMobileToast('❌ ' + e.message, true); }}
        }};

        window.onKiroMobileRetouchSaved = function(payload) {{
            if (!payload || !payload.rel_path) return;
            const cards = document.querySelectorAll('.asset-card');
            cards.forEach(card => {{
                if (card.dataset.rel === payload.rel_path) {{
                    const imgEl = card.querySelector('.card-img');
                    if (imgEl) {{
                        imgEl.dataset.origin = '0';
                        imgEl.src = imgApiUrl(payload.rel_path, false, payload.timestamp);
                    }}
                }}
            }});
        }};

        loadRosters().catch(err => window.showKiroMobileToast('로드 실패: ' + err.message, true));
    }})();
    </script>
</body>
</html>
"""


def register_mobile_viewer_route(app: Any, base_dir: Path | None = None) -> bool:
    """FastAPI / NiceGUI 앱에 /inspect 모바일 검수 뷰어 라우트를 중복 없이 1회 등록한다."""
    state_obj = getattr(app, "state", None)
    if state_obj is not None and getattr(state_obj, "_kiro_mobile_route_registered", False):
        return False
    if getattr(app, "_kiro_mobile_route_registered", False):
        return False

    @app.get("/inspect", response_class=HTMLResponse, include_in_schema=False)
    async def inspect_mobile_page(roster: str = "") -> HTMLResponse:
        return HTMLResponse(
            content=generate_mobile_viewer_html(default_roster=roster),
            headers={"Cache-Control": "no-store"},
        )

    if state_obj is not None:
        setattr(state_obj, "_kiro_mobile_route_registered", True)
    else:
        setattr(app, "_kiro_mobile_route_registered", True)
    return True


__all__ = [
    "generate_mobile_viewer_html",
    "register_mobile_viewer_route",
]

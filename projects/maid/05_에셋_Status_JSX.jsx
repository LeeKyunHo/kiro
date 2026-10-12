// [Project MAID - Custom Status Component]
// 젠잇 [에셋 - 템플릿] 등록용 컴포넌트 코드 (ID: Status)
// 특징: 메이드 4인 테마 컬러 매핑, 라이트 모드 #000000 검정 가시성, 슬림화 레이아웃, 원클릭 즉시 전송

function Status(props) {
  // 1. 치환 변수 안전 구독 (React 훅 최상위 선언 불변식)
  const gTurn = typeof useTemplateValue === "function" ? useTemplateValue("turn_count") : null;
  const gLoc = typeof useTemplateValue === "function" ? useTemplateValue("loc_cur") : null;

  // 2. 턴 숫자 파싱 (슬래시 제거 및 순수 숫자 정제)
  const rawTurn = props.turn || gTurn || "1";
  const displayTurn = String(rawTurn).split("/")[0].trim();

  // 3. 르 시엘 장소 코드 매핑 (001~005)
  const resolvedLoc = props.location || gLoc || "001";
  const locationMap = {
    "001": "메인 홀",
    "002": "카운터 석",
    "003": "백스테이지",
    "004": "라이브 무대",
    "005": "점장실 (밀실)"
  };
  const displayLocation = locationMap[resolvedLoc] || "르 시엘 구역";

  // 4. 캐릭터 및 스탯 정보
  const charName = props.char_name || "하나";
  const affNum = Math.min(100, Math.max(0, parseInt(props.affection || "20", 10)));
  const statName = props.stat_name || "스트레스";
  const statNum = Math.min(100, Math.max(0, parseInt(props.stat_value || "30", 10)));
  const reaction = props.reaction || "점장님의 눈치를 살피고 있습니다...";

  // 5. 캐릭터별 테마 악센트 컬러 매핑
  const themeMap = {
    "하나": { accent: "#ec4899", bgSubtle: "light-dark(#fdf2f8, #500724)" },
    "루비": { accent: "#f97316", bgSubtle: "light-dark(#fff7ed, #431407)" },
    "시온": { accent: "#6366f1", bgSubtle: "light-dark(#eef2ff, #1e1b4b)" },
    "유아": { accent: "#a855f7", bgSubtle: "light-dark(#faf5ff, #3b0764)" }
  };
  const currentTheme = themeMap[charName] || { accent: "#ec4899", bgSubtle: "light-dark(#fdf2f8, #500724)" };

  // 6. 관계 단계 (Phase) 자동 판정
  let phaseText = "Phase 1 비즈니스";
  const isSion = charName.indexOf("시온") !== -1;
  const p4Req = isSion ? 95 : 90;
  const p3Req = isSion ? 80 : 70;
  const p2Req = isSion ? 50 : 40;

  if (affNum >= p4Req) phaseText = "Phase 4 전속/헌신";
  else if (affNum >= p3Req) phaseText = "Phase 3 밀착/VIP";
  else if (affNum >= p2Req) phaseText = "Phase 2 신뢰/호감";

  // 7. 원클릭 즉시 전송 핸들러 (확인창 없이 즉시 발송)
  const handleAction = (text) => {
    if (typeof sendMessage === "function") {
      sendMessage(text, false);
    }
  };

  return (
    <div style={{
      margin: "12px 0",
      padding: "14px 16px",
      borderRadius: "14px",
      background: "var(--color-card, #ffffff)",
      background: "light-dark(#ffffff, #18181b)",
      border: "1px solid light-dark(#e4e4e7, #27272a)",
      boxShadow: "0 4px 12px rgba(0, 0, 0, 0.05)",
      fontFamily: "system-ui, -apple-system, sans-serif"
    }}>
      {/* 상단 헤더: 장소 배지 & TURN */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "10px" }}>
        <span style={{
          fontSize: "12px",
          fontWeight: "700",
          padding: "3px 8px",
          borderRadius: "6px",
          background: "light-dark(#f4f4f5, #27272a)",
          color: "light-dark(#18181b, #f4f4f5)"
        }}>
          📍 {displayLocation}
        </span>
        <span style={{
          fontSize: "13px",
          fontWeight: "800",
          color: "light-dark(#000000, #ffffff)"
        }}>
          TURN {displayTurn}
        </span>
      </div>

      {/* 캐릭터 이름 및 Phase 상태 */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: "8px" }}>
        <span style={{ fontSize: "15px", fontWeight: "800", color: "light-dark(#000000, #ffffff)" }}>
          {charName}
        </span>
        <span style={{ fontSize: "12px", fontWeight: "700", color: currentTheme.accent }}>
          {phaseText}
        </span>
      </div>

      {/* 호감도 게이지 바 */}
      <div style={{ marginBottom: "8px" }}>
        <div style={{ display: "flex", justifyContent: "space-between", fontSize: "11px", fontWeight: "700", color: "light-dark(#000000, #ffffff)", marginBottom: "3px" }}>
          <span>호감도 (Affection)</span>
          <span>{affNum}%</span>
        </div>
        <div style={{ width: "100%", height: "6px", background: "light-dark(#f4f4f5, #27272a)", borderRadius: "3px", overflow: "hidden" }}>
          <div style={{
            width: `${affNum}%`,
            height: "100%",
            background: `linear-gradient(90deg, #f472b6, ${currentTheme.accent})`,
            transition: "width 0.3s ease"
          }} />
        </div>
      </div>

      {/* 보조 스탯 (스트레스/피로도) 게이지 바 */}
      <div style={{ marginBottom: "10px" }}>
        <div style={{ display: "flex", justifyContent: "space-between", fontSize: "11px", fontWeight: "700", color: "light-dark(#000000, #ffffff)", marginBottom: "3px" }}>
          <span>{statName}</span>
          <span style={{ color: statNum >= 70 ? "#ef4444" : "light-dark(#000000, #ffffff)" }}>{statNum}/100</span>
        </div>
        <div style={{ width: "100%", height: "6px", background: "light-dark(#f4f4f5, #27272a)", borderRadius: "3px", overflow: "hidden" }}>
          <div style={{
            width: `${statNum}%`,
            height: "100%",
            background: statNum >= 70 ? "linear-gradient(90deg, #f87171, #ef4444)" : "linear-gradient(90deg, #4ade80, #22c55e)",
            transition: "width 0.3s ease"
          }} />
        </div>
      </div>

      {/* 캐릭터 속마음 리액션 말풍선 */}
      <div style={{
        padding: "8px 10px",
        borderRadius: "8px",
        background: "light-dark(#f8fafc, #27272a)",
        fontSize: "12px",
        fontStyle: "italic",
        color: "light-dark(#000000, #ffffff)",
        fontWeight: "600",
        marginBottom: "10px",
        borderLeft: `3px solid ${currentTheme.accent}`
      }}>
        "{reaction}"
      </div>

      {/* 원클릭 인터랙션 퀵 버튼 2종 */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "6px" }}>
        <button
          onClick={() => handleAction(`*${charName}의 손을 따뜻하게 잡으며 부드러운 눈빛으로 위로한다.*`)}
          style={{
            padding: "8px 0",
            borderRadius: "8px",
            border: `1px solid ${currentTheme.accent}`,
            background: currentTheme.bgSubtle,
            color: "light-dark(#000000, #ffffff)",
            fontSize: "12px",
            fontWeight: "700",
            cursor: "pointer"
          }}
        >
          💖 다정하게 격려
        </button>
        <button
          onClick={() => handleAction(`*${charName}에게 시원한 음료를 건네며 잠시 휴식을 권한다.*`)}
          style={{
            padding: "8px 0",
            borderRadius: "8px",
            border: "1px solid light-dark(#e2e8f0, #334155)",
            background: "light-dark(#f1f5f9, #1e293b)",
            color: "light-dark(#000000, #ffffff)",
            fontSize: "12px",
            fontWeight: "700",
            cursor: "pointer"
          }}
        >
          ☕ 음료 & 휴식 권유
        </button>
      </div>
    </div>
  );
}

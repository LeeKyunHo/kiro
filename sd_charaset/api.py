"""
WebUI API 클라이언트.

전역 `lru_cache` 세션 대신 **클래스로 캡슐화하고 주입**한다.

이유가 두 가지다. 첫째, 테스트에서 fake 클라이언트를 넣을 수 있다.
전역 상태에 의존하면 테스트가 서로 간섭한다. 둘째, 세션 수명이 객체
수명과 일치해 명시적으로 닫을 수 있다.

이 모듈은 페이로드를 **조립하지 않는다.** 조립은 `payload` 모듈의 순수
함수가 담당하고, 여기는 전송과 응답 해석만 한다. 그 경계가 GPU 없는
환경에서 페이로드 구조를 검증할 수 있게 만드는 근거다.
"""

from __future__ import annotations

import base64
from types import TracebackType
from typing import Any, Sequence

import requests

from . import config, payload
from .errors import ApiError, ApiUnavailableError
from .logging_setup import get_logger
from .models import ControlNetSpec, VramSnapshot

_logger = get_logger("api")


class WebUiClient:
    """
    Stable Diffusion WebUI REST API 클라이언트.

    컨텍스트 매니저로 쓰면 세션이 확실히 닫힌다.

        with WebUiClient() as client:
            client.generate(payload)
    """

    def __init__(
        self,
        host: str = config.API_HOST,
        session: requests.Session | None = None,
    ) -> None:
        self.host = host.rstrip("/")
        # 배치당 수십 건을 순차 요청하므로 커넥션을 유지하면 매 요청의
        # TCP 핸드셰이크가 사라진다.
        self._session = session if session is not None else requests.Session()
        self._owns_session = session is None

    # ── 컨텍스트 매니저 ──────────────────────────
    def __enter__(self) -> WebUiClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """직접 만든 세션만 닫는다. 주입된 세션은 호출자 소유다."""
        if self._owns_session:
            self._session.close()

    # ── 내부 헬퍼 ────────────────────────────────
    def _url(self, path: str) -> str:
        return f"{self.host}{path}"

    def _post(self, path: str, body: dict[str, Any], timeout: int) -> dict[str, Any]:
        url = self._url(path)
        try:
            response = self._session.post(url, json=body, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.ConnectionError as exc:
            raise ApiUnavailableError(
                "WebUI 에 연결할 수 없습니다.",
                "webui-user.bat 에 --api 를 넣고 실행했는지 확인하세요.",
            ) from exc
        except requests.exceptions.Timeout as exc:
            raise ApiError(f"요청 시간 초과 ({timeout}초): {path}") from exc
        except requests.exceptions.HTTPError as exc:
            raise ApiError(f"API 오류 응답 ({path}): {exc}") from exc
        except ValueError as exc:
            raise ApiError(f"응답이 JSON 이 아닙니다 ({path}): {exc}") from exc

    def _get_optional(self, path: str, timeout: int) -> dict[str, Any] | None:
        """
        조회 실패를 예외로 올리지 않는다.

        샘플러 목록, ControlNet 목록, VRAM 은 모두 "있으면 좋은" 정보다.
        조회 실패로 배치를 중단시키면 안 되므로 None 을 반환한다.
        """
        try:
            response = self._session.get(self._url(path), timeout=timeout)
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else {"_list": data}
        except Exception as exc:  # noqa: BLE001 — 부가 정보이므로 전부 흡수
            _logger.debug("조회 실패 (%s): %s", path, exc)
            return None

    # ── 공개 API ─────────────────────────────────
    def resolve_sampler(self) -> str:
        """
        지원 샘플러 목록과 대조해 사용 가능한 첫 후보를 반환한다.

        최신 WebUI/Forge 는 샘플러와 스케줄러가 분리되어
        "DPM++ 2M Karras" 가 목록에 없을 수 있다. 그러면 "DPM++ 2M" 으로
        폴백하고 스케줄러는 WebUI 기본값을 쓴다.
        """
        data = self._get_optional(config.SAMPLERS_PATH, config.TIMEOUT_SAMPLERS)
        if data is None:
            _logger.info("[SAMPLER] 목록 조회 실패 - 기본값 사용")
            return config.SAMPLER_CANDIDATES[0]

        raw = data.get("_list", data)
        names: set[str] = set()
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict) and isinstance(item.get("name"), str):
                    names.add(item["name"])

        for candidate in config.SAMPLER_CANDIDATES:
            if candidate in names:
                _logger.info("[SAMPLER] '%s' 감지됨", candidate)
                return candidate

        _logger.info(
            "[SAMPLER] 후보 미발견 - '%s' 로 전달", config.SAMPLER_CANDIDATES[0]
        )
        return config.SAMPLER_CANDIDATES[0]

    def resolve_controlnet(
        self, manual_module: str | None = None, manual_model: str | None = None
    ) -> ControlNetSpec | None:
        """
        ControlNet 전처리기와 모델을 해석한다. 실패하면 None.

        둘 다 수동 지정되면 조회를 생략한다. GPU 가 없는 환경에서 주입
        경로를 검증할 때의 우회로이기도 하다.

        매칭 실패 시 **조회된 목록 전체를 출력한다.** GPU 환경에서 그
        출력만 보고 바로 `--cn_module` / `--cn_model` 을 지정할 수 있게
        하려는 것이다. 검증 세션을 짧게 만드는 장치다.
        """
        if manual_module and manual_model:
            return ControlNetSpec(manual_module, manual_model, "manual")

        modules_data = self._get_optional(
            config.CN_MODULES_PATH, config.TIMEOUT_CONTROLNET_LIST
        )
        models_data = self._get_optional(
            config.CN_MODELS_PATH, config.TIMEOUT_CONTROLNET_LIST
        )

        if modules_data is None or models_data is None:
            _logger.warning("ControlNet 목록 조회 실패 - 참조 이미지 없이 생성합니다")
            _logger.warning("ControlNet 확장이 설치되어 있는지 확인하세요.")
            return None

        modules = _string_list(modules_data.get("module_list"))
        models = _string_list(models_data.get("model_list"))

        module = manual_module or payload.match_pattern(
            modules, config.IP_ADAPTER_MODULE_PATTERNS
        )
        model = manual_model or payload.match_pattern(
            models, config.IP_ADAPTER_MODEL_PATTERNS
        )

        if not module or not model:
            _logger.warning("IP-Adapter 모듈/모델을 찾지 못했습니다.")
            _logger.warning(
                "  모듈 후보 %s -> %s",
                list(config.IP_ADAPTER_MODULE_PATTERNS),
                module or "없음",
            )
            _logger.warning(
                "  모델 후보 %s -> %s",
                list(config.IP_ADAPTER_MODEL_PATTERNS),
                model or "없음",
            )
            _logger.warning("  사용 가능 모듈 (%d): %s", len(modules), modules)
            _logger.warning("  사용 가능 모델 (%d): %s", len(models), models)
            _logger.warning("  --cn_module / --cn_model 로 직접 지정하세요.")
            return None

        return ControlNetSpec(module, model, "auto")

    def generate(self, body: dict[str, Any]) -> bytes:
        """
        txt2img 를 호출해 PNG 바이트를 반환한다.

        Raises:
            ApiUnavailableError: 연결 불가. 배치를 중단시켜야 하는 실패.
            ApiError: 그 외 오류. 다음 코드로 계속 진행해도 되는 실패.
        """
        data = self._post(
            config.TXT2IMG_PATH, body, config.TIMEOUT_TXT2IMG
        )
        images = data.get("images") or []
        if not images or not isinstance(images[0], str):
            # 명시적으로 걸러야 KeyError/IndexError 대신 읽을 수 있는
            # 메시지가 남는다.
            raise ApiError("API 응답에 images 가 없습니다")

        try:
            return base64.b64decode(images[0])
        except (ValueError, TypeError) as exc:
            raise ApiError(f"이미지 디코딩 실패: {exc}") from None

    def interrogate(self, image_b64: str, model: str) -> str:
        """
        이미지에서 프롬프트를 역추출한다.

        Returns:
            추출된 caption 문자열.

        Raises:
            ApiUnavailableError, ApiError
        """
        body = payload.build_interrogate_payload(image_b64, model)
        data = self._post(
            config.INTERROGATE_PATH, body, config.TIMEOUT_INTERROGATE
        )
        caption = data.get("caption")
        if not isinstance(caption, str) or not caption.strip():
            raise ApiError("추출된 태그가 없습니다")
        return caption.strip()

    def vram_snapshot(self) -> VramSnapshot | None:
        """
        VRAM 피크를 조회한다. 실패하면 None (배치에 영향 없음).

        응답 구조가 버전마다 달라 `payload.extract_vram_peak` 이 여러 키를
        순차 탐색한다.
        """
        data = self._get_optional(config.MEMORY_PATH, config.TIMEOUT_MEMORY)
        if data is None:
            return None
        parsed = payload.extract_vram_peak(data)
        if parsed is None:
            return None
        peak, total = parsed
        return VramSnapshot(peak=peak, total=total)


def _string_list(raw: object) -> list[str]:
    """응답의 리스트 필드에서 문자열만 추린다."""
    if not isinstance(raw, (list, tuple)):
        return []
    return [item for item in raw if isinstance(item, str)]

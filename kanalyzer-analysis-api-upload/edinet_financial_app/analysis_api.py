from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from analysis_service import AnalysisServiceError, run_public_analysis
from edinet_api import EdinetApiError


LOGGER = logging.getLogger(__name__)


def create_app() -> FastAPI:
    app = FastAPI(title="KAnalyzer Analysis API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_allowed_origins(),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type"],
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/annual-reports/search")
    def search_annual_reports(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return run_public_analysis(payload)
        except AnalysisServiceError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except (EdinetApiError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            LOGGER.exception("analysis_api_unhandled_error")
            raise HTTPException(status_code=500, detail="分析処理中にエラーが発生しました。") from exc

    return app


def _allowed_origins() -> list[str]:
    raw = os.getenv("KANALYZER_ALLOWED_ORIGINS")
    if raw:
        return [origin.strip() for origin in raw.split(",") if origin.strip()]
    return [
        "https://kanalyzer-edinet-finance.ktsht.chatgpt.site",
        "https://market-note-jp.github.io",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8787",
        "http://127.0.0.1:8787",
    ]


app = create_app()

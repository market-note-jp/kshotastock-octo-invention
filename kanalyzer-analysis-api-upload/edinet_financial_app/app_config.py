from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


APP_NAME = "KAnalyzer"
CONFIG_FILE_NAME = "config.json"
DATA_DIR_NAME = "data"
KEYRING_SERVICE_NAME = "KAnalyzer"
KEYRING_USERNAME = "EDINET_API_KEY"
KEY_STORAGE_KEYRING = "keyring"
KEY_STORAGE_LOCAL_CONFIG = "local_config"


@dataclass(frozen=True)
class EdinetApiKeyStatus:
    api_key: str | None
    configured: bool
    config_path: Path
    storage: str
    storage_label: str
    security_note: str
    keyring_available: bool


def get_user_config_dir() -> Path:
    override = os.getenv("KANALYZER_CONFIG_DIR")
    if override:
        return Path(override).expanduser()

    appdata = os.getenv("APPDATA")
    if appdata:
        return Path(appdata) / APP_NAME

    if os.name == "nt":
        return Path.home() / "AppData" / "Roaming" / APP_NAME

    xdg_config_home = os.getenv("XDG_CONFIG_HOME")
    if xdg_config_home:
        return Path(xdg_config_home) / APP_NAME
    return Path.home() / ".config" / APP_NAME


def get_config_path() -> Path:
    return get_user_config_dir() / CONFIG_FILE_NAME


def get_user_data_dir() -> Path:
    path = get_user_config_dir() / DATA_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_edinet_api_key() -> str | None:
    config = _load_config()
    storage = config.get("edinet_api_key_storage")

    if storage == KEY_STORAGE_KEYRING:
        keyring_key = _get_keyring_password()
        if keyring_key:
            return keyring_key

    local_key = config.get("edinet_api_key")
    if isinstance(local_key, str) and local_key.strip():
        return local_key.strip()
    return None


def get_edinet_api_key_status() -> EdinetApiKeyStatus:
    config = _load_config()
    api_key = load_edinet_api_key()
    storage = str(config.get("edinet_api_key_storage") or "")
    keyring_available = _get_keyring_module() is not None

    if storage == KEY_STORAGE_KEYRING and api_key:
        storage_label = "Windows資格情報 / keyring"
        security_note = "APIキー本体はOSの資格情報ストアに保存し、config.jsonには保存方式だけを記録しています。"
    elif api_key:
        storage = KEY_STORAGE_LOCAL_CONFIG
        storage_label = "ローカル設定ファイル"
        security_note = "keyringを利用できないため、APIキーはローカルのconfig.jsonに保存されています。PC外へ共有しないでください。"
    else:
        storage = "unset"
        storage_label = "未設定"
        security_note = "EDINET APIキーはまだ保存されていません。"

    return EdinetApiKeyStatus(
        api_key=api_key,
        configured=bool(api_key),
        config_path=get_config_path(),
        storage=storage,
        storage_label=storage_label,
        security_note=security_note,
        keyring_available=keyring_available,
    )


def save_edinet_api_key(api_key: str) -> EdinetApiKeyStatus:
    cleaned_key = (api_key or "").strip()
    if not cleaned_key:
        raise ValueError("EDINET APIキーを入力してください。")
    if cleaned_key == "your_edinet_api_key_here":
        raise ValueError("EDINET APIキーが初期値のままです。金融庁EDINETで取得したAPIキーを入力してください。")

    config = _load_config()
    config["app_name"] = APP_NAME
    config["updated_at"] = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    keyring_module = _get_keyring_module()
    if keyring_module is not None:
        try:
            keyring_module.set_password(KEYRING_SERVICE_NAME, KEYRING_USERNAME, cleaned_key)
            config["edinet_api_key_storage"] = KEY_STORAGE_KEYRING
            config.pop("edinet_api_key", None)
            _write_config(config)
            return get_edinet_api_key_status()
        except Exception as exc:
            config["keyring_error"] = str(exc)

    config["edinet_api_key_storage"] = KEY_STORAGE_LOCAL_CONFIG
    config["edinet_api_key"] = cleaned_key
    _write_config(config)
    return get_edinet_api_key_status()


def _load_config() -> dict[str, Any]:
    path = get_config_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_config(config: dict[str, Any]) -> None:
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def _get_keyring_password() -> str | None:
    keyring_module = _get_keyring_module()
    if keyring_module is None:
        return None
    try:
        value = keyring_module.get_password(KEYRING_SERVICE_NAME, KEYRING_USERNAME)
    except Exception:
        return None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _get_keyring_module():
    if os.getenv("KANALYZER_DISABLE_KEYRING") == "1":
        return None
    try:
        import keyring  # type: ignore
    except Exception:
        return None
    return keyring

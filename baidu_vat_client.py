# baidu_vat_client.py
import os, json, time, base64, requests
from typing import Optional, Tuple

BAIDU_TOKEN_CACHE = "/tmp/baidu_token.json"
BAIDU_OAUTH = "https://aip.baidubce.com/oauth/2.0/token"
BAIDU_VAT_URL = "https://aip.baidubce.com/rest/2.0/ocr/v1/vat_invoice"

class BaiduVatClient:
    def __init__(self, ak: str, sk: str, timeout: int = 60):
        self.ak, self.sk, self.timeout = ak, sk, timeout

    # —— 1) token 缓存：优先用缓存，临期自动刷新 —— #
    def _load_cached_token(self) -> Optional[str]:
        try:
            j = json.loads(open(BAIDU_TOKEN_CACHE, "r", encoding="utf-8").read())
            if j.get("access_token") and j.get("expire_at", 0) - time.time() > 300:
                return j["access_token"]
        except Exception:
            pass
        return None

    def _save_cached_token(self, token: str, expires_in: int):
        expire_at = int(time.time()) + int(expires_in)
        json.dump({"access_token": token, "expire_at": expire_at}, open(BAIDU_TOKEN_CACHE, "w"), ensure_ascii=False)

    def _get_token(self) -> str:
        cached = self._load_cached_token()
        if cached:
            return cached
        r = requests.post(BAIDU_OAUTH, params={
            "grant_type": "client_credentials",
            "client_id": self.ak, "client_secret": self.sk
        }, timeout=15)
        jr = r.json()
        token = jr["access_token"]
        self._save_cached_token(token, jr.get("expires_in", 2592000))
        return token

    # —— 2) 主调用：image / pdf_file / ofd_file 三选一；不要手动 urlencode —— #
    def recognize(self, *, image_bytes: bytes = None, pdf_bytes: bytes = None, ofd_bytes: bytes = None) -> dict:
        if not any([image_bytes, pdf_bytes, ofd_bytes]):
            return {"__ocr_error__": "no_input", "detail": "need image/pdf/ofd bytes"}

        token = self._get_token()
        data = {"seal_tag": "false"}
        if image_bytes:
            data["image"] = base64.b64encode(image_bytes).decode("utf-8")
        elif pdf_bytes:
            data["pdf_file"] = base64.b64encode(pdf_bytes).decode("utf-8")
        else:
            data["ofd_file"] = base64.b64encode(ofd_bytes).decode("utf-8")

        # —— 指数退避重试：最多 5 次 —— #
        import time as _t
        for attempt in range(5):
            resp = requests.post(
                BAIDU_VAT_URL, params={"access_token": token}, data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded","Accept":"application/json"},
                timeout=self.timeout
            )
            try:
                jr = resp.json()
            except Exception:
                if attempt == 4:
                    return {"__ocr_error__": "bad_json", "http_status": resp.status_code, "raw": resp.text}
                _t.sleep(0.2 * (2 ** attempt)); continue

            # 统一错误映射
            if "error_code" in jr or "error_msg" in jr:
                code = str(jr.get("error_code") or "").strip()
                msg  = (jr.get("error_msg") or "").strip()

                # 兼容：有些网关只回纯文案（比如 Open api qps...），没有 error_code
                if not code and msg.lower().startswith("open api qps"):
                    code = "18"

                if code in {"18", "19"} and attempt < 4:  # QPS/并发类 → 重试
                    _t.sleep(0.2 * (2 ** attempt) + (0.05 * attempt))  # 指数退避 + 抖动
                    continue

                jr["__ocr_error__"] = f"{code}:{msg}" if code else msg
                jr["http_status"] = resp.status_code
                jr["log_id"] = jr.get("log_id")
                return jr

            # 正常
            jr["http_status"] = resp.status_code
            return jr

        # 理论到不了
        return {"__ocr_error__": "retry_exhausted"}

def load_ak_sk() -> Tuple[str, str]:
    # 从环境变量或你的 config.json 读取
    ak = os.getenv("BAIDU_AK"); sk = os.getenv("BAIDU_SK")
    if not ak or not sk:
        try:
            cfg = json.load(open("/srv/baidu_ocr_test/config.json", "r", encoding="utf-8"))
            ak, sk = cfg["BAIDU_AK"], cfg["BAIDU_SK"]
        except Exception:
            raise RuntimeError("找不到 BAIDU_AK/BAIDU_SK，请配置环境变量或提供 config.json")
    return ak, sk
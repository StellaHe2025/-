# app.py
# -*- coding: utf-8 -*-

import os
import json
import logging
from typing import Dict, Any, Optional

import httpx

from invoice_extractor import InvoiceExtractor
from expense_analyzer import ExpenseAnalyzer
from knowledge_retriever import KnowledgeRetriever
from invoice_verifier import InvoiceVerifier
from reimbursement_processor import ReimbursementProcessor

def _sanitize_env(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = value.strip()
    return v if v else None

log = logging.getLogger("app")
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 优先环境变量，其次项目内的 knowledge_base 目录
DEFAULT_KB_DIR = os.getenv("KB_DIR") or os.path.join(BASE_DIR, "knowledge_base")

def _norm_base_url(url: str) -> str:
    url = (url or "").strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url.rstrip("/")

# ---------- Baidu OAuth: 按需获取 access_token ----------
def fetch_baidu_access_token(api_key: str, secret_key: str) -> Optional[str]:
    if not api_key or not secret_key:
        return None
    url = (
        "https://aip.baidubce.com/oauth/2.0/token"
        f"?grant_type=client_credentials&client_id={api_key}&client_secret={secret_key}"
    )
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(url)
        if resp.status_code == 200:
            data = resp.json()
            token = data.get("access_token")
            if token:
                log.info("Baidu access_token 获取成功（OAuth）")
                return token
            log.warning(f"Baidu OAuth 无 token: {data}")
        else:
            log.warning(f"Baidu OAuth HTTP {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        log.warning(f"Baidu OAuth 调用异常: {e}")
    return None

def _load_config() -> Dict[str, Any]:
    """读 config.json；缺就用环境变量兜底。"""
    cfg: Dict[str, Any] = {}
    cfg_path = os.path.join(BASE_DIR, "config.json")
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:
            log.warning(f"读取 config.json 失败：{e}，改用环境变量")

    baidu_ocr = cfg.get("baidu_ocr", {})
    llm = cfg.get("llm", {})
    ragflow = cfg.get("ragflow", {})
    zhubajie_verify = cfg.get("zhubajie_verify", {})

    cfg["baidu_ocr"] = {
        "api_key": baidu_ocr.get("api_key", os.getenv("BAIDU_OCR_API_KEY", "")),
        "secret_key": baidu_ocr.get("secret_key", os.getenv("BAIDU_OCR_SECRET_KEY", "")),
        "access_token": baidu_ocr.get("access_token", os.getenv("BAIDU_OCR_ACCESS_TOKEN", "")),
    }

    llm_base = llm.get("base_url", os.getenv("LLM_BASE_URL", ""))
    cfg["llm"] = {
        "api_key": llm.get("api_key", os.getenv("LLM_API_KEY", "")),
        # 兜底补协议；如没配，仍可走 OpenAI 兼容默认
        "base_url": _norm_base_url(llm_base) or "https://api.openai.com/v1",
        "model": llm.get("model", os.getenv("LLM_MODEL", "gpt-3.5-turbo")),
    }

    cfg["ragflow"] = {
        "api_url": ragflow.get("api_url", os.getenv("RAGFLOW_API_URL", "") or None),
        "api_key": ragflow.get("api_key", os.getenv("RAGFLOW_API_KEY", "") or None),
        "knowledge_base_id": ragflow.get("knowledge_base_id", os.getenv("RAGFLOW_KB_ID", "") or None),
    }

    # 防 config.json"反向覆盖"：环境变量优先
    cfg["kb_dir"] = os.getenv("KB_DIR") or cfg.get("kb_dir") or DEFAULT_KB_DIR
    cfg["public_kb_base"] = os.getenv("PUBLIC_KB_BASE") or cfg.get("public_kb_base") or ""

    cfg["zhubajie_verify"] = {
        "app_code": zhubajie_verify.get("app_code", os.getenv("ZHUBAJIE_VERIFY_APP_CODE", "")),
    }
    return cfg

def _ensure_baidu_tokens(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """确保百度OCR的access_token存在，缺了就OAuth获取。"""
    ocr = cfg["baidu_ocr"]
    if not ocr.get("access_token"):
        token = fetch_baidu_access_token(ocr.get("api_key", ""), ocr.get("secret_key", ""))
        if token:
            ocr["access_token"] = token
    return cfg

def create_reimbursement_agent() -> ReimbursementProcessor:
    """初始化报销处理系统（本地知识库优先，路径 Linux/Win 均可）。"""
    config = _load_config()
    config = _ensure_baidu_tokens(config)

    kb_dir = os.path.abspath(config["kb_dir"])
    log.info("知识库路径：%s", kb_dir)

    # 2) 解析 KB 路径（环境 > config.json > 默认）
    env_kb = _sanitize_env(os.getenv("KB_DIR"))
    default_kb = os.path.abspath(os.path.join(os.path.dirname(__file__), "knowledge_base"))
    kb_dir = env_kb or config.get("kb_dir") or default_kb
    kb_dir = os.path.abspath(kb_dir)

    # 3) 防呆：确保目录存在且有文件
    if not os.path.isdir(kb_dir):
        raise FileNotFoundError(f"知识库目录不存在: {kb_dir}")
    # 可选：至少要有 1 个 .txt/.md
    has_docs = any(name.endswith((".txt",".md",".csv",".json")) for name in os.listdir(kb_dir))
    if not has_docs:
        raise RuntimeError(f"知识库为空或无可用文档: {kb_dir}")

    log.info(f"📚 知识库路径（最终生效）: {kb_dir}")

    # 4) 发票提取
    extractor = InvoiceExtractor(
        config["baidu_ocr"]["api_key"],
        config["baidu_ocr"]["secret_key"],
        config["baidu_ocr"]["access_token"],
    )
    # 2) 费用分析（OpenAI 兼容接口）
    analyzer = ExpenseAnalyzer(
        config["llm"]["api_key"],
        config["llm"]["base_url"],
        config["llm"]["model"],
    )
    # 3) 知识检索（本地优先 + 可选远端）
    retriever = KnowledgeRetriever(
        config.get("ragflow", {}).get("api_url"),
        config.get("ragflow", {}).get("api_key"),
        config.get("ragflow", {}).get("knowledge_base_id"),
        kb_dir,
    )
    # 4) 发票验真
    verifier = InvoiceVerifier(
        config["zhubajie_verify"]["app_code"],
    )
    # 5) 组装
    processor = ReimbursementProcessor(extractor, analyzer, retriever, verifier)
    log.info("✅ 报销处理系统初始化完成（本地知识库优先）")
    return processor
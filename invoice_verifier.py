# invoice_verifier.py — Aliyun(猪八戒) 发票验真封装（v2，兼容无代码，双金额、日期归一化、调试日志）
# -*- coding: utf-8 -*-
from typing import Dict, Any, Optional
import requests
import os

ALI_HOST = "https://fapiao.market.alicloudapi.com"
ALI_PATH_V2 = "/v2/invoice/query"

def _to_yyyymmdd(s: str) -> str:
    s = str(s or "").strip().replace("年","-").replace("月","-").replace("日","").replace("/","-")
    if "-" in s:
        p = s.split("-")
        if len(p) == 3 and all(p):
            return f"{p[0]}{p[1].zfill(2)}{p[2].zfill(2)}"
    if len(s) == 8 and s.isdigit():
        return s
    return s

def _to_2dec(x: Any) -> str:
    try:
        v = float(str(x).replace(",", "").strip())
        return f"{v:.2f}"
    except Exception:
        return ""

class InvoiceVerifier:
    """
    ReimbursementProcessor._call_verifier(payload, allow_without_jym=False) 适配
    - payload 里可能没有 fpdm/jym；本类会尽力把 body 凑齐
    """

    def __init__(self, appcode: Optional[str] = None, timeout: int = 10, debug: bool = False):
        self.appcode = appcode or os.environ.get("ALIYUN_FAPIAO_APPCODE", "")
        self.timeout = timeout
        self.debug = debug  # 打开后打印“已脱敏”的入参，便于查 1010

    def run(self, payload: Dict[str, Any], allow_without_jym: bool = False) -> Dict[str, Any]:
        return self.verify_invoice(payload, allow_without_jym)

    def verify(self, payload: Dict[str, Any], allow_without_jym: bool = False) -> Dict[str, Any]:
        return self.verify_invoice(payload, allow_without_jym)

    def verify_invoice(self, payload: Dict[str, Any], allow_without_jym: bool = False) -> Dict[str, Any]:
        if not self.appcode:
            return {"is_valid": False, "verify_message": "缺少阿里云 AppCode（ALIYUN_FAPIAO_APPCODE）。"}

        fpdm = str(payload.get("fpdm") or "").strip()
        fphm = str(payload.get("fphm") or "").strip()
        kprq = _to_yyyymmdd(payload.get("kprq") or "")
        # 可能来自 processor 的 je，这里不直接用，优先显式字段
        no_tax = _to_2dec(payload.get("noTaxAmount"))
        jshj   = _to_2dec(payload.get("jshj"))
        jym    = str(payload.get("jym") or "").strip()
        if len(jym) > 6:
            jym = jym[-6:]

        # 如果上游没明确给双金额，尝试从 payload 的其他键推断（常见命名）
        if not no_tax:
            no_tax = _to_2dec(payload.get("amount_excl_tax") or payload.get("total_amount") or payload.get("no_tax") or payload.get("je"))
        if not jshj:
            # 有些只给了价税合计，也兜一下
            jshj = _to_2dec(payload.get("amount_in_figures") or payload.get("total_with_tax") or
                            (float(payload.get("total_amount", 0)) + float(payload.get("total_tax", 0)) if payload.get("total_tax") is not None else ""))

        # 构造 body（该接口允许缺 fpdm；但至少需要 fphm+kprq+金额 之一）
        bodys: Dict[str, str] = {}
        if fpdm: bodys["fpdm"] = fpdm
        if fphm: bodys["fphm"] = fphm
        if kprq: bodys["kprq"] = kprq
        if no_tax: bodys["noTaxAmount"] = no_tax
        if jshj:   bodys["jshj"] = jshj
        if jym:    bodys["checkCode"] = jym

        # 最小必需校验（无代码场景至少要 号码+日期+（不含税或价税合计））
        need = []
        if "fphm" not in bodys: need.append("fphm")
        if "kprq" not in bodys: need.append("kprq")
        if ("noTaxAmount" not in bodys) and ("jshj" not in bodys): need.append("金额")
        if need and not allow_without_jym:
            return {"is_valid": False, "verify_message": f"验真要素不足（内部校验未过）：缺少 {','.join(need)}。"}
        if need and allow_without_jym:
            # 放行，但会在 debug 模式提示
            pass

        if self.debug:
            safe = {k: ("***" if k == "checkCode" else v) for k, v in bodys.items()}
            print(f"[InvoiceVerifier] POST {ALI_PATH_V2} with body={safe}")

        url = f"{ALI_HOST}{ALI_PATH_V2}"
        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Authorization": f"APPCODE {self.appcode}",
        }

        try:
            resp = requests.post(url, data=bodys, headers=headers, timeout=self.timeout)
            text = resp.text or ""
            try:
                data = resp.json()
            except Exception:
                data = {"raw": text}

            ok = False
            msg = ""
            code = str(data.get("code", ""))
            # 常见成功码：0 / "0"；部分返回 success=true / verify=true
            if code in ("0", "200", "OK"):
                ok = True
                msg = "验真成功"
            if not ok:
                if (isinstance(data.get("success"), bool) and data.get("success")) or \
                   (isinstance(data.get("verify"), bool) and data.get("verify")):
                    ok = True
                    msg = "验真成功"
            if not msg:
                msg = str(data.get("msg") or data.get("message") or "验真完成")

            # 如果验真成功且有校验码，显示校验码
            if ok and jym:
                msg = f"验真成功，校验码：{jym}"
            elif ok:
                # 验真成功但没有校验码，尝试从返回数据中获取发票信息
                invoice_data = data.get("data", {}) if isinstance(data.get("data"), dict) else {}
                invoice_number = invoice_data.get("fphm") or invoice_data.get("code") or ""
                if invoice_number:
                    msg = f"验真成功，发票号码：{invoice_number}"

            # 1010：四要素不一致——这里拼个更可读的提示
            if not ok and code == "1010":
                hint = []
                if not fpdm:
                    hint.append("本次未传发票代码（接口允许无代码，但需确保号码/日期/金额完全匹配）")
                if not no_tax and not jshj:
                    hint.append("金额字段缺失（建议同时传不含税与价税合计）")
                msg = f"{msg}；建议核对：号码/日期/金额精确值与小数位。{'；'.join(hint)}"

            return {"is_valid": bool(ok), "verify_message": msg, "verify_result": data}
        except requests.RequestException as e:
            return {"is_valid": False, "verify_message": f"验真接口网络异常：{e}"}
        except Exception as e:
            return {"is_valid": False, "verify_message": f"验真接口调用失败：{e}"}

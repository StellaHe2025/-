# invoice_extractor.py
import json
import base64
import requests
import os
import time
from typing import Dict, Any

from baidu_vat_client import BaiduVatClient, load_ak_sk

# === 放在 import 后面，全局节流器（每次调用间隔 ≥ 120ms） ===
import threading, time as _rt
_rate_lock = threading.Lock()
_last_call = 0.0

def _throttle(interval=0.12):
    global _last_call
    with _rate_lock:
        now = _rt.monotonic()
        wait = interval - (now - _last_call)
        if wait > 0:
            _rt.sleep(wait)
        _last_call = _rt.monotonic()

# === 统一税率/服务类型 ===
def _norm_tax_rate(raw):
    """把各种写法统一成 ('3%', 0.03) 这样的二元组"""
    if raw is None:
        return ("", None)
    # 列表形式： [{'row':'1','word':'3%'}]
    if isinstance(raw, list):
        for x in raw:
            if isinstance(x, dict) and x.get("word"):
                s = x["word"].strip()
                if s.endswith("%"):
                    try:
                        return (s, float(s.rstrip("%"))/100.0)
                    except Exception:
                        return (s, None)
    # 单值
    s = str(raw).strip()
    if s.endswith("%"):
        try:
            return (s, float(s.rstrip("%"))/100.0)
        except Exception:
            return (s, None)
    try:
        v = float(s)
        if v <= 1.0:   # 0.03 -> 3%
            return (f"{v*100:.0f}%", v)
        else:          # 3 -> 3%
            return (f"{v:.0f}%", v/100.0)
    except Exception:
        return (s, None)

def _first_word(arr):
    """从 [{'word': 'xxx'}] 里拿第一个 word"""
    if isinstance(arr, list) and arr:
        w = arr[0]
        if isinstance(w, dict):
            return w.get("word", "")
        return str(w)
    return ""

def _infer_service(rough: str, detail: str, seller: str) -> str:
    """把"服务/其他"升级成更细的类别"""
    txt = f"{rough} {detail} {seller}".lower()
    # 交通 / 打车
    if any(k in txt for k in ["客运","打车","出租","网约车","gaode","高德","didi","滴滴","首汽","t3","强生"]):
        return "交通/打车"
    # 广告/投放
    if any(k in txt for k in ["广告","投放","媒介","推广","banner","信息流"]):
        return "广告/投放"
    # 信息服务/软件
    if any(k in txt for k in ["信息服务","saas","云服务","软件","系统服务","技术服务","维护费"]):
        return "信息服务"
    # 会议/会务
    if any(k in txt for k in ["会议","会务","场地","会场"]):
        return "会议/会务"
    # 默认保底
    return "服务"

# 定义空OCR结果常量
EMPTY_OCR = {
    "invoice_number": "",           # 发票号码
    "invoice_code": "",             # 发票代码
    "invoice_date": "",             # 开票日期
    "seller_name": "",              # 销售方名称
    "seller_register_num": "",      # 销售方纳税人识别号
    "buyer_name": "",               # 购买方名称
    "buyer_register_num": "",       # 购买方纳税人识别号
    "total_amount": "",             # 不含税金额
    "total_tax": "",                # 税额
    "amount_in_figures": "",        # 含税金额（价税合计）
    "amount_in_words": "",          # 大写金额
    "check_code": "",               # 校验码
    "service_type": "",             # 服务类型
    "tax_rate": "",                 # 税率
    "invoice_type": "",             # 发票类型
    "remark": ""                    # 备注
}


def _wrap_ok(jr: dict) -> dict:
    wr = jr.get("words_result", {})
    # 如果words_result是列表且不为空，取第一个元素的result
    if isinstance(wr, list) and len(wr) > 0:
        wr = wr[0].get("result", {})
    elif isinstance(wr, dict) and "result" in wr:
        wr = wr.get("result", {})
    
    # 先把 OCR 原始字段取出来
    commodity_name = _first_word(wr.get("CommodityName"))
    service_type_raw = wr.get("ServiceType") or wr.get("InvoiceKind") or "服务"

    # 税率优先用 CommodityTaxRate；没有再回退 TaxRate / tax_rate
    tax_src = wr.get("CommodityTaxRate") or wr.get("TaxRate") or wr.get("tax_rate")
    tax_percent_str, tax_decimal = _norm_tax_rate(tax_src)

    invoice_data = {
        "invoice_number": wr.get("InvoiceNum","") or wr.get("InvoiceNumDigit",""),
        "invoice_code":   wr.get("InvoiceCode",""),
        "invoice_date":   wr.get("InvoiceDate",""),
        "seller_name":    wr.get("SellerName",""),
        "seller_register_num": wr.get("SellerRegisterNum","") or wr.get("SellerTaxID",""),
        "buyer_name":     wr.get("PurchaserName",""),
        "buyer_register_num":  wr.get("PurchaserRegisterNum","") or wr.get("PurchaserTaxID",""),
        "total_amount":   wr.get("TotalAmount",""),
        "total_tax":      wr.get("TotalTax",""),
        "amount_in_figures": wr.get("AmountInFiguers","") or wr.get("AmountInFigures",""),
        "amount_in_words":   wr.get("AmountInWords",""),
        "check_code":     wr.get("CheckCode","") or wr.get("Password",""),
        # ★ 明细里的人话服务名
        "service_type_detail": commodity_name or "",
        # ★ 先放 OCR 粗类别（服务/其他），后面再升级
        "service_type":   service_type_raw or "服务",
        # ★ 税率双口径
        "tax_rate":       tax_percent_str,      # 比如 "3%"
        "tax_rate_decimal": tax_decimal,        # 比如 0.03
        "invoice_type":   wr.get("InvoiceType",""),
        "remark":         wr.get("Remarks","") or wr.get("Remark",""),
    }

    # —— 用"明细 + 卖方名"把 service_type 升级成更细分 —— #
    invoice_data["service_type"] = _infer_service(
        invoice_data["service_type"],
        invoice_data["service_type_detail"],
        invoice_data["seller_name"],
    )

    # —— 若你有验真结果 verify_result，就再用 goodsData 覆盖一次（更准）——
    # 注意：在 _wrap_ok 函数中，我们没有 verify_result，这部分逻辑应该在其他地方处理
    # 这里保留结构，但不执行相关逻辑

    return {
        "invoice_info": invoice_data,
        "raw_ocr": {
            "log_id": jr.get("log_id"),
            "error_code": None,
            "error_msg": None
        }
    }


def _wrap_err(jr: dict) -> dict:
    # 百度错误 → 统一透传给前端
    code = jr.get("error_code")
    msg = jr.get("error_msg")
    return {
        "invoice_info": {"__ocr_error__": f"{code}:{msg}"},  # 例如 "216201:image format error"
        "raw_ocr": {
            "log_id": jr.get("log_id"),
            "error_code": code,
            "error_msg": msg
        }
    }


def ocr_vat_from_bytes(file_bytes: bytes, filename: str) -> dict:
    ak, sk = load_ak_sk()
    client = BaiduVatClient(ak, sk)

    name = (filename or "").lower()
    try:
        # ……你的代码前面解析了文件 bytes 和类型……
        # 在这里加一刀软限速
        _throttle()

        # 然后再调百度
        if name.endswith(".pdf"):
            jr = client.recognize(pdf_bytes=file_bytes)
        elif name.endswith(".ofd"):
            jr = client.recognize(ofd_bytes=file_bytes)
        else:
            jr = client.recognize(image_bytes=file_bytes)

        # 错误直接透传，不要"假装配额"
        if "__ocr_error__" in jr:
            return {"invoice_info": {"__ocr_error__": jr["__ocr_error__"]},
                    "raw_ocr": jr}

        # 你原来处理 jr 的地方改成：
        if "error_code" in jr:
            return _wrap_err(jr)
        else:
            return _wrap_ok(jr)

    except Exception as e:
        return {"invoice_info": {"__ocr_error__": f"client_exception:{e}"}, "raw_ocr": {}}


class InvoiceExtractor:
    def __init__(self, api_key: str, secret_key: str, access_token: str = None):
        self.api_key = api_key
        self.secret_key = secret_key
        self.access_token = access_token or self._get_access_token()
    
    def _get_access_token(self) -> str:
        """
        获取百度OCR的access_token
        """
        url = "https://aip.baidubce.com/oauth/2.0/token"
        params = {
            "grant_type": "client_credentials",
            "client_id": self.api_key,
            "client_secret": self.secret_key
        }
        
        response = requests.post(url, params=params)
        result = response.json()
        return result.get("access_token")
    
    def _log_quota_hint(self, ocr):
        try:
            ec = str(ocr.get("error_code"))
            em = ocr.get("error_msg", "")
            print(f"[BAIDU_OCR_ERR] code={ec} msg={em} ak_tail={self.api_key[-4:]} tz=UTC+8_reset@00:00")
        except Exception:
            pass
    
    def _dump_ocr_error(self, payload: dict):
        """把完整错误落盘，方便复制到百度 Trace 工具。"""
        try:
            path = "/tmp/last_ocr_error.json"
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            print(f"[BAIDU_OCR_ERR_DUMP] saved -> {path}")
        except Exception as e:
            print(f"[BAIDU_OCR_ERR_DUMP] failed: {e}")

    def _safe_json(self, resp) -> dict:
        """无论返回是不是 JSON，都尽量还原；同时打印关键信息。"""
        try:
            txt = resp.text
            data = resp.json() if txt else {}
        except Exception:
            data = {"_non_json_text": resp.text[:500] if resp and getattr(resp, "text", None) else ""}
        # 打印可读的诊断行（不含敏感 token）
        code = data.get("error_code")
        msg  = data.get("error_msg")
        log  = data.get("log_id")
        ts   = data.get("timestamp")
        print(f"[BAIDU_OCR_HTTP] status={resp.status_code} code={code} msg={msg} log_id={log} ts={ts}")
        return data

    def extract_from_image(self, image_path: str) -> Dict[str, Any]:
        """
        从图片中提取发票信息
        """
        # 读取图片文件
        with open(image_path, 'rb') as f:
            image_data = f.read()
        
        # 使用新的OCR方法
        result = ocr_vat_from_bytes(image_data, image_path)
        if "__ocr_error__" in result["invoice_info"]:
            return {**EMPTY_OCR, "__ocr_error__": result["invoice_info"]["__ocr_error__"]}
        
        invoice_info = result["invoice_info"]
        # 补充缺失的字段
        return self._fill_missing_fields(invoice_info)

    def extract_from_image_data(self, image_data: bytes) -> Dict[str, Any]:
        """
        从图片数据中提取发票信息
        """
        # 使用新的OCR方法
        result = ocr_vat_from_bytes(image_data, "image.jpg")
        if "__ocr_error__" in result["invoice_info"]:
            return {**EMPTY_OCR, "__ocr_error__": result["invoice_info"]["__ocr_error__"]}
        
        invoice_info = result["invoice_info"]
        # 补充缺失的字段
        return self._fill_missing_fields(invoice_info)
    
    def extract_from_pdf(self, pdf_path: str) -> Dict[str, Any]:
        """
        从PDF中提取发票信息
        """
        # 读取PDF文件
        with open(pdf_path, 'rb') as f:
            pdf_data = f.read()
        
        # 使用新的OCR方法
        result = ocr_vat_from_bytes(pdf_data, pdf_path)
        if "__ocr_error__" in result["invoice_info"]:
            return {**EMPTY_OCR, "__ocr_error__": result["invoice_info"]["__ocr_error__"]}
        
        invoice_info = result["invoice_info"]
        # 补充缺失的字段
        return self._fill_missing_fields(invoice_info)
    
    def extract_from_pdf_data(self, pdf_data: bytes) -> Dict[str, Any]:
        """
        从PDF数据中提取发票信息
        """
        # 使用新的OCR方法
        result = ocr_vat_from_bytes(pdf_data, "document.pdf")
        if "__ocr_error__" in result["invoice_info"]:
            return {**EMPTY_OCR, "__ocr_error__": result["invoice_info"]["__ocr_error__"]}
        
        invoice_info = result["invoice_info"]
        # 补充缺失的字段
        return self._fill_missing_fields(invoice_info)
    
    def _fill_missing_fields(self, invoice_info: Dict[str, Any]) -> Dict[str, Any]:
        """
        补充缺失的字段以匹配EMPTY_OCR结构
        """
        filled_info = EMPTY_OCR.copy()
        filled_info.update(invoice_info)
        
        # 如果没有提取到含税金额但有不含税金额和税额，则计算含税金额
        if not filled_info["amount_in_figures"] and filled_info["total_amount"] and filled_info["total_tax"]:
            try:
                total_amount = float(filled_info["total_amount"])
                total_tax = float(filled_info["total_tax"])
                filled_info["amount_in_figures"] = str(total_amount + total_tax)
            except (ValueError, TypeError):
                pass
        
        return filled_info
    
    # 添加方法别名以保持向后兼容
    def extract_invoice(self, file_path: str, file_type: str = 'image') -> Dict[str, Any]:
        """
        从文件中提取发票信息的通用方法
        
        Args:
            file_path: 文件路径
            file_type: 文件类型 ('image' 或 'pdf')
            
        Returns:
            提取的发票信息字典
        """
        if file_type == 'image':
            return self.extract_from_image(file_path)
        elif file_type == 'pdf':
            return self.extract_from_pdf(file_path)
        else:
            # 默认使用图片提取方法
            return self.extract_from_image(file_path)
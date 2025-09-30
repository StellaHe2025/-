# reimbursement_processor.py — 发票只处理一次；佐证材料参与风控比对但不单独渲染
# 功能：二维码/文本解析、数电票校验码兜底、无代码放行、双金额传参、佐证对比（日期/金额）、去重风险点
# -*- coding: utf-8 -*-
import os
import tempfile
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime
import re
import json
from urllib.parse import quote
from pathlib import Path

HARD_THRESHOLD_SCORE = 0.85  # 关键词打分达到则直接采用该会计科目

KB_DIR = Path(__file__).resolve().parent  # 如果知识库就在同目录；否则改成你的 kb 目录

def _load_kb_terms():
    expense_types = set()      # e.g. 差旅费、办公费、业务招待费、培训费、通讯费、会议费…
    account_subjects = set()   # e.g. 6603-差旅费、6601-办公费、管理费用-差旅费 等
    keyword_map = []           # [(keyword, account, weight, note), ...]

    # 1) 费用大类（accounting_rules.txt）
    try:
        text = (KB_DIR / "accounting_rules.txt").read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines():
            m = re.match(r"\d+\.\s*(\S+)", line.strip())
            if m:
                expense_types.add(m.group(1))  # 例如：差旅费、办公费、业务招待费…
    except Exception:
        pass  # 容错

    # 2) 科目口径手册（会计科目口径手册_rag版.md）
    try:
        md = (KB_DIR / "会计科目口径手册_rag版.md").read_text(encoding="utf-8", errors="ignore")
        # 抓"§660x_"或"入账科目"行
        for m in re.finditer(r"§(\d{4})[_-].*|入账科目.*?：\s*([0-9\-A-Za-z\u4e00-\u9fa5]+)", md):
            for g in m.groups():
                if g:
                    account_subjects.add(g.strip())
    except Exception:
        pass

    # 3) 关键词-科目 map（发票关键词-会计科目map表.txt）
    try:
        tbl = (KB_DIR / "发票关键词-会计科目map表.txt").read_text(encoding="utf-8", errors="ignore")
        for ln in tbl.splitlines():
            if not ln or ln.startswith("keyword"): 
                continue
            cols = [c.strip() for c in ln.split("\t")]
            if len(cols) >= 2:
                kw, acct = cols[0], cols[1]
                keyword_map.append((kw, acct))
                account_subjects.add(acct)
    except Exception:
        pass

    # 常见别名补齐（可选）
    alias = {
        "差旅费":"6603-差旅费",
        "办公费":"6601-办公费",
        "业务招待费":"6602-业务招待费",
        "会议费":"6604-会议费",
        "培训费":"6605-培训费",
        "通讯费":"6608-通讯费",
    }
    for k,v in alias.items():
        expense_types.add(k); account_subjects.add(v)

    return sorted(expense_types), sorted(account_subjects), keyword_map

EXPENSE_TYPES, ACCOUNT_SUBJECTS, KEYWORD_MAP = _load_kb_terms()

import re
from ast import literal_eval

def _scrub_title(title: str) -> str:
    """把类似 "{'title': '公司报销规则', 'url': ''}" 这种被塞进 title 的脏字符串，拆成干净标题。"""
    if not isinstance(title, str):
        return str(title)
    t = title.strip()
    # 1) 直接从形如 "{'title': 'xxx', 'url': ''}" 抠 'title'
    m = re.search(r"'title'\s*:\s*'([^']+)'", t)
    if m:
        return m.group(1).strip()

    # 2) 尝试把整串当 dict 解析，再取 title
    if (t.startswith("{") and t.endswith("}")) or (t.startswith("[") and t.endswith("]")):
        try:
            obj = literal_eval(t)
            if isinstance(obj, dict) and obj.get("title"):
                return str(obj.get("title")).strip()
        except Exception:
            pass

    # 3) 去掉路径和后缀，保留文件名主体
    base = t.rsplit("/", 1)[-1]
    base = base.rsplit("\\", 1)[-1]
    base = re.sub(r"\.(md|txt|pdf|docx?)$", "", base, flags=re.IGNORECASE)
    return base or "未知来源"
def _coerce_float_or_none(x):
    """把任何奇怪的分数安全转成 float 或 None。字符串空/None/转不动的，一律 None。"""
    if x is None:
        return None
    if isinstance(x, str):
        s = x.strip()
        if s == "" or s.lower() in {"none", "null", "nan"}:
            return None
        try:
            return float(s)
        except Exception:
            return None
    try:
        return float(x)
    except Exception:
        return None

def _safe_score(v) -> float | None:
    """把不可解析/为 0 的分数统一处理；0 -> None（前端大多不显示 None）。"""
    try:
        f = float(v)
        return None if f == 0.0 else f
    except Exception:
        return None

# --------------------------- 小工具 ---------------------------
def _safe_float(x, default=0.0):
    try:
        return float(str(x).replace(",", "").strip())
    except Exception:
        return default

def _has_any(texts: list[str], keys: list[str]) -> bool:
    t = " ".join([x for x in texts if x]).lower()
    return any(k in t for k in keys)

# 新增的工具函数
import re
from collections import defaultdict

KEYSETS = {
    "交通-打车/市内": ["打车","网约车","出租","滴滴","高德","曹操","首汽","T3","客运","快车","专车","顺风车","乘车码","行程单"],
    "差旅-住宿": ["住宿","酒店","宾馆","客房","入住","房费","住宿费","night","check-in","check out"],
    "差旅-长途交通": ["火车","动车","高铁","机票","航班","航空","车票","铁道","民航","登机","起飞","落地"],
    "餐饮/工作餐": ["餐","工作餐","餐费","就餐","早餐","午餐","晚餐","餐饮","围餐","盒饭","外卖"],
    "办公用品/低值易耗": ["办公","耗材","打印","复印","硒鼓","墨盒","文具","名片","印刷","纸张","装订"],
    "培训/会议/会务": ["培训","报名费","会务","会议费","讲座","研讨","会展","会议服务"],
    "快递/邮寄": ["快递","邮寄","运费","寄件","快运","物流"],
    "油费/路桥": ["加油","燃油","汽油","柴油","油费","ETC","过路费","过桥费","高速费","停车"],
    "通讯/网络": ["通信","通讯","电话费","话费","流量","宽带","网络","上网","固话","移动","联通","电信"],
}

EVIDENCE_TEMPLATES = {
    "交通-打车/市内": [
        "出差审批单/公务事由说明与行程是否一致",
        "打车行程记录或订单截图（起止点、时间、乘车人）",
        "支付凭证/发票金额与订单金额一致"
    ],
    "差旅-住宿": [
        "出差审批单与入住日期/城市匹配",
        "酒店订单/入住登记/结算单据",
        "同一行程有交通与住宿的关联证据"
    ],
    "差旅-长途交通": [
        "出差审批单与航班/车次匹配",
        "电子客票/行程单/登机牌或乘车记录",
        "往返合理性与费用合规性"
    ],
    "餐饮/工作餐": [
        "工作餐审批/会议纪要/参与人清单",
        "同城是否符合公司工作餐政策",
        "单价/人数/次数是否超制度阈值"
    ],
    "办公用品/低值易耗": [
        "采购申请单/入库单/领用台账",
        "可重复使用物品建立台账",
        "供应商、品名与办公场景匹配"
    ],
    "培训/会议/会务": [
        "培训/会议通知及参会名单",
        "费用明细与合同/订单一致",
        "发票抬头/税号无误"
    ],
    "快递/邮寄": [
        "寄件记录/面单与业务单据关联",
        "计费重量/路由合理性",
        "同客户/同项目集中寄件说明"
    ],
    "油费/路桥": [
        "用车审批/行驶路线与业务关系",
        "ETC/发卡单位账单或加油小票",
        "个人车报销按制度比例"
    ],
    "通讯/网络": [
        "号码/账号归属与岗位关联",
        "包月/流量套餐与报销周期匹配",
        "公司付费与个人垫付界面划分"
    ],
}

def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "").lower()

def infer_category_from_invoice(invoice_data: dict) -> dict:
    """从多字段自动抽取关键词并打分 → 返回 {category, reasons, hits}"""
    bag_fields = [
        invoice_data.get("service_type", ""),
        invoice_data.get("service_type_detail", ""),
        invoice_data.get("remark", ""),
        invoice_data.get("seller_name", ""),
    ]
    # goodsData 名称
    try:
        for g in (invoice_data.get("verify_result") or {}).get("data", {}).get("goodsData", []):
            bag_fields.append(g.get("name",""))
    except Exception:
        pass

    bag = _norm(" ".join(str(x) for x in bag_fields if x))
    scores = defaultdict(int)
    hits = defaultdict(list)

    for cat, keys in KEYSETS.items():
        for k in keys:
            if _norm(k) and _norm(k) in bag:
                scores[cat] += 1
                hits[cat].append(k)

    if scores:
        cat = max(scores.items(), key=lambda x: x[1])[0]
        return {
            "category": cat,
            "score": scores[cat],
            "hits": hits[cat],
            "evidence_required": EVIDENCE_TEMPLATES.get(cat, []),
        }
    # 没命中就 UNKNOWN
    return {"category": "UNKNOWN", "score": 0, "hits": [], "evidence_required": []}

def _normalize_subject(name: str) -> str:
    """把各种历史口径/关键词映射科目名统一到当前口径"""
    if not name:
        return name or ""
    n = str(name).strip()
    # 所有差旅相关后缀 → 统一到 6603-差旅费
    if any(k in n for k in ["差旅费-市内交通", "差旅费-交通", "差旅费-交通费", "管理费用-差旅费", "差旅-"]):
        return "6603-差旅费"
    # 明确禁止把住宿/差旅识别成办公费
    if "办公" in n:
        return "6603-差旅费"
    return n

def _infer_service_type(invoice_info, goods_names, user_note, remark):
    blob = " ".join([*(goods_names or []), user_note or "", remark or "", 
                     invoice_info.get("service_type","")]).lower()
    def hit(words): return any(w in blob for w in words)
    lodge = ["住宿","酒店","宾馆","客栈","房费","lodging","hotel"]
    trans = ["网约车","出租车","打车","车费","客运","交通","高铁","机票","动车","地铁","公交","滴滴","高德打车","曹操"]
    office = ["办公用品","文具","耗材","复印纸","打印纸","硒鼓","墨盒","印刷","名片"]
    if hit(lodge):  return "住宿服务"
    if hit(trans):  return "交通"
    if hit(office): return "办公"
    return invoice_info.get("service_type") or "未知"

def _choose_account_from_keywords(goods_names, user_note, remark):
    text = " ".join([*(goods_names or []), user_note or "", remark or ""])
    for kw, acct in KEYWORD_MAP:
        if kw and kw in text:
            return acct  # 直接按你公司 map 表选科目（优先级 < 明确的住宿/交通规则）
    return ""


def _parse_date_cn(date_str: Optional[str]):
    from datetime import datetime as _dt
    if not date_str:
        return None
    s = str(date_str).strip().replace("/", "-").replace("年", "-").replace("月", "-").replace("日", "")
    if len(s) == 8 and s.isdigit():
        try:
            return _dt.strptime(f"{s[:4]}-{s[4:6]}-{s[6:]}", "%Y-%m-%d")
        except Exception:
            pass
    for fmt in ("%Y-%m-%d",):
        try:
            return _dt.strptime(s, fmt)
        except Exception:
            continue
    return None

def json_dump(x: Any) -> str:
    return json.dumps(x, ensure_ascii=False, indent=2)

def _strip_field_hints(text: str) -> str:
    """
    去掉中文句子里夹带的英文字段名提示，例如：
    '发票总金额(total_amount)为...' -> '发票总金额为...'
    """
    if not isinstance(text, str):
        return text
    # 括号内是纯小写字母/下划线/数字的视作"字段名"
    return re.sub(r"\s*\(([a-z0-9_]+)\)\s*", "", text)

def _clean_obj(obj: Any) -> Any:
    """递归清洗：字符串去英文字段提示；列表/字典逐层清理。"""
    if isinstance(obj, str):
        return _strip_field_hints(obj)
    if isinstance(obj, list):
        return [_clean_obj(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _clean_obj(v) for k, v in obj.items()}
    return obj

def _ensure_list_field(d: Dict[str, Any], key: str) -> None:
    """把 d[key] 规范成 list，便于后续 append。"""
    v = d.get(key)
    if v is None:
        d[key] = []
    elif isinstance(v, list):
        return
    elif isinstance(v, str):
        d[key] = [v]
    else:
        d[key] = [str(v)]

def _enforce_now_date_in_text(text: Any, now_date: Optional[str]) -> Any:
    if not isinstance(text, str):
        return text
    if not now_date:
        # 没有 now_date 时，删掉任何"当前日期为…"的断言
        return re.sub(r"当前日期[为是]\s*\d{4}年\d{1,2}月\d{1,2}日", "当前日期未知", text)
    # 用 now_date 规范化
    nd = now_date.replace("-", "年", 1).replace("-", "月", 1) + "日" if "-" in now_date else now_date
    return re.sub(r"当前日期[为是]\s*\d{4}年\d{1,2}月\d{1,2}日", f"当前日期为{nd}", text)

# —— 金额字段归一化：不含税/税额/含税三者互推 ——
def _normalize_amount_fields(inv: Dict[str, Any]) -> Dict[str, Any]:
    excl = _safe_float(inv.get("amount_excl_tax") or inv.get("total_amount"))
    tax  = _safe_float(inv.get("total_tax"))
    incl = _safe_float(inv.get("amount_in_figures"))

    # 如果缺含税，用 不含税+税额 补
    if incl == 0 and (excl or tax):
        incl = round(excl + tax, 2)
        inv["amount_in_figures"] = incl

    # 如果缺税额，用 含税-不含税 补
    if tax == 0 and incl and excl:
        tax = round(incl - excl, 2)
        inv["total_tax"] = tax

    # 兜底把 amount_excl_tax / total_amount 都写上，方便前端映射
    if not inv.get("amount_excl_tax") and excl:
        inv["amount_excl_tax"] = round(excl, 2)
    if not inv.get("total_amount") and excl:
        inv["total_amount"] = round(excl, 2)

    return inv


def _hits_to_sources(hits):
    # hits 每条形如 {"doc","path","url","content","score"}
    out = []
    for h in (hits or []):
        title = h.get("doc") or os.path.basename(h.get("path",""))
        url = h.get("url")
        if not url and h.get("path"):
            # 兜底：万一 retriever 没给 url
            kb = os.getenv("KB_DIR") or os.path.join(os.path.dirname(__file__), "knowledge_base")
            rel = os.path.relpath(h["path"], kb).replace(os.sep, "/")
            base = os.getenv("PUBLIC_KB_BASE", "").rstrip("/")
            url = f"{base}/{quote(rel, safe='/')}" if base else None
        out.append({"title": title, "url": url, "score": round(float(h.get("score",0)), 4)})
    return out

# --------------------------- 来源规范化 ---------------------------
def _fix_sources_field(block: dict, contexts: list = None) -> dict:
    """
    统一把 block["sources_used"] / block["sources"] 清洗成:
    [{'title': str, 'url': str|None, 'score': float|None}]
    - 兼容字符串/字典/混合输入
    - 去路径、去扩展名、去"嵌套 title"
    - 分数安全转换；对0分或结构化上下文来源不展示分数
    - 去重
    """
    items = _merge_sources(block.get("sources_used"), block.get("sources"))

    def _to_item(x):
        d = _normalize_source(x)  # -> {'title','url','score'}
        # 1) 处理"嵌套 title"或把整个 dict 当字符串的情况
        t = d.get("title")
        if isinstance(t, str) and t.startswith("{'title':"):
            d["title"] = "未知来源"
        # 2) 去掉常见噪声：路径/扩展名/多余括号等（有 _scrub_title 就用；没有就保留原值）
        try:
            d["title"] = _scrub_title(d["title"])  # 你之前加的小工具
        except Exception:
            pass
        # 3) 分数安全转换（有 _safe_score 就用；没有就保留原值）
        try:
            d["score"] = _safe_score(d.get("score"))
        except Exception:
            # 兜底：把非数值分数清成 None
            try:
                d["score"] = float(d.get("score"))
            except Exception:
                d["score"] = None
        return d

    clean = [_to_item(x) for x in (items or []) if x]

    # 4) 对"结构化上下文"来源或明显为辅助来源的，隐藏score（避免前端看到一堆 0）
    STRUCT_TITLES = {"系统当前时间", "结构化规则-审批阈值", "结构化-调用侧上下文汇总"}
    for it in clean:
        title = (it.get("title") or "").strip()
        if title in STRUCT_TITLES or (it.get("score") in (0, 0.0, "0", "0.0")):
            it["score"] = None

    # 5) 去重
    block["sources_used"] = _dedup_sources(clean)

    # 6) 最后再清一次对象里的 None/空串
    return _clean_obj(block)

def _normalize_sources(sources):
    out = []
    for s in (sources or []):
        if isinstance(s, dict) and s.get("title"):
            out.append({"title": s["title"], "url": s.get("url"), "score": s.get("score", 0)})
        elif isinstance(s, str) and s.strip():
            base = os.getenv("PUBLIC_KB_BASE", "").rstrip("/")
            url = f"{base}/{quote(s)}" if base else None
            out.append({"title": os.path.splitext(os.path.basename(s))[0], "url": url, "score": 0})
    return out

# ---- utils: normalize + merge sources (REPLACE WHOLE) ----
def _normalize_source(s):
    """
    把任意形态的来源项规范成统一字典：
    {"title": "...", "url": str|None, "score": float}
    - 去路径、去扩展名，title 只留文件名主体
    """
    import os
    from urllib.parse import quote

    if not s:
        return None

    if isinstance(s, str):
        title = os.path.splitext(os.path.basename(s.strip()))[0]
        title = _scrub_title(title)  # << 新增：拆掉嵌在 title 里面的 dict 串
        return {"title": title, "url": None, "score": 0.0}
        

    if isinstance(s, dict):
        title = (
            s.get("title")
            or s.get("source")
            or s.get("doc")
            or s.get("file")
            or s.get("name")
        )
        if not title:
            return None
        # 标题统一为"文件名主体"
        title = os.path.splitext(os.path.basename(str(title)))[0]

        url = s.get("url")
        if url and not isinstance(url, str):
            url = str(url)

        score = s.get("score", 0)
        try:
            score = float(score)
        except Exception:
            score = 0.0

        return {"title": title, "url": url or None, "score": score}

    # 其他类型：兜底
    return {"title": os.path.splitext(os.path.basename(str(s)))[0], "url": None, "score": 0.0}


def _dedup_sources(items):
    """
    去重 & 统一结构:
    input: 可能是 str/dict 混合；title 里可能带 {'title': ...}
    output: [{'title': str, 'url': str|None, 'score': float|None}]
    """
    if not items:
        return []

    seen = set()
    out = []

    for it in items:
        # 1) 统一成 dict
        if isinstance(it, str):
            # 处理 "{"title": "..."}" 这种脏字符串
            t = it
            if t.startswith("{'title':") or t.startswith('{"title":'):
                title = "未知来源"
            else:
                title = t
            url = None
            score = None
        elif isinstance(it, dict):
            title = it.get("title") or it.get("name") or "未知来源"
            # 把 "{'title': ...}" 这种再清一次
            if isinstance(title, str) and (title.startswith("{'title':") or title.startswith('{"title":')):
                title = "未知来源"
            url = it.get("url") or it.get("link") or None
            score = _coerce_float_or_none(it.get("score"))
        else:
            # 非法类型直接跳
            continue

        # 2) 标题再做一次轻清洗（去掉明显的路径/扩展名噪声）
        if isinstance(title, str):
            t = title.strip()
            # 常见文件扩展名和路径痕迹简单裁剪
            for bad in (".md", ".txt", ".pdf"):
                if t.endswith(bad):
                    t = t[: -len(bad)]
            t = t.replace("\\", "/")
            if "/" in t:
                t = t.split("/")[-1]
            # 括号/全角空格等简单收拾
            t = t.replace("（RAGflow 版）", "RAGflow 版").replace("（", "(").replace("）", ")")
            title = t or "未知来源"

        # 3) 组 key 去重
        key = (title, url or "")
        if key in seen:
            continue
        seen.add(key)

        # 4) 输出时分数保持 None（隐藏 0 分视觉噪音）
        out.append({"title": title, "url": (url or None), "score": score})

    return out


def _sources_from_contexts(ctxs):
    """
    contexts 里可能是 dict / str / 混合；这里统一抽成来源字典数组。
    """
    out = []
    for c in (ctxs or []):
        if isinstance(c, dict):
            out.append(
                _normalize_source({
                    "title": c.get("source") or c.get("doc") or c.get("title") or c.get("file"),
                    "url": c.get("url"),
                    "score": c.get("score", 0),
                })
            )
        else:
            out.append(_normalize_source(c))
    return _dedup_sources([x for x in out if x])


def _merge_sources(a, b):
    """合并两个来源列表（任意形态），规范化 + 去重。"""
    items = []
    for seq in (a or []), (b or []):
        for s in (seq or []):
            norm = _normalize_source(s)
            if norm:
                items.append(norm)
    return _dedup_sources(items)

# --------------------------- 二维码/文本 五要素解析 ---------------------------
FPDM_PAT = re.compile(r"(?:fpdm|发票代码)[=:：\s]*([0-9]{10,12})")
FPHM_PAT = re.compile(r"(?:fphm|发票号码|号码)[=:：\s]*([0-9]{8,20})")
KPRQ_PAT = re.compile(r"(?:kprq|开票日期)[=:：\s]*([0-9]{8}|[0-9]{4}[-/年][0-9]{2}[-/月][0-9]{2})")
JE_PAT   = re.compile(r"(?:je|金额|不含税金额|金额（不含税）)[=:：\s]*(-?[0-9]+(?:\.[0-9]{1,2})?)")
JYM_PAT  = re.compile(r"(?:jym|校验码)[=:：\s]*([0-9]{6})")

CSV_LIKE_PAT = re.compile(
    r"\b01[,，]\s*([0-9]{10,12})[,，]\s*([0-9]{8,20})[,，]\s*([0-9]{8})[,，]\s*([0-9]{6})[,，]\s*(-?[0-9]+(?:\.[0-9]{1,2})?)"
)

def _norm_date8_or_dash(s: str) -> str:
    s = str(s).strip().replace("/", "-").replace("年", "-").replace("月", "-").replace("日", "")
    if re.fullmatch(r"\d{8}", s):
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s

def parse_from_qr_and_ocr(qr_text: str = "", ocr_text: str = "") -> Dict[str, Optional[str]]:
    raw = f"{qr_text}\n{ocr_text}".strip()

    m = CSV_LIKE_PAT.search(raw)
    if m:
        fpdm, fphm, kprq, jym, je = m.groups()
        return {"fpdm": fpdm, "fphm": fphm, "kprq": _norm_date8_or_dash(kprq),
                "je": je, "jym": jym, "inferred": False, "route": "csv-like"}

    def _pick(pat, raw_key=None):
        m1 = pat.search(raw)
        if m1:
            return m1.group(1)
        if raw_key:
            m2 = re.search(fr"{raw_key}=([0-9\-./]+)", raw)
            return m2.group(1) if m2 else None
        return None

    fpdm = _pick(FPDM_PAT, "fpdm")
    fphm = _pick(FPHM_PAT, "fphm")
    kprq = _pick(KPRQ_PAT, "kprq")
    je   = _pick(JE_PAT, "je")
    jym  = _pick(JYM_PAT, "jym")

    if kprq:
        kprq = _norm_date8_or_dash(kprq)

    looks_digital = False
    if fphm and len(fphm) == 20:
        looks_digital = True
    if re.search(r"(全面数字化|数电票|数电化|号码20位|电子发票(普通|专用)电子化)", raw):
        looks_digital = True

    inferred = False
    if looks_digital and (not jym) and fphm and len(fphm) >= 6:
        jym = fphm[-6:]
        inferred = True

    return {"fpdm": fpdm, "fphm": fphm, "kprq": kprq, "je": je, "jym": jym,
            "inferred": inferred, "route": "kv/heuristic"}

# --------------------------- 发票代码推断 ---------------------------
def _guess_fpdm_from_text(invoice_data: dict) -> Tuple[Optional[str], Optional[str]]:
    text_fields = []
    for k in ("raw_text", "remark", "content", "invoice_type", "seller_name", "buyer_name"):
        v = invoice_data.get(k)
        if isinstance(v, str) and v.strip():
            text_fields.append(v)
    blob = "\n".join(text_fields)

    m = re.search(r"(?:发票代码|代码)[^\d]{0,8}([0-9]{10,12})", blob)
    if m:
        return m.group(1), "regex:label_nearby"

    candidates = re.findall(r"(?<!\d)(\d{12})(?!\d)", blob)
    candidates = list(dict.fromkeys(candidates))
    candidates = [c for c in candidates if not re.fullmatch(r"([0-9])\1{11}", c)]
    if len(candidates) == 1:
        return candidates[0], "regex:singleton_12d"

    return None, None

# --------------------------- 主处理器 ---------------------------
class ReimbursementProcessor:
    def __init__(self, extractor, analyzer, retriever, verifier):
        self.extractor = extractor
        self.analyzer = analyzer
        self.retriever = retriever
        self.verifier = verifier

    def _safe_call(self, fn, fallback):
        """
        安全调用函数，即使出错也返回结果，确保HTTP状态为200
        用于包装可能失败的分析步骤
        """
        try:
            return fn() or fallback
        except Exception as e:
            # 统一结构：让前端能展示错误卡片，而不是 500
            return {**fallback, "error": f"{type(e).__name__}: {e}"}

    # 新增一个 run 方法，专门给 API 调用，它接收文件内容而不是路径
    def run(self, file_content: bytes, filename: str, user_input: str = "") -> Dict[str, Any]:
        import tempfile
        import os

        # 根据文件名判断是图片还是PDF
        file_type = 'pdf' if filename.lower().endswith('.pdf') else 'image'

        # 1. 在服务器上创建一个临时的空文件
        # delete=False 意思是文件关闭后先别删掉，我们一会儿自己删
        # suffix 可以给临时文件加个后缀，方便我们看日志时辨认
        with tempfile.NamedTemporaryFile(delete=False, suffix=f"_{filename}") as temp_f:
            # 2. 把接收到的文件内容，写入这个临时文件
            temp_f.write(file_content)
            # 3. 获取这个临时文件的完整路径
            temp_path = temp_f.name

        try:
            # 4. 用这个临时文件的路径，去调用你已经写好的、完整的处理流程
            #    这完美复用了你现有的所有逻辑！
            result = self.process_reimbursement(
                temp_path,
                file_type=file_type,
                user_input=user_input
            )
        finally:
            # 5. 任务完成，把这个临时文件删掉，保持服务器干净整洁
            if os.path.exists(temp_path):
                os.remove(temp_path)

        return result

    # 兼容旧 UI：吃 bytes 的入口（filename 可选，主要用于判断 pdf/image）
    def run(self, file_bytes: bytes, filename: str = "upload.bin",
            user_input: str = "", evidence_data=None) -> dict:
        suffix = os.path.splitext(filename)[1].lower() or ".bin"
        file_type = "pdf" if suffix == ".pdf" else "image"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as f:
            f.write(file_bytes)
            tmp_path = f.name
        try:
            return self.process_reimbursement(
                tmp_path, file_type=file_type, user_input=user_input, evidence_data=evidence_data
            )
        finally:
            try:
                os.remove(tmp_path)
            except Exception:
                pass

    # 兼容不同提取器命名
    def _extract_invoice(self, file_path: str, file_type: str = "image"):
        e = self.extractor
        candidates = [
            ("extract_invoice", (file_path,), {"file_type": file_type}),
            ("extract_from_file", (file_path,), {"file_type": file_type}),
            ("extract", (file_path,), {"file_type": file_type}),
            ("ocr_extract", (file_path,), {"file_type": file_type}),
            ("parse_invoice", (file_path,), {"file_type": file_type}),
            ("run", (file_path,), {"file_type": file_type}),
            ("extract_from_image" if file_type == "image" else "extract_from_pdf", (file_path,), {}),
        ]
        for name, args, kwargs in candidates:
            if hasattr(e, name):
                fn = getattr(e, name)
                try:
                    return fn(*args, **kwargs)
                except TypeError:
                    try:
                        return fn(*args)
                    except Exception:
                        pass
        raise AttributeError("InvoiceExtractor 需要提供以下任一方法：extract_invoice / extract_from_file / extract / ocr_extract / parse_invoice / run / extract_from_image / extract_from_pdf")

    # 调用验真
    def _call_verifier(self, payload: dict, allow_without_jym: bool = False):
        v = self.verifier
        candidates = [
            ("verify_invoice", (payload, allow_without_jym), {}),
            ("verify", (payload, allow_without_jym), {}),
            ("run", (payload, allow_without_jym), {}),
        ]
        for name, args, kwargs in candidates:
            if hasattr(v, name):
                fn = getattr(v, name)
                try:
                    return fn(*args, **kwargs)
                except TypeError:
                    try:
                        return fn(*args[:1])
                    except Exception:
                        pass
        return {"is_valid": False, "verify_message": "未找到可用验真方法（verify_invoice/verify/run）。"}

    # 新版验真路由
    def _verify_invoice(self, invoice_data: Dict[str, Any]):
        qr_text = invoice_data.get("qr_raw") or invoice_data.get("qr_text") or ""
        ocr_text = " ".join([str(invoice_data.get(k, "")) for k in (
            "raw_text", "remark", "invoice_type", "seller_name", "buyer_name",
            "password_area", "number_area", "content"
        ) if invoice_data.get(k)])

        parsed = parse_from_qr_and_ocr(qr_text, ocr_text)

        # 在构造 LLM 输入前：融合证据 & 纠偏 service_type
        goods_names = [g.get("name","") for g in (invoice_data.get("goodsData") or [])]
        service_type = _infer_service_type(invoice_data, goods_names, "", invoice_data.get("remark",""))
        invoice_data["service_type"] = service_type  # 覆盖给 LLM 的上下文

        fpdm = parsed.get("fpdm") or invoice_data.get("invoice_code") or ""
        fphm = parsed.get("fphm") or invoice_data.get("invoice_number") or ""
        kprq = parsed.get("kprq") or invoice_data.get("invoice_date") or ""
        je_excl = invoice_data.get("amount_excl_tax") or invoice_data.get("total_amount") or ""
        try:
            je_with = invoice_data.get("amount_in_figures") or (
                (float(invoice_data.get("total_amount", 0)) + float(invoice_data.get("total_tax", 0)))
                if invoice_data.get("total_tax") is not None else ""
            )
        except (ValueError, TypeError):
            je_with = ""
        jym  = parsed.get("jym") or invoice_data.get("check_code") or ""

        def _norm_cn_date(s: str) -> str:
            s = str(s).strip().replace("年", "-").replace("月", "-").replace("日", "").replace("/", "-")
            if len(s) == 8 and s.isdigit():
                return f"{s[:4]}-{s[4:6]}-{s[6:]}"
            return s
        if kprq:
            kprq = _norm_cn_date(kprq)

        if parsed.get("inferred") and jym:
            invoice_data["check_code"] = jym
            invoice_data["check_code_from"] = "号码后6(数电票兜底)"

        if not fpdm:
            guess, src = _guess_fpdm_from_text(invoice_data)
            if guess:
                fpdm = guess
                invoice_data["invoice_code"] = fpdm
                invoice_data["invoice_code_from"] = f"推断({src})"

        payload = {
            **({"fpdm": fpdm} if fpdm else {}),
            **({"fphm": fphm} if fphm else {}),
            **({"kprq": kprq} if kprq else {}),
            **({"noTaxAmount": je_excl} if je_excl else {}),
            **({"jshj": je_with} if je_with else {}),
            **({"jym": jym} if jym else {}),
            "total_amount": invoice_data.get("total_amount"),
            "total_tax": invoice_data.get("total_tax"),
            "amount_in_figures": invoice_data.get("amount_in_figures"),
            "amount_excl_tax": invoice_data.get("amount_excl_tax"),
        }

        has_min = bool(fphm and kprq and (je_excl or je_with))
        if fpdm and fphm and kprq and (je_excl or je_with) and jym:
            return self._call_verifier(payload, allow_without_jym=False)
        if fpdm and has_min:
            return self._call_verifier(payload, allow_without_jym=True)
        if (not fpdm) and has_min:
            return self._call_verifier(payload, allow_without_jym=True)

        miss = []
        if not fphm: miss.append("发票号码")
        if not kprq: miss.append("开票日期")
        if not (je_excl or je_with): miss.append("金额")
        if miss:
            return {"is_valid": False, "verify_message": f"验真要素不足：缺少 {','.join(miss)}。请上传原始 PDF/OFD 或清晰票面（含二维码）。"}
        return {"is_valid": False, "verify_message": "验真要素不足。"}

    def _fetch_hits(self, invoice_data: Dict[str, Any], user_input: Optional[str] = None, topk: int = 6) -> List[Dict[str, Any]]:
        """兼容不同 retriever API：尽可能把命中取回来，避免 AttributeError。"""
        r = getattr(self, "retriever", None)
        if r is None:
            print("Retriever 未初始化，返回空命中。")
            return []

        # 拼一个朴素 query（不依赖外部工具函数，避免再引入未定义名）
        q_parts = [
            invoice_data.get("service_type", ""),
            invoice_data.get("service_type_detail", ""),
            invoice_data.get("remark", ""),
            invoice_data.get("seller_name", ""),
            user_input or "",
        ]
        q = " ".join([str(x) for x in q_parts if x]).strip() or "发票 合规 报销 制度 费用"

        # 常见方法名候选 + 多种入参组合（谁能跑通用谁）
        candidates = [
            ("search_policy_documents", (q,), {"top_k": topk}),
            ("search_documents",        (q,), {"top_k": topk}),
            ("search_docs",             (q,), {"top_k": topk}),
            ("search_kb",               (q,), {"top_k": topk}),
            ("search",                  (invoice_data,), {"topk": topk}),
            ("retrieve",                (), {"query": q, "topk": topk}),
            ("query",                   (q,), {"topk": topk}),
        ]
        for name, args, kwargs in candidates:
            if hasattr(r, name):
                fn = getattr(r, name)
                for a, k in ((args, kwargs), (args, {}), ((), {"query": q, "top_k": topk}), ((q,), {}), ((invoice_data,), {})):
                    try:
                        res = fn(*a, **k)
                        return res or []
                    except TypeError:
                        continue
                    except Exception as e:
                        print(f"retriever.{name} 调用失败：{e}")

        # 万能兜底：如果 retriever 有 docs 字典，就先返回前 topk 个
        try:
            if hasattr(r, "docs") and isinstance(r.docs, dict):
                items = list(r.docs.items())[:topk]
                return [{"source": os.path.basename(k), "content": v, "score": 0.0} for k, v in items]
        except Exception:
            pass

        print("未匹配到可用的检索方法，返回空命中。")
        return []

    # ---------------- 主流程：新增 evidence_data 注入 & 风控后处理 ----------------
    def process_reimbursement(self, file_path: str, file_type: str = "image",
                              user_input: str = "", evidence_data: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        invoice_data = self._extract_invoice(file_path, file_type=file_type)
        invoice_data = _normalize_amount_fields(invoice_data)
        print(f"Extracted invoice data: {invoice_data}")
        
        # ===== 统一税率：支持 ["tax_rate": [{"row":"1","word":"3%"}]] / "3%" / 0.03 三种形态 =====
        tr = invoice_data.get("tax_rate")
        def _to_percent_string(x):
            if x is None: return ""
            if isinstance(x, str):
                return x if "%" in x else (f"{float(x)*100:.0f}%" if x.replace(".","",1).isdigit() else x)
            if isinstance(x, (int, float)):
                return f"{x*100:.0f}%"
            if isinstance(x, list) and x and isinstance(x[0], dict):
                w = (x[0].get("word") or "").strip()
                return w if w else ""
            return ""

        invoice_data["tax_rate"] = _to_percent_string(tr)
        
        # 提取后立刻做一个"可用性"检查
        if invoice_data.get("__ocr_error__"):
            msg = str(invoice_data["__ocr_error__"])
            return {
                # —— 前端可见：把关键诊断字段也透出去 —— #
                "invoice_info": invoice_data,
                "ocr_debug": {
                    "error": msg,
                    "error_code": invoice_data.get("__ocr_code__"),
                    "log_id": invoice_data.get("__ocr_log_id__"),
                    "timestamp": invoice_data.get("__ocr_timestamp__"),
                    "raw": invoice_data.get("__ocr_raw__"),   # 完整原样（含 result），复制给百度 Trace 就用这个
                    "dump_path": "/tmp/last_ocr_error.json"   # 服务器本地也有一份
                },

                "verification": { "is_valid": False,
                    "verify_message": f"OCR失败：{msg}；无法提取发票要素（号码/日期/金额）。" },
                "expense_type": "UNKNOWN",
                "accounting_analysis": {
                    "account_subject": "UNKNOWN",
                    "basis": "因OCR限流/失败，未能获得必要要素；停止后续判定以避免误判。",
                    "suggestions": ["更换时间/秘钥重试", "上传更清晰的PDF/OFD原件"],
                    "sources_used": []
                },
                "risk_analysis": {
                    "risk_level": "高",
                    "risk_points": ["OCR接口限流/失败，关键要素缺失导致无法验真"],
                    "basis": ["系统日志返回 __ocr_error__ 提示"],
                    "sources_used": []
                },
                "approval_analysis": {
                    "approval_notes": ["发票要素缺失，请补充或改日重传"],
                    "suggestions": ["改用备用OCR/手动录入关键字段后再提交"]
                }
            }
        
        # === 固定"今天"，供 LLM 使用 ===
        invoice_data["now_date"] = datetime.now().strftime("%Y-%m-%d")

        # === 调用retriever获取相关文档 ===
        hits = self._fetch_hits(invoice_data,user_input=user_input,topk=6)  # 命中里要有 doc/text/score/url

        # === 先验真，再做分析（拿到金额+货物/服务名） ===
        verify_result = self._verify_invoice(invoice_data)
        print(f"Verification result: {verify_result}")

        # 将验真金额写回（只在缺失时补齐）
        vr = (verify_result or {}).get("verify_result", {}) or {}
        vdata = (vr.get("data") or {}) if isinstance(vr, dict) else {}
        try:
            if vdata.get("sumamount"):
                invoice_data.setdefault("amount_in_figures", float(vdata["sumamount"]))
            if vdata.get("goodsamount"):
                invoice_data.setdefault("total_amount", float(vdata["goodsamount"]))
            if vdata.get("taxamount"):
                invoice_data.setdefault("total_tax", float(vdata["taxamount"]))
        except Exception:
            pass

        # 收集关键字用于"差旅"纠偏（发票、验真、用户输入都算上）
        goods_names = []
        try:
            for g in (vdata.get("goodsData") or []):
                nm = (g.get("name") or "").strip()
                if nm:
                    goods_names.append(nm)
        except Exception:
            pass

        # 在构造 LLM 输入前：融合证据 & 纠偏 service_type
        # 已移除：现在在 process_reimbursement 中处理
        # goods_names = [g.get("name","") for g in (vdata.get("goodsData") or [])]
        # service_type = _infer_service_type(invoice_data, goods_names, user_input, invoice_data.get("remark",""))
        # invoice_data["service_type"] = service_type  # 覆盖给 LLM 的上下文
        # 写回验真明细，后续 flags / 模型都能看到"住宿服务"
        # invoice_data["goodsData"] = vdata.get("goodsData") or []
        # mapped_acct = _choose_account_from_keywords(goods_names, user_input, invoice_data.get("remark",""))  # 已移除：现在在 process_reimbursement 中处理

        hint_blob = " ".join([
            (invoice_data.get("seller_name") or ""),
            (invoice_data.get("service_type") or ""),
            (invoice_data.get("remark") or ""),
            " ".join(goods_names),
            str(user_input or "")
        ]).lower()

        # 命中关键词 -> 强制改为差旅费，并把 service_type 调整为"交通"
        travel_keys = ["打车", "网约车", "出租车", "客运", "运输服务", "行程单", "高德", "滴滴", "快车", "的士"]
        if any(k in hint_blob for k in travel_keys):
            invoice_data["service_type"] = "交通"   # 给模型更强的暗示
            force_travel = True
        else:
            force_travel = False

        # 把 evidence 元数据塞进 invoice_data，方便 LLM 有感知
        # 若 API 层已把 evidence_data 传进来就用；否则兜底：除主票据外的其它上传文件名塞入
        evidence_data = evidence_data or invoice_data.get("evidence_list") or []
        invoice_data["evidence_list"] = evidence_data

        # ===== 通用 flags（可选但实用）=====
        signals = []
        # goodsData.name
        vr_data = (verify_result.get("verify_result") or {}).get("data", {}) if isinstance(verify_result, dict) else {}
        for g in vr_data.get("goodsData") or []:
            n = (g.get("name") or "").strip()
            if n: signals.append(n)
        signals += [invoice_data.get("service_type_detail",""), invoice_data.get("remark",""), invoice_data.get("seller_name",""), user_input or "", invoice_data.get("filename","")]

        flags = invoice_data.setdefault("flags", {})
        flags["has_lodging"] = _has_any(signals, ["住宿","酒店","宾馆","客房","房费","入住"])
        flags["has_taxi"]    = _has_any(signals, ["打车","出租","网约车","客运","高德","滴滴","曹操","首汽","t3"])
        flags["has_meal"]    = _has_any(signals, ["餐饮","宴请","招待","酒水"])
        flags["has_meeting"] = _has_any(signals, ["会议","会务","会场","场地费"])

        # ===== 先让 LLM 给结论（内置强规则已在 analyzer 里跑过）=====
        llm_decision = self._safe_call(
            lambda: self.analyzer.analyze_invoice(
                {"invoice_info": invoice_data, "verify_result": verify_result, "now_date": invoice_data.get("now_date"),
                 "words_result": {}, # 这里可以添加OCR结果，如果需要的话
                },
                user_input=user_input or ""
            ),
            {"expense_type": "UNKNOWN", "account_subject": "UNKNOWN", "confidence": 0.0}
        )

        expense_type = llm_decision.get("expense_type") or "UNKNOWN"
        mapped_account = llm_decision.get("account_subject") or "UNKNOWN"
        
        # ===== 通用仲裁机制 =====
        confidence = float(llm_decision.get("confidence") or 0.0)

        if expense_type == "UNKNOWN" or mapped_account == "UNKNOWN" or confidence < 0.75:
            keyword_account = _choose_account_from_keywords(
                [g.get("name","") for g in (vr_data.get("goodsData") or [])],
                user_input,
                invoice_data.get("remark","")
            )
            if keyword_account:
                if "差旅" in keyword_account:
                    expense_type = "差旅费" if "住宿" not in keyword_account else "差旅费-住宿"
                    mapped_account = "6603-差旅费"
                    confidence = max(confidence, 0.85)
                elif "办公费" in keyword_account:
                    expense_type = "办公费"
                    mapped_account = "6601-办公费"
                    confidence = max(confidence, 0.85)
                elif "业务招待" in keyword_account:
                    expense_type = "业务招待费"
                    mapped_account = "6602-业务招待费"
                    confidence = max(confidence, 0.85)
                elif "会议费" in keyword_account:
                    expense_type = "会议费"
                    mapped_account = "6604-会议费"
                    confidence = max(confidence, 0.85)
                elif "培训费" in keyword_account:
                    expense_type = "培训费"
                    mapped_account = "6605-培训费"
                    confidence = max(confidence, 0.85)
                elif "通讯费" in keyword_account:
                    expense_type = "通讯费"
                    mapped_account = "6608-通讯费"
                    confidence = max(confidence, 0.85)

        # 费用类型纠偏：命中交通/打车词时，直接判定为差旅费
        if force_travel or (invoice_data.get("service_type") == "交通"):
            expense_type = "差旅费"
            # initial_analysis["expense_type"] = "差旅费"
        print(f"初步分析费用类型: {expense_type}")

        # === 新增：做一次 KB 检索，带上 URL，传给三个分析器 ===
        # 1) 组合一个查询串（费用类型 + 用户说明 + 卖方名等关键信息）
        query_bits = [
            str(expense_type or ""),
            str(user_input or ""),
            str(invoice_data.get("seller_name") or ""),
            str(invoice_data.get("invoice_type") or ""),
            "报销 审批 依据 风险 会计科目 验真 有效期"
        ]
        query = " ".join([q for q in query_bits if q.strip()])

        hits = []
        try:
            hits = self.retriever.search_policy_documents(query, top_k=5)
        except Exception:
            hits = []

        # 2) 把命中转成 sources_used，带 url（如果 retriever 没给 url，会用 PUBLIC_KB_BASE 兜底）
        sources_used = _hits_to_sources(hits)
        

        # 关键词映射兜底
        text_blob = " ".join(str(invoice_data.get(k, "")) for k in [
            "service_type", "remark", "invoice_type", "seller_name", "buyer_name"
        ])
        kw_candidates = self.retriever.score_accounts(text_blob, top_k=3)

        # ★ 统一变量名：只用 mapped_account
        kw_direct = _choose_account_from_keywords(
            locals().get("goods_names", []),  # 阻止未定义
            user_input,
            invoice_data.get("remark","")
        )
        mapped_account = ""
        if kw_direct:
            mapped_account = kw_direct
        elif kw_candidates and kw_candidates[0].get("score", 0) >= HARD_THRESHOLD_SCORE:
            mapped_account = kw_candidates[0].get("account", "")

        # 会计科目
        print("开始进行会计科目匹配分析...")
        acc_pkg = self.retriever.get_accounting_rules(expense_type)
        acc_contexts: List[Dict[str, str]] = []
        for name in ("会计科目口径手册_rag版.md", "accounting_rules.txt", "公司报销规则.txt", "公司报销制度.md"):
            try:
                if hasattr(self.retriever, "docs") and name in self.retriever.docs:
                    acc_contexts.append({"source": name, "content": self.retriever.docs[name]})
            except Exception:
                pass
        for t in acc_pkg.get("texts", []):
            if isinstance(t, str) and t.strip():
                acc_contexts.append({"source": "知识库片段", "content": t})

        # 添加更精准的检索关键词提示
        qhint_terms = []
        for k in ("service_type", "service_type_detail", "remark", "seller_name"):
            v = str(invoice_data.get(k) or "").strip()
            if v:
                qhint_terms.append(v)
        qhint_terms += ["差旅", "交通", "审批阈值", "报销时限", "证据链", "发票要素", "合规"]

        query_hint = " ".join(qhint_terms)
        # 使用增强的查询提示检索更多相关上下文
        try:
            acc_contexts += self.retriever.search_policy_documents(query_hint, top_k=8)
        except Exception:
            pass

        # 1) 先把 context 名字并进来
        sources_used = _merge_sources(sources_used, _sources_from_contexts(acc_contexts))

        # ===== 会计科目详细分析（LLM 版），把知识库片段塞进去提升说理性 =====
        accounting_analysis = self._safe_call(
            lambda: self.analyzer.analyze_accounting_subjects(
                invoice_data, expense_type=expense_type, contexts=(acc_contexts + hits)
            ),
            {"account_subject": "UNKNOWN", "basis": "", "suggestions": [], "sources_used": []}
        )
        accounting_analysis = _clean_obj(accounting_analysis)

        # 2) 再把模块自己的 sources 并进来（得到对象数组，已去重）
        accounting_analysis["sources_used"] = _merge_sources(
            accounting_analysis.get("sources_used"), sources_used
        )
        # ===== 把最终"科目"回填，如果 LLM detailed 返回为空就用前面的 subject =====
        final_account_subject = accounting_analysis.get("account_subject") or mapped_account or "UNKNOWN"
        
        # 如果最终科目是UNKNOWN，尝试使用映射的科目
        if final_account_subject == "UNKNOWN":
            final_account_subject = mapped_account if mapped_account else "UNKNOWN"
        
        # 确保最终科目设置到分析结果中
        accounting_analysis["account_subject"] = final_account_subject

        # 关键词映射纠偏
        # mapped_account 是你已有的关键词映射结果（打分≥HARD_THRESHOLD_SCORE才会给）
        # if mapped_account:
        #     mapped_account = _normalize_subject(mapped_account)
        #     ai_acc = (accounting_analysis.get("account_subject") or "").strip()
        #     if not ai_acc:
        #         accounting_analysis["account_subject"] = mapped_account
        #         accounting_analysis.setdefault("basis", "")
        #         accounting_analysis["basis"] += "；依据关键词映射表高置信度匹配"
        #     elif mapped_account in ("差旅费-交通费","差旅费-市内交通费","差旅费") and "办公" in ai_acc:
        #         accounting_analysis["account_subject"] = mapped_account
        #         accounting_analysis.setdefault("suggestions", []).append("按费用类型一致性已将科目从"办公费"纠偏为差旅相关")

        # 住宿场景硬约束：如果费用类型是差旅费或存在住宿标识
        # 且会计科目包含"办公"或以"6601"开头，则强制使用"6603-差旅费"
        if (expense_type == "差旅费" or flags.get("has_lodging", False)):
            ai_acc = (accounting_analysis.get("account_subject") or "").strip()
            if "办公" in ai_acc or ai_acc.startswith("6601"):
                accounting_analysis["account_subject"] = "6603-差旅费"
                accounting_analysis.setdefault("suggestions", []).append("按住宿场景规范将科目从'办公费'强制归并为差旅费")

        # 差旅费 -> 锁定会计科目（可按你公司口径改）
        if expense_type == "差旅费":
            accounting_analysis["account_subject"] = "6603-差旅费"
            accounting_analysis["account_subject"] = _normalize_subject(accounting_analysis["account_subject"])
            _ensure_list_field(accounting_analysis, "basis")
            accounting_analysis["basis"].append("命中差旅关键词，按口径归集为差旅费。")
            # 引用来源用合并，确保是统一结构
            accounting_analysis["sources_used"] = _merge_sources(
                accounting_analysis.get("sources_used"),
                ["发票关键词-会计科目map表.txt"]
            )

        # —— 打车/市内交通 → 强制归并到差旅费 —— 
        subject_hint_blob = " ".join([
            expense_type or "",
            str(invoice_data.get("service_type") or ""),
            str(invoice_data.get("remark") or ""),
            str(invoice_data.get("seller_name") or ""),
            str(user_input or "")
        ])
        if ("差旅" in (expense_type or "")) or any(k in subject_hint_blob for k in ["打车", "网约车", "出租车", "行程单", "高德", "滴滴", "快车", "的士"]):
            forced = "6603-差旅费"
            accounting_analysis["account_subject"] = forced
            accounting_analysis["account_subject"] = _normalize_subject(accounting_analysis["account_subject"])
            _ensure_list_field(accounting_analysis, "basis")
            accounting_analysis["basis"].append("命中交通/差旅关键词，强制归并到差旅费。")
            accounting_analysis["sources_used"] = _merge_sources(
                accounting_analysis.get("sources_used"),
                ["发票关键词-会计科目map表.txt"]
            )

        ai_subj = (accounting_analysis.get("account_subject") or "")
        accounting_analysis["account_subject"] = _normalize_subject(ai_subj)

        # 审计员：只要是差旅或识别到住宿证据，禁止落到办公费
        acc_subject = (accounting_analysis.get("account_subject") or "").strip()
        # expense_type = (initial_analysis.get("expense_type") or invoice_data.get("expense_type") or "").strip()

        if (("差旅" in expense_type) or any("住宿" in (g or "") for g in goods_names)) \
           and (("办公" in acc_subject) or acc_subject.startswith("6601")):
            accounting_analysis["account_subject"] = "6603-差旅费"
            _ensure_list_field(accounting_analysis, "basis")
            accounting_analysis["basis"].append("根据住宿/差旅强信号，将误判的'办公费'纠偏为'6603-差旅费'。")

        # 风险点
        print("开始进行发票风险点分析...")
        ver_contexts: List[Dict[str, str]] = []
        for name in ("发票验真要点_rag版.md", "verification_points.txt"):
            try:
                if hasattr(self.retriever, "docs") and name in self.retriever.docs:
                    ver_contexts.append({"source": name, "content": self.retriever.docs[name]})
            except Exception:
                pass
        if getattr(self.retriever, "verification_window_days", None):
            ver_contexts.append({
                "source": "结构化规则-验真有效期",
                "content": f"发票有效期（验真指导）约 {self.retriever.verification_window_days} 天"
            })

        _now = invoice_data.get("now_date")
        if _now:
            ver_contexts.append({"source": "系统当前时间", "content": f"今天是 {_now}（调用方提供）。"})

        # 1) 先把 context 名字并进来
        sources_used = _merge_sources(sources_used, _sources_from_contexts(ver_contexts))

        risk_analysis = self._safe_call(
            lambda: self.analyzer.generate_risk_analysis(invoice_data, contexts=(ver_contexts + hits), flags=flags),
            {"risk_points": [], "basis": "", "risk_level": "未知", "sources_used": []}
        )
        # 2) 再把模块自己的 sources 并进来（得到对象数组，已去重）
        risk_analysis["sources_used"] = _merge_sources(
            risk_analysis.get("sources_used"), sources_used
        )
        risk_analysis = _clean_obj(risk_analysis)               # ★新增

        # —— 新增：basis 为空，用来源兜底 —— #
        if not risk_analysis.get("basis"):
            seeds = risk_analysis.get("sources_used", [])
            risk_analysis["basis"] = [
                (f"命中《{s.get('title','知识库片段')}》相似度 {float(s.get('score',0)):.3f}"
                 if isinstance(s.get('score'), (int,float)) else f"命中《{s.get('title','知识库片段')}》")
                for s in seeds[:5]
            ]

        # —— 硬校验补充 ——（价税合计、缺要素、报销周期）
        hard_risks = self._hard_risk_checks(invoice_data)
        for r in hard_risks:
            if r not in risk_analysis.get("risk_points", []):
                risk_analysis.setdefault("risk_points", []).append(r)

        # —— 佐证对比：从 evidence filename 抽取的日期/金额 与发票对齐 —— 
        self._evidence_enrich_and_align(invoice_data, risk_analysis)

        # —— 如果用户已上传相关佐证，移除"请上传行程单/票据"类提示 —— 
        self._dedup_evidence_related_warnings(invoice_data, risk_analysis)

        print(f"Risk analysis result: {risk_analysis}")

        # —— 验真（提前）——
        verify_result = self._verify_invoice(invoice_data)
        print(f"Verification result: {verify_result}")

        vr = (verify_result or {}).get("verify_result", {})
        vdata = (vr or {}).get("data", {}) if isinstance(vr, dict) else {}

        try:
            if vdata.get("sumamount"):
                invoice_data.setdefault("amount_in_figures", float(vdata["sumamount"]))
            if vdata.get("goodsamount"):
                invoice_data.setdefault("total_amount", float(vdata["goodsamount"]))
            if vdata.get("taxamount"):
                invoice_data.setdefault("total_tax", float(vdata["taxamount"]))
            _normalize_amount_fields(invoice_data)
        except Exception:
            pass

        # 强制补齐 amount_in_figures（至少有含税总额）
        excl = _safe_float(invoice_data.get("total_amount"))
        tax  = _safe_float(invoice_data.get("total_tax"))
        if not invoice_data.get("amount_in_figures") and (excl or tax):
            invoice_data["amount_in_figures"] = round((excl or 0) + (tax or 0), 2)

        # 审批要点
        print("开始进行报销审核要点分析...")
        ap_pkg = self.retriever.get_approval_process(invoice_data)
        ap_contexts: List[Dict[str, str]] = []
        for name in ("approval_process.txt", "公司报销制度.md"):
            try:
                if hasattr(self.retriever, "docs") and name in self.retriever.docs:
                    ap_contexts.append({"source": name, "content": self.retriever.docs[name]})
            except Exception:
                pass
        ap_contexts.append({"source": "结构化规则-审批阈值", "content": json_dump(ap_pkg)})

        _now = invoice_data.get("now_date")
        if _now:
            ap_contexts.append({"source": "系统当前时间", "content": f"今天是 {_now}（调用方提供）。"})

        # 1) 先把 context 名字并进来
        sources_used = _merge_sources(sources_used, _sources_from_contexts(ap_contexts))

        # 将flags信息添加到invoice_data中，供审核模块使用
        invoice_data["flags"] = flags

        # 构造上下文摘要信息
        cat_info = infer_category_from_invoice({"verify_result": verify_result, **invoice_data})
        context_summary = {
            "detected_category": cat_info["category"],   # 例：交通-打车/市内
            "keyword_hits": cat_info["hits"],            # 例：["打车","客运","行程单"]
            "suggested_evidence": cat_info["evidence_required"]
        }

        # 把原来想传给模型的结构化信息，写入到 contexts 里供 RAG 使用
        extra_struct_ctx = [
            {"source": "结构化-调用侧上下文汇总", "content": json_dump({
                "context_summary": context_summary,
                "user_input": user_input,
                "verify_result_brief": {
                    "is_valid": (verify_result or {}).get("is_valid"),
                    "verify_message": (verify_result or {}).get("verify_message"),
                },
                "now_date": invoice_data.get("now_date")
            })}
        ]
        approval_analysis = self._safe_call(
            lambda: self.analyzer.generate_approval_notes(
                invoice_data,                   # ← 按现有签名传参
                expense_type,
                contexts=(ap_contexts + hits + extra_struct_ctx),
                flags=flags
            ),
            {"approval_notes": [], "basis": "", "suggestions": [], "sources_used": []}
        ) or {}

        # 2) 再把模块自己的 sources 并进来（得到对象数组，已去重）
        approval_analysis["sources_used"] = _merge_sources(
            approval_analysis.get("sources_used"), sources_used
        )
        approval_analysis["sources_used"] = _dedup_sources(
    [ _normalize_source(x) for x in (approval_analysis.get("sources_used") or []) if x ]
)
        approval_analysis = _clean_obj(approval_analysis)       # ★新增
        # —— 新增：basis 为空，用来源兜底 —— #
        if not approval_analysis.get("basis"):
            seeds = approval_analysis.get("sources_used", [])
            approval_analysis["basis"] = [
                (f"命中《{s.get('title','知识库片段')}》相似度 {float(s.get('score',0)):.3f}"
                 if isinstance(s.get('score'), (int,float)) else f"命中《{s.get('title','知识库片段')}》")
                for s in seeds[:5]
            ]
        
        if ap_pkg.get("selected"):
            approval_analysis.setdefault("approval_notes", []).append(
                f"【制度阈值】类别={ap_pkg['category']} 金额区间={ap_pkg['selected']['min']}~{ap_pkg['selected'].get('max','∞')}元，审批链：{ap_pkg['selected']['approvers']}"
            )
        # 审批要点（已有 approval_analysis 后面，保持原来 append 制度阈值的代码不动）
        if not approval_analysis.get("approval_notes"):
            # 再兜底：即便模型没写，也把结构化阈值直出一条
            sel = ap_pkg.get("selected")
            if sel:
                approval_analysis["approval_notes"] = [
                    f"【结构化阈值】类别={ap_pkg.get('category')} 金额区间={sel['min']}~{sel.get('max','∞')}元，审批链：{sel['approvers']}"
                ]
        print(f"Approval analysis result: {approval_analysis}")

        # 验真
        verify_result = self._verify_invoice(invoice_data)
        print(f"Verification result: {verify_result}")

        vr = (verify_result or {}).get("verify_result", {})
        vdata = (vr or {}).get("data", {}) if isinstance(vr, dict) else {}
        try:
            if vdata.get("sumamount"):
                invoice_data.setdefault("amount_in_figures", float(vdata["sumamount"]))
            if vdata.get("goodsamount"):
                invoice_data.setdefault("total_amount", float(vdata["goodsamount"]))
            if vdata.get("taxamount"):
                invoice_data.setdefault("total_tax", float(vdata["taxamount"]))
            _normalize_amount_fields(invoice_data)
        except Exception:
            pass

        # 强制补齐amount_in_figures
        excl = _safe_float(invoice_data.get("total_amount"))
        tax  = _safe_float(invoice_data.get("total_tax"))
        if not invoice_data.get("amount_in_figures") and (excl or tax):
            invoice_data["amount_in_figures"] = round(excl + tax, 2)

        # —— 字段别名，兼容前端各种取法 —— 
        for blk in (accounting_analysis, risk_analysis, approval_analysis):
            if isinstance(blk, dict):
                if blk.get("sources_used") and not blk.get("references"):
                    blk["references"] = blk["sources_used"]     # 引用来源别名
                if blk is approval_analysis and not blk.get("approval_points"):
                    blk["approval_points"] = blk.get("approval_notes", [])  # 审核注意事项别名

        # **关键：从验真结果拿数据（金额 / 明细）并“只在缺失时补齐”**
        vr    = (verify_result or {}).get("verify_result", {}) or {}
        vdata = (vr.get("data") or {}) if isinstance(vr, dict) else {}

        # 金额三件套补齐
        try:
            if vdata.get("sumamount"):
                invoice_data.setdefault("amount_in_figures", float(vdata["sumamount"]))
            if vdata.get("goodsamount"):
                invoice_data.setdefault("total_amount",     float(vdata["goodsamount"]))
            if vdata.get("taxamount"):
                invoice_data.setdefault("total_tax",        float(vdata["taxamount"]))
        except Exception:
            pass

        # **关键：回填明细 & 用明细/备注/用户输入纠偏服务类型**
        invoice_data["goodsData"] = vdata.get("goodsData") or []
        goods_names = [g.get("name","") for g in invoice_data["goodsData"]]
        invoice_data["service_type"] = _infer_service_type(
            invoice_data, goods_names, user_input, invoice_data.get("remark","")
        )

        # 强制规范化模型输出中的日期表述
        nd = invoice_data.get("now_date")
        for blk in (risk_analysis, approval_analysis):
            if isinstance(blk, dict):
                for k in ("risk_points", "basis", "approval_notes", "suggestions"):
                    v = blk.get(k)
                    if isinstance(v, list):
                        blk[k] = [_enforce_now_date_in_text(x, nd) for x in v]
                    elif isinstance(v, str):
                        blk[k] = _enforce_now_date_in_text(v, nd)

        # 汇总
        result = {
            "invoice_info": invoice_data,
            "expense_type": expense_type,
            "accounting_analysis": accounting_analysis,
            "risk_analysis": risk_analysis,
            "approval_analysis": approval_analysis,
            "verification": verify_result,
            "keyword_account_candidates": kw_candidates,
            "processed_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        result["policy_warnings"] = self._collect_policy_warnings(invoice_data, verify_result)

        # 给前端一个纯文本引用来源，防止前端只认识字符串数组
        def _titles(srcs):
            out = []
            for s in (srcs or []):
                if isinstance(s, dict):
                    t = s.get("title") or s.get("name") or s.get("source")
                    if t:
                        out.append(t)
                elif isinstance(s, str):
                    out.append(s)
            return out

        # 确保三块分析结果都经过同一把清洗
        result["accounting_analysis"] = _fix_sources_field(result["accounting_analysis"], contexts=hits)
        result["risk_analysis"] = _fix_sources_field(result["risk_analysis"], contexts=hits)
        result["approval_analysis"] = _fix_sources_field(result["approval_analysis"], contexts=hits)

        for blk_key in ("accounting_analysis", "risk_analysis", "approval_analysis"):
            blk = result.get(blk_key) or {}
            if blk.get("sources_used") and not blk.get("references_text"):
                blk["references_text"] = _titles(blk["sources_used"])

        # 金额别名，避免前端拿错字段
        info = result.get("invoice_info", {})
        if "amount_in_figures" in info and "amount_with_tax" not in info:
            info["amount_with_tax"] = info["amount_in_figures"]   # 含税
        if "total_amount" in info and "amount_excl_tax" not in info:
            info["amount_excl_tax"] = info["total_amount"]        # 不含税
        if "total_tax" in info and "tax_amount" not in info:
            info["tax_amount"] = info["total_tax"]                # 税额

        # 总收尾
        for blk_key in ("accounting_analysis", "risk_analysis", "approval_analysis"):
            blk = result.get(blk_key) or {}
            # 二次规范来源结构 + 去除将 dict 字面量当标题的脏字符串
            blk = _fix_sources_field(blk)
            # 文本里的 (total_amount) 等英文字段提示、以及"当前日期为XXXX"统一清理/规范
            blk = _clean_obj(blk)
            result[blk_key] = blk

        return result

    # ---------------- 规则校验 ----------------
    def _hard_risk_checks(self, invoice_data: Dict[str, Any]) -> List[str]:
        risks: List[str] = []
        excl = _safe_float(invoice_data.get("total_amount"))
        tax = _safe_float(invoice_data.get("total_tax"))
        incl = _safe_float(invoice_data.get("amount_in_figures") or _safe_float(excl + tax))
        if round(excl + tax, 2) != round(incl, 2):
            risks.append("价税合计与不含税+税额不一致")

        if not invoice_data.get("invoice_number"):
            risks.append("发票号码缺失")
        if invoice_data.get("invoice_type", "").find("电子") >= 0 and not invoice_data.get("check_code"):
            risks.append("电子发票校验码缺失")

        inv_dt = _parse_date_cn(invoice_data.get("invoice_date"))
        if inv_dt:
            # 原来：days = (datetime.now() - inv_dt).days
            # 修改为：优先用 invoice_data['now_date']，没有就不要写"距今/超过XX天"类风险
            now_dt = None
            now_raw = invoice_data.get("now_date")
            if now_raw:
                try:
                    if isinstance(now_raw, str):
                        # 兼容 "2025年08月21日" / "2025-08-21"
                        s = now_raw.replace("年", "-").replace("月", "-").replace("日", "").replace("/", "-").strip()
                        if len(s) == 8 and s.isdigit():
                            s = f"{s[:4]}-{s[4:6]}-{s[6:]}"
                        from datetime import datetime
                        now_dt = datetime.fromisoformat(s)
                    else:
                        now_dt = now_raw  # 已是 datetime
                except Exception:
                    now_dt = None

            if now_dt:
                days = (now_dt - inv_dt).days
                max_days = 180
                if days > max_days:
                    risks.append(f"已超过公司报销周期 {max_days} 天（实际 {days} 天）")
                elif days > 90:
                    risks.append(f"已超过验真有效期指导 90 天（实际 {days} 天），需补充说明或特批")
        return risks

    def _collect_policy_warnings(self, invoice_data: Dict[str, Any], verify_result: Dict[str, Any]) -> List[str]:
        warnings: List[str] = []
        for key in ("invoice_number", "invoice_date", "total_amount"):
            if not invoice_data.get(key):
                warnings.append(f"发票信息不完整，缺少 {key}")
        return warnings

    # ---------------- 佐证比对&清洗 ----------------
    def _evidence_enrich_and_align(self, invoice_data: Dict[str, Any], risk_analysis: Dict[str, Any]) -> None:
        """利用 evidence_data 中 filename 解析出的日期/金额，对比发票"""
        evs: List[Dict[str, Any]] = invoice_data.get("evidence_list") or []
        if not evs:
            return
        # 汇总证据的日期/金额线索
        ev_dates = []
        ev_amounts = []
        ev_types = set()
        for e in evs:
            t = (e.get("type") or "").strip()
            if t:
                ev_types.add(t)
            d = e.get("derived_date")
            if d:
                try:
                    ev_dates.append(_parse_date_cn(d))
                except Exception:
                    pass
            a = e.get("derived_amount")
            if a is not None:
                try:
                    ev_amounts.append(float(a))
                except Exception:
                    pass

        # 对比日期：任何一个佐证日期与发票开票日相差 > 90 天，提示一次
        inv_dt = _parse_date_cn(invoice_data.get("invoice_date"))
        if inv_dt and ev_dates:
            for d in ev_dates:
                if not d:
                    continue
                delta = abs((inv_dt - d).days)
                if delta > 90:
                    msg = f"佐证日期与发票日期相差 {delta} 天（>90 天）"
                    if msg not in risk_analysis.get("risk_points", []):
                        risk_analysis.setdefault("risk_points", []).append(msg)
                        risk_analysis.setdefault("basis", []).append("依据《verification_points.txt》验真有效期与《公司报销制度.md》超期报销提示")
                        risk_analysis.setdefault("sources_used", []).extend(["verification_points.txt", "公司报销制度.md"])
                        break

        # 对比金额：如有佐证金额线索，且与发票不含税/价税合计明显不一致，提示一次
        inv_excl = _safe_float(invoice_data.get("total_amount"))
        inv_incl = _safe_float(invoice_data.get("amount_in_figures"))
        if ev_amounts and (inv_excl or inv_incl):
            ref = inv_incl or inv_excl
            for a in ev_amounts:
                if abs(a - ref) >= 0.05:  # 容忍 5 分差
                    msg = f"佐证金额线索（{a:.2f}）与发票金额（{ref:.2f}）不一致"
                    if msg not in risk_analysis.get("risk_points", []):
                        risk_analysis.setdefault("risk_points", []).append(msg)
                        risk_analysis.setdefault("basis", []).append("金额一致性核验（内部控制）")
                    break

        # 把 evidence 的"已具备类型"挂到发票上，方便 LLM少提无效建议
        invoice_data["evidence_types_present"] = sorted(list(ev_types))


    def _dedup_evidence_related_warnings(self, invoice_data: dict, risk_analysis: dict) -> None:
        """去重/合并与'证据/佐证/证据链'相关的重复风险点，避免LLM多次同义表达。"""
        pts = list(risk_analysis.get("risk_points") or [])
        if not pts:
            return
        out, seen = [], set()
        for p in pts:
            key = re.sub(r"[。；;，,.\s]", "", str(p))
            # 归一关键类目
            if any(k in key for k in ("证据链", "证据不足", "佐证", "evidence")):
                norm = "证据链不完整/佐证不足"
            else:
                norm = key
            if norm in seen:
                continue
            seen.add(norm)
            out.append(p)
        risk_analysis["risk_points"] = out

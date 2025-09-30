# -*- coding: utf-8 -*-

import json
import httpx
from typing import List, Dict, Any, Optional

# ===== 通用规则：候选类别、会计科目、触发关键词 =====
RULE_BOOK = [
    # 差旅 - 住宿
    {"expense_type": "差旅费-住宿", "account": "6603-差旅费", "keys": ["住宿","酒店","宾馆","客房","房费","入住","连住"]},
    # 差旅 - 市内交通/打车
    {"expense_type": "差旅费-市内交通/打车", "account": "6603-差旅费", "keys": ["打车","网约车","出租","客运","高德","滴滴","曹操","首汽","t3"]},
    # 差旅 - 城际交通（火车/机票等）
    {"expense_type": "差旅费-城际交通", "account": "6603-差旅费", "keys": ["机票","航班","航空","登机","铁路","火车票","高铁","动车","车次","航段"]},
    # 办公费
    {"expense_type": "办公费", "account": "6601-办公费", "keys": ["办公用品","文具","耗材","复印纸","打印纸","硒鼓","墨盒","碳粉","名片","印刷","装订"]},
    # 会议费
    {"expense_type": "会议费", "account": "6604-会议费", "keys": ["会议","会务","场地费","会场","会展","布展"]},
    # 培训费
    {"expense_type": "培训费", "account": "6605-培训费", "keys": ["培训","课程","学费","讲师费","认证","考试费"]},
    # 业务招待费（吃饭/宴请）
    {"expense_type": "业务招待费", "account": "6602-业务招待费", "keys": ["宴请","招待","餐饮","饭店","酒楼","酒水","包间"]},
    # 通讯费
    {"expense_type": "通讯费", "account": "6608-通讯费", "keys": ["通信","通讯","话费","流量","宽带","固话","电话费","光纤"]},
    # 快递/邮寄 -> 归到办公费更稳妥
    {"expense_type": "办公费", "account": "6601-办公费", "keys": ["快递","运单","物流","邮寄","邮费","快件","顺丰","中通","圆通","EMS"]},
    # 信息/软件/技术服务 -> 先归"管理费用-其他"避免你公司自定义细目不一致
    {"expense_type": "管理费用-其他", "account": "6601-办公费", "keys": ["信息服务","软件订阅","SaaS","技术服务","咨询","平台使用","维护费"]},
]

# 信号权重：票面/验真明细 > 备注/用户输入 > 卖方名 > 文件名
_SOURCE_WEIGHTS = {"goods": 1.2, "service_type_detail": 1.2, "remark": 0.9, "user": 0.9, "seller": 0.5, "file": 0.3}

def _collect_signal_texts(invoice_data: dict, user_note: str = "") -> dict:
    inv = invoice_data.get("invoice_info", {}) if "invoice_info" in invoice_data else invoice_data
    vr = (invoice_data.get("verify_result") or {}).get("data", {}) if isinstance(invoice_data.get("verify_result"), dict) else {}
    words = invoice_data.get("words_result") or {}

    goods = []
    # goodsData.name（验真）
    for g in vr.get("goodsData") or []:
        n = (g.get("name") or "").strip()
        if n: goods.append(n)
    # OCR CommodityName
    for itm in words.get("CommodityName") or []:
        w = itm.get("word","").strip()
        if w: goods.append(w)
    # 自带的 invoice_info.goods（如有）
    if isinstance(inv.get("goods"), list):
        for g in inv["goods"]:
            goods.append(g if isinstance(g, str) else g.get("word",""))

    return {
        "goods": [g for g in dict.fromkeys(goods) if g][:20],
        "service_type_detail": inv.get("service_type_detail","") or words.get("ServiceType","") or inv.get("service_type",""),
        "remark": inv.get("remark",""),
        "seller": inv.get("seller_name",""),
        "file": inv.get("filename",""),
        "user": user_note or "",
    }

def _rule_vote(signals: dict) -> tuple[str, str, float, list]:
    """
    返回: (expense_type, account, score, evidence)
    score 用于与 LLM 结果仲裁：>=2.2 视为强匹配，可覆盖 LLM；>=1.0 可兜底 UNKNOWN
    """
    corpus_parts = []
    evidence = []
    # 组成加权文本
    corpus_parts += [(" ".join(signals["goods"]), _SOURCE_WEIGHTS["goods"])]
    corpus_parts += [(signals["service_type_detail"], _SOURCE_WEIGHTS["service_type_detail"])]
    corpus_parts += [(signals["remark"], _SOURCE_WEIGHTS["remark"])]
    corpus_parts += [(signals["user"], _SOURCE_WEIGHTS["user"])]
    corpus_parts += [(signals["seller"], _SOURCE_WEIGHTS["seller"])]
    corpus_parts += [(signals["file"], _SOURCE_WEIGHTS["file"])]

    best = ("UNKNOWN", "UNKNOWN", 0.0)
    for rule in RULE_BOOK:
        sc = 0.0
        hit_terms = []
        for text, w in corpus_parts:
            t = (text or "").lower()
            for k in rule["keys"]:
                if k.lower() in t:
                    sc += 1.0 * w
                    hit_terms.append(k)
        if sc > best[2]:
            best = (rule["expense_type"], rule["account"], sc)
            evidence = list(dict.fromkeys(hit_terms))
    return (*best, evidence)

# 定义费用类型和会计科目集合（从reimbursement_processor.py中获取）
EXPENSE_TYPES = ["差旅费", "办公费", "业务招待费", "培训费", "通讯费", "会议费"]
ACCOUNT_SUBJECTS = ["6601-办公费", "6602-业务招待费", "6603-差旅费", "6604-会议费", "6605-培训费", "6608-通讯费"]

SYSTEM_PROMPT = f"""
你是企业报销单的"会计科目判定器"。只允许从如下集合中选择：
- 费用类型集合：{", ".join(EXPENSE_TYPES)}，或 UNKNOWN
- 会计科目集合：{", ".join(ACCOUNT_SUBJECTS)}，或 UNKNOWN

优先级规则（从高到低）：
1) 若任一来源（验真 goodsData.name、发票明细、备注、用户输入、文件名）含【住宿/酒店/宾馆/房费】→ 费用类型=差旅费，会计科目=6603-差旅费。
2) 含【网约车/出租车/打车/客运/交通/高铁/机票/地铁/公交/滴滴/高德打车/曹操】→ 费用类型=差旅费，会计科目=6603-差旅费。
3) 仅当出现【办公用品/文具/耗材/复印纸/打印纸/硒鼓/墨盒/印刷/名片】时才可判为"6601-办公费"。
4) 仅出现"服务/服务费"等泛词，且无上面任何明确线索 → 返回 UNKNOWN（不得臆测）。
5) 多线索冲突按 1>2>3 处理；输出时列出触发的具体证据。

仅输出以下 JSON（不多字）：
{{
  "expense_type": "<{ '|'.join(EXPENSE_TYPES) }> 或 UNKNOWN",
  "account_subject": "<{ '|'.join(ACCOUNT_SUBJECTS) }> 或 UNKNOWN",
  "evidence": ["简短证据1","简短证据2"],
  "confidence": 0.0
}}
"""

def _fewshot_blocks():
    return [
        {
          "role":"user",
          "content":json.dumps({
            "invoice_info":{"service_type":"服务","goods":["*住宿服务*住宿费"],"remark":""},
            "user_note":"住宿费报销","evidence_list":[]
          }, ensure_ascii=False)
        },
        {
          "role":"assistant",
          "content":json.dumps({
            "expense_type":"差旅费","account_subject":"6603-差旅费",
            "evidence":["goods含'住宿费'","用户写明住宿"],"confidence":0.95
          }, ensure_ascii=False)
        },
        {
          "role":"user",
          "content":json.dumps({
            "invoice_info":{"service_type":"服务","goods":[],"remark":""},
            "user_note":"","evidence_list":[]
          }, ensure_ascii=False)
        },
        {
          "role":"assistant",
          "content":json.dumps({
            "expense_type":"UNKNOWN","account_subject":"UNKNOWN",
            "evidence":["仅有泛化'服务'且无其他线索"],"confidence":0.3
          }, ensure_ascii=False)
        },
        {
          "role":"user",
          "content":json.dumps({
            "invoice_info":{"service_type":"客运服务","goods":["*运输服务*客运服务费"],"remark":"上海出差打车"},
            "user_note":"网约车费用报销","evidence_list":[]
          }, ensure_ascii=False)
        },
        {
          "role":"assistant",
          "content":json.dumps({
            "expense_type":"差旅费","account_subject":"6603-差旅费",
            "evidence":["goods含'客运服务费'","备注含'出差/打车'"],"confidence":0.92
          }, ensure_ascii=False)
        },
        {
          "role":"user",
          "content":json.dumps({
            "invoice_info":{"service_type":"信息服务","goods":["*办公用品*复印纸"],"remark":"采购复印纸"},
            "user_note":"复印纸两箱","evidence_list":[]
          }, ensure_ascii=False)
        },
        {
          "role":"assistant",
          "content":json.dumps({
            "expense_type":"办公费","account_subject":"6601-办公费",
            "evidence":["goods含'办公用品/复印纸'"],"confidence":0.9
          }, ensure_ascii=False)
        }
    ]

def _build_context_block(contexts):
    """
    把检索到的上下文整理成一个可读的 prompt 片段。
    兼容 text/content 是 dict/list 的情况，统一转为字符串。
    """
    import json, os

    lines = []
    for c in (contexts or []):
        if isinstance(c, dict):
            # 名称：source/doc/title/file 任取其一
            name = c.get("source") or c.get("doc") or c.get("title") or c.get("file") or "未知来源"
            try:
                name = os.path.splitext(os.path.basename(str(name)))[0]
            except Exception:
                name = str(name)

            # 正文：优先 text，其次 content
            raw = c.get("text", None)
            if raw is None:
                raw = c.get("content", "")

            # 统一转成字符串
            if isinstance(raw, (dict, list)):
                try:
                    raw = json.dumps(raw, ensure_ascii=False)
                except Exception:
                    raw = str(raw)
            else:
                raw = "" if raw is None else str(raw)

            text = raw.strip()
            if not text:
                continue

            # 可选显示 score
            score = c.get("score")
            try:
                score = float(score)
            except Exception:
                score = None

            if score is not None:
                lines.append(f"【{name} | score={score:.4f}】\n{text}")
            else:
                lines.append(f"【{name}】\n{text}")

        else:
            # 非 dict 的上下文，直接字符串化
            s = "" if c is None else str(c)
            s = s.strip()
            if s:
                lines.append(s)

    joined = "\n\n".join(lines)
    # 控制总体长度，防炸 prompt
    if len(joined) > 3500:
        joined = joined[:3500] + "…"
    return joined or "（无命中上下文）"


class ExpenseAnalyzer:
    def __init__(self, api_key: str, base_url: str, model: str):
        # 兼容 OpenAI/DashScope Chat Completions
        self.api_key = api_key or ""
        self.base_url = (base_url or "").rstrip("/")
        self.model = model or "gpt-3.5-turbo"

    def analyze_with_llm(self, invoice_data: Dict[str, Any], user_input: str = "") -> Dict[str, Any]:
        # 1) 统一收集信号，做规则投票
        sig = _collect_signal_texts(invoice_data, user_input)
        rule_exp, rule_acc, rule_score, rule_hits = _rule_vote(sig)

        # 2) 若规则命中很强（>=2.2），直接采用（例如：酒店+住宿费+备注入住）
        if rule_score >= 2.2:
            return {
                "expense_type": rule_exp,
                "account_subject": rule_acc,
                "evidence": [f"规则强匹配: {', '.join(rule_hits)}"],
                "confidence": min(0.98, 0.8 + rule_score/10.0),
            }

        # 3) 让 LLM 做语义判定（保留你原有 few-shot、SYSTEM 提示）
        invoice_info = invoice_data.get("invoice_info", {})
        now_date = invoice_data.get("now_date", "")
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages += _fewshot_blocks()
        messages += [{
            "role": "user",
            "content": json.dumps({
                "invoice_info": {
                    "invoice_type": invoice_info.get("invoice_type", ""),
                    "service_type": sig["service_type_detail"],
                    "remark": sig["remark"],
                    "goods": sig["goods"],
                    "filename": sig["file"],
                    "seller_name": sig["seller"],
                },
                "user_note": sig["user"],
                "evidence_list": invoice_data.get("evidence_list", []),
                "now": now_date
            }, ensure_ascii=False)
        }]
        resp = self._chat_messages(messages)
        data = self._safe_json(resp, fallback={"expense_type":"UNKNOWN","account_subject":"UNKNOWN","evidence":[],"confidence":0.0})

        # 4) 仲裁：若 LLM 低置信或 UNKNOWN，而规则得分≥1.0，就用规则兜底
        conf = float(data.get("confidence") or 0.0)
        if (data.get("expense_type") in ("", None, "UNKNOWN") or conf < 0.75) and rule_score >= 1.0:
            data["expense_type"] = rule_exp
            data["account_subject"] = rule_acc
            data["evidence"] = list(set((data.get("evidence") or []) + [f"规则兜底: {', '.join(rule_hits)}"]))
            data["confidence"] = max(conf, min(0.9, 0.6 + rule_score/10.0))
            return data

        # 5) 若 LLM 给出结论，但与规则强冲突（规则≥2.5），则以规则覆盖，避免离谱
        if rule_score >= 2.5 and (data.get("expense_type") != rule_exp):
            data["expense_type"] = rule_exp
            data["account_subject"] = rule_acc
            data["evidence"] = list(set((data.get("evidence") or []) + [f"规则覆盖LLM: {', '.join(rule_hits)}"]))
            data["confidence"] = max(conf, 0.9)

        # 6) 反向补全：若只给了科目，推回费用类别（维持你原逻辑）
        if data.get("expense_type") in (None, "", "UNKNOWN"):
            subj = (data.get("account_subject") or "")
            for k in EXPENSE_TYPES:
                if k in subj:
                    data["expense_type"] = k
                    break
        return data

    # 方法别名，保持向后兼容
    def analyze_invoice(self, invoice_data: Dict[str, Any], user_input: str = "") -> Dict[str, Any]:
        return self.analyze_with_llm(invoice_data, user_input)
        
    def analyze_accounting_subjects(self, invoice_data: Dict[str, Any], expense_type: str, contexts=None) -> Dict[str, Any]:
        return self.generate_accounting_analysis(invoice_data, expense_type, contexts)
    
    def analyze_risk_points(self, invoice_data: Dict[str, Any], user_input: str, contexts=None) -> Dict[str, Any]:
        return self.generate_risk_analysis(invoice_data, contexts)
    
    def analyze_invoice_risk(self, invoice_data: Dict[str, Any], user_input: str, contexts=None) -> Dict[str, Any]:
        ret = self.generate_risk_analysis(invoice_data, contexts)
        ret = self._postfix_basis(ret, contexts)
         # sources_used 只做轻度去重（最终在 processor 里再统一一次）
        ret["sources_used"] = _sources_from_contexts(contexts, ret.get("sources_used"))
        return ret
    
    def analyze_approval_notes(self, invoice_data: Dict[str, Any], user_input: str, contexts=None) -> Dict[str, Any]:
        ret = self.generate_approval_notes(invoice_data, user_input, contexts)
        ret = self._postfix_basis(ret, contexts)
        ret["sources_used"] = _sources_from_contexts(contexts, ret.get("sources_used"))
        return ret

    def _postfix_basis(self, ret, contexts):
        # 没写出 basis 时，至少把命中的文档名+分数列出来，避免前端空白
        basis = ret.get("basis")
        if not basis:
            basis = []
        if isinstance(basis, str):
            basis = [basis] if basis.strip() else []
        if not basis and contexts:
            for h in contexts[:5]:
                t = (h.get("title") or h.get("doc") or "知识库片段").strip()
                sc = float(h.get("score") or 0)
                basis.append(f"命中《{t}》，相似度 {sc:.3f}")
        ret["basis"] = basis
        return ret

    # ---------- 公共工具 ----------
    def _chat(self, system: str, user: str) -> str:
        """最小可用的 OpenAI 兼容 Chat Completions"""
        base = (getattr(self, "base_url", "") or "").strip()
        # ---- 兜底：补协议 & 去掉尾部斜杠 ----
        if base and not base.startswith(("http://", "https://")):
            base = "https://" + base
        base = base.rstrip("/") or "https://api.openai.com/v1"  # 默认走 OpenAI 兼容口

        url = f"{base}/chat/completions"   # ← 统一用补齐后的 base
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload = {
            "model": self.model,
            "temperature": 0.2,
            "max_tokens": 900,
            "response_format": {"type":"json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        with httpx.Client(timeout=60) as client:
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        try:
            return data["choices"][0]["message"]["content"]
        except Exception:
            return json.dumps({"error": "LLM response parse failed", "raw": data})

    def _chat_messages(self, messages: List[Dict[str, str]]) -> str:
        """使用消息列表调用模型"""
        base = (getattr(self, "base_url", "") or "").strip()
        # ---- 兜底：补协议 & 去掉尾部斜杠 ----
        if base and not base.startswith(("http://", "https://")):
            base = "https://" + base
        base = base.rstrip("/") or "https://api.openai.com/v1"  # 默认走 OpenAI 兼容口

        url = f"{base}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload = {
            "model": self.model,
            "temperature": 0.2,
            "messages": messages,
        }
        with httpx.Client(timeout=60) as client:
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        try:
            return data["choices"][0]["message"]["content"]
        except Exception:
            return json.dumps({"error": "LLM response parse failed", "raw": data})

    @staticmethod
    def _safe_json(text: str, fallback: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return json.loads(text)
        except Exception:
            # 有些模型会包一层```json
            t = text.strip().strip("`").strip()
            if t.lower().startswith("json"):
                t = t[4:].strip()
            try:
                return json.loads(t)
            except Exception:
                return fallback

    # ---------- 会计科目 ----------
    def generate_accounting_analysis(
        self,
        invoice_data: Dict[str, Any],
        expense_type: str,
        contexts: Optional[List[Dict[str, str]]] = None
    ) -> Dict[str, Any]:
        ctx = _build_context_block(contexts)
        sys = (
            "你是企业会计与费用合规分析助手。"
            "【硬限制】会计科目必须从如下集合中选择："
            f" {', '.join(ACCOUNT_SUBJECTS)} 或 UNKNOWN；严禁输出集合外的科目名称。\n"
            "【判定优先级（从高到低）】\n"
            " 1) 命中【住宿/酒店/宾馆/房费】→ 科目=6603-差旅费。\n"
            " 2) 命中【网约车/出租车/打车/客运/交通/高铁/机票/地铁/公交/滴滴/高德打车/曹操】→ 科目=6603-差旅费。\n"
            " 3) 仅在命中【办公用品/文具/耗材/复印纸/打印纸/硒鼓/墨盒/印刷/名片】时才可判为6601-办公费。\n"
            " 4) 若仅见\"服务/服务费\"等泛词且无确证 → 返回 UNKNOWN（不得臆测）。\n"
            "【一致性】当费用类型为\"差旅费\"时，严禁输出\"办公费\"等不相干科目。\n"
            "【一致性约束】若 flags.has_lodging 为 true，则不得输出任何与'办公费'相关的科目或措辞。\n"
            "【时间】只允许使用调用方提供的 now_date，不得虚构\"今天/距今X天\"。\n"
            "输出严格 JSON：{"
            '  "account_subject":"…","basis":"…","suggestions":["…"],"sources_used":["文件名"] }'
        )
        user = (
            f"【当前日期】{invoice_data.get('now_date', '（未提供）')}（由调用方传入）\n"
            f"【发票要素】\n{invoice_data}\n\n"
            f"【费用类型猜测】{expense_type}\n\n"
            f"【知识库片段】\n{ctx}\n\n"
            "任务：判断最合适的会计科目（到二级/三级），并给出依据与建议。"
            "输出严格 JSON：{\n"
            '  "account_subject": "…",\n'
            '  "basis": "…（可多段，直接在句中用《文件名》标注）",\n'
            '  "suggestions": ["…","…"],\n'
            '  "sources_used": ["文件名1","文件名2"]\n'
            "}"
        )
        resp = self._chat(sys, user)
        data = self._safe_json(resp, fallback={"account_subject": "", "basis": "", "suggestions": [], "sources_used": []})
        if not data.get("sources_used"):
            data["sources_used"] = [c.get("source") for c in (contexts or []) if c.get("source")]
        return data

    # ---------- 风险点 ----------
    def generate_risk_analysis(
        self,
        invoice_data: Dict[str, Any],
        contexts: Optional[List[Dict[str, str]]] = None,
        flags: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        flags = flags or {}
        ctx = _build_context_block(contexts)
        # —— 明确当前日期：只允许使用调用方注入的 now_date
        now_date = (invoice_data.get("now_date") or "").strip()
        now_line = f"【系统当前日期】{now_date}" if now_date else "【系统当前日期】未知（禁止臆测）"

        sys = (
            "你是一名企业费用合规与发票风控分析助手。"
            "【时间约束】不得推测/编造“今天”，一律用 now_date 与 invoice_date 计算。"
            "【已知事实约束】若 flags.has_lodging 为 true，表示该单据已明确属于'住宿'场景；"
            "在这种情况下严禁输出\"未明确体现住宿/出差相关\"之类否定语，应改为："
            "\"已明确为住宿服务，但缺少××证据/已超期/证据链不完整\"等。"
            "输出只允许 JSON。"
        )
        user = (
            f"{now_line}\n"
            f"【发票要素】\n{invoice_data}\n\n"
            f"【flags】\n{flags}\n\n"
            f"【知识库片段】\n{ctx}\n\n"
            "任务：列出主要风险点、依据、并给出风险等级（低/中/高）。"
            "输出严格 JSON：{\n"
            '  "risk_points": ["…","…"],\n'
            '  "basis": ["…（可多段，直接在句中用《文件名》标注）","…"],\n'
            '  "risk_level": "低|中|高",\n'
            '  "sources_used": ["文件名1","文件名2"]\n'
            "}"
        )
        resp = self._chat(sys, user)
        data = self._safe_json(resp, fallback={"risk_points": [], "basis": [], "risk_level": "中", "sources_used": []})
        if not data.get("sources_used"):
            sources = []
            for c in (contexts or []):
                if isinstance(c, dict) and c.get("source"):
                    sources.append(c.get("source"))
                elif isinstance(c, str):
                    # 如果是字符串，我们将其作为来源添加
                    sources.append("结构化数据")
            data["sources_used"] = sources
        return data

    # ---------- 审批要点 ----------
    def generate_approval_notes(
        self,
        invoice_data=None,
        expense_type: str = "",
        contexts=None,
        flags=None,
        **kwargs,   # ← 关键：吃掉未知关键字，避免 unexpected kw
    ):
        """
        纯 RAG 版审批要点：
        - 只依据 contexts（知识库摘录）生成，禁止臆造；
        - 每条注意事项/建议必须标注来源文件名(或小节)；
        - 严格遵守日期约束：只用 now_date 与票面/验真日期，禁止"今天/距今X天"等猜测；
        - 空或无引用会自动重试一次（仍只用 contexts）；最后仍不合格则给出"未匹配来源"的非空兜底。
        """
        """
        兼容层：同时支持老调用(payload/user_input/extra_ctx)和新调用(invoice_data/contexts/flags)。
        任何缺项一律补默认值，永不抛异常到上层。
        """
        try:
            # 1) 兼容老风格
            if invoice_data is None and "payload" in kwargs:
                payload = kwargs.get("payload") or {}
                invoice_data = payload.get("invoice_info") or {}

                extra_ctx = []
                if "extra_ctx" in kwargs and kwargs["extra_ctx"]:
                    extra_ctx.append({"source": "extra_ctx", "content": json_dump(kwargs["extra_ctx"])})
                if "user_input" in kwargs and kwargs["user_input"]:
                    extra_ctx.append({"source": "user_input", "content": str(kwargs["user_input"])})
                contexts = (contexts or []) + extra_ctx

            # 2) 兜底默认值
            invoice_data = invoice_data or {}
            contexts = list(contexts or [])
            flags = flags or {}

            # 3) 调核心实现（你现有的逻辑/我给你的新版逻辑都塞到这里）
            return self._generate_approval_notes_core(invoice_data, expense_type, contexts, flags)

        except Exception as e:
            # 4) fail-soft：永不抛 500，给出结构化错误
            return {
                "approval_notes": [],
                "basis": "",
                "suggestions": [f"审批要点生成失败：{type(e).__name__}"],
                "sources_used": [],
                "error": f"{type(e).__name__}: {e}"
            }

    def _generate_approval_notes_core(self, invoice_data, expense_type, contexts, flags):
        """
        这里放你"真正的、稳定的"审批要点生成逻辑。
        """
        # 从invoice_data中获取flags，保持向后兼容
        flags = flags or invoice_data.get("flags", {})
        
        # 1) 组织知识库片段（用于展示+引用）
        ctx = _build_context_block(contexts)
        source_titles = []
        for c in (contexts or []):
            if isinstance(c, dict):
                t = c.get("source") or c.get("doc") or c.get("file")
                if t:
                    source_titles.append(str(t))
            elif isinstance(c, str):
                source_titles.append("结构化数据")
        # 去重，限制长度避免 prompt 过大
        source_titles = list(dict.fromkeys(source_titles))[:20]

        # 2) 明确"只能用调用方给的时间"，禁止模型臆测日期
        now_date = (invoice_data.get("now_date") or "").strip()
        now_line = f"【系统当前日期】{now_date}" if now_date else "【系统当前日期】未知（禁止臆测/禁止使用'今天/昨日/距今X天'等表达）"

        # 3) 构造严格 System Prompt（仅用知识库+必须引用+时间约束）
        sys_prompt = (
            "你是费用报销的审核官。你将收到【发票要素】与【知识库摘录】。\n"
            "【硬性要求】\n"
            "1) 只允许依据【知识库摘录】生成'审批注意事项''相关建议''判断依据'，禁止编造未出现的制度条款。\n"
            "2) 每一条 approval_notes / suggestions **末尾**必须用括号标注来源文件名或小节，如：(公司报销制度.md §差旅费)。\n"
            "3) basis 必须为一段话，且**至少包含1处来源文件名**。\n"
            "4) 严禁返回空数组；若确实在摘录中找不到依据，请明确写出'未在知识库找到直接依据'并标注(无匹配来源)。\n"
            "5) 【时间约束】只允许使用调用方传入的 now_date 与发票/验真日期进行描述；禁止出现'今天/昨日/本月/距今X天'等推测性措辞。\n"
            "6) 输出必须是严格 JSON：{"
            ' "approval_notes":[...], "basis":"...", "suggestions":[...], "sources_used":[ "...", ... ] }。\n'
            "7) 仅可引用【知识库摘录】中真实出现过的文件名；禁止虚构来源。\n"
            "8) 若 flags.has_lodging 为 true，表示场景已明确为'住宿'，请避免使用否定语（如'未明确体现住宿'），改为'已明确为住宿，但缺失××证据'的表述。\n"
        )

        # 4) 准备让模型更容易在摘录内"命中"的检索术语（非规则，只是提示）
        q_terms = []
        inv = invoice_data or {}
        for k in ("service_type", "service_type_detail", "remark", "seller_name"):
            v = str(inv.get(k) or "").strip()
            if v:
                q_terms.append(f"{k}:{v}")
        # 验真 goodsData 名称
        try:
            vr = (inv.get("verify_result") or {}).get("data", {}) if isinstance(inv.get("verify_result"), dict) else {}
            for g in (vr.get("goodsData") or []):
                name = str(g.get("name") or "").strip()
                if name:
                    q_terms.append(f"goods:{name}")
        except Exception:
            pass

        # 5) User Prompt：把一切输入与摘录塞给模型（禁止越界）
        user_prompt = (
            f"{now_line}\n"
            f"【费用类型】{expense_type}\n"
            f"【flags】{flags}\n\n"
            f"【发票要素】\n{inv}\n\n"
            f"【关键术语】{', '.join(q_terms)}\n\n"
            "【知识库摘录】\n"
            f"{ctx}\n\n"
            "只依据【知识库摘录】输出严格 JSON。"
        )

        def _call_once(extra_hint: str = "") -> Dict[str, Any]:
            out = self._chat(sys_prompt + extra_hint, user_prompt)
            return self._safe_json(out, fallback={"approval_notes": [], "basis": "", "suggestions": [], "sources_used": []})

        def _looks_good(d: Dict[str, Any]) -> bool:
            an = d.get("approval_notes") or []
            sg = d.get("suggestions") or []
            bs = str(d.get("basis") or "")
            if not (an and sg and bs.strip()):
                return False
            # 至少出现一次来源标注：括号/文件名.md/《文件名》
            cite_hit = False
            corpus = " ".join([*(an or []), *(sg or []), bs])
            if any(x for x in source_titles if x and x in corpus):
                cite_hit = True
            if (".md" in corpus) or ("（" in corpus and "）" in corpus) or ("(" in corpus and ")" in corpus) or ("《" in corpus and "》" in corpus):
                cite_hit = True
            return cite_hit

        # 6) 第一次生成
        res = _call_once()

        # 7) 自动重试（只用知识库、必须引用；提示模型优先在命中词附近找）
        if not _looks_good(res):
            hint = (
                "\n【复核提醒】你上次输出存在'无引用/数组为空'问题。"
                "请仅在【知识库摘录】中检索'差旅/交通/审批阈值/报销时限/证据链/发票要素'等关键词邻近段落，"
                "每条注意事项/建议末尾标注来源文件名(如：公司报销制度.md)。严禁返回空数组，严禁使用未出现过的文件名。"
                "时间描述一律基于 now_date 与票面/验真日期。"
            )
            res = _call_once(hint)

        # 8) 最终兜底：仍不合格 → 明确写"未匹配来源"，但保持非空
        if not _looks_good(res):
            if not res.get("approval_notes"):
                res["approval_notes"] = ["未在知识库摘录中找到可直接适用的条款，请补充制度或材料。(无匹配来源)"]
            if not res.get("suggestions"):
                res["suggestions"] = ["请补充与本票据相关的制度条款或佐证材料后再提交审核。(无匹配来源)"]
            if not res.get("basis"):
                res["basis"] = "当前检索片段不足以支持条款级判断，建议扩充知识库或优化检索（不臆造时间与规则）。"

        # 9) sources_used 为空时，用实际上下文来源填充，便于前端显示
        if not res.get("sources_used"):
            res["sources_used"] = source_titles[:8]

        # --- 兼容 LLM 返回格式：把 approval_notes 统一成列表 ---
        notes = res.get("approval_notes") or []
        if isinstance(notes, str):
            # 支持把一整段换行/前缀符号切成列表
            notes = [s.lstrip("•-·* ").strip() for s in notes.splitlines() if s.strip()]
        res["approval_notes"] = notes

        # --- 统一 sources_used：只保留"标题字符串"，过滤 dict/字典样式字符串 ---
        import ast
        raw_sources = (res.get("sources_used") or []) + [c.get("source") for c in (contexts or []) if isinstance(c, dict) and c.get("source")]

        clean_titles = []
        for x in raw_sources:
            title = None
            if isinstance(x, dict):
                title = x.get("title") or x.get("source") or x.get("doc") or x.get("name")
            elif isinstance(x, str):
                xs = x.strip()
                # 尝试把 "{'title': '...', 'url': ''}" 解析回 dict
                if xs.startswith("{") and xs.endswith("}"):
                    try:
                        obj = ast.literal_eval(xs)
                        if isinstance(obj, dict):
                            title = obj.get("title") or obj.get("source") or obj.get("doc") or obj.get("name")
                    except Exception:
                        pass
                if title is None:
                    title = xs
            if title:
                # 去掉路径/扩展名
                base = title.rsplit("/", 1)[-1]
                base = base.rsplit("\\", 1)[-1]
                if "." in base:
                    base = base.rsplit(".", 1)[0]
                clean_titles.append(base)

        # 去重
        seen = set()
        res["sources_used"] = [t for t in clean_titles if not (t in seen or seen.add(t))]

        return res

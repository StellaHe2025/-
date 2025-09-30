# knowledge_retriever.py — 结构化规则增强版
# -*- coding: utf-8 -*-
import os
import re
import json
import csv as csv_module
from typing import Dict, List, Any, Tuple, Optional
from urllib.parse import quote
import logging


# 可选：保留你之前的 TF-IDF / RAGFlow 混合检索能力
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity


import numpy as np

# 放在文件顶部 import 区域附近
import os
from urllib.parse import quote

def _get_public_kb_base(cfg: dict | None = None) -> str:
    """环境变量优先，其次 config；去掉尾斜杠"""
    base = (os.getenv("PUBLIC_KB_BASE") or (cfg or {}).get("public_kb_base") or "").strip()
    return base.rstrip("/")

def _mk_kb_url(public_base: str, rel_path: str) -> str:
    """把相对文件名转成可点 URL；中文要 quote"""
    if not public_base:
        return ""
    # 你暴露的是 /kb/ 下的文件名；如果有子目录，保持相对路径
    return f"{public_base}/{quote(rel_path)}"

logger = logging.getLogger("knowledge_retriever")
logger.setLevel(logging.INFO)

class KnowledgeRetriever:
    """
    本地优先的知识检索器 + 结构化规则解析：
    - 读取 knowledge_base 目录下的 *.txt/*.md 文件
    - 构建 TF-IDF 索引供语义召回
    - 解析《公司报销规则.txt》《公司报销制度.md》《approval_process.txt》《verification_points.txt》
      形成结构化 policy/阈值/注意事项
    - 从《发票关键词-会计科目map表.txt》加载关键词->科目 的加权映射，提供得分接口
    - 仍保留旧接口：get_accounting_rules / get_approval_process / get_verification_points
    """
    def __init__(self, ragflow_api_url: str = None, api_key: str = None, kb_id: str = None,
                 local_knowledge_base_path: str = None):
        # 远端（可选）
        self.api_url = ragflow_api_url or ""
        self.api_key = api_key or ""
        self.kb_id = kb_id or ""
        self.headers = {'Authorization': f'Bearer {self.api_key}'} if self.api_key else {}

        # 本地库
        self.base = os.path.abspath(local_knowledge_base_path or "./knowledge_base")
        self.docs: Dict[str, str] = {}
        self.filenames: List[str] = []
        self._load_local_corpus()

        # TF-IDF 索引
        self.vectorizer = TfidfVectorizer(max_features=5000)
        self.doc_vectors = None
        if self.docs:
            self._build_tfidf_index()

        # 结构化规则
        self.policies: List[Dict[str, Any]] = []             # 通用 policy 列表
        self.approval_thresholds: Dict[str, List[Dict]] = {}  # 各费用类别的金额审批阈值
        self.verification_window_days: Optional[int] = None   # 验真"有效期"指导（如90）
        self.keyword_map: List[Dict[str, Any]] = []           # 关键词->科目 的权重表

        self._extract_policies_from_rules_table()             # 公司报销规则.txt
        self._extract_policies_from_system_doc()              # 公司报销制度.md
        self._extract_thresholds_from_approval()              # approval_process.txt
        self._extract_verify_window()                         # verification_points.txt
        self._load_keyword_map()                              # 发票关键词-会计科目map表.txt

    # ----------------------------------------------------------------------
    # 本地索引
    # ----------------------------------------------------------------------
    def _load_local_corpus(self):
        if not os.path.isdir(self.base):
            logger.warning("知识库路径不存在：%s", self.base)
            return
        for fn in os.listdir(self.base):
            if not any(fn.endswith(ext) for ext in (".txt", ".md")):
                continue
            p = os.path.join(self.base, fn)
            try:
                with open(p, "r", encoding="utf-8", errors="ignore") as f:
                    txt = f.read()
            except Exception as e:
                logger.exception("读取失败 %s: %s", p, e)
                continue
            self.docs[fn] = txt
            self.filenames.append(fn)
            logger.info("正在处理文件: %s", fn)
            logger.info("成功读取文件 %s，内容长度: %s", fn, len(txt))
        logger.info("成功构建索引，共 %d 个文档", len(self.docs))

    def _build_tfidf_index(self):
        corpus = [self.docs[fn] for fn in self.filenames]
        self.doc_vectors = self.vectorizer.fit_transform(corpus)
    
    def _path_to_url(self, abs_path: str) -> Optional[str]:
        pub = self._public_kb_base()
        if not pub:
            return None
        try:
            rel = os.path.relpath(abs_path, self.base).replace(os.sep, "/")
            rel = quote(rel, safe="/")  # 关键：对中文名做 URL 编码
            return f"{pub}/{rel}"
        except Exception:
            return None
            
    def _public_url_base(self) -> str:
        return (os.getenv("PUBLIC_KB_BASE", "").rstrip("/") or "")
    
    def _to_url(self, filename: str) -> str:
        base = self._public_url_base()
        if not base:  # 没设置就不返回
            return ""
        # knowledge_base 下的文件名（包含中文）需要编码
        return f"{base}/{quote(filename)}"

    # ----------------------------------------------------------------------
    # 语义检索
    # ----------------------------------------------------------------------
    def search_policy_documents(self, query: str, top_k: int = 3) -> List[Dict[str, Any]]:
        """
        混合策略：先本地 TF-IDF，再（可选）请求 RAGFlow（如果你真有 kb_id）
        返回 list[{"source": {"title": str, "url": str}, "content": 片段, "score": float}]
        """
        results = self._search_local_knowledge_base(query, top_k=top_k)
        # 可选补充：RAGFlow（此处仅占位，若你要启用，自己替换为真实 API）
        # rag_results = self._search_ragflow(query, top_k=top_k)
        # results.extend(rag_results)
        # 去重 & 排序
        uniq = {}
        for r in results:
            # 使用 content 的前 50 个字符作为唯一标识
            k = (r.get("content", "")[:50] if r.get("content") else "")
            if k not in uniq:
                uniq[k] = r
        results = sorted(uniq.values(), key=lambda x: x["score"], reverse=True)[:top_k]
        return results

    def _search_local_knowledge_base(self, query: str, top_k: int = 3) -> List[Dict[str, Any]]:
        if self.doc_vectors is None:
            return []
        q_vec = self.vectorizer.transform([query])
        sims = cosine_similarity(q_vec, self.doc_vectors)[0]
        idxs = np.argsort(-sims)[:max(top_k, 3)]
        results = []
        for i in idxs:
            fn = self.filenames[i]
            score = float(sims[i])
            snippet = self._best_snippet(self.docs[fn], query)
            # 假设命中的文件路径是 abs_path（如 /srv/streamlit-app/knowledge_base/发票管理办法.md）
            # 显示给前端的标题只用文件名
            rel_name = os.path.basename(fn)
            public_base = _get_public_kb_base(getattr(self, "cfg", {}))  # 传入配置以便 fallback
            source_item = {
                "title": os.path.splitext(rel_name)[0],  # 去掉后缀
                "url": _mk_kb_url(public_base, rel_name)
            }
            results.append({"doc": fn, "content": snippet, "score": score, "source": source_item})
        if results:
            logger.info("本地检索命中 TopK：")
            for r in results[:top_k]:
                logger.info(" - %s | score=%.4f", r["doc"], r["score"])
        return results[:top_k]

    def _best_snippet(self, txt: str, query: str, span: int = 240) -> str:
        q = query.strip().lower()
        txt_l = txt.lower()
        pos = txt_l.find(q.split()[0]) if q else -1
        if pos < 0:
            return txt[:span].replace("\n", " ")
        left = max(0, pos - span // 2)
        right = min(len(txt), pos + span // 2)
        return txt[left:right].replace("\n", " ")

    # ----------------------------------------------------------------------
    # 结构化规则抽取
    # ----------------------------------------------------------------------
    def _extract_policies_from_rules_table(self):
        """
        解析《公司报销规则.txt》：形如
        rule_key\tcategory\tparam\tvalue\tdesc
        invoice_date\tcompliance\tmax_days_before_today\t180\t...
        entertainment_tax\tspecial\tno_deduction\tenabled\t...
        """
        fn = "公司报销规则.txt"
        if fn not in self.docs:
            return
        lines = [ln for ln in self.docs[fn].splitlines() if ln.strip()]
        header_seen = False
        for ln in lines:
            if ln.strip().startswith("#") or ln.strip().startswith("..."):
                continue
            parts = re.split(r"\s+", ln.strip(), maxsplit=4)
            if not header_seen:
                # 跳过表头
                if parts[:5] == ["rule_key", "category", "param", "value", "desc"]:
                    header_seen = True
                continue
            if len(parts) < 5:
                continue
            rule_key, category, param, value, desc = parts[:5]
            self.policies.append({
                "source": fn, "rule_key": rule_key, "category": category,
                "param": param, "value": value, "desc": desc
            })

    def _extract_policies_from_system_doc(self):
        """
        从《公司报销制度.md》中提炼关键字眼，特别是"6个月/180天内报销"等。
        """
        fn = "公司报销制度.md"
        if fn not in self.docs:
            return
        txt = self.docs[fn]
        # 报销周期 6个月 / 180天
        if re.search(r"6个月（?180天）?内|6个月内|180\s*天内", txt):
            self.policies.append({
                "source": fn, "rule_key": "period_limit_policy",
                "category": "policy", "param": "max_days", "value": "180",
                "desc": "费用发生后6个月（180天）内报销"
            })

    def _extract_thresholds_from_approval(self):
        """
        解析《approval_process.txt》：不同费用类别的金额审批阈值
        例如：差旅费 审批流程 金额在1000-5000元：部门经理初审，分管副总审批
        """
        fn = "approval_process.txt"
        if fn not in self.docs:
            return
        txt = self.docs[fn]
        blocks = re.split(r"\n\s*\d\.\s*", txt)  # 切分小节
        cat_map = {
            "差旅费": "travel",
            "办公费": "office",
            "业务招待费": "entertain",
            "培训费": "training",
        }
        for k, key in cat_map.items():
            pattern = rf"{k}审批流程：([\s\S]*?)(?:\n\d\.\s|\Z)"
            m = re.search(pattern, txt)
            if not m: 
                continue
            seg = m.group(1)
            ths = []
            for ln in seg.splitlines():
                ln = ln.strip()
                # 金额在1000-5000元：部门经理初审，分管副总审批
                m2 = re.search(r"金额在(\d+)\s*-\s*(\d+)元：(.+)", ln)
                m3 = re.search(r"金额在(\d+)元以下：(.+)", ln)
                m4 = re.search(r"金额在(\d+)元以上：(.+)", ln)
                if m2:
                    ths.append({"min": int(m2.group(1)), "max": int(m2.group(2)), "approvers": m2.group(3)})
                elif m3:
                    ths.append({"min": 0, "max": int(m3.group(1)), "approvers": m3.group(2)})
                elif m4:
                    ths.append({"min": int(m4.group(1)), "max": None, "approvers": m4.group(2)})
            if ths:
                self.approval_thresholds[key] = ths

        # 额外规则：超过3个月原则上不予报销（提示）
        if re.search(r"超过3个月.*不予报销", txt):
            self.policies.append({
                "source": fn, "rule_key": "over_3m_hint",
                "category": "policy", "param": "warn_days", "value": "90",
                "desc": "超过3个月原则上不予报销，需特批"
            })

    def _extract_verify_window(self):
        """
        从《verification_points.txt》提炼"有效期（一般3个月=90天）"的验真指导
        """
        fn = "verification_points.txt"
        if fn not in self.docs:
            return
        txt = self.docs[fn]
        m = re.search(r"有效期.*（?一般为.*?(\d+)\s*个月.*）", txt)
        if m:
            self.verification_window_days = int(m.group(1)) * 30
        elif re.search(r"(\d+)\s*天\s*内.*有效", txt):
            self.verification_window_days = int(re.search(r"(\d+)\s*天", txt).group(1))

    def _load_keyword_map(self):
        """
        读《发票关键词-会计科目map表.txt》，tab/空白分隔：
        keyword account weight note
        """
        fn = "发票关键词-会计科目map表.txt"
        if fn not in self.docs:
            return
        rows = []
        for i, ln in enumerate(self.docs[fn].splitlines()):
            if not ln.strip() or ln.strip().startswith("#"):
                continue
            if i == 0 and "keyword" in ln and "account" in ln:
                continue
            parts = re.split(r"\s+", ln.strip(), maxsplit=3)
            if len(parts) >= 3:
                kw, account, weight = parts[:3]
                note = parts[3] if len(parts) == 4 else ""
                try:
                    w = float(weight)
                except:
                    w = 0.5
                rows.append({"keyword": kw, "account": account, "weight": w, "note": note})
        self.keyword_map = rows

    # ----------------------------------------------------------------------
    # 对外接口
    # ----------------------------------------------------------------------
    def get_accounting_rules(self, expense_type_hint: str = "") -> Dict[str, Any]:
        """
        返回与会计科目判定相关的"文本 + 结构化规则 + 关键词得分候选"
        """
        texts = []
        for name in ("会计科目口径手册_rag版.md", "accounting_rules.txt", "公司报销规则.txt"):
            if name in self.docs:
                texts.append(self.docs[name])
        structured = [p for p in self.policies if p["category"] in ("business","special","compliance")]
        return {"texts": texts, "structured_policies": structured, "keyword_map_head": self.keyword_map[:12]}

    def get_approval_process(self, invoice_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        根据金额 + 费用类型提示，返回需要的审批人层级
        返回格式：
        {
            "category": "费用类别",
            "rules": [{"min": 0, "max": 1000, "approvers": "..."}, ...],
            "matched": {"min": 500, "max": 1000, "approvers": "..."}
        }
        """
        amt = 0.0
        try:
            amt = float(invoice_data.get("amount_in_figures") or invoice_data.get("total_amount") or 0)
        except:
            pass

        # 费用类型猜测
        hint = (invoice_data.get("service_type") or invoice_data.get("remark") or "").lower()
        cat = "travel" if ("住" in hint or "差旅" in hint or "酒店" in hint) else \
              "entertain" if ("宴请" in hint or "招待" in hint or "餐饮" in hint) else \
              "office"
        rules = self.approval_thresholds.get(cat, [])
        matched = None
        
        # 查找匹配的审批规则
        for rule in rules:
            if rule["max"] is None and amt >= rule["min"]:
                matched = rule
                break
            if rule["max"] is not None and (amt >= rule["min"] and amt <= rule["max"]):
                matched = rule
                break

        return {
            "category": cat,
            "rules": rules,
            "matched": matched
        }

    def get_verification_points(self, invoice_data: Dict[str, Any]) -> List[str]:
        """
        返回验真要点（文本片段 + 有效期天数提示）
        """
        pts = []
        if "发票验真要点_rag版.md" in self.docs:
            pts.append(self.docs["发票验真要点_rag版.md"])
        if "verification_points.txt" in self.docs:
            pts.append(self.docs["verification_points.txt"])
        if self.verification_window_days:
            pts.append(f"【结构化规则】发票有效期（验真指导）约 {self.verification_window_days} 天。")
        return pts

    def score_accounts(self, text: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """
        使用《发票关键词-会计科目map表》做加权打分。
        返回：[{account, score, matched: [kw...]}]
        """
        text_l = (text or "").lower()
        scores: Dict[str, float] = {}
        matched: Dict[str, List[str]] = {}
        for row in self.keyword_map:
            kw = row["keyword"].lower()
            if kw and kw in text_l:
                acc = row["account"]
                w = float(row["weight"])
                scores[acc] = scores.get(acc, 0.0) + w
                matched.setdefault(acc, []).append(kw)
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]
        return [{"account": acc, "score": sc, "matched": matched.get(acc, [])} for acc, sc in ranked]

    # 兼容旧处理：从 doc 字典中找 content
    def _extract_content(self, doc: Dict) -> str:
        if not isinstance(doc, dict):
            return str(doc) if doc else ""
        for k in ("content","text","document","body","answer","response"):
            v = doc.get(k)
            if isinstance(v, str) and v.strip():
                return v
        return ""
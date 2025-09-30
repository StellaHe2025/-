# 发票报销智能助手
用户上传增值税发票后，网页自动提取发票信息、进行发票验真、提示发票风险点、分析会计科目费用类型、匹配公司报销制度提示报销要点，并可以一键下载报告。

> 用户上传**增值税发票**与佐证材料，系统自动：**OCR 提取要素 → 发票验真 → 风险点提示 → 会计科目/费用类型分析 → 对照公司报销制度给出要点**，并支持**一键下载报告**。

## ✨ 功能特性

- **多票据上传**：支持一次上传多张发票与相关佐证材料（如行程单、差旅单）。
- **OCR 提取**：优先调用**百度增值税发票 OCR**，可配置离线/备用 OCR（例如 Tesseract）。
- **发票验真**：对票面信息进行结构化校验与（可配置的）验真流程。
- **知识库对照**：从本地知识库（Markdown/HTML/TXT）检索相关规则与口径，辅助 LLM 做合规判断与提示。
- **费用类型与会计科目建议**：根据发票项目与上下文生成建议，尽可能贴合公司口径。
- **风险扫描**：如**超期报销**、**金额不一致**、**缺佐证**、**重复报销**（可配合历史记录）等。
- **结果可视化**：前端页面展示关键要素、风险点与建议；支持导出 CSV/Excel（如已实现）。
- **轻量后端**：基于 **FastAPI** 提供 HTTP API；本地开发与云端部署都很顺手。

---

## 🏗️ 架构总览

```

浏览器前端 (index.html + script.js)
│  上传文件 / 查看结果
▼
FastAPI 网关 (api_app.py)
│  /api/invoices
▼
业务引擎 (app.py / reimbursement_processor.py)
├─ OCR 适配器 (baidu_vat_client.py / invoice_extractor.py)
├─ 发票验真 (invoice_verifier.py)
├─ 规则检索 (knowledge_retriever.py)  ← 本地知识库
└─ 费用/科目分析 (expense_analyzer.py) ← 可调用 LLM

```

---

## 📁 目录结构（核心）

```

.
├─ api_app.py                  # FastAPI 入口与路由（/api/invoices 等）
├─ app.py                      # 应用装配/统一编排（agent/管线）
├─ baidu_vat_client.py         # 百度增值税发票 OCR 客户端
├─ invoice_extractor.py        # OCR 抽取 orchestrator（含兜底与清洗）
├─ invoice_verifier.py         # 验真与规则级校验
├─ expense_analyzer.py         # 费用类型/会计科目分析（可调用 LLM）
├─ knowledge_retriever.py      # 本地知识库加载与检索
├─ reimbursement_processor.py   # “发票+佐证”整合判断与风控逻辑
├─ index.html                  # 前端页面
├─ script.js                   # 前端交互逻辑（上传/渲染/下载）
├─ requirements.txt            # Python 依赖
└─ knowledge_base/             # ← 你自建的知识库目录（建议）
├─ 会计科目口径手册_rag版.md
├─ 发票验真要点_rag版.md
├─ 公司报销制度.md
├─ 发票关键词-会计科目map表.txt
└─ 结构化规则-审批阈值.txt

````

---

## 🚀 本地快速开始

### 1) 环境准备

```bash
git clone https://github.com/StellaHe2025/Chinese_Invoice_Reimbursement_Assistant.git
cd Chinese_Invoice_Reimbursement_Assistant
python -m venv .venv
source .venv/bin/activate   # Windows 用 .venv\\Scripts\\activate
pip install -r requirements.txt
````

### 2) 配置环境变量

在根目录创建 `.env` 文件：

```env
BAIDU_OCR_ACCESS_TOKEN=你的token
KB_DIR=/absolute/path/to/knowledge_base
DASHSCOPE_API_KEY=sk-xxxx
HOST=0.0.0.0
PORT=8000
```

### 3) 启动服务

```bash
uvicorn api_app:app --host 0.0.0.0 --port 8000 --reload
```

### 4) 打开前端

直接用浏览器打开 `index.html`，或起本地静态服务器：

```bash
python -m http.server 5173
```

确认 `script.js` 中的 `API_BASE` 指向后端地址。

---

## 🔌 API 说明

### POST `/api/invoices`

上传发票与佐证文件，返回结构化结果。

```json
{
  "items": [
    {
      "filename": "发票A.pdf",
      "invoice_info": {
        "invoice_code": "011001900111",
        "invoice_number": "234120000",
        "invoice_date": "2024-03-18",
        "seller_name": "郑州双象酒店管理有限公司",
        "buyer_name": "某某公司",
        "amount_excl_tax": 802.83,
        "total_tax": 48.17,
        "amount_in_figures": "851.00",
        "tax_rate": "6%",
        "invoice_type": "电子发票(专用发票)"
      },
      "verify_result": { "is_valid": true, "verify_message": "验真成功" },
      "expense_analysis": {
        "expense_type": "差旅费/住宿",
        "account_subject": "6603-差旅费"
      },
      "risks": [
        { "level": "high", "code": "OVERDUE", "message": "发票超过6个月" }
      ]
    }
  ]
}
```

---

## 🧠 知识库与检索

* 文档放在 `knowledge_base/`。
* `knowledge_retriever.py` 会加载文本并做相似度检索，Top-K 片段作为证据输入分析。
* 建议文档分节清晰，检索效果更佳。

---

## 🌐 部署建议

* 使用 **systemd** 管理 uvicorn 服务。
* 用 **Nginx** 做反向代理并配置 HTTPS。
* 前端静态文件可直接由 Nginx 托管。

---

## 🗺️ Roadmap

* [ ] Q&A 机器人（报销制度助手）
* [ ] 支持出租车发票等更多票种
* [ ] 用户登录与历史记录去重
* [ ] 超期/风险规则固化为策略引擎
* [ ] 交互式 OCR 纠错

---

## 🙌 致谢

* 阿里云（通义灵码 Qwen-3-Coder——coder agent、通义千问LLM大模型——Qwen plus/flash、ECS云服务器、域名购买）
* 百度 OCR发票识别
* 猪八戒 发票验真
* AI军师 ChatGPT-5、Gemini 2.5 pro、Claude sonnet 4
* 初版后端代码 cursor
* 前端代码 bolt.new
* FastAPI / Uvicorn
* 知识库文档撰写 DeepSeek



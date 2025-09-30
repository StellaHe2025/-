// ===== 基础 DOM =====
const fileInput       = document.getElementById('fileInput');
const dropZone        = document.getElementById('dropZone');
const fileList        = document.getElementById('fileList');
const selectFileBtn   = document.getElementById('selectFileBtn');
const clearFilesBtn   = document.getElementById('clearFilesBtn');
const startAnalysisBtn= document.getElementById('startAnalysisBtn');
const noteTextarea    = document.getElementById('noteTextarea');

const initialEl = document.getElementById('initialContent');
const loadingEl = document.getElementById('loadingSpinner');
const resultsEl = document.getElementById('resultsContainer');

// 添加调试信息
console.log('DOM Elements:', {
  fileInput,
  dropZone,
  fileList,
  selectFileBtn,
  clearFilesBtn,
  startAnalysisBtn,
  noteTextarea,
  initialEl,
  loadingEl,
  resultsEl
});

const API_BASE = '/api';
let selectedFiles = [];
let isUploading = false;

/* ---------- 工具（一次即可） ---------- */
// ==== DOM 安全工具 ====
const $ = (id) => document.getElementById(id);
const setHTML = (id, html) => { const el = $(id); if (el) el.innerHTML = html; };
const setText = (id, text) => { const el = $(id); if (el) el.textContent = text; };
const show = (id) => { const el = $(id); if (el) el.style.display = ""; };
const hide = (id) => { const el = $(id); if (el) el.style.display = "none"; };

// 1) 在工具函数区，加上这两个
const showByClass = (el) => el && el.classList.remove('hidden');
const hideByClass = (el) => el && el.classList.add('hidden');

// ---- DOM 安全助手 ----
const safeSetHTML = (id, html) => { const el = $(id); if (el) el.innerHTML = html; };
const safeSetText = (id, text) => { const el = $(id); if (el) el.textContent = (text ?? "—"); };

// 统一用的 DOM id（把这里改成你页面里真实存在的 id！）
const DOM = {
  amountWithTax: "invoice_incl",     // 含税金额
  amountExclTax: "invoice_excl",     // 不含税金额
  taxAmount:     "invoice_tax",      // 税额
  expenseType:   "expense_type",     // 费用类型在HTML中没有直接显示元素，保留在这里以备后用
  subject:       "account_subject",  // 会计科目在HTML中没有直接显示元素，保留在这里以备后用

  // 会计科目引用来源
  accSources:    "accounting_sources",

  // 风险
  riskPoints:    "risk_points",      // 风险点在HTML中没有直接显示元素，保留在这里以备后用
  riskBasis:     "risk_basis",       // 风险判断依据在HTML中没有直接显示元素，保留在这里以备后用
  riskSources:   "risk_sources",

  // 审批
  apprNotes:     "approval_notes",   // 审批注意事项在HTML中没有直接显示元素，保留在这里以备后用
  apprBasis:     "approval_basis",   // 审批判断依据在HTML中没有直接显示元素，保留在这里以备后用
  apprSug:       "approval_suggestions", // 审批建议在HTML中没有直接显示元素，保留在这里以备后用
  apprSources:   "approval_sources",
};

// 渲染小工具
const money = (v) => (v === 0 || v) ? Number(v).toFixed(2) : "—";
const renderList = (items, empty = "暂无") => {
  const a = Array.isArray(items) ? items.filter(Boolean) : [];
  if (!a.length) return `<span class="text-gray-400 text-sm">${empty}</span>`;
  return `<ul class="space-y-1">${a.map(x => `<li>• ${x}</li>`).join("")}</ul>`;
};
const toTitles = (list) => (list || []).map(s => {
  if (typeof s === "string") return s;
  if (s && typeof s === "object") return s.title || s.name || s.source || "";
  return "";
}).filter(Boolean);

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
const toArr = (v) => Array.isArray(v) ? v.filter(x => x != null && x !== '') : (v ? [v] : []);
const fmtMoney = (v) => (v == null || v === '') ? '—' : `¥${(+v).toFixed(2)}`;
const fmtPct = (v) => {
  if (v == null || v === '') return '—';
  const n = (typeof v === 'string' && v.includes('%')) ? parseFloat(v) : Number(v) * 100;
  return Number.isFinite(n) ? `${Math.round(n * 100) / 100}%` : String(v);
};
const li = (t, color = 'text-gray-500') =>
  `<li class="flex items-start text-xs text-gray-600">
     <i class="fas fa-circle ${color} text-[6px] mt-1 mr-2"></i><span>${esc(t)}</span>
   </li>`;

// —— 3.1 放在 script.js 顶部或工具函数区 —— //
function explainOcrError(errStr, rawLogId) {
  const res = { code: null, msg: errStr || "未知错误", logId: rawLogId || null, ui: "" };
  const s = String(errStr || "").toLowerCase();

  // ★ 纯文案也映射（例如 "Open api qps request limit reached"）
  if (!s.includes(":")) {
    if (s.startsWith("open api qps")) res.code = "18";
   } 
  if (typeof errStr === "string" && errStr.includes(":")) {
    const i = errStr.indexOf(":");
    res.code = errStr.slice(0, i).trim();
    res.msg  = errStr.slice(i + 1).trim();
  }

  const map = {
    "216201": "文件/图片格式错误（常见于 base64 被二次 url 编码，或文件损坏）",
    "17":     "日调用额度达到上限（或该接口权限/计费未生效）",
    "18":     "QPS 超限（并发/频率过快）"
  };
  const tip = map[res.code] || res.msg || "OCR 调用失败";
  res.ui = `OCR失败（code=${res.code ?? "?"}）：${tip}` + (res.logId ? `；log_id=${res.logId}` : "");
  return res;
}

// 一个简易 toast（你项目里如果已有全局 toast，用你自己的）
function toastError(text) {
  console.error(text);
  alert(text); // 先用最简单的；以后可换成你现有的 UI 组件
}

// 动态按需加载 SheetJS（只加载一次）
let __xlsxReady = null;
function ensureXlsxLib() {
  if (__xlsxReady) return __xlsxReady;
  __xlsxReady = new Promise((resolve, reject) => {
    if (window.XLSX) return resolve();
    const s = document.createElement('script');
    s.src = 'https://cdn.jsdelivr.net/npm/xlsx@0.18.5/dist/xlsx.full.min.js';
    s.onload = () => resolve();
    s.onerror = () => reject(new Error('SheetJS 加载失败'));
    document.head.appendChild(s);
  });
  return __xlsxReady;
}

/* ---------- 统一规范化（字段兜底映射） ---------- */
function normalizePayload(raw) {
  const r = raw || {};

  // 分组别名兜底
  const invoice    = r.invoice_info        || r.invoice        || {};
  const accounting = r.accounting_analysis || r.analysis       || {};
  const risk       = r.risk_analysis       || r.risk           || {};
  const approval   = r.approval_analysis   || r.approval       || {};
  const verification = r.verification      || r.check          || {};

  // 字段别名兜底（→ 规范字段）
  return {
    invoice: {
      type:          invoice.invoice_type   ?? invoice.type,
      date:          invoice.invoice_date   ?? invoice.date,
      number:        invoice.invoice_number ?? invoice.number,
      tax_rate:      invoice.tax_rate       ?? invoice.rate,
      total_amount:  invoice.total_amount   ?? invoice.total ?? invoice.amount_total,
      // 添加税额和含税金额字段
      total_tax:     invoice.total_tax      ?? invoice.tax_amount,
      amount_in_figures: invoice.amount_in_figures ?? invoice.amount_with_tax,
      service_type:  invoice.service_type   ?? invoice.item  ?? invoice.project,
      seller_name:   invoice.seller_name    ?? invoice.seller,
      buyer_name:    invoice.buyer_name     ?? invoice.buyer,
    },
    accounting: {
      expense_type:   r.expense_type                 ?? accounting.expense_type ?? accounting.type,
      account_subject:accounting.account_subject     ?? accounting.subject,
      scope:          accounting.scope,
      basis:          accounting.basis               ?? accounting.rules ?? accounting.match_basis,
      sources:        accounting.sources             ?? accounting.references,
      advice:         accounting.advice              ?? accounting.suggestions,
    },
    risk: {
      risk_level:     risk.risk_level,
      risk_score:     risk.risk_score,
      basis:          risk.basis                    ?? risk.criteria  ?? risk.judgement,
      sources:        risk.sources                  ?? risk.references,
      points:         risk.risk_points              ?? risk.points,
    },
    approval: {
      // 映射后端的 approval_notes
      focus_points:   approval.focus_points         ?? approval.approval_notes ?? approval.checklist,
      suggestions:    approval.suggestions          ?? approval.tips,
      basis:          approval.basis                ?? approval.criteria,
      sources:        approval.sources              ?? approval.references ?? approval.sources_used,
    },
    verification // 目前 UI 不用，但先保留
  };
}

/* ---------- 结果容器 HTML（严格按 bolt new 结果结构） ---------- */
function buildResultsHTML(vm) {
  const inv  = vm.invoice;
  const acc  = vm.accounting;
  const risk = vm.risk;
  const appr = vm.approval;

  // 风险徽标外观
  const score = Number(risk.risk_score);
  const level = risk.risk_level || (Number.isFinite(score) ? (score < 0.34 ? '风险较低' : score < 0.67 ? '风险中等' : '风险较高') : '风险未知');
  const badgeBg = level.includes('低') ? 'bg-green-50 border-green-200' : level.includes('高') ? 'bg-red-50 border-red-200' : 'bg-yellow-50 border-yellow-200';
  const badgeIc = level.includes('低') ? 'text-green-500' : level.includes('高') ? 'text-red-500' : 'text-yellow-500';
  const badgeTx = level.includes('低') ? 'text-green-800' : level.includes('高') ? 'text-red-800' : 'text-yellow-800';

  // ——发票金额字段——
  const excl = Number(inv.total_amount ?? 0);
  const tax  = Number(inv.total_tax ?? 0);
  const incl = inv.amount_in_figures != null
    ? Number(inv.amount_in_figures)
    : (isFinite(excl + tax) ? +(excl + tax).toFixed(2) : null);
  
  const formatMoney = (v) => (v==null||!isFinite(v)||v==='') ? '—' : `¥${(+v).toFixed(2)}`;

  return `
  <div class="space-y-6 max-w-4xl mx-auto">

    <!-- 发票信息 -->
    <section class="bg-white rounded-xl shadow-sm border border-gray-200">
      <div class="p-6">
        <h3 class="text-lg font-semibold text-gray-900 mb-4 flex items-center">
          <i class="fas fa-info-circle text-blue-500 mr-2"></i> 发票信息
        </h3>
        <div class="space-y-4">
          <div class="grid grid-cols-2 gap-4">
            <div><label class="text-sm text-gray-500">发票类型</label><p class="text-sm mt-1">${esc(inv.type)||'—'}</p></div>
            <div><label class="text-sm text-gray-500">开票日期</label><p class="text-sm mt-1">${esc(inv.date)||'—'}</p></div>
          </div>
          <div class="grid grid-cols-2 gap-4">
            <div><label class="text-sm text-gray-500">发票号码</label><p class="text-sm mt-1">${esc(inv.number)||'—'}</p></div>
            <div><label class="text-sm text-gray-500">税率</label><p class="text-sm mt-1">${fmtPct(inv.tax_rate)}</p></div>
          </div>
          <div class="grid grid-cols-2 gap-4">
            <div><label class="text-sm text-gray-500">不含税金额</label><p class="text-sm mt-1" id="invoice_excl">${formatMoney(excl)}</p></div>
            <div><label class="text-sm text-gray-500">税额</label><p class="text-sm mt-1" id="invoice_tax">${formatMoney(tax)}</p></div>
          </div>
          <div class="grid grid-cols-2 gap-4">
            <div><label class="text-sm text-gray-500">价税合计/含税金额</label><p class="text-sm text-blue-600 font-semibold mt-1" id="invoice_incl">${formatMoney(incl)}</p></div>
            <div><label class="text-sm text-gray-500">发票项目</label><p class="text-sm mt-1">${esc(inv.service_type)||'—'}</p></div>
          </div>
          <div id="invoice_tips"></div>
          <div class="grid grid-cols-2 gap-4">
            <div><label class="text-sm text-gray-500">开票单位</label><p class="text-sm mt-1">${esc(inv.seller_name)||'—'}</p></div>
            <div><label class="text-sm text-gray-500">购买单位</label><p class="text-sm mt-1">${esc(inv.buyer_name)||'—'}</p></div>
          </div>
        </div>
      </div>
    </section>

    <!-- 费用分析 -->
    <section class="bg-white rounded-xl shadow-sm border border-gray-200">
      <div class="p-6">
        <h3 class="text-lg font-semibold text-gray-900 mb-4 flex items-center">
          <i class="fas fa-chart-pie text-green-500 mr-2"></i> 费用分析
        </h3>
        <div class="space-y-4">
          <div class="flex items-center justify-between p-3 bg-blue-50 rounded-lg">
            <span class="text-sm text-gray-700">费用类型</span>
            <span class="text-sm text-blue-600 font-semibold">${esc(acc.expense_type)||'—'}</span>
          </div>
          <div>
            <h4 class="text-sm font-medium text-gray-700 mb-1">适用范围</h4>
            <p class="text-xs text-gray-600">${esc(acc.scope || (Array.isArray(acc.basis)?acc.basis.join('；'):acc.basis) || '') || '—'}</p>
          </div>
        </div>
      </div>
    </section>

    <!-- 会计科目 -->
    <section class="bg-white rounded-xl shadow-sm border border-gray-200">
      <div class="p-6">
        <h3 class="text-lg font-semibold text-gray-900 mb-4 flex items-center">
          <i class="fas fa-calculator text-purple-500 mr-2"></i> 会计科目
        </h3>
        <div class="space-y-4">
          <div class="p-4 bg-purple-50 border border-purple-200 rounded-lg">
            <div class="flex items-center justify-between mb-2">
              <span class="text-sm font-medium text-purple-800">会计科目</span>
              <span class="text-xs bg-purple-100 text-purple-700 px-2 py-1 rounded">推荐</span>
            </div>
            <p class="text-sm text-purple-700">${esc(acc.account_subject)||'—'}</p>
          </div>
          <div><h4 class="text-sm font-medium text-gray-700 mb-1">匹配依据</h4><ul class="space-y-1">${
            toArr(acc.basis).map(t=>li(t,'text-purple-500')).join('') || '<li class="text-xs text-gray-400">暂无</li>'
          }</ul></div>
          <div><h4 class="text-sm font-medium text-gray-700 mb-1">引用来源</h4><ul class="space-y-1" id="accounting_sources"></ul></div>
          <div><h4 class="text-sm font-medium text-gray-700 mb-1">相关建议</h4><ul class="space-y-1">${
            toArr(acc.advice).map(t=>li(t,'text-purple-500')).join('') || '<li class="text-xs text-gray-400">暂无</li>'
          }</ul></div>
        </div>
      </div>
    </section>

    <!-- 发票风险提示 -->
    <section class="bg-white rounded-xl shadow-sm border border-gray-200">
      <div class="p-6">
        <h3 class="text-lg font-semibold text-gray-900 mb-4 flex items-center">
          <i class="fas fa-exclamation-triangle text-yellow-500 mr-2"></i> 发票风险提示
        </h3>
        <div class="space-y-4">
          <div class="p-3 rounded-lg border ${badgeBg}">
            <div class="flex items-center space-x-2">
              <i class="fas fa-shield-alt ${badgeIc}"></i>
              <span class="text-sm font-medium ${badgeTx}">${esc(level)}</span>
            </div>
          </div>
          <div><h4 class="text-sm font-medium text-gray-700 mb-1">判断依据</h4><ul class="space-y-1">${
            toArr(risk.basis).map(t=>li(t,'text-yellow-500')).join('') || '<li class="text-xs text-gray-400">暂无</li>'
          }</ul></div>
          <div><h4 class="text-sm font-medium text-gray-700 mb-1">风险点</h4><ul class="space-y-1">${
            toArr(risk.points).map(t=>li(t,'text-yellow-500')).join('') || '<li class="text-xs text-gray-400">暂无</li>'
          }</ul></div>
          <div><h4 class="text-sm font-medium text-gray-700 mb-1">引用来源</h4><ul class="space-y-1" id="risk_sources"></ul></div>
        </div>
      </div>
    </section>

    <!-- 报销审核要点 -->
    <section class="bg-white rounded-xl shadow-sm border border-gray-200">
      <div class="p-6">
        <h3 class="text-lg font-semibold text-gray-900 mb-4 flex items-center">
          <i class="fas fa-lightbulb text-orange-500 mr-2"></i> 报销审核要点
        </h3>
        <div class="space-y-6">
          <div><h4 class="text-sm font-medium text-gray-700 mb-2">审核注意事项</h4><ul class="space-y-2">${
            toArr(appr.focus_points).map(t=>li(t,'text-orange-400')).join('') || '<li class="text-xs text-gray-400">暂无</li>'
          }</ul></div>
          <div><h4 class="text-sm font-medium text-gray-700 mb-2">相关建议</h4><ul class="space-y-2">${
            toArr(appr.suggestions).map(t=>li(t,'text-orange-400')).join('') || '<li class="text-xs text-gray-400">暂无</li>'
          }</ul></div>
          <div><h4 class="text-sm font-medium text-gray-700 mb-2">判断依据</h4><ul class="space-y-2">${
            toArr(appr.basis).map(t=>li(t,'text-orange-400')).join('') || '<li class="text-xs text-gray-400">暂无</li>'
          }</ul></div>
          <div><h4 class="text-sm font-medium text-gray-700 mb-2">引用来源</h4><ul class="space-y-2" id="approval_sources">
            ${(toArr(appr.sources) || []).map(t => li(t, 'text-orange-400')).join('') || '<li class="text-xs text-gray-400">暂无</li>'}
          </ul></div>
        </div>
      </div>
    </section>
    
    <!-- 发票验真 -->
    <section class="bg-white rounded-xl shadow-sm border border-gray-200">
      <div class="p-6">
        <h3 class="text-lg font-semibold text-gray-900 mb-4 flex items-center">
          <i class="fas fa-shield-alt text-indigo-500 mr-2"></i> 发票验真
        </h3>
        <div class="space-y-4">
          <div id="verificationResult" class="p-4 rounded-lg border"></div>
        </div>
      </div>
    </section>
    
    <!-- 下载按钮区域 -->
    <div class="flex justify-center space-x-3 py-4">
      <button id="downloadExcel" class="px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700 transition-colors flex items-center">
        <i class="fas fa-file-excel mr-2"></i> 下载 Excel
      </button>
      <button id="downloadCsv" class="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors flex items-center">
        <i class="fas fa-file-csv mr-2"></i> 下载 CSV
      </button>
    </div>
  </div>`;
}

/* ---------- 渲染工具函数 ---------- */
function renderSources(list, boxId){
  const el = document.getElementById(boxId);
  if (!el) return;
  const arr = Array.isArray(list) ? list.filter(Boolean) : [];
  if (!arr.length) { 
    el.innerHTML = `<li class="text-xs text-gray-400">暂无</li>`; 
    return; 
  }
  el.innerHTML = arr.map(s => {
    if (typeof s === "string") return `<li class="flex items-start text-xs text-gray-600"><i class="fas fa-circle text-gray-500 text-[6px] mt-1 mr-2"></i><span>${s}</span></li>`;
    const t = s?.title || s?.name || "未命名来源";
    const u = s?.url || "";
    return `<li class="flex items-start text-xs text-gray-600"><i class="fas fa-circle text-gray-500 text-[6px] mt-1 mr-2"></i><span>${u ? `<a href="${u}" target="_blank" rel="noreferrer" class="text-blue-600 hover:underline">${t}</a>` : t}</span></li>`;
  }).join("");
}

/* ---------- 唯一入口：结果渲染 ---------- */
function renderResults(data) {
  // 保险：后端返回不规范也不崩
  data = data || {};
  const info    = data.invoice_info || {};
  const acc     = data.accounting_analysis || {};
  const risk    = data.risk_analysis || {};
  const appr    = data.approval_analysis || {};

  // 金额字段（我们后端已经加了别名）
  const withTax = info.amount_with_tax ?? info.amount_in_figures ??
                  ((info.total_amount!=null && info.total_tax!=null) ? (Number(info.total_amount)+Number(info.total_tax)) : null);
  const exTax   = info.amount_excl_tax ?? info.total_amount;
  const tax     = info.tax_amount ?? info.total_tax;

  safeSetText(DOM.amountWithTax, money(withTax));
  safeSetText(DOM.amountExclTax, money(exTax));
  safeSetText(DOM.taxAmount,     money(tax));

  // 费用类型 & 科目
  safeSetText(DOM.expenseType, data.expense_type || "—");
  safeSetText(DOM.subject,     acc.account_subject || "—");

  // 来源（对象数组或字符串都兼容）
  const accSrc = acc.references_text || toTitles(acc.sources_used);
  const riskSrc = risk.references_text || toTitles(risk.sources_used);
  const apprSrc = appr.references_text || toTitles(appr.sources_used);

  safeSetHTML(DOM.accSources, renderList(accSrc));
  safeSetHTML(DOM.riskSources, renderList(riskSrc));
  safeSetHTML(DOM.apprSources, renderList(apprSrc));

  // 风险/审批正文
  safeSetHTML(DOM.riskPoints, renderList(risk.risk_points));
  safeSetText(DOM.riskBasis,  risk.basis || "—");

  safeSetHTML(DOM.apprNotes,  renderList(appr.approval_notes));
  safeSetText(DOM.apprBasis,  appr.basis || "—");
  safeSetHTML(DOM.apprSug,    renderList(appr.suggestions));

  // 方便排查：在控制台留一份最后数据
  window.__lastInvoiceResult = data;
}

// 添加渲染发票验真结果的函数
function renderVerificationResult(verification) {
  const el = document.getElementById('verificationResult');
  if (!el) return;
  const isValid = verification.is_valid ?? true;

  // 兜底拿号：优先 data.fphm，其次 data.code，再次 verification.invoice_number
  const d = verification?.verify_result?.data || {};
  const invNo = d.fphm || d.code || verification.invoice_number || "";

  // 如果 message 里已经带了号码，就别重复渲染"发票号码：xxx"这一行
  let msg = verification.verify_message || verification.message || '验真通过';
  if (isValid && invNo && !/发票号码/.test(msg)) {
    msg = `验真成功，发票号码：${invNo}`;
  }

  if (isValid) {
    el.className = 'p-4 rounded-lg border bg-green-50 border-green-200';
    el.innerHTML = `
      <div class="flex items-center space-x-2 text-green-800">
        <i class="fas fa-check-circle text-green-500"></i>
        <span class="text-sm font-medium">验真通过</span>
      </div>
      <p class="text-sm text-green-700 mt-1">${msg}</p>
    `;
  } else {
    el.className = 'p-4 rounded-lg border bg-red-50 border-red-200';
    el.innerHTML = `
      <div class="flex items-center space-x-2 text-red-800">
        <i class="fas fa-exclamation-circle text-red-500"></i>
        <span class="text-sm font-medium">验真未通过</span>
      </div>
      <p class="text-sm text-red-700 mt-2">${msg}</p>
      <p class="text-sm text-red-700 mt-2">
        建议前往 <a href="https://inv-veri.chinatax.gov.cn/" target="_blank" rel="noopener noreferrer" class="text-red-600 underline hover:text-red-800">国家税务总局查验平台</a> 手动验真
      </p>
    `;
  }
}

// 添加showResults函数定义
function showResults() {
  initialEl?.classList.add('hidden');
  loadingEl?.classList.add('hidden');
  resultsEl?.classList.remove('hidden');
}

// —— 3.2 在你处理上传后的 fetch 逻辑里（拿到后端 JSON 的地方） —— //
// 假设变量 data 是后端返回
async function handleApiResponse(data) {
  // 1) 先判错
  const err = data?.invoice_info?.__ocr_error__;
  if (err) {
    const logId = data?.raw_ocr?.log_id ?? null;
    const x = explainOcrError(err, logId);
    toastError(x.ui);
    // 这里你可以把错误也渲染到右侧"审核注意"区
    const el = document.getElementById("risk-notes");
    if (el) el.textContent = x.ui;
    
    // 确保隐藏加载状态并显示初始内容
    stopLoadingAndShowInitial();
    return; // 不中断的话，后面渲染会写空值，容易误导
  }

  // 2) 正常渲染票面信息
  // 你现有的 renderResults(data) 走原逻辑就行
  renderResults(data);
}

// ===== 工具：渲染文件列表 =====
function renderFileList() {
  if (!fileList) return;
  fileList.innerHTML = '';
  if (!selectedFiles.length) {
    fileList.innerHTML = `<div class="text-gray-400 text-sm">文件列表为空</div>`;
    if (startAnalysisBtn) startAnalysisBtn.disabled = true;
    return;
  }
  if (startAnalysisBtn) startAnalysisBtn.disabled = false;

  selectedFiles.forEach((f, idx) => {
    const row = document.createElement('div');
    row.className = 'flex items-center justify-between text-sm border rounded px-2 py-1 bg-white';
    row.innerHTML = `
      <div class="truncate max-w-[75%]">
        <i class="fa-regular fa-file mr-2"></i>${f.name}
        <span class="text-xs text-gray-400 ml-2">(${(f.size/1024/1024).toFixed(2)}MB)</span>
      </div>
      <button data-idx="${idx}" class="text-gray-500 hover:text-red-500">
        <i class="fas fa-times"></i>
      </button>`;
    row.querySelector('button')?.addEventListener('click', (e) => {
      const i = Number(e.currentTarget.dataset.idx);
      selectedFiles.splice(i, 1);
      renderFileList();
    });
    fileList.appendChild(row);
  });
}

// ===== 事件：选择文件按钮 =====
selectFileBtn?.addEventListener('click', (e) => {
  e.preventDefault();
  e.stopPropagation();
  fileInput?.click();
});

// ===== 事件：点击空白的 dropZone 也可打开选择窗 =====
dropZone?.addEventListener('click', (e) => {
  if (e.target === e.currentTarget) {
    fileInput?.click();
  }
});

// ===== 事件：文件 input 变更 =====
fileInput?.addEventListener('change', (e) => {
  const files = Array.from(e.target.files || []);
  if (!files.length) return;
  selectedFiles.push(...files);
  renderFileList();
});

// ===== 事件：拖拽 =====
['dragenter', 'dragover'].forEach(evt => {
  dropZone?.addEventListener(evt, (e) => {
    e.preventDefault();
    e.stopPropagation();
    dropZone.classList.add('border-blue-500');
  });
});

['dragleave', 'drop'].forEach(evt => {
  dropZone?.addEventListener(evt, (e) => {
    e.preventDefault();
    e.stopPropagation();
    dropZone.classList.remove('border-blue-500');
  });
});

dropZone?.addEventListener('drop', (e) => {
  const files = Array.from(e.dataTransfer?.files || []);
  if (!files.length) return;
  selectedFiles.push(...files);
  renderFileList();
});

// ===== 事件：清空文件 =====
clearFilesBtn?.addEventListener('click', (e) => {
  e.preventDefault();
  e.stopPropagation();
  if (fileInput) fileInput.value = '';
  selectedFiles = [];
  renderFileList();
});

// ===== 上传 + 分析 =====
// 3) 把 startLoading/stopLoading 这两个也用 class 切换，别再只改 style.display：
const startLoading = () => { 
  hideByClass(initialEl); 
  hideByClass(resultsEl); 
  showByClass(loadingEl); 
};

const stopLoading = () => { 
  hideByClass(loadingEl); 
};

const stopLoadingAndShowResults = () => {
  hideByClass(loadingEl);
  showByClass(resultsEl);
  hideByClass(initialEl);
};

const stopLoadingAndShowInitial = () => {
  hideByClass(loadingEl);
  hideByClass(resultsEl);
  showByClass(initialEl);
};

async function submitAndRender(files, userRemark) {
  try {
    startLoading();

    const form = new FormData();
    for (const f of files) form.append("files", f);
    form.append("note", userRemark || "");

    const resp = await fetch("https://engine.pynythd.cn/api/invoices", { // 你的 API 地址
      method: "POST",
      body: form,
    });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const data = await resp.json();

    // ====== 统一按后端真实字段名取值（后端返回结构见 reimbursement_processor.py）======
    const inv  = data.invoice_info || {};
    const acc  = data.accounting_analysis || {};
    const risk = data.risk_analysis || {};
    const appr = data.approval_analysis || {};
    const verf = data.verification || {};
    const kwc  = data.keyword_account_candidates || [];
    const when = data.processed_time || "";
    const warns = data.policy_warnings || [];

    // ====== 渲染：所有写 DOM 的地方换成 setHTML/setText（不会因 null 崩溃）======
    // 发票基本信息
    setText("invoice-number", inv.invoice_number || "—");
    setText("invoice-date",   inv.invoice_date   || "—");
    setText("seller-name",    inv.seller_name    || "—");
    setText("buyer-name",     inv.buyer_name     || "—");
    setText("tax-rate",       inv.tax_rate       || "—");
    // 金额相关：不含税、税额、价税合计
    setText("invoice_excl",    inv.total_amount != null ? String(inv.total_amount) : "—");
    setText("invoice_tax",     inv.total_tax    != null ? String(inv.total_tax)    : "—");
    setText("invoice_incl",    inv.amount_in_figures != null ? String(inv.amount_in_figures) : "—");

    // 会计科目
    setText("account_subject", acc.account_subject || "未判定");
    setHTML("account-basis",   (acc.basis && String(acc.basis).replace(/\n/g,"<br>")) || "（未在知识库命中直接依据）");
    setHTML("account-suggestions", (acc.suggestions || []).map(s=>`<li>${s}</li>`).join("") || "<li>—</li>");

    // 风险点
    setHTML("risk_points", (risk.risk_points || []).map(r=>`<li>${r}</li>`).join("") || "<li>—</li>");
    setHTML("risk-basis",  (risk.basis || []).map(b=>`<li>${b}</li>`).join("") || "<li>—</li>");
    setText("risk-level",  risk.risk_level || "—");

    // 审批要点
    setHTML("approval_notes", (appr.approval_notes || []).map(a=>`<li>${a}</li>`).join("") || "<li>—</li>");
    setHTML("approval-basis", (appr.basis ? String(appr.basis).split(/\n+/).map(b=>`<li>${b}</li>`).join("") : "<li>—</li>"));

    // 验真结果
    setText("verify-msg", (verf.verify_message || (verf.success===false && verf.message) || "—"));

    // 关键词候选和规则提醒（可选）
    setHTML("kw-candidates", (kwc || []).map(k=>`<li>${k.account}: ${k.score}</li>`).join(""));
    setHTML("policy-warnings", (warns || []).map(w=>`<li>${w}</li>`).join(""));

    // 处理时间
    setText("processed-time", when || "");

    // 命中文献/引用来源（如果你页面有这个块）
    const uniqSrc = new Set([
      ...(acc.sources_used || []),
      ...(risk.sources_used || []),
      ...(appr.sources_used || [])
    ]);
    setHTML("accounting_sources", Array.from(uniqSrc).map(s=>`<li>${s}</li>`).join("") || "<li>—</li>");
    setHTML("risk_sources", Array.from(uniqSrc).map(s=>`<li>${s}</li>`).join("") || "<li>—</li>");
    setHTML("approval_sources", Array.from(uniqSrc).map(s=>`<li>${s}</li>`).join("") || "<li>—</li>");

    // 最后让"分析中"消失，展示结果容器
    hideByClass(loadingEl);
    showByClass(resultsEl);

  } catch (e) {
    stopLoading();
    console.error(e);
    alert("上传或解析失败：" + e.message);
  } finally {
    stopLoading();  // ← **无论成功失败都停掉 loading**
  }
}

async function uploadAndAnalyze() {
  if (isUploading || selectedFiles.length === 0) return;
  isUploading = true;

  // UI: 显示加载
  startLoading();

  try {
    const form = new FormData();
    selectedFiles.forEach(f => form.append('files', f));
    // 额外告诉后端：哪些是"非主票据"的佐证
    const evidenceHint = selectedFiles.slice(1).map(f => ({ filename: f.name }));
    form.append('evidence_hint', JSON.stringify(evidenceHint));

    const res = await fetch('https://engine.pynythd.cn/api/invoices', { method: 'POST', body: form });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const ct = res.headers.get('content-type') || '';
    if (!ct.includes('application/json')) {
      const text = await res.text();
      throw new Error(`Bad content-type: ${ct}. body: ${text.slice(0,200)}`);
    }
    const json = await res.json();

    // —— 3.3 在你原来 fetch 处替换调用 —— //
    // 使用新的入口处理API响应
    await handleApiResponse(json);
    
    // 如果没有OCR错误，继续正常渲染流程
    if (!json?.invoice_info?.__ocr_error__) {
      // 2) 在 uploadAndAnalyze() 的 `const json = await res.json();` 之后，替换你现在的"渲染"部分
      try {
        if (!json || typeof json !== "object") throw new Error("空响应");
        
        // 规范化后用你写好的模板一次性把右侧结果页面画出来
        const vm = normalizePayload(json);
        if (resultsEl) {
          resultsEl.innerHTML = buildResultsHTML(vm);
          // 把三块"引用来源"填进去（这三个容器是 buildResultsHTML 里生成的）
          renderSources(vm.accounting.sources, 'accounting_sources');
          renderSources(vm.risk.sources,       'risk_sources');
          renderSources(vm.approval.sources,   'approval_sources');
          // 验真结果卡片
          renderVerificationResult(json.verification || {});
          
          // 为下载按钮添加事件监听器
          const downloadExcelBtn = document.getElementById('downloadExcel');
          const downloadCsvBtn = document.getElementById('downloadCsv');
          
          if (downloadExcelBtn) {
            downloadExcelBtn.addEventListener('click', (e) => {
              e.preventDefault();
              e.stopPropagation();
              downloadAsExcel(json);
            });
          }
          
          if (downloadCsvBtn) {
            downloadCsvBtn.addEventListener('click', (e) => {
              e.preventDefault();
              e.stopPropagation();
              downloadAsCsv(json);
            });
          }
        }

        // 切换 UI：关 loading，开结果
        stopLoadingAndShowResults();

        // 方便调试
        window.__lastInvoiceResult = json;
      } catch (e) {
        console.error("Render failed:", e, json);
        alert("解析失败：响应格式异常（看控制台 __lastInvoiceResult）");
        // 出错时回到初始状态
        stopLoadingAndShowInitial();
      }
    } else {
      // 有OCR错误时也要确保loading状态被移除
      stopLoadingAndShowInitial();
    }
  } catch (err) {
    console.error(err);
    alert('上传或解析失败：' + (err?.message || err));
    // 回到初始
    stopLoadingAndShowInitial();
  } finally {
    isUploading = false;
  }
}

// 绑定按钮事件 & 初始化
startAnalysisBtn?.addEventListener('click', (e) => {
  e.preventDefault();
  e.stopPropagation();
  uploadAndAnalyze(); // ← 真正触发上传+分析
});

// 初始渲染一次文件列表（用于控制按钮禁用态）
renderFileList();

// 使用事件委托处理下载按钮点击事件
document.addEventListener('click', function(e) {
  // 处理Excel下载
  if (e.target.id === 'downloadExcel' || (e.target.closest && e.target.closest('#downloadExcel'))) {
    e.preventDefault();
    e.stopPropagation();
    if (window.__lastInvoiceResult) {
      downloadAsExcel(window.__lastInvoiceResult);
    } else {
      alert('暂无数据可下载');
    }
  }
  
  // 处理CSV下载
  if (e.target.id === 'downloadCsv' || (e.target.closest && e.target.closest('#downloadCsv'))) {
    e.preventDefault();
    e.stopPropagation();
    if (window.__lastInvoiceResult) {
      downloadAsCsv(window.__lastInvoiceResult);
    } else {
      alert('暂无数据可下载');
    }
  }
});

// 别名桥接
const renderResultsFromApi = renderResults;

// ===== 下载功能函数 =====

// 下载为Excel文件
async function downloadAsExcel(data) {
  try {
    await ensureXlsxLib(); // 确保 SheetJS 已加载

    // 复用你现有的转换函数：把结果对象 => 二维数组 rows（第一行是表头）
    const rows = convertToRows(data);

    // 生成 worksheet / workbook
    const ws = XLSX.utils.aoa_to_sheet(rows);
    // 可选：自动列宽（根据内容长度粗略估算）
    const colWidths = rows[0].map((_, ci) => {
      let maxLen = 8;
      for (let r = 0; r < rows.length; r++) {
        const v = rows[r][ci] == null ? '' : String(rows[r][ci]);
        maxLen = Math.max(maxLen, v.length);
      }
      return { wch: Math.min(60, Math.max(10, Math.ceil(maxLen * 1))) };
    });
    ws['!cols'] = colWidths;

    const wb = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(wb, ws, '报销分析报告');

    // 写出 ArrayBuffer → Blob（真正的 .xlsx）
    const wbout = XLSX.write(wb, { bookType: 'xlsx', type: 'array' });
    const blob = new Blob([wbout], {
      type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    });

    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `发票分析结果_${new Date().toISOString().slice(0,10)}.xlsx`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  } catch (e) {
    console.error('导出 .xlsx 失败：', e);
    alert('导出 Excel 失败：' + (e && e.message ? e.message : e));
  }
}

// 下载为CSV文件
function downloadAsCsv(data) {
  const csvContent = convertToCsvFormat(data);
  const blob = new Blob(['\ufeff' + csvContent], {
    type: 'text/csv;charset=utf-8'
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = `发票分析结果_${new Date().toISOString().slice(0, 10)}.csv`;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

// 转换为CSV格式
function convertToCsvFormat(data) {
  const rows = convertToRows(data);
  return rows.map(row =>
    row.map(field => {
      const s = (field == null) ? '' : String(field);
      if (/[",\r\n]/.test(s)) return `"${s.replace(/"/g, '""')}"`;
      return s;
    }).join(',')
  ).join('\r\n');
}

// 将数据转换为行数组
function convertToRows(data) {
  const rows = [];
  
  // 添加表头，严格按照图片中的顺序
  rows.push(['文件名', '发票类型', '发票号码', '开票日期', '不含税金额', '税额', '价税合计', '费用类型', '会计科目', '风险等级', '验真结果']);
  
  // 从window.__lastInvoiceResult获取完整数据，如果没有则使用传入的数据
  const resultData = window.__lastInvoiceResult || data;
  
  // 发票信息
  const invoice = resultData.invoice || resultData.invoice_info || {};
  // 费用分析和会计科目信息
  const accounting = resultData.accounting || resultData.accounting_analysis || {};
  // 风险信息
  const risk = resultData.risk || resultData.risk_analysis || {};
  // 验真信息
  const verification = resultData.verification || {};
  
  // 提取需要的字段
  const fileName = (selectedFiles && selectedFiles.length > 0) ? selectedFiles[0].name : 'invoice.pdf'; // 从上传的文件中获取实际文件名
  const invoiceType = invoice.type || invoice.invoice_type || '';
  const invoiceNumber = invoice.number || invoice.invoice_number || '';
  const invoiceDate = invoice.date || invoice.invoice_date || '';
  
  // ——发票金额字段——
  const excl = Number(invoice.total_amount ?? 0);
  const tax  = Number(invoice.total_tax ?? 0);
  const incl = invoice.amount_in_figures != null
    ? Number(invoice.amount_in_figures)
    : (isFinite(excl + tax) ? +(excl + tax).toFixed(2) : null);
  
  // 金额处理，确保格式正确
  const amountExcludingTax = isFinite(excl) ? excl.toFixed(2) : '';
  const taxAmount = isFinite(tax) ? tax.toFixed(2) : '';
  const amountIncludingTax = isFinite(incl) ? incl.toFixed(2) : '';
  
  const expenseType = resultData.expense_type || accounting.expense_type || '';
  const accountSubject = accounting.account_subject || '';
  const riskLevel = risk.risk_level || '';
  const verificationResult = verification.is_valid !== undefined ? 
    (verification.is_valid ? '通过' : '未通过') : 
    (verification.verification_result || verification.result || '通过'); // 默认通过
  
  // 添加数据行
  rows.push([
    fileName,
    invoiceType,
    invoiceNumber,
    invoiceDate,
    amountExcludingTax,
    taxAmount,
    amountIncludingTax,
    expenseType,
    accountSubject,
    riskLevel,
    verificationResult
  ]);
  
  return rows;
}

// 添加 SheetJS 库支持
// 注意：需要先引入 xlsx.js 库，可以通过 CDN 引入
// <script src="https://cdnjs.cloudflare.com/ajax/libs/xlsx/0.18.5/xlsx.full.min.js"></script>


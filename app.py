"""
发票行程单识别工具
==================
Tab 1 — 行程单识别：上传航空运输电子客票行程单 PDF，逐页提取金额并汇总
Tab 2 — 发票识别：上传增值税发票等 PDF，逐页提取金额并汇总

启动方式：streamlit run app.py
"""

import streamlit as st
import pdfplumber
import re
import pandas as pd
from io import BytesIO
from datetime import datetime

# ── 页面配置 ─────────────────────────────────────────────
st.set_page_config(
    page_title="发票行程单识别工具",
    page_icon="🧾",
    layout="wide",
)


# ═══════════════════════════════════════════════════════════
# 通用工具
# ═══════════════════════════════════════════════════════════

def clean_amount(s: str) -> float | None:
    s = str(s).strip().replace(",", "").replace("，", "").replace(" ", "")
    s = re.sub(r"^[¥￥CNYcnyRMBrmb]+", "", s)
    s = re.sub(r"[^0-9.]$", "", s)
    try:
        v = float(s)
        return round(v, 2) if v > 0 else None
    except (ValueError, TypeError):
        return None


def _dedup(items: list[dict]) -> list[dict]:
    seen: set[float] = set()
    out: list[dict] = []
    for it in items:
        k = round(it["amount"], 2)
        if k not in seen:
            seen.add(k)
            out.append(it)
    return out


# ═══════════════════════════════════════════════════════════
# 行程单提取
# ═══════════════════════════════════════════════════════════

def _itinerary_from_text(page_text: str) -> list[dict]:
    items: list[dict] = []
    for line in page_text.split("\n"):
        if not re.search(r"(合计|总计|应付|实付|金额合计)", line):
            continue
        nums = re.findall(r"(\d{1,3}(?:,\d{3})*\.\d{2})", line)
        if not nums:
            continue
        v = clean_amount(nums[-1])
        if v:
            items.append({"amount": v, "source": line.strip()[:120]})
    return items


def _itinerary_from_tables(tables) -> list[dict]:
    items: list[dict] = []
    for tbl in (tables or []):
        if not tbl:
            continue
        for row in tbl:
            if not row:
                continue
            row_text = " ".join(str(c) for c in row if c)
            if not re.search(r"(合计|总计|小计)", row_text):
                continue
            for cell in reversed(row):
                if cell is None:
                    continue
                v = clean_amount(cell)
                if v:
                    items.append({"amount": v, "source": row_text[:120]})
                    break
    return items


def extract_itinerary_page(page_text: str, tables) -> list[dict]:
    items = _itinerary_from_text(page_text)
    items += _itinerary_from_tables(tables)
    if not items:
        nums = re.findall(r"(\d{1,3}(?:,\d{3})*\.\d{2})", page_text)
        vals = [clean_amount(n) for n in nums]
        vals = [v for v in vals if v is not None]
        if vals:
            items.append({"amount": max(vals), "source": "降级：本页最大金额"})
    return _dedup(items)


# ═══════════════════════════════════════════════════════════
# 发票提取
# ═══════════════════════════════════════════════════════════

_INVOICE_PATTERNS = [
    (r"价税合计[：:]*[¥￥]?(\d{1,3}(?:,\d{3})*\.\d{2})", "价税合计"),
    (r"合计金额[：:]*[¥￥]?(\d{1,3}(?:,\d{3})*\.\d{2})", "合计金额"),
    (r"总金额[：:]*[¥￥]?(\d{1,3}(?:,\d{3})*\.\d{2})", "总金额"),
    (r"金额合计[：:]*[¥￥]?(\d{1,3}(?:,\d{3})*\.\d{2})", "金额合计"),
    (r"发票金额[：:]*[¥￥]?(\d{1,3}(?:,\d{3})*\.\d{2})", "发票金额"),
    (r"应付金额[：:]*[¥￥]?(\d{1,3}(?:,\d{3})*\.\d{2})", "应付金额"),
]


def extract_invoice_page(page_text: str) -> list[dict]:
    compact = re.sub(r"\s+", "", page_text)
    items: list[dict] = []

    for pat, label in _INVOICE_PATTERNS:
        for m in re.finditer(pat, compact):
            v = clean_amount(m.group(1))
            if v:
                items.append({"amount": v, "source": label})

    # "价税合计" 邻近金额
    for m in re.finditer(r"价税合计", compact):
        nearby = compact[m.start(): m.start() + 80]
        for n in re.findall(r"(\d{1,3}(?:,\d{3})*\.\d{2})", nearby):
            v = clean_amount(n)
            if v:
                items.append({"amount": v, "source": "价税合计(邻近)"})

    if not items:
        nums = re.findall(r"(\d{1,3}(?:,\d{3})*\.\d{2})", page_text)
        vals = [clean_amount(n) for n in nums]
        vals = [v for v in vals if v is not None]
        if vals:
            items.append({"amount": max(vals), "source": "降级：本页最大金额"})

    return _dedup(items)


# ═══════════════════════════════════════════════════════════
# 通用 PDF 处理
# ═══════════════════════════════════════════════════════════

def process_pdfs(uploaded_files, extract_fn, progress_placeholder) -> pd.DataFrame:
    """逐页处理上传文件，每页调用 extract_fn(txt, tables) 返回 DataFrame。"""
    rows: list[dict] = []
    total = len(uploaded_files)

    for i, f in enumerate(uploaded_files):
        progress_placeholder.progress(
            (i + 0.1) / max(total, 1),
            text=f"读取: {f.name}  ({i + 1}/{total})",
        )

        try:
            raw = f.read()
        except Exception as e:
            rows.append({"文件名": f.name, "页码": "-", "来源": "", "识别金额": 0.0, "状态": "read_err", "备注": str(e)})
            continue

        try:
            pdf = pdfplumber.open(BytesIO(raw))
        except Exception as e:
            rows.append({"文件名": f.name, "页码": "-", "来源": "", "识别金额": 0.0, "状态": "open_err", "备注": str(e)})
            continue

        has_text = False
        for pn, page in enumerate(pdf.pages, 1):
            progress_placeholder.progress(
                (i + pn / max(len(pdf.pages), 1)) / max(total, 1),
                text=f"{f.name}  第{pn}/{len(pdf.pages)}页",
            )
            txt = page.extract_text() or ""
            if not txt.strip():
                continue
            has_text = True
            tables = page.extract_tables() or []
            items = extract_fn(txt, tables)
            for it in items:
                rows.append({
                    "文件名": f.name,
                    "页码": f"第{pn}页",
                    "来源": it["source"],
                    "识别金额": it["amount"],
                    "状态": "ok",
                    "备注": "",
                })
        pdf.close()

        if not has_text:
            rows.append({"文件名": f.name, "页码": "-", "来源": "", "识别金额": 0.0, "状态": "no_text", "备注": "全部页面均无文字，可能为扫描件"})

    progress_placeholder.empty()
    return pd.DataFrame(rows)


# ═══════════════════════════════════════════════════════════
# 结果渲染
# ═══════════════════════════════════════════════════════════

def render_results(df: pd.DataFrame, prefix: str):
    if df.empty:
        return

    total_amount = df["识别金额"].sum()
    ok_count = int((df["状态"] == "ok").sum())

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("📄 文件数", df["文件名"].nunique())
    c2.metric("📋 识别条目", len(df))
    c3.metric("✅ 成功条目", ok_count)
    c4.metric("💰 汇总金额", f"¥{total_amount:,.2f}")

    st.markdown("---")
    st.caption("📊 识别明细（每页每条各占一行）")

    # 状态标签映射
    status_labels = {
        "ok": "✅ 识别成功",
        "no_text": "⚠️ 无文本",
        "read_err": "❌ 读取失败",
        "open_err": "❌ 打开失败",
    }

    disp = df.copy()
    disp["识别金额"] = disp["识别金额"].apply(lambda x: f"¥{x:,.2f}" if x > 0 else "-")
    disp["状态"] = disp["状态"].map(status_labels)
    st.dataframe(disp[["文件名", "页码", "来源", "识别金额", "状态"]], use_container_width=True, hide_index=True)

    # 来源详情
    with st.expander("🔍 查看提取来源"):
        for _, row in df.iterrows():
            if row["状态"] != "ok":
                continue
            st.markdown(f"**{row['文件名']}** {row['页码']} → ¥{row['识别金额']:,.2f}（{row['来源']}）")

    # 导出
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    ca, cb = st.columns(2)

    export_df = df.copy()
    export_df["状态"] = export_df["状态"].map(status_labels)
    ca.download_button("导出 CSV", data=export_df.to_csv(index=False, encoding="utf-8-sig"),
                       file_name=f"{prefix}_识别结果_{ts}.csv", mime="text/csv", use_container_width=True)
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        export_df.to_excel(w, sheet_name="识别结果", index=False)
    cb.download_button("导出 Excel", data=buf.getvalue(),
                       file_name=f"{prefix}_识别结果_{ts}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       use_container_width=True)


# ═══════════════════════════════════════════════════════════
# Tab 1 — 行程单识别
# ═══════════════════════════════════════════════════════════

def tab_itinerary():
    st.subheader("✈️ 行程单识别")
    st.caption("上传航空运输电子客票行程单 PDF（支持多页合并），自动提取每张行程单的金额并汇总")

    uploaded = st.file_uploader(
        "上传行程单 PDF", type=["pdf"], accept_multiple_files=True,
        key="itin_up",
    )

    if not uploaded:
        st.info("👆 上传行程单 PDF 后点击下方按钮开始识别")
        return

    if st.button("🔍 开始识别行程单", type="primary", use_container_width=True, key="itin_btn"):
        with st.spinner("正在逐页识别行程单…"):
            p = st.empty()
            df = process_pdfs(uploaded, lambda t, tb: extract_itinerary_page(t, tb), p)
            st.session_state["itin_df"] = df

    if "itin_df" in st.session_state and not st.session_state["itin_df"].empty:
        st.markdown("---")
        render_results(st.session_state["itin_df"], "行程单")


# ═══════════════════════════════════════════════════════════
# Tab 2 — 发票识别
# ═══════════════════════════════════════════════════════════

def tab_invoice():
    st.subheader("🧾 发票识别")
    st.caption("上传增值税发票 / 普通发票 PDF（支持多页合并），自动提取每张发票的金额并汇总")

    uploaded = st.file_uploader(
        "上传发票 PDF", type=["pdf"], accept_multiple_files=True,
        key="inv_up",
    )

    if not uploaded:
        st.info("👆 上传发票 PDF 后点击下方按钮开始识别")
        return

    if st.button("🔍 开始识别发票", type="primary", use_container_width=True, key="inv_btn"):
        with st.spinner("正在逐页识别发票…"):
            p = st.empty()
            df = process_pdfs(uploaded, lambda t, _: extract_invoice_page(t), p)
            st.session_state["inv_df"] = df

    if "inv_df" in st.session_state and not st.session_state["inv_df"].empty:
        st.markdown("---")
        render_results(st.session_state["inv_df"], "发票")


# ═══════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════

def main():
    st.title("🧾 发票行程单识别工具")
    st.caption("两个独立功能，互不干扰 — 选择下方标签页操作")

    tab1, tab2 = st.tabs(["✈️ 行程单识别", "🧾 发票识别"])

    with tab1:
        tab_itinerary()

    with tab2:
        tab_invoice()


if __name__ == "__main__":
    main()

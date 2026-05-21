"""
发票行程单识别工具
==================
两个独立功能：
  1. 行程单识别 — 上传航空运输电子客票行程单PDF，提取金额并汇总
  2. 发票识别   — 上传增值税发票等PDF，提取金额并汇总

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
    initial_sidebar_state="expanded",
)


# ═══════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════

def extract_text_and_tables(file_bytes: bytes):
    """从 PDF 字节流中提取文本和表格。返回 (全文, 表格列表)。"""
    full_text_parts: list[str] = []
    tables_data: list[list[list[str | None]]] = []

    with pdfplumber.open(BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                full_text_parts.append(text)

            tables = page.extract_tables()
            for tbl in tables:
                if tbl and len(tbl) > 0:
                    tables_data.append(tbl)

    return "\n".join(full_text_parts), tables_data


def clean_amount(s: str) -> float | None:
    """从字符串中清理并解析金额，失败返回 None。"""
    s = str(s).strip().replace(",", "").replace("，", "").replace(" ", "")
    s = re.sub(r"^[¥￥CNYcnyRMBrmb]+", "", s)
    s = re.sub(r"[^0-9.]$", "", s)
    try:
        val = float(s)
        if val > 0:
            return round(val, 2)
    except (ValueError, TypeError):
        pass
    return None


def find_currency_amounts(text: str) -> list[float]:
    """在文本中找出所有形如价格的数字（带两位小数）。"""
    pattern = r"(\d{1,3}(?:,\d{3})*\.\d{2})"
    amounts: list[float] = []
    for m in re.finditer(pattern, text):
        val = clean_amount(m.group(1))
        if val is not None:
            amounts.append(val)
    return amounts


# ═══════════════════════════════════════════════════════════
# 行程单金额提取
# ═══════════════════════════════════════════════════════════

def extract_itinerary_total(text: str, tables) -> float | None:
    """从行程单中提取「合计」金额。按优先级依次尝试三种策略。"""

    # 策略1：文本中搜索"合计"行
    for line in text.split("\n"):
        if re.search(r"(合计|总计|应付|实付|金额合计)", line):
            nums = re.findall(r"(\d{1,3}(?:,\d{3})*\.\d{2})", line)
            if nums:
                for n in reversed(nums):
                    val = clean_amount(n)
                    if val is not None:
                        return val

    # 策略2：表格中搜索"合计"行
    for table in tables:
        if not table:
            continue
        for row in table:
            if not row:
                continue
            row_text = " ".join(str(c) for c in row if c)
            if re.search(r"(合计|总计|小计)", row_text):
                for cell in reversed(row):
                    if cell is None:
                        continue
                    val = clean_amount(cell)
                    if val is not None:
                        return val

    # 策略3：取全文最大金额（降级策略）
    amounts = find_currency_amounts(text)
    if amounts:
        return max(amounts)

    return None


# ═══════════════════════════════════════════════════════════
# 发票金额提取
# ═══════════════════════════════════════════════════════════

def extract_invoice_total(text: str) -> float | None:
    """从发票中提取「价税合计」金额。按优先级依次尝试三种策略。"""

    text_compact = re.sub(r"\s+", "", text)

    # 策略1：明确关键字 + 金额
    keyword_patterns = [
        r"价税合计[：:]*[¥￥]?(\d{1,3}(?:,\d{3})*\.\d{2})",
        r"价税合计[：:]*.*?[¥￥]?(\d{1,3}(?:,\d{3})*\.\d{2})",
        r"合计金额[：:]*[¥￥]?(\d{1,3}(?:,\d{3})*\.\d{2})",
        r"总金额[：:]*[¥￥]?(\d{1,3}(?:,\d{3})*\.\d{2})",
        r"发票金额[：:]*[¥￥]?(\d{1,3}(?:,\d{3})*\.\d{2})",
        r"金额合计[：:]*[¥￥]?(\d{1,3}(?:,\d{3})*\.\d{2})",
        r"应付金额[：:]*[¥￥]?(\d{1,3}(?:,\d{3})*\.\d{2})",
    ]

    for pat in keyword_patterns:
        m = re.search(pat, text_compact)
        if m:
            val = clean_amount(m.group(1))
            if val is not None:
                return val

    # 策略2：找到"价税合计"位置，向后取邻近金额
    idx = text_compact.find("价税合计")
    if idx >= 0:
        nearby = text_compact[idx : idx + 80]
        nums = re.findall(r"(\d{1,3}(?:,\d{3})*\.\d{2})", nearby)
        for n in nums:
            val = clean_amount(n)
            if val is not None:
                return val

    # 策略3：取全文最大金额
    amounts = find_currency_amounts(text)
    if amounts:
        return max(amounts)

    return None


# ═══════════════════════════════════════════════════════════
# 侧边栏 UI
# ═══════════════════════════════════════════════════════════

def render_sidebar():
    """渲染侧边栏，返回当前选择的模式。"""
    st.sidebar.markdown("## ⚙️ 功能选择")

    mode = st.sidebar.radio(
        "选择识别模式",
        ["✈️ 行程单识别", "🧾 发票识别"],
        key="mode_radio",
    )

    st.sidebar.markdown("---")
    st.sidebar.markdown("### 📋 使用说明")

    if "行程单" in mode:
        st.sidebar.markdown("""
**适用文件：** 航空运输电子客票行程单 PDF

1. 上传一个或多个行程单 PDF
2. 点击「开始识别」
3. 查看汇总金额，可导出 Excel / CSV

**识别逻辑：** 文本「合计/总计」→ 表格合计行 → 全文最大金额
        """)
    else:
        st.sidebar.markdown("""
**适用文件：** 增值税发票、普通发票等 PDF

1. 上传一个或多个发票 PDF
2. 点击「开始识别」
3. 查看汇总金额，可导出 Excel / CSV

**识别逻辑：** 「价税合计」关键字 → 邻近金额 → 全文最大金额
        """)

    st.sidebar.markdown("---")
    st.sidebar.markdown("### ⚠️ 注意事项")
    st.sidebar.markdown("""
- 仅支持**文字型 PDF**（电子行程单/电子发票）
- 扫描件/图片型 PDF 需搭配 Tesseract OCR
- 金额单位：人民币（元）
    """)

    return mode


# ═══════════════════════════════════════════════════════════
# 批量处理
# ═══════════════════════════════════════════════════════════

def process_files(uploaded_files, mode: str) -> pd.DataFrame:
    """批量处理上传文件并返回结果 DataFrame。"""
    results: list[dict] = []
    progress_bar = st.progress(0, text="准备中…")
    total = len(uploaded_files)

    for i, uploaded_file in enumerate(uploaded_files):
        progress_bar.progress(
            (i + 1) / total,
            text=f"正在处理: {uploaded_file.name}  ({i + 1}/{total})",
        )

        try:
            file_bytes = uploaded_file.read()
        except Exception as e:
            results.append({
                "文件名": uploaded_file.name,
                "状态": "❌ 读取失败",
                "识别金额": 0.00,
                "备注": str(e),
            })
            continue

        text, tables = extract_text_and_tables(file_bytes)

        if not text.strip():
            results.append({
                "文件名": uploaded_file.name,
                "状态": "⚠️ 无文本",
                "识别金额": 0.00,
                "备注": "PDF 可能为扫描件或图片，无法提取文字",
            })
            continue

        # 根据模式选择提取器
        if "行程单" in mode:
            amount = extract_itinerary_total(text, tables)
        else:
            amount = extract_invoice_total(text)
            if amount is None:
                amount = extract_itinerary_total(text, tables)

        if amount is not None:
            results.append({
                "文件名": uploaded_file.name,
                "状态": "✅ 识别成功",
                "识别金额": amount,
                "备注": "",
            })
        else:
            preview = text.strip()[:300].replace("\n", " │ ")
            results.append({
                "文件名": uploaded_file.name,
                "状态": "⚠️ 未识别到金额",
                "识别金额": 0.00,
                "备注": f"文本预览: {preview}…",
            })

    progress_bar.empty()
    return pd.DataFrame(results)


# ═══════════════════════════════════════════════════════════
# 结果渲染
# ═══════════════════════════════════════════════════════════

def render_results(df: pd.DataFrame, mode: str):
    """渲染结果表格与汇总信息。"""
    total_amount = df["识别金额"].sum()
    success_count = int((df["状态"] == "✅ 识别成功").sum())
    fail_count = len(df) - success_count

    st.markdown("---")
    cols = st.columns(4)
    cols[0].metric("📄 文件总数", len(df))
    cols[1].metric("✅ 成功识别", success_count)
    cols[2].metric("⚠️ 未能识别", fail_count)
    cols[3].metric("💰 汇总金额", f"¥{total_amount:,.2f}")

    # 明细表
    st.markdown("---")
    st.subheader("📊 识别明细")

    df_display = df.copy()
    df_display["识别金额"] = df_display["识别金额"].apply(
        lambda x: f"¥{x:,.2f}" if x > 0 else "—"
    )

    st.dataframe(df_display, use_container_width=True, hide_index=True)

    # 原始文本（可折叠）
    with st.expander("🔍 查看详细提取信息"):
        for _, row in df.iterrows():
            st.markdown(f"**{row['文件名']}** — {row['状态']}")
            if row["备注"]:
                st.text(row["备注"])
            st.markdown("---")

    # 导出
    st.markdown("---")
    st.subheader("📥 导出结果")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    c1, c2 = st.columns(2)
    csv_data = df.to_csv(index=False, encoding="utf-8-sig")
    c1.download_button(
        "导出 CSV", data=csv_data,
        file_name=f"识别结果_{timestamp}.csv",
        mime="text/csv", use_container_width=True,
    )

    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="识别结果", index=False)
    c2.download_button(
        "导出 Excel", data=buffer.getvalue(),
        file_name=f"识别结果_{timestamp}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )


# ═══════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════

def main():
    st.title("🧾 发票行程单识别工具")
    st.caption("上传 PDF → 自动识别金额 → 一键汇总导出")

    mode = render_sidebar()

    uploaded_files = st.file_uploader(
        "📤 上传 PDF 文件",
        type=["pdf"],
        accept_multiple_files=True,
        help="支持同时上传多个 PDF，单文件上限 200 MB",
    )

    if not uploaded_files:
        st.info("👆 请先在左侧选择识别模式，然后上传 PDF 文件")
        return

    if st.button("🔍 开始识别", type="primary", use_container_width=True):
        with st.spinner("正在识别中，请稍候…"):
            df = process_files(uploaded_files, mode)
        render_results(df, mode)


if __name__ == "__main__":
    main()

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


def _deduplicate(items: list[dict]) -> list[dict]:
    """按金额去重（同页同金额只保留一条）。"""
    seen: set[float] = set()
    result: list[dict] = []
    for item in items:
        key = round(item["amount"], 2)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


# ═══════════════════════════════════════════════════════════
# 单页金额提取（行程单）
# ═══════════════════════════════════════════════════════════

def _extract_itinerary_from_text(page_text: str) -> list[dict]:
    """从单页文本中提取所有行程单「合计」金额。"""
    items: list[dict] = []

    for line in page_text.split("\n"):
        if not re.search(r"(合计|总计|应付|实付|金额合计)", line):
            continue
        nums = re.findall(r"(\d{1,3}(?:,\d{3})*\.\d{2})", line)
        if not nums:
            continue
        # 取该行最后一个金额（通常是合计值）
        val = clean_amount(nums[-1])
        if val is not None:
            src = line.strip()[:120]
            items.append({"amount": val, "source": src})

    return items


def _extract_itinerary_from_tables(tables) -> list[dict]:
    """从单页表格中提取所有行程单「合计」金额。"""
    items: list[dict] = []

    for table in tables:
        if not table:
            continue
        for row in table:
            if not row:
                continue
            row_text = " ".join(str(c) for c in row if c)
            if not re.search(r"(合计|总计|小计)", row_text):
                continue
            # 从右往左找第一个数字
            for cell in reversed(row):
                if cell is None:
                    continue
                val = clean_amount(cell)
                if val is not None:
                    items.append({"amount": val, "source": row_text[:120]})
                    break  # 每行只取一个

    return items


def extract_itinerary_page(page_text: str, tables) -> list[dict]:
    """从单页行程单提取所有金额（去重后返回）。"""
    items = _extract_itinerary_from_text(page_text)
    items += _extract_itinerary_from_tables(tables)

    if not items:
        # 降级：取本页所有带小数金额的最大值（通常就是合计）
        all_nums = re.findall(r"(\d{1,3}(?:,\d{3})*\.\d{2})", page_text)
        valid = [clean_amount(n) for n in all_nums]
        valid = [v for v in valid if v is not None]
        if valid:
            max_val = max(valid)
            items.append({"amount": max_val, "source": "降级策略：本页最大金额"})

    return _deduplicate(items)


# ═══════════════════════════════════════════════════════════
# 单页金额提取（发票）
# ═══════════════════════════════════════════════════════════

_INVOICE_PATTERNS = [
    (r"价税合计[：:]*[¥￥]?(\d{1,3}(?:,\d{3})*\.\d{2})", "价税合计"),
    (r"价税合计[：:]*.*?[¥￥]?(\d{1,3}(?:,\d{3})*\.\d{2})", "价税合计(宽)"),
    (r"合计金额[：:]*[¥￥]?(\d{1,3}(?:,\d{3})*\.\d{2})", "合计金额"),
    (r"总金额[：:]*[¥￥]?(\d{1,3}(?:,\d{3})*\.\d{2})", "总金额"),
    (r"发票金额[：:]*[¥￥]?(\d{1,3}(?:,\d{3})*\.\d{2})", "发票金额"),
    (r"金额合计[：:]*[¥￥]?(\d{1,3}(?:,\d{3})*\.\d{2})", "金额合计"),
    (r"应付金额[：:]*[¥￥]?(\d{1,3}(?:,\d{3})*\.\d{2})", "应付金额"),
]


def extract_invoice_page(page_text: str) -> list[dict]:
    """从单页发票提取所有金额（去重后返回）。"""
    text_compact = re.sub(r"\s+", "", page_text)
    items: list[dict] = []

    # 策略1：关键字正则（finditer 捕获所有匹配）
    for pat, label in _INVOICE_PATTERNS:
        for m in re.finditer(pat, text_compact):
            val = clean_amount(m.group(1))
            if val is not None:
                items.append({"amount": val, "source": label})

    # 策略2：找到"价税合计"位置，取邻近金额
    for m in re.finditer(r"价税合计", text_compact):
        start = m.start()
        nearby = text_compact[start : start + 80]
        nums = re.findall(r"(\d{1,3}(?:,\d{3})*\.\d{2})", nearby)
        for n in nums:
            val = clean_amount(n)
            if val is not None:
                items.append({"amount": val, "source": "价税合计(邻近)"})

    if not items:
        # 降级：取本页最大金额
        all_nums = re.findall(r"(\d{1,3}(?:,\d{3})*\.\d{2})", page_text)
        valid = [clean_amount(n) for n in all_nums]
        valid = [v for v in valid if v is not None]
        if valid:
            max_val = max(valid)
            items.append({"amount": max_val, "source": "降级策略：本页最大金额"})

    return _deduplicate(items)


# ═══════════════════════════════════════════════════════════
# 侧边栏 UI
# ═══════════════════════════════════════════════════════════

def render_sidebar() -> str:
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
（支持单页或多页合并的 PDF）

1. 上传一个或多个行程单 PDF
2. 点击「开始识别」
3. 每页/每条合计各占一行，自动汇总

**识别逻辑：**  
文本「合计」行 → 表格合计行 → 本页最大金额
        """)
    else:
        st.sidebar.markdown("""
**适用文件：** 增值税发票、普通发票等 PDF  
（支持单页或多页合并的 PDF）

1. 上传一个或多个发票 PDF
2. 点击「开始识别」
3. 每张发票各占一行，自动汇总

**识别逻辑：**  
「价税合计」关键字 → 邻近金额 → 本页最大金额
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
# 批量处理（按页逐条）
# ═══════════════════════════════════════════════════════════

def process_files(uploaded_files, mode: str) -> pd.DataFrame:
    """逐页处理上传文件，每页可提取多条金额，返回结果 DataFrame。"""
    results: list[dict] = []
    progress_bar = st.progress(0, text="准备中…")
    total_files = len(uploaded_files)

    for i, uploaded_file in enumerate(uploaded_files):
        progress_bar.progress(
            (i + 0.1) / total_files,
            text=f"读取: {uploaded_file.name}  ({i + 1}/{total_files})",
        )

        try:
            file_bytes = uploaded_file.read()
        except Exception as e:
            results.append({
                "文件名": uploaded_file.name,
                "页码": "—",
                "来源": "",
                "状态": "❌ 读取失败",
                "识别金额": 0.00,
                "备注": str(e),
            })
            continue

        try:
            pdf = pdfplumber.open(BytesIO(file_bytes))
        except Exception as e:
            results.append({
                "文件名": uploaded_file.name,
                "页码": "—",
                "来源": "",
                "状态": "❌ 打开失败",
                "识别金额": 0.00,
                "备注": str(e),
            })
            continue

        total_pages = len(pdf.pages)
        file_has_text = False

        for page_num, page in enumerate(pdf.pages, 1):
            progress_bar.progress(
                (i + page_num / max(total_pages, 1)) / total_files,
                text=f"{uploaded_file.name} 第{page_num}/{total_pages}页",
            )

            page_text = page.extract_text() or ""
            if not page_text.strip():
                continue

            file_has_text = True
            tables = page.extract_tables() or []

            # 根据模式选择提取器
            if "行程单" in mode:
                items = extract_itinerary_page(page_text, tables)
            else:
                items = extract_invoice_page(page_text)

            for item in items:
                results.append({
                    "文件名": uploaded_file.name,
                    "页码": f"第{page_num}页",
                    "来源": item["source"],
                    "状态": "✅ 识别成功",
                    "识别金额": item["amount"],
                    "备注": "",
                })

        pdf.close()

        if not file_has_text:
            results.append({
                "文件名": uploaded_file.name,
                "页码": "—",
                "来源": "",
                "状态": "⚠️ 无文本",
                "识别金额": 0.00,
                "备注": "PDF 全部页面均无法提取文字，可能为扫描件",
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

    st.markdown("---")
    cols = st.columns(4)
    cols[0].metric("📄 文件总数", df["文件名"].nunique())
    cols[1].metric("📋 识别条目", len(df))
    cols[2].metric("✅ 成功条目", success_count)
    cols[3].metric("💰 汇总金额", f"¥{total_amount:,.2f}")

    # 明细表
    st.markdown("---")
    st.subheader("📊 识别明细（每页每条各占一行）")

    df_display = df.copy()
    df_display["识别金额"] = df_display["识别金额"].apply(
        lambda x: f"¥{x:,.2f}" if x > 0 else "—"
    )

    st.dataframe(df_display, use_container_width=True, hide_index=True)

    # 来源详情（可折叠）
    with st.expander("🔍 查看提取来源"):
        for _, row in df.iterrows():
            if row["状态"] != "✅ 识别成功":
                continue
            st.markdown(
                f"**{row['文件名']}** {row['页码']}  "
                f"→ ¥{row['识别金额']:,.2f}  "
                f"（来源: {row['来源']}）"
            )

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
    st.caption("上传 PDF → 逐页识别金额 → 一键汇总导出")

    mode = render_sidebar()

    uploaded_files = st.file_uploader(
        "📤 上传 PDF 文件",
        type=["pdf"],
        accept_multiple_files=True,
        help="支持同时上传多个 PDF，单文件上限 200 MB；多页合并 PDF 会自动逐页识别",
    )

    if not uploaded_files:
        st.info("👆 请先在左侧选择识别模式，然后上传 PDF 文件")
        return

    if st.button("🔍 开始识别", type="primary", use_container_width=True):
        with st.spinner("正在逐页识别中，请稍候…"):
            df = process_files(uploaded_files, mode)
        render_results(df, mode)


if __name__ == "__main__":
    main()

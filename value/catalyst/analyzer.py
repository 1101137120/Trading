"""
催化劑分析器
Phase 1：關鍵詞比對快速評分
Phase 2：Claude API 深度語意分析（需設定 ANTHROPIC_API_KEY）
"""
import json
import logging
import os
from typing import Optional

logger = logging.getLogger("catalyst.analyzer")

# ── Phase 1：關鍵詞分類與權重 ──────────────────────────────────────────────

CATALYST_KEYWORDS: dict[str, tuple[float, list[str]]] = {
    # (基礎分, [關鍵詞列表])
    "擴產":   (3.0, ["擴產", "新廠", "增設產線", "擴充產能", "建廠", "投資設備", "新增產能"]),
    "訂單":   (3.0, ["大單", "長約", "訂單能見度", "backlog", "取得訂單", "獲得合約",
                      "供應協議", "策略合作", "戰略合夥"]),
    "AI題材": (4.0, ["AI伺服器", "AI晶片", "CoWoS", "HBM", "GB200", "Blackwell",
                      "液冷", "散熱模組", "算力", "NVIDIA", "AMD", "資料中心"]),
    "記憶體復甦": (3.5, ["DRAM", "HBM", "DDR5", "記憶體漲價", "記憶體需求", "出貨量增",
                          "庫存去化", "記憶體缺貨", "NAND", "南亞科", "華邦電", "力積電"]),
    "轉機":   (2.5, ["虧轉盈", "毛利率提升", "毛利改善", "獲利創高", "EPS 創新高",
                      "庫存回補", "去化完畢", "訂單回溫", "業績反轉"]),
    "股東回報": (2.0, ["買回庫藏股", "現金股利", "特別股息", "股票買回", "發放特別股利"]),
    "新產品": (2.5, ["新產品", "新技術", "突破", "首款", "量產", "通過認證", "導入客戶"]),
    "法說展望": (2.0, ["樂觀", "看好", "展望佳", "能見度高", "滿手訂單", "客戶回補"]),
}

NEGATIVE_KEYWORDS: list[str] = [
    "虧損擴大", "獲利衰退", "大幅下滑", "客戶砍單", "庫存去化緩慢",
    "財報重編", "掏空", "違約", "停工", "火災", "重大損失", "下修展望",
]


def keyword_score(announcements: list[dict], name: str = "") -> dict:
    """
    Phase 1：關鍵詞快速評分
    回傳: {
        "score": float (0~10),
        "tags": list[str],        # 命中的催化劑類別
        "matched": list[str],     # 命中的關鍵詞
        "negative": bool,         # 是否有負面訊號
        "source": "keyword"
    }
    """
    all_text = " ".join(
        f"{a.get('title', '')} {a.get('category', '')}"
        for a in announcements
    )

    tags: list[str] = []
    matched: list[str] = []
    total_score = 0.0

    for category, (base_score, keywords) in CATALYST_KEYWORDS.items():
        for kw in keywords:
            if kw in all_text:
                total_score += base_score
                tags.append(category)
                matched.append(kw)
                break  # 每個類別只加一次分

    # 負面訊號折扣
    negative_hit = any(kw in all_text for kw in NEGATIVE_KEYWORDS)
    if negative_hit:
        total_score *= 0.3

    # 公告數量加分（有更多公告代表公司活躍）
    total_score += min(len(announcements) * 0.2, 1.0)

    return {
        "score": round(min(total_score, 10.0), 2),
        "tags": list(set(tags)),
        "matched": list(set(matched)),
        "negative": negative_hit,
        "source": "keyword",
    }


def ai_score(
    code: str,
    name: str,
    announcements: list[dict],
    fundamentals: dict,
    api_key: Optional[str] = None,
    model: str = "claude-haiku-4-5-20251001",
    news: Optional[list[dict]] = None,
) -> Optional[dict]:
    """
    Phase 2：Claude API 深度語意分析
    回傳: {
        "score": float (0~10),
        "tags": list[str],
        "catalyst_summary": str,
        "risk": str,
        "horizon": str,   # "短期(1-3月)" / "中期(3-6月)" / "長期"
        "source": "claude"
    }
    失敗時回傳 None（由上層 fallback 到 keyword_score）
    """
    _api_key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
    if not _api_key:
        return None

    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic 套件未安裝，跳過 AI 分析（pip install anthropic）")
        return None

    if not announcements:
        return None

    # 組成輸入文字
    news_lines = "\n".join(
        f"- [{a.get('date', '')}] {a.get('title', '')}"
        for a in announcements[:20]  # 最多送 20 筆
    )
    fundamentals_text = (
        f"PE={fundamentals.get('pe', 'N/A')}, "
        f"PB={fundamentals.get('pb', 'N/A')}, "
        f"殖利率={fundamentals.get('yield_pct', 'N/A')}%"
    )

    # 鉅亨新聞（若有提供）
    news_section = ""
    if news:
        recent_titles = "\n".join(
            f"- {n.get('title', '')}"
            for n in news[:10]
            if n.get("title")
        )
        if recent_titles:
            news_section = f"\n近期市場新聞（鉅亨）：\n{recent_titles}\n"

    prompt = f"""你是台股催化劑分析師，請分析以下股票的近期公告：

股票：{name}（{code}）
基本面：{fundamentals_text}

近期重大訊息（最近 60 天）：
{news_lines}{news_section}

請判斷並以 JSON 回傳（不要有其他文字）：
{{
  "score": <0~10的浮點數，10=極強催化劑，0=無催化劑或負面>,
  "tags": [<催化劑類別，如"擴產","AI題材","轉機","訂單"等>],
  "catalyst_summary": "<一句話說明最主要的催化劑>",
  "risk": "<一句話說明最大風險>",
  "horizon": "<短期(1-3月)|中期(3-6月)|長期>"
}}

評分標準：
- 7-10：明確近期催化劑，例如 AI 訂單爆發、新廠量產、獲利大幅改善
- 4-6：有催化劑但兌現時間不確定，或只是產業受惠
- 1-3：僅有輕微正面消息或題材
- 0：無催化劑、消息負面、或公告太少無法判斷"""

    try:
        client = anthropic.Anthropic(api_key=_api_key)
        message = client.messages.create(
            model=model,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        # 清理可能的 markdown code block（容錯：不完整的 ``` 也能處理）
        if "```" in raw:
            parts = raw.split("```")
            raw = parts[1] if len(parts) >= 2 else raw.replace("```", "")
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        result = json.loads(raw)
        result["source"] = "claude"
        result["score"] = float(result.get("score", 0))
        return result
    except json.JSONDecodeError as e:
        logger.warning(f"{code} Claude 回傳非 JSON: {e}")
        return None
    except Exception as e:
        logger.warning(f"{code} Claude API 失敗: {e}")
        return None


def analyze(
    code: str,
    name: str,
    announcements: list[dict],
    fundamentals: dict,
    use_ai: bool = True,
    api_key: Optional[str] = None,
    news: Optional[list[dict]] = None,
) -> dict:
    """
    統一入口：優先用 AI，失敗時 fallback 到關鍵詞比對
    """
    result = None
    if use_ai:
        result = ai_score(code, name, announcements, fundamentals, api_key=api_key, news=news)

    if result is None:
        result = keyword_score(announcements, name)

    return result

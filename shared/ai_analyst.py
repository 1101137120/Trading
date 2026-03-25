"""
Claude AI 新聞情緒分析
用法: analyze_news(code, name, news_items) -> AnalysisResult
"""
import os
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class AnalysisResult:
    sentiment: str          # "正面" / "負面" / "中性"
    score: float            # -1.0 ~ 1.0
    summary: str            # 一句話摘要
    has_news: bool          # 是否有新聞可分析


_NO_NEWS = AnalysisResult(sentiment="中性", score=0.0, summary="無近期新聞", has_news=False)


def analyze_news(code: str, name: str, news_items: list[dict]) -> AnalysisResult:
    """
    呼叫 Claude API 分析新聞情緒。
    需要環境變數 ANTHROPIC_API_KEY。
    若 API key 未設定或新聞為空則回傳中性結果。
    """
    if not news_items:
        return _NO_NEWS

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.debug("ANTHROPIC_API_KEY 未設定，跳過 AI 分析")
        return _NO_NEWS

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
    except ImportError:
        logger.warning("anthropic 套件未安裝")
        return _NO_NEWS

    news_text = "\n".join(
        f"- {n['title']}" + (f"：{n['summary'][:80]}" if n.get("summary") else "")
        for n in news_items[:5]
    )

    prompt = f"""以下是台股 {code} {name} 的近期新聞標題與摘要：

{news_text}

請分析這些新聞對該股票短線（1-3天）的影響，回答格式如下（純文字，不加 markdown）：
情緒: 正面/負面/中性
分數: 數字（-1.0 到 1.0，正面為正，負面為負）
摘要: 一句話說明主要利多或利空因素

只回答這三行，不要其他說明。"""

    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        return _parse_response(msg.content[0].text)
    except Exception as e:
        logger.warning(f"AI 分析失敗 ({code}): {e}")
        return _NO_NEWS


def _parse_response(text: str) -> AnalysisResult:
    sentiment = "中性"
    score = 0.0
    summary = ""

    for line in text.strip().splitlines():
        line = line.strip()
        if line.startswith("情緒:"):
            sentiment = line.split(":", 1)[1].strip()
        elif line.startswith("分數:"):
            try:
                score = float(line.split(":", 1)[1].strip())
                score = max(-1.0, min(1.0, score))
            except ValueError:
                pass
        elif line.startswith("摘要:"):
            summary = line.split(":", 1)[1].strip()

    return AnalysisResult(sentiment=sentiment, score=score, summary=summary, has_news=True)

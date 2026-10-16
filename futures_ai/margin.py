"""Fetches the current TAIFEX margin requirement for the traded product (原始保證金)."""

from __future__ import annotations

import html
import re
import urllib.request

MARGIN_URL = "https://www.taifex.com.tw/cht/5/indexMarging"
PRODUCT_ZH = {"TMF": "微型臺指期貨", "MTX": "小型臺指期貨", "TXF": "臺股期貨"}


def fetch_margin(product: str) -> dict | None:
  """Returns {"settlement", "maintenance", "initial"} in NT$, or None if parsing fails."""
  req = urllib.request.Request(MARGIN_URL, headers={"User-Agent": "Mozilla/5.0 tw-futures-ai-trader"})
  with urllib.request.urlopen(req, timeout=15) as r:
    page = r.read().decode("utf-8", errors="ignore")
  name = PRODUCT_ZH.get(product, product)
  for m in re.finditer(re.escape(name), page):
    seg = html.unescape(re.sub(r"<[^>]+>", "|", page[m.start(): m.start() + 600]))
    nums = re.findall(r"\d{1,3}(?:,\d{3})+", seg)
    if len(nums) >= 3:
      a, b, c = (int(x.replace(",", "")) for x in nums[:3])
      return {"settlement": a, "maintenance": b, "initial": c}
  return None

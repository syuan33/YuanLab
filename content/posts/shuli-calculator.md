---
title: "81数理速查计算器"
date: 2026-10-23
draft: false
---

81数理（实为 80 数理，超过 80 则循环）源自姓名学，每个数字对应一种吉凶含义。
输入任意正整数，自动按 `(n - 1) % 80 + 1` 取余并显示对应数理。

{{< shuli-calculator >}}

数据来自 `scripts/shuli.py`，通过 `scripts/export_shuli.py` 导出为 `shuli.json`。

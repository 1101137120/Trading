---
name: feedback_autonomy
description: User preferences on autonomy and execution
type: feedback
---

不要自動執行 bash 命令（尤其是跑回測）。建議修改後讓使用者自己執行。只有在使用者明確說「幫我跑」或「執行」時才主動執行。

**Why:** 使用者想掌控執行時機，不希望 Claude 自作主張跑耗時指令。

**How to apply:** 程式碼或腳本修改完成後，告知使用者可以自己跑，不要自己 bash 執行。

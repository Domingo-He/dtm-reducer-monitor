"""「關於」頁的內容（單機儀表板與中央畫面共用）。"""
from __future__ import annotations

from . import __version__
from .i18n import _


def info() -> dict:
    return {
        "name": "DTM",
        "title": _("DTM：Fanuc 減速機干擾扭矩監控系統"),
        "version": __version__,
        "stage": _("Beta：判斷門檻待現場驗證"),
        "author": "Domingo He",
        "license": "Apache License 2.0",
        "items": [
            {"h": _("開發方式"), "t": _("我設計、AI 寫程式、我驗證。判斷邏輯、門檻與現場流程來自作者的維修經驗；"
                                    "程式碼由 AI（Anthropic Claude）依設計撰寫，作者負責審查、測試與現場判斷。")},
            {"h": _("授權"), "t": _("Apache License 2.0：可以自由使用、修改、再散布，請保留著作權與授權聲明（LICENSE、NOTICE）。"
                                  "轉載或改作請標示原作者 Domingo He 與專案出處。")},
            {"h": _("不擔保"), "t": _("本軟體依「現狀」提供，不附任何擔保。監控結果只是保養決策的參考；換件、停機與安全相關的判斷，"
                                   "請由合格人員依原廠手冊與現場狀況決定。")},
            {"h": _("專利"), "t": _("作者沒有申請專利，並以技術揭露文件公開方法細節。作者無法保證不涉及他人在各國的專利，"
                                  "使用前請自行確認。")},
            {"h": _("商標"), "t": _("FANUC 是 FANUC CORPORATION 的商標。本專案與 FANUC 沒有關係，未經其認可或贊助，"
                                  "提到型號只是為了說明相容的機型。")},
            {"h": _("維護"), "t": _("作者以個人身分、非商業方式維護，不販售、不提供付費支援。問題與建議請用 GitHub Issues。")},
            {"h": _("第三方元件"), "t": _("WinSW（MIT 授權），用於安裝 Windows 服務；html2canvas（MIT 授權），用於全頁截圖。"
                                     "備品價格的匯率資料來自 ExchangeRate-API（備援 currency-api）。")},
            {"h": _("資安"), "t": _("發現安全性問題請不要公開回報，依 SECURITY.md 私下通知作者。")},
        ],
    }

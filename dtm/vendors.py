"""服務商資訊（2.1）：「保養與備品」頁底部的原廠服務據點，以及貴廠自己的服務窗口。

- 原廠據點是各地 FANUC 公司在官網公開的聯絡資料（查詢日期見 CHECKED），依觀看者的畫面語言選預設地區：
  繁中 → 台灣、簡中 → 中國大陸、英文 → 美國、日文 → 日本、韓文 → 韓國、越南文 → 越南。
  外籍同仁用其他語言看畫面時，管理員可以在「設定 → 服務商」直接指定地區。
- 官網沒有公布 Email 與聯絡人；貴廠被指派的業務或服務工程師、客戶編號，請在「設定 → 服務商」填寫。
- FANUC 是 FANUC CORPORATION 的商標；本專案與 FANUC 沒有任何關係，這裡只列出公開的聯絡方式，
  不使用商標圖形。聯絡資料可能變更，請以官網為準。
"""
from __future__ import annotations

from .i18n import N_, _, get_lang

CHECKED = "2026-10-18"
LANG_REGION = {"zh-TW": "tw", "zh-CN": "cn", "en": "us", "ja": "jp", "ko": "kr", "vi": "vn"}
REGIONS = ("tw", "cn", "us", "jp", "kr", "vn")
REGION_NAMES = {"tw": N_("台灣"), "cn": N_("中國大陸"), "us": N_("美國"), "jp": N_("日本"), "kr": N_("韓國"),
                "vn": N_("越南")}

# 電話的 label 是 msgid（畫面依語言顯示）；公司名稱、地址照各地官網原文（addr）與英文（addr_en），
# 官網只有英文地址的據點 addr 留空，不自行翻譯
VENDORS: dict[str, dict] = {
    "tw": {
        "company": "台灣發那科股份有限公司", "company_en": "TAIWAN FANUC CORPORATION",
        "website": "https://www.fanuc.tw/",
        "sources": ["https://www.fanuc.tw/service.php", "https://www.fanuc.co.jp/en/service/asia/taiwan.html"],
        "offices": [
            {"name": N_("技術中心（營業部）・台中"), "addr": "407-68 台中市台中工業區16路10號",
             "addr_en": "No.10, 16th Road, Taichung Industrial Park, Taichung 40768, Taiwan",
             "phones": [(N_("機器人專線（台中）"), "04-2359-2827"), (N_("總機"), "04-2359-0522")],
             "fax": "04-2359-0771"},
            {"name": N_("台北分公司"), "addr": "",
             "addr_en": "16F-7, No.7, Sec.3, New Taipei Boulevard, Xinzhuang Dist., New Taipei City 24250, Taiwan",
             "phones": [(N_("電話"), "02-8522-7076")], "fax": "02-8522-8288"},
            {"name": N_("台南分公司"), "addr": "",
             "addr_en": "6F-1, No.30, Zhongzheng S. Rd., Yongkang Dist., Tainan City 71067, Taiwan",
             "phones": [(N_("電話"), "06-282-3161")], "fax": "06-282-3162"},
        ],
    },
    "cn": {
        "company": "上海发那科机器人有限公司", "company_en": "SHANGHAI-FANUC Robotics CO., LTD.",
        "website": "https://www.shanghai-fanuc.com.cn/",
        "sources": ["https://www.shanghai-fanuc.com.cn/", "https://www.fanuc.co.jp/en/service/asia/shanghai.html"],
        "offices": [
            {"name": N_("全國服務熱線（24 小時）"), "addr": "", "addr_en": "",
             "phones": [(N_("服務熱線"), "400-820-0001")], "fax": ""},
            {"name": N_("上海總部"), "addr": "",
             "addr_en": "No.1500 Fulian Road, Baoshan Area, Shanghai 201906, P.R. China",
             "phones": [(N_("電話"), "021-5032-7700")], "fax": "021-5032-7711"},
        ],
    },
    "us": {
        "company": "FANUC America Corporation", "company_en": "FANUC America Corporation",
        "website": "https://www.fanucamerica.com/",
        "sources": ["https://www.fanucamerica.com/support/robot/field-service/usa-locations/michigan-headquarters"],
        "offices": [
            {"name": N_("美洲總部"), "addr": "3900 W. Hamlin Road, Rochester Hills, MI 48309, USA",
             "addr_en": "3900 W. Hamlin Road, Rochester Hills, MI 48309, USA",
             "phones": [(N_("電話"), "888-326-8287 (888-FANUC-US)")], "fax": ""},
        ],
    },
    "jp": {
        "company": "ファナック株式会社", "company_en": "FANUC CORPORATION",
        "website": "https://www.fanuc.co.jp/",
        "sources": ["https://www.fanuc.co.jp/ja/service/domestic.html"],
        "offices": [
            {"name": N_("機器人保養窗口（日本國內）"), "addr": "〒401-0597 山梨県南都留郡忍野村忍草3580（本社）",
             "addr_en": "3580 Shibokusa, Oshino-mura, Minamitsuru-gun, Yamanashi 401-0597, Japan (head office)",
             "phones": [(N_("電話"), "0120-240-613")], "fax": "0120-240-673"},
        ],
    },
    "kr": {
        "company": "KOREA FANUC SERVICE CORPORATION", "company_en": "KOREA FANUC SERVICE CORPORATION",
        "website": "https://www.fanuc.co.jp/en/service/asia/koreaservice.html",
        "sources": ["https://www.fanuc.co.jp/en/service/asia/koreaservice.html"],
        "offices": [
            {"name": N_("昌原總部"), "addr": "",
             "addr_en": "188, Sahwa-ro, Uichang-gu, Changwon-si, Gyeongsangnam-do 51390, Republic of Korea",
             "phones": [(N_("電話"), "055-282-0122")], "fax": "055-261-6958"},
            {"name": N_("天安分公司"), "addr": "",
             "addr_en": "33, 4sandan 5-gil, Jiksan-eup, Seobuk-gu, Cheonan-si, Chungcheongnam-do 31040, Republic of Korea",
             "phones": [(N_("電話"), "041-582-0122")], "fax": "041-584-0817"},
        ],
    },
    "vn": {
        "company": "Công ty TNHH FANUC Việt Nam", "company_en": "FANUC VIETNAM COMPANY LIMITED",
        "website": "https://www.fanuc.com/fvl/",
        "sources": ["https://www.fanuc.co.jp/en/service/asia/vietnam.html"],
        "offices": [
            {"name": N_("胡志明市總部"), "addr": "",
             "addr_en": "Lot A3b, Road No. 19D and 20A, E-Office Park, Tan Thuan Export Processing Zone, "
                        "Tan Thuan Ward, District 7, Ho Chi Minh City, Vietnam",
             "phones": [(N_("電話"), "028-7309-7970"), (N_("電話"), "028-7309-7971")], "fax": "028-7309-7950"},
            {"name": N_("河內分公司"), "addr": "",
             "addr_en": "Rm No.210 Techno Center, Thang Long Industrial Park, Thien Loc Commune, Hanoi City, Vietnam",
             "phones": [(N_("電話"), "028-7309-7970")], "fax": "028-7309-7950"},
        ],
    },
}
VENDORS_INTL = {"tw": "+886", "cn": "+86", "us": "+1", "jp": "+81", "kr": "+82", "vn": "+84"}


def region_for(cfg: dict, lang: str | None = None) -> str:
    r = str((cfg.get("service") or {}).get("region") or "auto")
    if r in VENDORS:
        return r
    return LANG_REGION.get(lang or get_lang(), "tw")


def _entry(key: str) -> dict:
    v = VENDORS[key]
    return {"key": key, "region": _(REGION_NAMES[key]), "company": v["company"], "company_en": v["company_en"],
            "website": v["website"], "sources": v["sources"], "intl": VENDORS_INTL.get(key, ""),
            "offices": [{"name": _(o["name"]), "addr": o["addr"], "addr_en": o["addr_en"], "fax": o["fax"],
                         "phones": [{"label": _(lb), "number": num} for lb, num in o["phones"]]} for o in v["offices"]]}


def view(cfg: dict, lang: str | None = None) -> dict:
    """給「保養與備品」頁：目前地區的原廠據點、可切換的其他地區、貴廠窗口。"""
    s = cfg.get("service") or {}
    cur = region_for(cfg, lang)
    contact = {k: str(s.get(k) or "") for k in ("contact_name", "contact_phone", "contact_email", "customer_no", "note")}
    return {"region": cur, "pinned": str(s.get("region") or "auto") != "auto", "checked": CHECKED,
            "entries": [_entry(k) for k in REGIONS], "contact": contact, "has_contact": any(contact.values()),
            "note": _("FANUC 是 FANUC CORPORATION 的商標；本系統與 FANUC 沒有任何關係。以下是各地官網公開的聯絡資料"
                      "（查詢日期 {d}），可能變更，請以官網為準。", d=CHECKED)}

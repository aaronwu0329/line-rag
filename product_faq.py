# -*- coding: utf-8 -*-
import re
import difflib

PRODUCT_CATALOG = {
    "ibm": {
        "aliases": ["ibm", "ibm watson", "watsonx", "ibm.watson", "ibm.watson.ai",
                    "watsonx.ai", "watsonx.data", "watsonx.governance"],
        "items": ["watsonx.ai", "watsonx.data", "watsonx.governance","turbonomic,","Instana","IBM Vault","IBM Terraform","IBM Guardium","IBM Aspera"],
        "blurb": "🧠 IBM：企業級 AI 與資料平台，涵蓋基礎模型、湖倉與 AI 治理，支援導入與維運。"
    },
    "paloalto": {
        "aliases": ["paloalto", "palo alto", "palo alto networks", "pan", "prisma", "cortex", "strata"],
        "items": ["Strata NGFW", "Prisma (SASE/Cloud)", "Cortex (XDR/XSOAR)"],
        "blurb": "🛡️ Palo Alto Networks：次世代防火牆、SASE 與 XDR/SOAR，提供端到端雲地資安。"
    },
    "qlik": {
        "aliases": ["qlik", "qlik sense", "qlik cloud", "qlikview"],
        "items": ["Qlik Sense", "Qlik Cloud", "Replicate/Compose"],
        "blurb": "📊 Qlik：自助式分析＋資料整合，從資料取用到互動儀表板的一站式方案。"
    },
    "splunk": {
        "aliases": ["splunk", "splunk enterprise", "splunk cloud", "observability", "soar"],
        "items": ["Splunk Enterprise/Cloud", "Observability", "SOAR/SIEM"],
        "blurb": "🔭 Splunk：可觀測性與安全分析平台，支援 SIEM/SOAR 與雲端/在地部署。"
    },
    "suse": {
        "aliases": ["suse", "sles", "suse linux", "rancher"],
        "items": ["SUSE Linux Enterprise Server (SLES)", "Rancher"],
        "blurb": "🦎 SUSE：企業 Linux 與 Rancher 容器管理，覆蓋叢集與邊緣場景。"
    },
    "synopsys": {
        "aliases": ["synopsys", "coverity", "black duck", "bsimm"],
        "items": ["Coverity (SAST)", "Black Duck (OSS治理)", "BSIMM"],
        "blurb": "🔒 Synopsys：應用程式安全與開源治理（SAST／OSS 掃描）全流程落地。"
    },
    "tibco": {
        "aliases": ["tibco", "spotfire", "tibco ebx", "tdv", "tibco data virtualization"],
        "items": ["Spotfire", "EBX", "Data Virtualization (TDV)"],
        "blurb": "🔗 TIBCO：分析、主數據與資料虛擬化，靈活整合異質資料。"
    },
    "mongodb": {
        "aliases": ["mongodb", "mongo db", "mongo", "mongodb atlas", "atlas"],
        "items": ["MongoDB Atlas (雲端資料庫服務)", "MongoDB Enterprise", "Compass (GUI 工具)", "Atlas Search", "Atlas Charts"],
        "blurb": "🍃 MongoDB：文件型資料庫與 Atlas 雲端服務，快速開發、彈性擴展。"
    },
    "cloudera": {
        "aliases": ["cloudera", "cdp", "cloudera data platform", "cloudera manager",
                    "cloudera machine learning", "cml", "cdh", "hortonworks", "克勞德拉"],
        "items": ["Cloudera Data Platform (CDP)",
                  "Cloudera Manager",
                  "Cloudera Machine Learning (CML)",
                  "Cloudera DataFlow (NiFi/ETL)",
                  "Cloudera Data Engineering / Data Warehouse"],
        "blurb": "☁️ Cloudera：企業級大數據平台，整合治理、湖倉、串流與機器學習。"
    },
    "cloudcasa": {
        "aliases": ["cloudcasa", "cloud casa", "catalogic cloudcasa", "雲端casa", "雲端備份"],
        "items": ["CloudCasa SaaS 備份服務", "Kubernetes 多雲備份與復原", "跨叢集/跨區域資料保護", "應用一致性快照"],
        "blurb": "💾 CloudCasa：Kubernetes 專用雲端備份與復原服務，支援多雲、多叢集資料保護。"
    },
    "sas": {
        "aliases": ["sas", "sas 9", "sas viya", "統計分析系統"],
        "items": [
            "SAS 9（資料分析與管理套件）",
            "SAS Viya（雲端分析平台）",
            "高效能統計建模",
            "預測分析與商業智慧"
        ],
        "blurb": "📈 SAS：全球領先的統計分析與商業智慧平台，涵蓋資料管理、預測分析與決策支援。"
    },
    
}



# === 伺服器型號清單 ===
SERVER_MODEL_DB = {
    "ibm_power": {
        "aliases": [
            "ibm power", "power", "power10", "power 系列", "power server", "ibm power server",
            "power 伺服器", "power 服務器",
            "ibm 伺服器", "ibm 服務器", "ibm server", "ibm servers", "ibm 的 伺服器"
        ],
        "label": "IBM Power（Power10）",
        "models": ["S1012", "S1014", "S1022", "S1022s", "L1022", "S1024", "L1024", "E1050", "E1080"]
    }
}

# 快速查 alias → key
_ALIAS_TO_KEY = {}
for k, v in PRODUCT_CATALOG.items():
    for a in v["aliases"]:
        _ALIAS_TO_KEY[a.lower()] = k

_ALLOWED_BRANDS = list(PRODUCT_CATALOG.keys())

# ---- 伺服器 alias 索引 ----
_SERVER_ALIAS_TO_KEY = {}
for k, v in SERVER_MODEL_DB.items():
    for a in v["aliases"]:
        _SERVER_ALIAS_TO_KEY[a.lower()] = k

def _normalize_text(q: str) -> str:
    q = (q or "").strip()
    q = re.sub(r"[./\\_]+", " ", q)
    q = re.sub(r"\s{2,}", " ", q)
    # ⬇️ 新增：移除句尾常見標點（含全形）
    q = re.sub(r"[\s\?？!！。．、，~～…]+$", "", q)
    return q


# --- Intent X: 功能介紹 --- 
def _is_function_intent(q: str) -> bool: 
    qn = _normalize_text(q).lower()
    return any([
        # 中文常見問法（正式＋口語）
        re.search(r"(你|你們|這個|這套|此系統).*(有什麼|有哪些).*(功能|服務)", qn),
        re.search(r"(你|你們|這個|這套|此系統).*(能|可以|會|支援|提供).*(做|幹嘛|提供|支援|處理|完成)", qn),
        re.search(r"(功能|服務|能力|用途)(有(哪些|什麼)|是什麼|包含什麼)", qn),
        re.search(r"(能做什麼|可以做什麼|你會什麼|能幹嘛|你可以幹什麼)", qn),
        re.search(r"(介紹|說明).*(功能|服務|用途)", qn),
        re.search(r"(支援|支援哪些).*(功能|服務|語言|平台)", qn),
        re.search(r"(可以|能不能|可不可以).*(幫我|協助|做到|完成)", qn),

        # 英文變體
        re.search(r"(what\s+can\s+(you|this|it)\s+do)", qn),
        re.search(r"(what\s+do\s+you\s+do|what\s+does\s+(your\s+bot|this)\s+do)", qn),
        re.search(r"(capabilit(y|ies)|features?)", qn),
        re.search(r"(supported\s*(features?|capabilities?)|support\s+(what|which))", qn),
        re.search(r"(can\s+you\s+(help|do|provide))", qn),
    ])

def _function_sentence() -> str:
    return (
        "我可以讓你快速了解 Palsys 代理的產品，"
        "包含 IBM、Palo Alto Networks、Qlik、Splunk、SUSE、Synopsys、TIBCO、MongoDB、Cloudera、Cloudcasa、SAS、INSTANA"
        
    )
def _is_company_intro_intent(q: str) -> bool:
    qn = _normalize_text(q).lower()
    return any([
    # 中文正式說法
    re.search(r"(關於你們|關於我們|公司介紹|企業介紹|品牌介紹|公司簡介|企業簡介|品牌簡介|更多資訊|更多資料|朋昶數位科技|朋昶數位|朋昶|Palsys)", qn),
    re.search(r"(想|希望|可以|是否).*(更|多|想要).*(了解|認識).*(你們|貴公司|公司|品牌|朋昶數位|朋昶數位科技|Palsys)", qn),

    # 中文口語問法
    re.search(r"(介紹|簡介).*(你們公司|貴公司|公司|品牌|朋昶數位|朋昶數位科技|朋昶|Palsys)", qn),
    re.search(r"(你們|貴公司|公司|品牌|朋昶數位|朋昶數位科技|朋昶|Palsys).*(是做什麼|是什麼|幹嘛|在做什麼)", qn),
    re.search(r"(你們公司|貴公司|公司|品牌|朋昶數位|朋昶數位科技|Palsys).*(介紹一下|簡單介紹|簡介一下|說明一下|講一下)", qn),
    re.search(r"(想要|可以|能不能)?.*(知道|了解|認識).*(你們公司|公司|品牌|朋昶數位|朋昶數位科技|Palsys)", qn),
    re.search(r"(朋昶|palsys)\s*(公司)?\s*(介紹|簡介)", qn),

    # 英文
    re.search(r"(learn\s*more|about\s*(you|us|company|palsys)|tell\s*me\s*(more|about.*company))", qn),
    re.search(r"(introduce|introduction|overview|summary).*(company|your company|about you|palsys)", qn),
    re.search(r"(what does|what is).*(your company|the company|palsys)", qn),
    ])

def _company_intro_sentence() -> str:
    return (
        "🏢 公司介紹\n\n"
        "🔹朋昶數位科技專注於資訊產品代理與整合服務，致力於引進資安、數據、軟體、雲端等解決方案。\n\n"
        
        "🔹我們為企業客戶提供專業且高效的產品與技術支援，並與眾多國際品牌建立穩固合作關係。\n\n"
        
        "🔹累積深厚的產業經驗，協助客戶掌握科技脈動、提升競爭力。\n\n"
        
        "🌐 Palsys 官方網站\n https://www.palsys.com.tw/"
    )

def _is_official_site_intent(q: str) -> bool:
    qn = _normalize_text(q).lower()
    return any([
        re.search(r"(官網|官方網站|公司網站|企業官網|品牌官網)", qn),
        re.search(r"(網址|連結|網站連結|官網連結)", qn),
        re.search(r"(有沒有|在哪|提供).*(網址|官網|網站|連結)", qn),
        re.search(r"(official\s*(site|website)|company\s*(site|website)|web\s*page)", qn),
        re.search(r"(where).*(website|site|link)", qn),
    ])
def _official_site_sentence() -> str:
    return "Palsys 官網： https://www.palsys.com.tw/"


# --- Intent 0: 問伺服器型號 ---
def _is_server_model_intent(q: str) -> bool:
    qn = _normalize_text(q).lower()
    has_server = re.search(r"(伺服器|服務器|server)", qn) is not None
    has_model  = re.search(r"(型號|機型|機種|機型有哪些|型號有哪些)", qn) is not None
    has_what_models = re.search(r"(有什麼|有哪些).*(型號|機型|機種)", qn) is not None
    our_models = re.search(r"(你們|貴公司|貴司).*(伺服器|服務器|server).*(型號|機型|機種)", qn) is not None
    brand_server = ("ibm" in qn) and has_server
    our_models_no_server = re.search(r"(你們|貴公司|貴司).*(機種|型號)", qn) is not None
    ask_server_general = has_server and re.search(r"(有什麼|有哪些|介紹|簡介|說明)", qn) is not None
    return (
        (has_server and has_model) or
        has_what_models or
        our_models or
        brand_server or
        our_models_no_server or
        ask_server_general
    )

def _detect_server_brand_key(raw_q: str) -> str | None:
    raw = (raw_q or "").lower()
    q = _normalize_text(raw_q).lower()
    for key, data in SERVER_MODEL_DB.items():
        for alias in data["aliases"]:
            if alias.lower() in raw or alias.lower() in q:
                return key
    if ("ibm" in raw or "ibm" in q) and re.search(r"(伺服器|服務器|server)", q):
        return "ibm_power"
    if re.search(r"(你們|貴公司|貴司)", q) and re.search(r"(伺服器|服務器|server)", q):
        return "ibm_power"
    if re.search(r"(你們|貴公司|貴司).*(機種|型號)", q):
        return "ibm_power"
    if re.search(r"(伺服器|服務器|server)", q):
        return "ibm_power"
    for token in q.split():
        if token in _SERVER_ALIAS_TO_KEY:
            return _SERVER_ALIAS_TO_KEY[token]
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9.+-]*", q)
    for w in words:
        near = difflib.get_close_matches(w, list(_SERVER_ALIAS_TO_KEY.keys()), n=1, cutoff=0.75)
        if near:
            return _SERVER_ALIAS_TO_KEY[near[0]]
    return None

def _server_model_sentence(skey: str) -> str | None:
    data = SERVER_MODEL_DB.get(skey)
    if not data:
        return None
    label = data.get("label", "伺服器")
    models = data.get("models", [])
    if not models:
        return None
    return f"{label} 目前常見伺服器型號包含：" + "、".join(models) + "。若需要更細的型號規格，請直接指名型號，我會再為你查詢。"

# --- Intent 1: 公司總表 ---
def _is_company_product_list_intent(q: str) -> bool:
    q = _normalize_text(q)

    patterns = [
        # 你們/貴公司 有哪些 品牌/廠商/夥伴/合作夥伴（「有哪些」在前）
        r"(你們|貴公司|公司|貴司).*(有哪些|有什麼).*(品牌|廠商|廠牌|供應商|合作廠商|夥伴|合作夥伴|合作伙伴|vendor|vendors|partner|partners?)",

        # ✅ 專門處理「合作夥伴有哪些」這種語序
        r"(合作夥伴|合作伙伴).*(有哪些|有什麼)",

        # 你們 代理/經銷/合作（在前） 哪些/什麼/誰（在後）
        r"(你們|貴公司|公司|貴司).*(代理|經銷|合作|夥伴|合作夥伴|合作伙伴|represent|carry).*(哪些|什麼|誰)",

        # 反向語序：哪些/什麼 在前，後面接 合作/代理 關鍵詞
        r"(你們|貴公司|公司|貴司).*(哪些|什麼).*(代理|經銷|合作|夥伴|合作夥伴|合作伙伴)",

        # 問名單/清單/總覽
        r"(品牌|廠商|供應商|合作廠商|夥伴|合作夥伴|合作伙伴|vendor|vendors|partner|partners?).*(名單|清單|總覽|一覽|有哪些|有誰|list)",

        # 英文
        r"(what|which)\s+(brands?|vendors?|partners?)\s+(do\s+you\s+(have|carry|work\s+with|represent))",
        r"(do\s+you\s+(have|carry|represent|work\s+with))\s+(brands?|vendors?|partners?)",
        r"(brands?|vendors?|partners?)\s*(list|overview)?$",
    ]
    return any(re.search(p, q, flags=re.IGNORECASE) for p in patterns)


def _company_product_list_sentence() -> str:
    brands = ["IBM","Palo Alto Networks","Qlik","Splunk","SUSE","Synopsys","TIBCO","MongoDB","Cloudera","Cloudcasa","SAS","INSTANA"]
    return "我們目前代理的主要產品包含：" + "、".join(brands) + "。"

# --- Intent 1.5: 產品快速介紹 / 一覽清單 ---
_BRIEF_OVERVIEW_PATTERNS = [
    # ✅ 直白問法：你/你們 有哪些/那些/什麼 產品 → 觸發產品總覽/清單
    r"^(你|你們|貴公司|公司|貴司)\s*有\s*什麼\s*(產品|產品線|產品項目|產品服務)\s*[嗎呢]?$",
    r"^(你|你們|貴公司|公司|貴司)(的)?\s*有?\s*([哪那]些|什麼)\s*(產品|產品線|產品項目|產品服務)\s*[嗎呢]?$",
    r"^(你|你們|貴公司|公司|貴司)\s*(產品|產品線|產品項目|產品服務)\s*([哪那]些|有哪些|有什麼)\s*[嗎呢]?$",
    r"^有\s*([哪那]些|什麼)\s*(產品|產品線|產品項目|產品服務)\s*[嗎呢]?$",
    r"^(可以|能|能否)?\s*(列出|給我|看看)\s*(你|你們|貴公司|公司|貴司)(的)?\s*(產品|產品線|產品項目|產品服務)\s*(清單|列表)?\s*[嗎呢]?$",
    r"^(what|which)\s+(products?|product\s*lines?)\s+(do\s+you\s+have)\s*\??$",

    # 🆕 ✅ 新增：詢問「服務項目」也當作產品／方案總覽
    r"(你們|貴公司|公司|貴司).*(提供|有).*(哪些|那些|什麼).*(服務項目|服務內容|服務|solutions?)",
    r"^(貴公司|你們|公司|貴司)\s*(提供|有)\s*(哪些|那些|什麼)\s*(服務項目|服務內容|服務)\s*[嗎呢]?$",
    r"^你們\s*(提供|有)\s*(哪些|那些|什麼)\s*(服務項目|服務內容|服務)\s*[嗎呢]?$",


    # === Solutions / 解決方案 ===
    r"(你們|貴公司|公司|貴司).*(提供|有).*(哪些|那些|什麼).*(解決方案|方案)",
    r"(解決方案|方案).*(總覽|一覽|清單|目錄|概覽|概要|overview|summary|high[- ]?level|brief|快速|簡短|一句|重點)",
    r"(solutions?)\s*(overview|summary|high[- ]?level|brief)",
    r"(what\s+(solutions?|offerings?)\s+do\s+you\s+have)",

    # ✅ 指名品牌（朋昶 / Palsys）
    r"(朋昶|palsys)\s*(提供|有).*(哪些|那些|什麼).*(解決方案|方案)",
    r"(朋昶|palsys).*(有哪些|有什麼).*(解決方案|方案)",
    r"^(提供|有).*(哪些|那些|什麼).*(解決方案|方案)$",

    # ✅ 雲端解決方案
    r"(你們|貴公司|公司|貴司).*(提供|有).*(哪些|那些|什麼).*(雲端解決方案)",
    r"(朋昶|palsys).*(提供|有).*(哪些|那些|什麼).*(雲端解決方案)",
    r"(公司).*(提供).*(哪些|那些|什麼).*(雲端解決方案)",

    # === Products / 產品（一般問法）===
    r"(你們|貴公司|公司|貴司).*(提供|有).*(哪些|那些|什麼).*(產品|產品線|產品項目|產品服務)",
    r"(朋昶|palsys)\s*(提供|有).*(哪些|那些|什麼).*(產品|產品線|產品項目|產品服務)",
    r"(朋昶|palsys).*(有哪些|有什麼).*(產品|產品線|產品項目|產品服務)",
    r"^(提供|有).*(哪些|那些|什麼).*(產品|產品線|產品項目|產品服務)$",
    r"(what\s+products?\s+do\s+you\s+have)",

    # 🆕 === 代理產品 / 代理清單 ===
    r"(你們|貴公司|公司|貴司).*(代理|經銷).*(哪些|那些|什麼).*(產品|品牌|清單)",
    r"(代理|經銷).*(產品|品牌).*(清單|列表|總覽|有哪些|有什麼)",
    r"(朋昶|palsys).*(代理|經銷).*(哪些|那些|什麼).*(產品|品牌|清單)",
    r"(代理產品|代理品牌|代理清單)",

    # === 產品快速介紹 / 簡介（保留）===
    r"^產品\s*(快速)?\s*(介紹|簡介)$",
    r"(產品)\s*(快速)\s*(介紹|簡介)",
    r"(產品)\s*(簡介|介紹)\s*(快速|重點|摘要|brief|high[- ]?level)?",

    # 原本的（保留）
    r"(幫我|請).*分(類|門別類).*(產品)",
    r"(分(類|門別類)|整理).*產品.*(介紹|簡介|說明)",
    r"(快速|簡短|一句|摘要).*產品.*(介紹|簡介)",
    r"(八|8).*大項.*(產品|分類)",
    r"(總覽|overview).*(產品)",
    r"(各自|每個|每一個).*(產品|品牌).*(介紹|簡介|一句話|說明)",
    r"(各自|分別).*(幫我|請你)?.*(介紹|簡介|說明).*(產品|品牌)",
    r"(幫我|請|可以|能|可否).*(介紹|簡介|說明)(一下|下)?(你們|貴公司|貴司)?.*(產品|分類).*(簡短|快速|重點|重點版)?",
    r"(產品|分類).*(簡介|介紹|說明).*(快速|重點|一句話|高層次|high[- ]?level|brief)",
    r"(總覽|一覽|一覽表|一頁看懂|總表|目錄|catalog).*(產品|方案|分類)",
    r"(快速|高層次|概觀|概覽|鳥瞰|摘要|summary|overview).*(產品|方案|分類)",
    r"(八大|8大|八項|8項).*(介紹|總覽|簡介)",
    r"(give|need|want).*(a )?(brief|short|high[- ]?level|concise).*(overview|summary).*(products?)",
    r"(products?|portfolio).*(overview|summary|high[- ]?level|brief)",
    r"(介紹|簡介|說明).*(各項|所有|全部)?(產品|品牌)",
    r"^(介紹|簡介|說明)[^\n]{0,20}?(產品|品牌)",

    # ✅ 口語：產品一覽 / 清單 / 列表 / 目錄 / 菜單
    r"(產品|品牌).*(一覽|列表|清單|目錄|菜單|大全|總覽)",
    r"(一覽|列表|清單|目錄|菜單|catalog|menu).*(產品|品牌)",
    r"(全部|所有|一次|一口氣).*(看|列出|給我).*(產品|清單|列表|目錄)",

    # ✅ 口語：你們賣什麼 / 有哪些東西
    r"(你們|貴公司|公司|貴司)?.*(有賣什麼|賣什麼|都賣什麼|有什麼在賣)",
    r"(你們|貴公司|公司|品牌).*(有哪些|有什麼).*(東西|產品|品牌)",

    # ✅ 一頁看完／快速看
    r"(一頁|一張表|快速).*(看|看完|看懂).*(產品|品牌|清單|總覽)",

    # ✅ 英文口語
    r"(product|products?|line[-\s]?up|lineup|catalog|menu|list)(\s*(overview|summary|all))?",
    r"(show|see|give me|list)\s+(all\s+)?(products?|brands?)",
    r"(what\s+do\s+you\s+sell|what\s+products\s+do\s+you\s+have)",

    # ✅ 明確需求：幫我分類不同產品 與/愈 產品介紹
    r"幫我分類(不同)?(產品|方案).*(與|愈).*(產品|方案)?(介紹|簡介)",
]


def _is_brief_overview_intent(q: str) -> bool:
    q = _normalize_text(q).lower()
    return any(re.search(p, q) for p in _BRIEF_OVERVIEW_PATTERNS)

def _brand_display_name(key: str) -> str:
    return {
        "ibm": "IBM",
        "paloalto": "Palo Alto Networks",
        "qlik": "Qlik",
        "splunk": "Splunk",
        "suse": "SUSE",
        "synopsys": "Synopsys",
        "tibco": "TIBCO",
        "mongodb": "MongoDB",
        "cloudera": "CLOUDERA",
        "cloudcasa": "Cloudcasa",
        "sas": "SAS",
        "instana": "IBM INSTANA"
    }.get(key.lower(), key)


def _company_brief_overview_sentence() -> str:
    order = ["ibm","paloalto","qlik","splunk","suse","synopsys","tibco","mongodb","cloudera","cloudcasa","sas"]
    rows = ["📦 產品總覽\n"]

    for k in order:
        blurb = (PRODUCT_CATALOG.get(k, {}) or {}).get("blurb", "")
        if "：" in blurb:
            head, tail = blurb.split("：", 1)
            emoji = head[0] if not re.match(r"[A-Za-z0-9]", head[0]) else "•"
            rows.append(f"{emoji} {_brand_display_name(k)}")
            rows.append(f"  └ {tail.strip().rstrip('。')}。")
        else:
            rows.append(f"• {_brand_display_name(k)}")
            rows.append("  └ 主要方案與服務。")
        rows.append("")

    rows.append("👉 想看某一品牌細項，可直接問：例如「Qlik 有哪些產品？」")
    return "\n".join(rows)


# --- Intent 2: 品牌細項 ---
_BRAND_INV_PATTERNS = [
    # === 中文常見問法 ===
    r"(有|提供).*(哪些|什麼).*(產品|產品線|方案|服務|軟體|工具)",
    r"(有哪些|有什麼).*(產品|產品線|方案|服務|軟體|工具|)",
    r"(產品|產品線|方案|服務|軟體|工具).*(有哪些|有什麼|推薦|清單)",
    r"(能不能|可以).*(介紹|說明).*(產品|產品線|方案|軟體|工具)",
    r"(我想|請).*(了解|知道|看看).*(產品|產品線|軟體|方案)",
    r"(貴公司|你們家).*(主打|代理|經銷|販售).*(哪些|什麼).*(產品|產品線|軟體|工具)",

    # === 英文問法 ===
    r"(what).*(products?|solutions?|softwares?|tools?|product\s*lines?)",
    r"(could|can).*(you|u).*(introduce|show|tell me about).*(products?|solutions?|softwares?)",
]


def _detect_brand_key(raw_q: str) -> str | None:
    raw = (raw_q or "").lower()
    q = _normalize_text(raw_q).lower()
    for key, data in PRODUCT_CATALOG.items():
        for alias in data["aliases"]:
            if alias.lower() in raw or alias.lower() in q:
                return key.lower()
    for token in q.split():
        if token in _ALIAS_TO_KEY:
            return _ALIAS_TO_KEY[token].lower()
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9.+-]*", q)
    for w in words:
        near = difflib.get_close_matches(w, list(_ALIAS_TO_KEY.keys()), n=1, cutoff=0.75)
        if near:
            return _ALIAS_TO_KEY[near[0]].lower()
    return None


def _is_brand_inventory_intent(q: str) -> bool:
    q = _normalize_text(q).lower()
    return any(re.search(p, q) for p in _BRAND_INV_PATTERNS)

def _brand_inventory_sentence(key: str) -> str | None:
    items = PRODUCT_CATALOG.get(key, {}).get("items", [])
    brand_name = _brand_display_name(key)
    if items:
        return f"{brand_name} 主要方案包含：" + "、".join(items) + "。"
    return None

# --- 主流程 ---
def try_company_or_brand_list(user_query: str) -> str | None:
     # -3) 官網詢問
    if _is_official_site_intent(user_query):
        return _official_site_sentence()
    
    # 1) 8 大項快速介紹
    if _is_brief_overview_intent(user_query):
        return _company_brief_overview_sentence()


    # -2) 公司介紹（優先度最高，先判斷）
    if _is_company_intro_intent(user_query):
        return _company_intro_sentence()

    # -1) 功能介紹
    if _is_function_intent(user_query):
        return _function_sentence()

    # 0) 伺服器型號
    if _is_server_model_intent(user_query):
        skey = _detect_server_brand_key(user_query)
        if skey:
            return _server_model_sentence(skey)
        return None


    # 2) 品牌細項
    key = _detect_brand_key(user_query)
    if key and _is_brand_inventory_intent(user_query):
        sent = _brand_inventory_sentence(key)
        if sent:
            return sent
        blurb = PRODUCT_CATALOG.get(key, {}).get("blurb")
        if blurb:
            return blurb

    # 3) 公司總表
    if _is_company_product_list_intent(user_query):
        return _company_product_list_sentence()

    # 4) 其餘交給 RAG
    return None

# --- 弱查詢補齊 ---
def autocomplete_weak_query(user_query: str) -> str:
    q = _normalize_text(user_query)
    if len(q) <= 4 or len(q.split()) <= 2:
        terms = re.findall(r"[A-Za-z][A-Za-z0-9.+-]*", q)
        base = " ".join(terms) if terms else q
        return f"{base} 產品 方案 是什麼 有哪些"
    return q

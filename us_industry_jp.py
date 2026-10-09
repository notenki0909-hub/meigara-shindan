# -*- coding: utf-8 -*-
"""
米国株の個別レポートに表示する、yfinance(Yahoo Finance)の細かい業種名（industry）の日本語対応表。
analyze_us._industry_label() から遅延importで使う（このファイルが無い・壊れていても、英語のまま表示されるだけ）。
表示専用。採点・分類（GICSセクター）には使わない。対応表に無い業種は、英語のまま表示する。
"""
INDUSTRY_JP = {
    # 素材
    "Agricultural Inputs": "農業資材（肥料など）", "Aluminum": "アルミニウム", "Building Materials": "建材",
    "Chemicals": "化学", "Coking Coal": "原料炭", "Copper": "銅", "Gold": "金", "Lumber & Wood Production": "木材・林産物",
    "Other Industrial Metals & Mining": "その他の産業用金属・鉱業", "Other Precious Metals & Mining": "その他の貴金属・鉱業",
    "Paper & Paper Products": "紙・パルプ", "Silver": "銀", "Specialty Chemicals": "特殊化学品", "Steel": "鉄鋼", "Uranium": "ウラン",
    # 通信・メディア
    "Advertising Agencies": "広告代理店", "Broadcasting": "放送", "Electronic Gaming & Multimedia": "電子ゲーム・マルチメディア",
    "Entertainment": "エンターテインメント", "Internet Content & Information": "インターネットコンテンツ・情報",
    "Publishing": "出版", "Telecom Services": "通信サービス",
    # 一般消費財
    "Apparel Manufacturing": "アパレル製造", "Apparel Retail": "アパレル小売", "Auto & Truck Dealerships": "自動車・トラック販売",
    "Auto Manufacturers": "自動車メーカー", "Auto Parts": "自動車部品", "Department Stores": "百貨店",
    "Footwear & Accessories": "靴・アクセサリー", "Furnishings, Fixtures & Appliances": "家具・内装・家電", "Gambling": "ギャンブル",
    "Home Improvement Retail": "ホームセンター（住宅リフォーム小売）", "Internet Retail": "ネット小売", "Leisure": "レジャー",
    "Lodging": "宿泊", "Luxury Goods": "高級品", "Packaging & Containers": "包装・容器", "Personal Services": "個人向けサービス",
    "Recreational Vehicles": "レクリエーション用車両", "Residential Construction": "住宅建設", "Resorts & Casinos": "リゾート・カジノ",
    "Restaurants": "外食", "Specialty Retail": "専門小売", "Textile Manufacturing": "繊維製造", "Travel Services": "旅行サービス",
    # 生活必需品
    "Beverages - Brewers": "ビール", "Beverages - Non-Alcoholic": "清涼飲料", "Beverages - Wineries & Distilleries": "ワイン・蒸留酒",
    "Confectioners": "菓子", "Discount Stores": "ディスカウントストア", "Education & Training Services": "教育・研修サービス",
    "Farm Products": "農産物", "Food Distribution": "食品卸売", "Grocery Stores": "食料品店",
    "Household & Personal Products": "家庭用品・パーソナルケア", "Packaged Foods": "加工食品", "Tobacco": "たばこ",
    # エネルギー
    "Oil & Gas Drilling": "石油・ガス掘削", "Oil & Gas E&P": "石油・ガス探鉱・生産", "Oil & Gas Equipment & Services": "石油・ガス機器・サービス",
    "Oil & Gas Integrated": "石油・ガス（総合）", "Oil & Gas Midstream": "石油・ガス中流（パイプライン）",
    "Oil & Gas Refining & Marketing": "石油精製・販売", "Thermal Coal": "一般炭",
    # 金融
    "Asset Management": "資産運用", "Banks - Diversified": "銀行（総合）", "Banks - Regional": "地方銀行",
    "Capital Markets": "資本市場（証券・投資銀行）", "Credit Services": "クレジットサービス", "Financial Conglomerates": "金融コングロマリット",
    "Financial Data & Stock Exchanges": "金融データ・取引所", "Insurance Brokers": "保険ブローカー", "Insurance - Diversified": "保険（総合）",
    "Insurance - Life": "生命保険", "Insurance - Property & Casualty": "損害保険", "Insurance - Reinsurance": "再保険",
    "Insurance - Specialty": "専門保険", "Mortgage Finance": "住宅ローン金融", "Shell Companies": "特別買収目的会社（SPAC）",
    # ヘルスケア
    "Biotechnology": "バイオテクノロジー", "Diagnostics & Research": "診断・研究", "Drug Manufacturers - General": "医薬品（大手）",
    "Drug Manufacturers - Specialty & Generic": "医薬品（専門・ジェネリック）", "Health Information Services": "ヘルスケア情報サービス",
    "Healthcare Plans": "医療保険", "Medical Care Facilities": "医療施設", "Medical Devices": "医療機器", "Medical Distribution": "医療品卸売",
    "Medical Instruments & Supplies": "医療器具・消耗品", "Pharmaceutical Retailers": "医薬品小売",
    # 資本財
    "Aerospace & Defense": "航空宇宙・防衛", "Airlines": "航空会社", "Airports & Air Services": "空港・航空関連サービス",
    "Building Products & Equipment": "建築資材・設備", "Business Equipment & Supplies": "事務機器・用品", "Conglomerates": "コングロマリット",
    "Consulting Services": "コンサルティング", "Electrical Equipment & Parts": "電気機器・部品", "Engineering & Construction": "エンジニアリング・建設",
    "Farm & Heavy Construction Machinery": "農業・建設重機", "Industrial Distribution": "産業用品卸売", "Infrastructure Operations": "インフラ運営",
    "Integrated Freight & Logistics": "総合物流", "Marine Shipping": "海運", "Metal Fabrication": "金属加工",
    "Pollution & Treatment Controls": "環境・汚染対策", "Railroads": "鉄道", "Rental & Leasing Services": "レンタル・リース",
    "Security & Protection Services": "セキュリティ・警備", "Specialty Business Services": "専門ビジネスサービス",
    "Specialty Industrial Machinery": "特殊産業機械", "Staffing & Employment Services": "人材・雇用サービス",
    "Tools & Accessories": "工具・アクセサリー", "Trucking": "トラック輸送", "Waste Management": "廃棄物処理",
    # 不動産
    "Real Estate - Development": "不動産開発", "Real Estate - Diversified": "不動産（総合）", "Real Estate Services": "不動産サービス",
    "REIT - Diversified": "REIT（総合）", "REIT - Healthcare Facilities": "REIT（ヘルスケア施設）", "REIT - Hotel & Motel": "REIT（ホテル）",
    "REIT - Industrial": "REIT（産業用）", "REIT - Mortgage": "REIT（モーゲージ）", "REIT - Office": "REIT（オフィス）",
    "REIT - Residential": "REIT（住宅）", "REIT - Retail": "REIT（商業施設）", "REIT - Specialty": "REIT（特殊）",
    # 情報技術
    "Communication Equipment": "通信機器", "Computer Hardware": "コンピューター機器", "Consumer Electronics": "家電",
    "Electronic Components": "電子部品", "Electronics & Computer Distribution": "電子機器・コンピューター卸売",
    "Information Technology Services": "ITサービス", "Scientific & Technical Instruments": "科学・技術機器",
    "Semiconductor Equipment & Materials": "半導体製造装置・材料", "Semiconductors": "半導体",
    "Software - Application": "ソフトウェア（アプリケーション）", "Software - Infrastructure": "ソフトウェア（インフラ）", "Solar": "太陽光",
    # 公益事業
    "Utilities - Diversified": "公益事業（総合）", "Utilities - Independent Power Producers": "独立系発電事業者",
    "Utilities - Regulated Electric": "規制電力", "Utilities - Regulated Gas": "規制ガス", "Utilities - Regulated Water": "規制水道",
    "Utilities - Renewable": "再生可能エネルギー（公益）",
}

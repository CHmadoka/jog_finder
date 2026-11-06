"""所有平台选择器与常量集中配置,便于 DOM 变化后维护。

优先使用页面内嵌 JSON(比 DOM 选择器稳定),DOM 选择器作为兜底。
"""
from __future__ import annotations

# ============ 前程无忧 job51 ============
JOB51 = {
    # we.51job.com 城市代码(常用城市;未收录时回退全国)
    "city_codes": {
        "全国": "000000",
        "北京": "010000",
        "上海": "020000",
        "广州": "030200",
        "深圳": "040000",
        "天津": "050000",
        "武汉": "180200",
        "西安": "200200",
        "南京": "070200",
        "杭州": "080200",
        "成都": "090200",
        "重庆": "060200",
        "苏州": "070300",
        "长沙": "190200",
        "郑州": "170200",
        "宁波": "080300",
        "合肥": "150200",
        "厦门": "060300",
        "无锡": "070400",
        "青岛": "120300",
    },
    # 列表页内嵌 JSON 变量名(按优先级)
    "result_globals": ["window.__SEARCH_RESULT__", "window.__INITIAL_STATE__"],
    # 内嵌 JSON 中岗位数组的候选键
    "result_list_keys": ["engine_search_result", "engine_jds", "items", "jobs"],
    # DOM 兜底:列表卡片
    "card": ".j_joblist .e, .j_joblist .el",
    "card_title": ".jname .title, .tspan.ttl a, .t a",
    "card_company": ".cname, .t2 a",
    "card_salary": ".sal, .t4",
    "card_info": ".d.at, .msg span",
    "card_href": "a.e_2, a.t",
    # 登录态检测:页头出现"登录"入口即认为未登录
    "login_entry": "a:has-text('登录'), .login, [class*='login'][href*='login']",
    # 详情页 JSON-LD(JobPosting)
    "detail_ld_json": 'script[type="application/ld+json"]',
    # 详情页 DOM 兜底
    "detail_title": ".t1, [class*='position'] h1, h1",
    "detail_desc": ".job_msg, .bmsg, [class*='job'] [class*='desc'], .jdesc",
    "detail_welfare": ".jtag .t2 span, .co_welfare",
}

# ============ Boss直聘 boss ============
BOSS = {
    # Boss 城市使用区划代码(101010100 北京等);未收录走全站并让用户在页面上选择
    "city_codes": {
        "全国": "100010000",
        "北京": "101010100",
        "上海": "101020100",
        "广州": "101280100",
        "深圳": "101280600",
        "杭州": "101210100",
        "成都": "101270100",
        "武汉": "101200100",
        "南京": "101190100",
        "西安": "101110100",
        "苏州": "101190400",
        "天津": "101030100",
        "重庆": "101040100",
        "长沙": "101250100",
        "郑州": "101180100",
        "合肥": "101220100",
        "厦门": "101230200",
        "青岛": "101120200",
        "宁波": "101210400",
        "无锡": "101190200",
    },
    # 列表卡片与字段(2024-2025 结构)
    "card": ".job-card-wrapper, .job-card-left",
    "card_title": ".job-name, .job-title",
    "card_salary": ".salary",
    "card_company": ".company-name a, .company-name",
    "card_tags": ".job-info .tag-list li, .tag-list li",
    "card_info_bottom": ".job-info .job-info-footer, .job-card-footer .info-desc, .job-info-footer",
    "card_hr": ".job-info .info-public, .info-public",
    "card_hr_tags": ".job-info .info-publis, .job-info-footer .job-pub-time",
    "card_link": "a",
    "detail_desc": ".job-sec-text, .job-detail-section .job-sec-text",
    "detail_highlight": ".job-highlight",
    "detail_welfare": ".job-tags span",
    "detail_title": ".name, .job-banner .name",
    "detail_company": ".company, .name + .company",
    # 风控页面特征
    "verify_markers": [
        "text=安全验证",
        "text=验证码",
        "text=拖动滑块",
        ".geetest_panel",
        "#nc_1_wrapper",
    ],
}

# ============ 猎聘 liepin ============
LIEPIN = {
    "city_codes": {
        "全国": "000",
        "北京": "010",
        "上海": "020",
        "广州": "050020",
        "深圳": "050090",
        "杭州": "070",
        "成都": "280",
        "武汉": "170",
        "南京": "060",
        "西安": "270",
        "苏州": "060080",
        "天津": "030",
        "重庆": "290",
        "长沙": "190",
        "郑州": "220",
        "合肥": "240",
        "厦门": "230",
        "青岛": "110",
        "宁波": "080",
        "无锡": "060090",
    },
    # 列表页内嵌 JSON:猎聘 SSR 数据 window.__INITIAL_STATE__ / __NEXT_DATA__
    "result_globals": ["window.__INITIAL_STATE__", "window.__NEXT_DATA__"],
    "result_list_keys": ["jobList", "cardList", "list"],
    "card": ".job-list .job-card-pc-container, ul.sojobs li, .job-card-box",
    "card_title": ".job-title, .job-info h3 a",
    "card_salary": ".job-info .salary, .job-salary",
    "card_company": ".company-info .company-name, .sojob .company-name a",
    "card_tags": ".job-info .job-info-list span, .labels span",
    "card_link": "h3 a, .job-title a",
    "detail_ld_json": 'script[type="application/ld+json"]',
    "detail_desc": ".job-intro-container, .job-item-desc, [class*='job-detail']",
    "detail_title": "h1, .title-info h1",
    "detail_hr": ".boss-info-container, .job-publisher",
    "login_entry": "a:has-text('登录'), .login-btn, [data-nick]",
}
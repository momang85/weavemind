# -*- coding: utf-8 -*-
"""来源注册表：回答"这条资料是谁发布的、怎么取的、能用来做什么、预算是多少"。

为什么要单独一张表（专项 §4）：此前"来源"散落在各处——路由表按 market/metric 匹配
行情源、`source_health` 按名字做冷却、准入判据自己看域名。结果是同一个供应商的
**不同端点**被当成一件事：巨潮的公告查询其实早就通了，而它的财务数据族需要 mcode，
两者却共用一句"巨潮未连通"；东财某个接口参数写错，也差点被记成"站点封了整家"。

四条纪律写进表里：

1. **按端点记账**（`source_id` 是端点，不是公司）：`upstream_family` 表示同一上游，
   熔断与结论都落在端点粒度；
2. **能力与健康分开**：`capability` 说"这条路径现在能不能做"，健康状态另由
   `adapters.source_health` 在运行期维护（`health_source` 指向它的键名）；
3. **授权范围如实写**：`license_scope` 是"我们能拿它做什么"，不是"供应商承诺了什么"；
4. **只有亲自验过的才写 `verified`**：没验的写 `unverified` 并留原因，
   不得拿宣传页当可靠性承诺。

本表不缓存、不联网、不做判定，只是可被读侧引用的静态说明。
"""

from __future__ import annotations

# 原因码词表（诊断层统一用词，专项 §3）：一个端点出的问题，不得跨端点误报成另一个成因。
NOT_IMPLEMENTED = "not_implemented"
AUTH_REQUIRED = "auth_required"
RATE_LIMITED = "rate_limited"
NETWORK_ERROR = "network_error"
POLICY_BLOCKED = "policy_blocked"
PROTOCOL_ERROR = "protocol_error"
SCHEMA_CHANGED = "schema_changed"
EMPTY_RESULT = "empty_result"
IRRELEVANT_RESULT = "irrelevant_result"
PARSE_REJECTED = "parse_rejected"
UNKNOWN_CAUSE = "unknown_cause"

REASON_CODES = (NOT_IMPLEMENTED, AUTH_REQUIRED, RATE_LIMITED, NETWORK_ERROR,
                POLICY_BLOCKED, PROTOCOL_ERROR, SCHEMA_CHANGED, EMPTY_RESULT,
                IRRELEVANT_RESULT, PARSE_REJECTED, UNKNOWN_CAUSE)

REASON_LABEL = {
    NOT_IMPLEMENTED: "这条端点还没实现（不是故障，也不是站点限制）",
    AUTH_REQUIRED: "需要账号/令牌，当前请求没有（含 401/403）",
    RATE_LIMITED: "被限流/反爬（含 429/456/验证码）",
    NETWORK_ERROR: "连接或传输失败（DNS、超时、重置、TLS）",
    POLICY_BLOCKED: "被**本项目的**出域/安全策略拒绝——不是站点的问题",
    PROTOCOL_ERROR: "非 2xx、重定向不跟随、响应不是约定格式",
    SCHEMA_CHANGED: "结构变了（约定的字段不见了），说明上游改了契约",
    EMPTY_RESULT: "查询成功但确实没有匹配的披露（**有前提的否定**）",
    IRRELEVANT_RESULT: "有返回，但没有一条命中要问的东西（期间/文种/主体不符）",
    PARSE_REJECTED: "取回的正文被准入判据拒绝（错误页/别家年报/无报告特征）",
    UNKNOWN_CAUSE: "原因未定——不要用「大概是被封了」填空",
}

# 端点注册表：键是**端点**，不是供应商。
SOURCES: dict[str, dict] = {
    "cninfo_disclosure_query": {
        "label": "巨潮资讯·公告查询",
        "upstream_family": "cninfo",
        "access_method": "http_post_form",
        "authority": "official_disclosure",
        "authority_basis": "交易所指定的法定信息披露平台，公告正文即原始披露",
        "supported_materials": ("年度报告", "年度报告摘要", "季度/中期报告",
                                "临时公告", "问询与回复"),
        "license_scope": "public_disclosure_cite_with_source",
        "license_note": ("公开披露文件可按来源引用；本项目不声称获得再分发或商业使用授权，"
                         "也不把「能看到」等同于「可转售」"),
        "capability": "available",
        "budget": {"timeout_s": 25, "max_bytes": 2 * 1024 * 1024,
                   "calls_per_task": 2, "note": "先查 orgId 映射（可缓存），再查一次公告列表"},
        "health_source": "cninfo_disclosure_query",
        "params_contract": "cninfo-hisannouncement-v1",
        "verified": "2026-09-29",
        "verified_evidence": "docs/evidence/a2_official_discovery_20260929.md",
        "verified_note": ("实机：600031 取回 4 条年报类公告；000711 取回 17 条，"
                          "其中 finalpage/2025-09-05/1224639904.PDF 与用户此前给的"
                          "京蓝 2020 年报（更正后）直链**逐字符一致**"),
    },
    "cninfo_org_map": {
        "label": "巨潮资讯·证券-orgId 映射",
        "upstream_family": "cninfo",
        "access_method": "http_get_json",
        "authority": "platform_metadata",
        "authority_basis": "平台自身的证券索引（不是披露内容，只用于构造参数）",
        "supported_materials": (),
        "license_scope": "platform_metadata_internal_use",
        "license_note": "只做参数映射用，不写进研究结论，也不作为事实来源",
        "capability": "available",
        "budget": {"timeout_s": 25, "max_bytes": 8 * 1024 * 1024, "calls_per_task": 1,
                   "note": "进程内按 TTL 缓存（6258 条 ≈ 0.6 MB）"},
        "health_source": "cninfo_org_map",
        "params_contract": "cninfo-org-map-v1",
        "verified": "2026-09-29",
        "verified_evidence": "docs/evidence/a2_official_discovery_20260929.md",
        "verified_note": "实机：szse_stock.json 200/592391 字节/6258 条；sse_stock.json 404",
    },
    "cninfo_annual_rows": {
        "label": "巨潮资讯·财务数据族（webapi）",
        "upstream_family": "cninfo",
        "access_method": "http_webapi_token",
        "authority": "official_disclosure_derived",
        "authority_basis": "平台对披露内容的再加工（派生数据，不等于原文口径）",
        "supported_materials": ("结构化财务指标",),
        "license_scope": "requires_account_and_terms",
        "license_note": "需 mcode 令牌与产品条款，未取得；本项目不自行采购",
        "capability": "not_implemented",
        "blocked_reason": AUTH_REQUIRED,
        "budget": {"timeout_s": 25, "max_bytes": 2 * 1024 * 1024, "calls_per_task": 0},
        "health_source": "cninfo_annual_rows",
        "params_contract": "",
        "verified": "unverified",
        "verified_evidence": "",
        "verified_note": "2026-09-07 探针：非浏览器请求 405/超时，疑似 mcode 门禁；未再试",
    },
    "eastmoney_ashare_annual": {
        "label": "东方财富·A 股主要财务指标",
        "upstream_family": "eastmoney",
        "access_method": "http_get_json",
        "authority": "third_party_structured",
        "authority_basis": "第三方结构化数据（派生），可作对照与交叉检查，不能替代原文",
        "supported_materials": ("结构化财务指标",),
        "license_scope": "public_web_reference",
        "license_note": "公开网页接口；不做批量抓取，遵守既有预算；不声称再分发授权",
        "capability": "available",
        "budget": {"timeout_s": 20, "max_bytes": 4 * 1024 * 1024, "calls_per_task": 4},
        "health_source": "eastmoney_annual",
        "params_contract": "eastmoney-datacenter-v1(filter=裸代码)",
        "verified": "2026-09-29",
        "verified_evidence": "docs/evidence/q0_ci_and_second_company_20260929.md",
        "verified_note": ("实机：002304.SZ 与 600031.SH 各 2 行；带交易所后缀会让接口"
                          "回 code 9201「参数错误为空」——参数契约写在适配器内部"),
    },
    "sse_bulletin_query": {
        "label": "上交所·上市公司公告查询",
        "upstream_family": "sse",
        "access_method": "http_get_json",
        "authority": "official_disclosure",
        "authority_basis": "交易所自建披露查询（与巨潮互为镜像，**不算独立事实源**）",
        "supported_materials": ("年度报告", "临时公告"),
        "license_scope": "public_disclosure_cite_with_source",
        "license_note": "与巨潮镜像同一份披露时只计一次事实来源",
        "capability": "unverified",
        "blocked_reason": EMPTY_RESULT,
        "budget": {"timeout_s": 25, "max_bytes": 2 * 1024 * 1024, "calls_per_task": 0},
        "health_source": "sse_bulletin_query",
        "params_contract": "sse-queryCompanyBulletinNew-v1",
        "verified": "2026-09-29",
        "verified_evidence": "docs/evidence/a2_official_discovery_20260929.md",
        "verified_note": ("实机 4 种参数（宽查询 / 只加日期 / 年报+日期 / 深市代码对照）"
                          "全部 status=200 且 total=0、pageSize 回显 10（请求写的是 25）"
                          "→ **不是被我们的过滤条件筛空**，而是这条匿名 GET 契约打不通。"
                          "不再盲试参数：沪市公司改走巨潮（已实测可用）"),
    },
    "eastmoney_hk_annual": {
        "label": "东方财富·港股主要财务指标",
        "upstream_family": "eastmoney",
        "access_method": "http_get_json",
        "authority": "third_party_structured",
        "authority_basis": "第三方结构化数据（派生），用前先看口径与披露日",
        "supported_materials": ("结构化财务指标",),
        "license_scope": "public_web_reference",
        "license_note": "公开网页接口；按既有预算取用，不声称再分发授权",
        "capability": "available",
        "budget": {"timeout_s": 25, "max_bytes": 4 * 1024 * 1024, "calls_per_task": 2},
        "health_source": "eastmoney_hk",
        "params_contract": "eastmoney-datacenter-v1(filter=裸代码)",
        "verified": "2026-09-29",
        "verified_evidence": "docs/evidence/a2_official_discovery_20260929.md",
        "verified_note": ("实机 00700.HK：200 / 243025 字节 / 96 行；适配器原样调用 12 年、"
                          "最新 2025 年（收入 7517.66 亿元）→ **此前记的 `URLError 10061` "
                          "已不成立**（取件通道修复后复测通过）。报价主机 "
                          "`push2.eastmoney.com` 仍 `RemoteDisconnected`（另一条链路，"
                          "年报研究用不到；`quote.eastmoney.com` 200 可用）"),
    },
}


def describe(source_id: str) -> dict:
    """取一个端点的注册信息（副本）；未知 id 返回 `{}`，让调用方自己决定怎么报。"""
    got = SOURCES.get(str(source_id or ""))
    return dict(got) if got else {}


def by_family(family: str) -> list[str]:
    """同一上游下的**各个端点**（专项 §4：供应商级结论必须能拆回端点级）。"""
    want = str(family or "").strip().lower()
    return sorted(sid for sid, meta in SOURCES.items()
                  if str(meta.get("upstream_family") or "").lower() == want)


def health_source(source_id: str) -> str:
    """该端点用于健康/冷却记账的键名（默认与端点 id 同名）。"""
    meta = SOURCES.get(str(source_id or "")) or {}
    return str(meta.get("health_source") or source_id or "")


def capability(source_id: str) -> str:
    """端点的**能力**状态：available / not_implemented / unverified / disabled。"""
    return str((SOURCES.get(str(source_id or "")) or {}).get("capability") or "unknown")


def reason_text(code: str) -> str:
    return REASON_LABEL.get(str(code or ""), REASON_LABEL[UNKNOWN_CAUSE])

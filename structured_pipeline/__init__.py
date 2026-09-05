# -*- coding: utf-8 -*-
"""structured_pipeline——结构化数据域编排逻辑（C2 深化拆分第二块）。

从 orchestrator_v2 抽出的结构化数据预载/注入/合并方法（原样搬迁）。
orchestrator 通过混入 StructuredPipelineMixin 保留全部方法签名。
依赖约定：不在顶层 import orchestrator_v2；用到其模块级常量
（_RANKING_SOURCES/_STRUCTURED_SOURCE_LABELS）时在表达式内延迟导入。
"""
import csv
import json
import logging
import time
from pathlib import Path

from workspace import task_data_dir, task_project_dir
from ws_helpers import push_progress

# 保持与 orchestrator_v2 相同的 logger 名：日志行为（含测试 assertLogs）零变化
logger = logging.getLogger("orchestrator_v2")


class StructuredPipelineMixin:

    @staticmethod
    def _wants_financial_data(goal: str) -> bool:
        """目标是否要求财务/财报类数据（触发快照页抓取与回灌清洗）。"""
        g = str(goal or "").lower()
        return any(k in g for k in (
            "财报", "年报", "季报", "营收", "净利润", "负债", "财务", "业绩",
            "financial", "revenue", "earnings", "annual report",
        ))

    def _recycle_fetch_into_clean(self, task_id: str, goal: str, result: dict) -> None:
        """快照页回灌清洗闭环：把 web_fetch 抓到的完整正文并入清洗输入，
        重新生成 clean_chart_data.json 并重跑探索图，让财务数字进入图表与摘要。"""
        if not self._wants_financial_data(goal):
            return
        try:
            parsed = json.loads(str(result.get("result") or ""))
        except Exception:
            parsed = None
        if not isinstance(parsed, dict):
            return
        text = str(parsed.get("text") or "").strip()
        if len(text) < 300:
            logger.info("Snapshot fetch too short, skip recycle (%d chars)", len(text))
            return
        try:
            proj = task_project_dir(task_id)
            snap = proj / "fetch_snapshot.json"
            snaps: list[dict] = []
            if snap.exists():
                try:
                    snaps = json.loads(snap.read_text(encoding="utf-8"))
                except Exception:
                    snaps = []
            entry = {
                "title": str(parsed.get("title") or ""),
                "url": str(parsed.get("url") or ""),
                "text": text,
            }
            if not any(s.get("url") == entry["url"] for s in snaps):
                snaps.append(entry)
            snap.write_text(
                json.dumps(snaps, ensure_ascii=False, indent=1), encoding="utf-8",
            )
            src = proj / "search_results.json"
            items = json.loads(src.read_text(encoding="utf-8")) if src.exists() else []
            docs = list(items) + [
                {
                    "title": s.get("title") or "",
                    "url": s.get("url") or "",
                    "snippet": s.get("text") or "",
                }
                for s in snaps
            ]
            from clean_data import clean_and_structure
            clean = clean_and_structure(docs, goal=goal)
            (proj / "clean_chart_data.json").write_text(
                json.dumps(clean, ensure_ascii=False, indent=1), encoding="utf-8",
            )
            # 结构化源（东方财富）数据重新并入，避免被搜索清洗覆盖
            self._remerge_structured_financials(task_id)
            # P1-3：crypto/macro 结构化行情行同样重新并入（供数据驱动图表兜底）
            self._remerge_structured_points(task_id)
            logger.info(
                "Snapshot recycle: %d docs, market_data=%d, shares=%d, trends=%d",
                len(docs),
                len(clean.get("market_data") or []),
                len(clean.get("market_share") or []),
                len(clean.get("market_trends") or []),
            )
            self._generate_search_charts(task_id, goal)
            self._render_clean_chart_data(task_id, goal)
        except Exception as exc:
            logger.warning("snapshot recycle failed: %s", str(exc)[:150])

    def _structured_data_preload(
        self, task_id: str, goal: str, project: str | None = None,
    ) -> dict | None:
        """结构化数据源预载：按目标路由到 financial（东方财富/SEC）或
        新增的 crypto/macro/news 适配器；命中后写入工作区并合并到
        clean_chart_data.json，供搜索清洗、图表与报告引用。失败静默回退搜索链路。"""
        try:
            from adapters.router import route_structured
            data = route_structured(goal)
            if not data:
                # P2-6 预载失败可见化：首次未命中（如瞬时接口异常）等待 2s
                # 重试一次，第二次仍返回 None 才放弃并告警，避免静默占位
                logger.warning(
                    "Structured preload miss for %s, retry once after 2s",
                    task_id,
                )
                time.sleep(2)
                data = route_structured(goal)
                if not data:
                    logger.warning(
                        "Structured preload retry failed for %s: "
                        "route_structured returned None", task_id,
                    )
                    return None
            source = str(data.get("source") or "")
            metadata = data.get("metadata") or {}
            is_financial = source in (
                "eastmoney_datacenter", "eastmoney_ashare", "sec_edgar",
            ) or (not source and isinstance(data.get("financials"), list))
            is_financial = is_financial or source == "multi_entity"
            proj = task_project_dir(task_id, project)
            if not hasattr(self, "_task_structured_data"):
                self._task_structured_data = {}
            self._task_structured_data[task_id] = data
            # 财务数据：沿用既有 financials.json 通道（报告注入/失败回退/验收溯源）
            if is_financial:
                # P2-5 把 resolver 返回的 market/name/code 与候选列表写入任务上下文，
                # 报告生成时在 [结构化财务数据] 段标注数据源选择依据
                prefs = getattr(self, "_task_market_resolution", None)
                if prefs is None:
                    prefs = {}
                    self._task_market_resolution = prefs
                try:
                    from adapters.resolver import market_preference
                    pref = market_preference()
                except Exception:
                    pref = "hk"
                if source == "multi_entity":
                    prefs[task_id] = {
                        "entities": [
                            {
                                "name": str(
                                    (ent.get("resolution") or {}).get("name")
                                    or ent.get("name") or ""
                                ),
                                "market": str(ent.get("market") or ""),
                                "code": str(
                                    ent.get("code")
                                    or ent.get("stock_code") or ""
                                ),
                                "preference": pref,
                                "alternatives": (
                                    (ent.get("resolution") or {})
                                    .get("resolved_alternatives") or []
                                ),
                            }
                            for ent in data.get("companies") or []
                        ],
                    }
                else:
                    resolution = data.get("resolution") or {}
                    if resolution:
                        prefs[task_id] = {
                            "name": str(resolution.get("name") or ""),
                            "market": str(resolution.get("market") or ""),
                            "code": str(resolution.get("stock_code") or ""),
                            "preference": pref,
                            "alternatives": resolution.get(
                                "resolved_alternatives"
                            ) or [],
                        }
                (proj / "financials.json").write_text(
                    json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8",
                )
                snap = proj / "fetch_snapshot.json"
                snaps: list[dict] = []
                if snap.exists():
                    try:
                        snaps = json.loads(snap.read_text(encoding="utf-8"))
                    except Exception:
                        snaps = []
                entries: list[dict] = []
                if source == "multi_entity":
                    for ent in data.get("companies") or []:
                        ent_source = str(
                            (ent.get("metadata") or {}).get("source") or ""
                        )
                        source_label = __import__("orchestrator_v2", fromlist=["_STRUCTURED_SOURCE_LABELS"])._STRUCTURED_SOURCE_LABELS.get(
                            ent_source, ent_source or "结构化数据源",
                        )
                        entries.append({
                            "title": (
                                f"{ent.get('name')} 主要财务指标"
                                f"（{source_label}）"
                            ),
                            "url": (ent.get("raw") or {}).get("url", ""),
                            "text": (ent.get("raw") or {}).get("text", ""),
                        })
                else:
                    entries.append({
                        "title": (
                            f"{metadata.get('company')} 主要财务指标"
                            "（东方财富数据中心）"
                        ),
                        "url": (data.get("raw") or {}).get("url", ""),
                        "text": (data.get("raw") or {}).get("text", ""),
                    })
                snapshot_changed = False
                for entry in entries:
                    if entry["url"] and not any(
                        s.get("url") == entry["url"] for s in snaps
                    ):
                        snaps.append(entry)
                        snapshot_changed = True
                if snapshot_changed:
                    snap.write_text(
                        json.dumps(snaps, ensure_ascii=False, indent=1),
                        encoding="utf-8",
                    )
                clean_path = proj / "clean_chart_data.json"
                clean = {}
                if clean_path.exists():
                    try:
                        clean = json.loads(clean_path.read_text(encoding="utf-8"))
                    except Exception:
                        clean = {}
                if source == "multi_entity":
                    for ent in data.get("companies") or []:
                        clean = self._merge_structured_financials(
                            clean, ent.get("financials") or [],
                            (ent.get("raw") or {}).get("url", ""),
                            entity=ent.get("name"),
                        )
                else:
                    clean = self._merge_structured_financials(
                        clean, data.get("financials") or [],
                        (data.get("raw") or {}).get("url", ""),
                    )
                clean_path.write_text(
                    json.dumps(clean, ensure_ascii=False, indent=1), encoding="utf-8",
                )
                if source == "multi_entity":
                    hit_msg = (
                        f"结构化数据源命中：{metadata.get('entities')} 个实体"
                        "（多实体对比）"
                    )
                    log_msg = (
                        "Structured multi-entity financials loaded for %s: "
                        "%s entities"
                    )
                    log_args = (task_id, metadata.get("entities"))
                else:
                    hit_msg = (
                        f"结构化数据源命中：{metadata.get('company')} "
                        f"{metadata.get('annual_count')} 份年报"
                    )
                    log_msg = "Structured financials loaded for %s: %s annuals"
                    log_args = (task_id, metadata.get("annual_count"))
                push_progress(self._messaging, task_id, "log",
                              {"type": "data", "agent": "orchestrator",
                               "message": hit_msg,
                               "timestamp": self._now_iso()})
                logger.info(log_msg, *log_args)
                return data
            # 新数据源（crypto/macro/news）：统一写 structured_data.json，
            # 可数值化的点序列并入 clean_chart_data 的 market_trends 供绘图
            (proj / "structured_data.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8",
            )
            # A 方案：排行数据转写为 data/ranking.csv，让 data_analyzer
            # （只认 CSV/JSON）无需再找"新 CSV"即可直接消费
            self._export_ranking_csv(task_id, data, project)
            self._merge_structured_points(task_id, data, project)
            # P1-3：预载即尝试数据驱动图表（crypto/macro 行情点 ≥2 即可渲染），
            # 保证只有"预载 → 报告"两步的加密任务也能出图
            self._render_clean_chart_data(task_id, goal)
            label = metadata.get("label") or source
            push_progress(self._messaging, task_id, "log",
                          {"type": "data", "agent": "orchestrator",
                           "message": f"结构化数据源命中：{label}",
                           "timestamp": self._now_iso()})
            logger.info("Structured data loaded for %s: %s", task_id, source)
            return data
        except Exception as exc:
            logger.warning("Structured data preload failed: %s", str(exc)[:150])
            return None

    @staticmethod
    def _export_ranking_csv(
        task_id: str, data: dict, project: str | None = None,
    ) -> Path | None:
        """把排行来源（eastmoney/tencent/sina）的 rows 转写为 data/ranking.csv。

        列与适配器 payload 保持一致（rank/code/name/price/change_pct/
        volume_hand/volume_wan_hand/amount_yuan/amount_yi/turnover_pct/
        market_cap_yi），data_analyzer/code_execution 可直接 pd.read_csv 消费；
        sina_ranking 全市场数据（fetched_count 可 >50）整表导出，不做截断，
        供统计类任务计算前 N% 成交额占比（分母=全市场合计）；
        非排行来源返回 None（不产生无关文件）。
        """
        try:
            source = str(data.get("source") or "")
            if source not in __import__("orchestrator_v2", fromlist=["_RANKING_SOURCES"])._RANKING_SOURCES:
                return None
            rows = (data.get("data") or {}).get("rows") or []
            if not rows:
                return None
            fetched_count = int(
                (data.get("data") or {}).get("fetched_count") or len(rows)
            )
            csv_path = task_data_dir(task_id, project) / "ranking.csv"
            columns = [
                "rank", "code", "name", "price", "change_pct",
                "volume_hand", "volume_wan_hand",
                "amount_yuan", "amount_yi",
                "turnover_pct", "market_cap_yi",
            ]
            with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(columns)
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    writer.writerow([row.get(c, "") for c in columns])
            logger.info(
                "ranking.csv exported for %s: %d rows (fetched %d)",
                task_id, len(rows), fetched_count,
            )
            return csv_path
        except Exception as exc:
            logger.warning("ranking csv export failed: %s", str(exc)[:120])
            return None

    @staticmethod
    def _merge_structured_points(
        task_id: str, data: dict, project: str | None = None,
    ) -> None:
        """把 crypto/macro 的点序列并入 clean_chart_data 的 market_trends，
        让新适配器的数据能走既有"清洗 → 绘图 → 报告"通道。"""
        try:
            source = str(data.get("source") or "")
            payload = data.get("data") or {}
            meta = data.get("metadata") or {}
            rows: list[dict] = []
            if source == "coingecko":
                try:
                    from adapters.coingecko import coin_zh
                    series = f"{coin_zh(meta.get('coin'))}行情"
                except Exception:
                    series = "加密货币行情"
                for key, label, unit in (
                    ("price", "当前价格", "USD"),
                    ("market_cap", "市值", "USD"),
                    ("volume_24h", "24小时成交量", "USD"),
                    ("change_24h", "24小时涨跌幅", "%"),
                ):
                    if payload.get(key) is not None:
                        rows.append({
                            "type": "market_size",
                            "label": label,
                            "value": float(payload[key]),
                            "unit": unit,
                            "source": "coingecko",
                            "caliber": "CoinGecko 实时行情",
                            "series": series,
                        })
            elif source == "macro":
                series = str(payload.get("series") or "")
                for pt in (payload.get("points") or [])[-40:]:
                    try:
                        rows.append({
                            "type": "market_trends",
                            "label": f"{series} {pt.get('date')}",
                            "value": float(pt.get("value")),
                            "unit": "",
                            "year": None,
                            "source": "fred",
                            "series": series,
                        })
                    except (TypeError, ValueError):
                        continue
            elif source in __import__("orchestrator_v2", fromlist=["_RANKING_SOURCES"])._RANKING_SOURCES:
                metric = str(payload.get("metric") or "amount")
                market_label = str(payload.get("market") or "")
                if not market_label:
                    market_label = (
                        "美股" if source == "tencent_us_ranking" else "A股"
                    )
                series = (
                    f"{market_label}成交额排行"
                    if metric != "volume" else f"{market_label}成交量排行"
                )
                caliber = (
                    "新浪行情中心全市场排行"
                    if source == "sina_ranking"
                    else (
                        "腾讯行情实时排行"
                        if source.startswith("tencent")
                        else "东方财富行情中心实时排行"
                    )
                )
                for row in (payload.get("rows") or [])[:20]:
                    label = f"{row.get('rank')}.{row.get('name')}"
                    if metric == "volume":
                        v = row.get("volume_wan_hand")
                        unit = "万手"
                    else:
                        v = row.get("amount_yi")
                        unit = "亿元"
                    if v is not None:
                        rows.append({
                            "type": "market_size",
                            "label": label,
                            "value": float(v),
                            "unit": unit,
                            "source": source,
                            "caliber": caliber,
                            "series": series,
                        })
                    if row.get("change_pct") is not None:
                        rows.append({
                            "type": "market_size",
                            "label": f"{label}涨跌幅",
                            "value": float(row["change_pct"]),
                            "unit": "%",
                            "source": source,
                            "caliber": caliber,
                            "series": f"{series}涨跌幅",
                        })
                    # 最新价行：供"量价散点"（价格 × 成交量/成交额）成图
                    if row.get("price") is not None:
                        rows.append({
                            "type": "market_size",
                            "label": f"{label}最新价",
                            "value": float(row["price"]),
                            "unit": "元",
                            "source": source,
                            "caliber": caliber,
                            "series": f"{series}最新价",
                        })
            if rows:
                proj = task_project_dir(task_id, project)
                clean_path = proj / "clean_chart_data.json"
                clean = {}
                if clean_path.exists():
                    try:
                        clean = json.loads(clean_path.read_text(encoding="utf-8"))
                    except Exception:
                        clean = {}
                md = list(clean.get("market_data") or [])
                seen = {(r.get("label"), r.get("value"), r.get("unit")) for r in md}
                for r in rows:
                    k = (r.get("label"), r.get("value"), r.get("unit"))
                    if k not in seen:
                        seen.add(k)
                        md.append(r)
                clean["market_data"] = md
                clean_path.write_text(
                    json.dumps(clean, ensure_ascii=False, indent=1),
                    encoding="utf-8",
                )
        except Exception as exc:
            logger.warning("merge structured points failed: %s", str(exc)[:120])

    def _remerge_structured_points(self, task_id: str) -> None:
        """搜索清洗/快照回灌后，把 crypto/macro 结构化行重新并入
        clean_chart_data.json，避免被 clean_file 重写覆盖（数据驱动兜底依赖）。"""
        try:
            proj = task_project_dir(task_id)
            sd_path = proj / "structured_data.json"
            if not sd_path.exists():
                return
            sd = json.loads(sd_path.read_text(encoding="utf-8"))
            self._merge_structured_points(task_id, sd)
            self._export_ranking_csv(task_id, sd)
        except Exception as exc:
            logger.warning("remerge structured points failed: %s", str(exc)[:120])

    @staticmethod
    def _structured_injection(task_id: str) -> str:
        """构造报告/总结步骤的 [结构化数据] 内容块：
        财务 → financials.json（沿用既有表格）；crypto/macro/news/行情排行 →
        structured_data.json（新通道）。返回空串表示无结构化数据。"""
        try:
            proj = task_project_dir(task_id)
            fin_path = proj / "financials.json"
            if fin_path.exists():
                fin = json.loads(fin_path.read_text(encoding="utf-8"))
                if str(fin.get("source") or "") == "multi_entity":
                    blocks: list[str] = []
                    for ent in fin.get("companies") or []:
                        fs = (ent.get("financials") or [])[-12:]
                        m = ent.get("metadata") or {}
                        if not fs:
                            continue
                        unit = m.get("unit") or "亿元"
                        currency = str(m.get("currency") or "").strip()
                        currency_note = ""
                        if currency:
                            currency_note = f"，币种：{currency}"
                            low_c = currency.lower()
                            if not any(
                                k in low_c
                                for k in ("cny", "rmb", "人民币", "元")
                            ):
                                currency_note += (
                                    "（非人民币口径：若报告以人民币呈现，须换算并"
                                    "注明换算说明，或明确标注原币种与口径差异）"
                                )
                        retrieved_at = str(m.get("retrieved_at") or "")
                        src_name = __import__("orchestrator_v2", fromlist=["_STRUCTURED_SOURCE_LABELS"])._STRUCTURED_SOURCE_LABELS.get(
                            str(m.get("source") or ""),
                            str(m.get("source") or "结构化数据源"),
                        )
                        rows = [
                            "| 年份 | 营收 | 归母净利润 | 毛利率% | 总负债 | 经营现金流 |",
                            "|---|---|---|---|---|---|",
                        ]
                        for f in fs:
                            rows.append(
                                f"| {f.get('year')} | {f.get('revenue')} | "
                                f"{f.get('net_profit')} | "
                                f"{f.get('gross_margin')} | "
                                f"{f.get('total_liabilities')} | "
                                f"{f.get('operating_cashflow')} |"
                            )
                        blocks.append(
                            "\n[结构化财务数据]（"
                            f"{ent.get('name')}，来自 {src_name}，单位：{unit}"
                            + currency_note
                            + (
                                f"，数据获取时间 {retrieved_at}"
                                if retrieved_at else ""
                            )
                            + "，权威数据源，优先引用）\n"
                            + "\n".join(rows)
                        )
                    if blocks:
                        return (
                            "\n\n" + "\n\n".join(blocks)
                            + "\n规则：报告/总结中的财务数字优先引用以上各实体表格，"
                            "并在来源处标注实体与数据源；对比结论必须基于表中数值；"
                            "本表未覆盖的数字若无溯源，标注"
                            "'基于模型知识，未在本次检索中验证'；禁止编造年份；"
                            "报告的数据截至/报告日期必须引用各表标注的数据获取时间，"
                            "禁止使用模型回忆的日期。"
                        )
                fs = (fin.get("financials") or [])[-12:]
                m = fin.get("metadata") or {}
                if fs:
                    unit = m.get("unit") or "亿元"
                    # P2-4：metadata.currency 存在时标注币种；非人民币口径要求
                    # 报告换算或注明口径差异，避免 HKD 数值被直接当人民币陈述
                    currency = str(m.get("currency") or "").strip()
                    currency_note = ""
                    if currency:
                        currency_note = f"，币种：{currency}"
                        low_c = currency.lower()
                        if not any(
                            k in low_c for k in ("cny", "rmb", "人民币", "元")
                        ):
                            currency_note += (
                                "（非人民币口径：若报告以人民币呈现，须换算并"
                                "注明换算说明，或明确标注原币种与口径差异）"
                            )
                    retrieved_at = str(m.get("retrieved_at") or "")
                    src_name = {
                        "eastmoney_datacenter": "东方财富数据中心（港交所披露）",
                        "eastmoney_ashare": "东方财富数据中心（A股财报）",
                        "sec_edgar": "SEC EDGAR（10-K 年报）",
                    }.get(str(m.get("source")), str(m.get("source")))
                    rows = [
                        "| 年份 | 营收 | 归母净利润 | 毛利率% | 总负债 | 经营现金流 |",
                        "|---|---|---|---|---|---|",
                    ]
                    for f in fs:
                        rows.append(
                            f"| {f.get('year')} | {f.get('revenue')} | "
                            f"{f.get('net_profit')} | {f.get('gross_margin')} | "
                            f"{f.get('total_liabilities')} | "
                            f"{f.get('operating_cashflow')} |"
                        )
                    return (
                        "\n\n[结构化财务数据]（来自 " + src_name + "，单位：" + unit
                        + currency_note
                        + (f"，数据获取时间 {retrieved_at}" if retrieved_at else "")
                        + "，权威数据源，优先引用）\n" + "\n".join(rows)
                        + "\n规则：报告/总结中的财务数字优先引用本表，并在来源处标注"
                        + src_name + "；本表未覆盖的数字若无溯源，标注"
                        "'基于模型知识，未在本次检索中验证'；禁止编造年份；"
                        "报告的数据截至/报告日期必须引用本表标注的数据获取时间，"
                        "禁止使用模型回忆的日期。"
                        + (
                            "币种口径：本表币种为 " + currency + "，报告必须注明币种；"
                            "以人民币口径呈现时必须换算并注明换算说明，或明确标注"
                            "原币种与口径差异，禁止把本表数值直接当作人民币陈述。"
                            if currency else ""
                        )
                    )
            sd_path = proj / "structured_data.json"
            if sd_path.exists():
                sd = json.loads(sd_path.read_text(encoding="utf-8"))
                source = str(sd.get("source") or "")
                payload = sd.get("data") or {}
                meta = sd.get("metadata") or {}
                retrieved_at = str(meta.get("retrieved_at") or "")
                time_hint = f"，数据获取时间 {retrieved_at}" if retrieved_at else ""
                lines: list[str] = []
                if source == "coingecko":
                    try:
                        from adapters.coingecko import coin_zh
                        name = coin_zh(meta.get("coin")) or "加密货币"
                    except Exception:
                        name = meta.get("coin") or "加密货币"
                    vs = str(meta.get("vs_currency") or "usd").upper()
                    lines = [
                        "| 指标 | 数值 |",
                        "|---|---|",
                        f"| 当前价格 | {payload.get('price')} {vs} |",
                        f"| 市值 | {payload.get('market_cap')} {vs} |",
                        f"| 24小时成交量 | {payload.get('volume_24h')} {vs} |",
                        f"| 24小时涨跌幅 | {payload.get('change_24h')}% |",
                    ]
                    block_title = (
                        f"[结构化数据]（{name} 实时行情，CoinGecko{time_hint}）"
                    )
                elif source == "macro":
                    series = str(payload.get("series") or "")
                    label = str(payload.get("indicator") or series)
                    lines = ["| 日期 | 数值 |", "|---|---|"]
                    for pt in (payload.get("points") or [])[-24:]:
                        lines.append(f"| {pt.get('date')} | {pt.get('value')} |")
                    block_title = (
                        f"[结构化数据]（{label}，FRED 宏观指标{time_hint}）"
                    )
                elif source == "news":
                    query = str(payload.get("query") or "")
                    lines = ["| 标题 | 来源时间 | 链接 |", "|---|---|---|"]
                    for it in (payload.get("items") or [])[:15]:
                        title = str(it.get("title") or "").replace("|", "｜")
                        pub = str(it.get("published") or "")
                        link = str(it.get("link") or "")
                        lines.append(f"| {title} | {pub} | {link} |")
                    block_title = (
                        f"[结构化数据]（新闻列表，Google News RSS，"
                        f"查询：{query or '默认'}{time_hint}）"
                    )
                elif source in __import__("orchestrator_v2", fromlist=["_RANKING_SOURCES"])._RANKING_SOURCES:
                    metric = str(payload.get("metric") or "amount")
                    market_label = str(payload.get("market") or "")
                    if not market_label:
                        market_label = (
                            "美股" if source == "tencent_us_ranking" else "A股"
                        )
                    if source == "sina_ranking":
                        # 新浪全市场：报告块给前 20 行摘要即可，全量在 ranking.csv
                        source_label = "新浪行情中心"
                        row_limit = 20
                        title = (
                            f"{market_label}成交量排行（全市场）"
                            if metric == "volume"
                            else f"{market_label}成交额排行（全市场）"
                        )
                    else:
                        source_label = (
                            "腾讯行情"
                            if source.startswith("tencent")
                            else "东方财富行情中心"
                        )
                        row_limit = 10
                        title = (
                            f"{market_label}成交量排行（前十）"
                            if metric == "volume"
                            else f"{market_label}成交额排行（前十）"
                        )
                    lines = [
                        "| 排名 | 代码 | 名称 | 最新价 | 涨跌幅% | "
                        "成交量(万手) | 成交额(亿元) | 换手率% |",
                        "|---|---|---|---|---|---|---|---|",
                    ]
                    for row in (payload.get("rows") or [])[:row_limit]:
                        lines.append(
                            f"| {row.get('rank')} | {row.get('code')} | "
                            f"{row.get('name')} | {row.get('price')} | "
                            f"{row.get('change_pct')} | "
                            f"{row.get('volume_wan_hand')} | "
                            f"{row.get('amount_yi')} | {row.get('turnover_pct')} |"
                        )
                    # 部分数据降级：报告块必须标注覆盖率（fetched_count/total），
                    # 避免把不完整全市场数据当作全量口径陈述
                    coverage_note = ""
                    if payload.get("partial"):
                        fetched = payload.get("fetched_count")
                        total = payload.get("total")
                        if total:
                            coverage_note = (
                                f"，部分数据：已获取 {fetched}/{total} 只"
                            )
                        else:
                            coverage_note = (
                                f"，部分数据：已获取 {fetched} 只"
                                "（全市场总数未知）"
                            )
                    block_title = (
                        f"[结构化数据]（{title}，{source_label}"
                        f"{coverage_note}{time_hint}）"
                    )
                if lines:
                    return (
                        f"\n\n{block_title}\n" + "\n".join(lines)
                        + "\n规则：报告/总结优先引用本表内容，并在来源处标注数据源；"
                        "表中没有的信息若无溯源，标注'基于模型知识，未在本次检索中验证'；"
                        "报告的数据截至/报告日期必须引用本表标注的数据获取时间"
                        "（retrieved_at），禁止使用模型回忆的日期；"
                        "若上下文中没有任何结构化数据块，须在报告末尾标注"
                        "'数据截至日期：未获取（本次任务未提供结构化数据）'。"
                    )
        except Exception:
            pass
        return ""

    @staticmethod
    def _merge_structured_financials(
        clean: dict, financials: list, source_url: str,
        entity: str | None = None,
    ) -> dict:
        """把适配器结构化财务行并入 clean_chart_data 的 market_data（去重）。

        entity 非空时标签带实体前缀（多实体对比，如“宁德时代2024年营收”），
        避免不同公司同年指标互相覆盖。
        """
        clean = dict(clean or {})
        md = list(clean.get("market_data") or [])
        seen = {(r.get("label"), r.get("value"), r.get("unit")) for r in md}
        for f in financials or []:
            if not isinstance(f, dict):
                continue
            for key, label, unit in (
                ("revenue", "营收", "亿元"), ("net_profit", "归母净利润", "亿元"),
                ("gross_profit", "毛利润", "亿元"), ("gross_margin", "毛利率", "%"),
                ("operating_profit", "经营利润", "亿元"),
                ("total_assets", "总资产", "亿元"), ("total_liabilities", "总负债", "亿元"),
                ("operating_cashflow", "经营现金流", "亿元"),
            ):
                v = f.get(key)
                if v is None:
                    continue
                label = f"{entity}{f.get('year')}年{label}" if entity else (
                    f"{f.get('year')}年{label}"
                )
                row = {
                    "type": "market_size",
                    "label": label,
                    "value": v, "unit": unit,
                    "year": f.get("year"),
                    "source": source_url,
                    "caliber": f"年报口径（{f.get('report_type') or ''}）",
                }
                k = (row["label"], row["value"], row["unit"])
                if k not in seen:
                    seen.add(k)
                    md.append(row)
        clean["market_data"] = md
        return clean

    def _remerge_structured_financials(self, task_id: str) -> None:
        """搜索清洗/快照回灌后，把结构化源数据重新并入 clean_chart_data.json。"""
        try:
            proj = task_project_dir(task_id)
            fin_path = proj / "financials.json"
            clean_path = proj / "clean_chart_data.json"
            if not fin_path.exists() or not clean_path.exists():
                return
            fin = json.loads(fin_path.read_text(encoding="utf-8"))
            clean = json.loads(clean_path.read_text(encoding="utf-8"))
            if str(fin.get("source") or "") == "multi_entity":
                for ent in fin.get("companies") or []:
                    clean = self._merge_structured_financials(
                        clean, ent.get("financials") or [],
                        (ent.get("raw") or {}).get("url", ""),
                        entity=ent.get("name"),
                    )
            else:
                clean = self._merge_structured_financials(
                    clean, fin.get("financials") or [],
                    (fin.get("raw") or {}).get("url", ""),
                )
            clean_path.write_text(
                json.dumps(clean, ensure_ascii=False, indent=1), encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("remerge structured financials failed: %s", str(exc)[:120])

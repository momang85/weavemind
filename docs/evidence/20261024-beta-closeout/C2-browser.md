# C2 浏览器与真实文件走查报告（BETA-CLOSE-1024）

日期：2026-10-24｜执行：lead｜实例 8085（真实缓存）/ 8086（50 家合成演示，隔离）

## 1. 结论一览

| 项 | 结果 | 证据 |
|---|---|---|
| 真实浏览器全链路（momang 账号）：材料 → 覆盖预览 → 真实适配器取数 → 数据集 → 筛选 → 冻结运行 → 证据面板 | **PASS** | `screens/c1-03-momang-real-collect-run.png` |
| 50 家公司走查（合成数据，明确标合成） | **PASS（API + UI）** | `screens/c2-01-synthetic-50.png`、`logs/c2-synthetic-50.log` |
| 研究清单 200 条边界 | **PASS（API）** | `logs/c2-synthetic-50.log`：200 条、`integrity=ok`、rev 稳定 |
| 分页 / 过滤 / 排序 | **PARTIAL** | 三态计数（通过/未通过/资料不足）与「只看」过滤、展示排序控件在 UI 可见；50 家单页显示，**未构造 >1 页数据做翻页走查** |
| 跨账号模板导入 | **未验（代码路径在，未做浏览器双账号实物走查）** | 模板导出接口 200（789 字节 CSV 3 行）；导入走浏览器文件选择，受下条限制 |
| 原生文件选择器上传 | **未验（环境阻塞）** | Chrome 扩展未开「允许访问文件网址」→ `upload needs Chrome's per-extension file access`；**不以 `File`/`DataTransfer` 注入冒充原生路径通过** |
| 下载模板落盘 | **未验（环境阻塞）** | 点击「下载模板（CSV）」页面无报错，但 `C:\Users\ding0\Downloads` 无新文件（目录 mtime 未变）；服务端模板接口内容正确 |
| 未保存表单保留 / 超时重试 / 乱序响应 / 双标签同步 / 后台恢复 | **未验** | 本轮未构造相应时序；代码层面：请求带 seq/AbortController 超时（探测 60s、整批 300s）、轮询式加载，但**没有实测证据** |
| 390/420 px 窄屏 + 键盘焦点 | **未验** | 该浏览器扩展不提供视口仿真，无法在真实 390/420 宽度渲染；需人工在 DevTools 设备模式下按 `C2-browser-checklist` 复核 |

## 2. 50 家合成演示走查（可复现）

```
# 生成合成缓存（100 行 × 50 家，metadata.synthetic=true, official=false）
python tmp/…/c2_synthetic_50.py     # 实际脚本见下方命令
# 启动隔离实例 8086（真实缓存不被触碰）
C2_TMP=tmp/c2-8086 python tmp/c2_instance_8086.py
```

实测：`pool=50`、物化数据集 `synthetic=True / kind=synthetic_demo / official=False`、运行 `counts={pass:30, fail:20, insufficient:0, total:50}`（0.1s，零网络）、成本 `network_requests=0 / external_fee=unknown`。
UI 横幅：「本次结果基于合成演示数据 / 不进入真实候选、不进入交付与验收证据」；数据来源行：「c2_synthetic_50｜100 行｜合成演示数据（不进真实候选）」。
**合成数据不计入市场覆盖**，真实缓存 `official_disclosure_cache.json`（sha256 `4dc828ef…`）未被改动。

## 3. 200 条清单边界

200 次 `add`（150 个新代码 + 50 个已存在）→ 清单 200 条、`integrity=ok`、`rev=db87c0291d9831cc`；重复代码不重复入列（去重生效）。

## 4. 需人工补做（附步骤，均为 5 分钟内可完成）

1. **原生上传**：chrome://extensions → Kimi → 详情 → 打开「允许访问文件网址」，重跑「上传 CSV 数据」选 `tmp/c2-upload.csv`，确认数据集出现且 `user_provided=true`。
2. **下载落盘**：点「下载模板（CSV）」，看 Chrome 下载气泡与下载目录；如仍不落盘，检查 `chrome://settings/downloads` 与安全软件拦截。
3. **跨账号模板导入**：A 账号导出规则模板（JSON）→ B 账号导入 → 校验规则条数与字段一致（预期：模板只带规则，不带数据）。
4. **窄屏**：DevTools 设备模式 390×844 / 420×900，检查无横向滚动、表格可读、Tab 焦点可见。
5. **时序**：断网/慢网下点「整批取数」验证 300s 超时提示与可重试；双标签同改清单验证后写方收到 409 提示。

# WATCH-BRIEF-1024 执行记录（已执行，2026-10-24）

- 用户指令：按「当前收口 → 统一公司页 → 自选变化简报」顺序推进；本批只做 W0 + W1 + W2，W3 为验收；后续（研究逻辑卡正式功能、同行比较、新因子、自然语言选股、云分享、回测扩展、自动提醒）**不在本批**。
- 前置：TRIAL-READY-1024 的 G1–G6 门槛先过（T0 计算一致性由 pkg-hygiene，T1 研究材料由 research-materials），本批 W1/W2 接入不越过门槛。
- 浏览器：用户保持控制关闭；本轮未新开浏览器会话，真实浏览器走查为既有会话 `w1-company` 的产出（13 张截图），Lead 只审阅。
- 集成人：`web_ui.py` / `screen_store.py` / 公共前端文件的唯一集成人 = lead。

## 产出（`docs/evidence/20261024-watch-brief/`）

- `W0-contract.md` — 入口清单、公司页字段契约与 8 态、简报状态机与 7 类变化、比较基准规则、计划新文件、集成人声明。
- `W1-company.md` — 统一公司页（三入口、run/dataset/选择输入/not_in_run/corrupt、证据面板、纳入移出）+ 未验项；§10 Lead 集成追记。
- `W2-brief.md` — 「查看变化」两场景（换数值/改规则）、幂等、基准不推进、409 冲突 + 未验项；§9 Lead 集成追记。
- `W3-acceptance.md` — 最终构建指纹、集成冒烟（8094）、11 个测试文件 exit 0、分通道证据、未验项。
- `release-gates.md` — TRIAL-READY G1–G6 修复前后实测与判定；W 批增量门槛；分发条件。
- `manual-acceptance.md` — 用户侧人工复核清单（必看项、机器判不了项、本机限制）。
- `screens/` 13 张（w1-01…w1-10、w2-01…w2-03）、`logs/` 15 个接口与 UI 取证文件。

## 最终包

`tmp/watch-brief-package/weavemind-rc2-20261024T141228Z-cd6a388f.zip`（sha256 `657c0d36…b737`，365 文件，`--verify` exit 0）。旧候选包已被取代或注明不可分发。

## 边界

未 commit / 未 push / 未建分支；生产 8080 无监听；临时端口全部停净；真实缓存、真实密钥、用户数据未动。
审计披露：临时实例走查向 `logs/audit.jsonl` 追加 43 行 admin 动作记录（实例脚本未重定向 `AUDIT_FILE`，已记为遗留缺陷；日志 append-only，只披露不删改，详见 `W3-acceptance.md` §7）。

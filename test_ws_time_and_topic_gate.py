"""实机复验（2026-10-22）两条后端缺陷的回归测试。

复验任务 `ui-a0d82a26c4`（贵州茅台 600519 两期研究）暴露：
(c) `ws_helpers._phase_event` 用 `time.gmtime()` 输出**裸 UTC 无偏移**，
    同一屏的反思「开始/进行中/完成」比其它日志条目早 8 小时，且前端无法识别；
(f) `clean_data._strong_subject_tokens` 先字母序 `sorted()` 再 `[:12]`，
    把目标里排在第 39 位的「贵州茅台」截掉 → 公司名级门控形同失效。
"""

import datetime
import re
import time
import unittest

import clean_data
import ws_helpers


class TestPhaseEventTimestamp(unittest.TestCase):
    """(c) 阶段事件时间戳必须是带偏移的本地墙钟，不能是裸 UTC。"""

    def test_now_local_iso_carries_offset(self):
        s = ws_helpers._now_local_iso()
        # 2026-10-21T21:49:10+08:00 —— 必须有偏移段，否则会被当成本地时刻读
        self.assertRegex(s, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$",
                         f"阶段事件时间戳仍无时区偏移：{s!r}")

    def test_now_local_iso_is_local_wall_clock(self):
        parsed = datetime.datetime.fromisoformat(ws_helpers._now_local_iso())
        # 与本地当前时刻相差应在几秒内（不是 UTC：UTC 会差 8 小时）
        delta = abs((datetime.datetime.now().astimezone() - parsed).total_seconds())
        self.assertLess(delta, 30, "阶段事件时间戳不是本地墙钟")
        self.assertIsNotNone(parsed.tzinfo, "时间戳缺时区信息")

    def test_phase_event_payload_is_parseable_as_local(self):
        """_phase_event 推给前端的 payload 时间戳必须可被解析且带偏移。"""
        sent = []

        class _FakeMessaging:
            def publish(self, channel, msg):
                sent.append((channel, msg))

        ws_helpers._phase_event(_FakeMessaging(), "ui-test", "反思", "begin", "反思 开始")
        self.assertEqual(len(sent), 1)
        _channel, msg = sent[0]
        payload = msg["payload"]
        self.assertEqual(msg["task_id"], "ui-test")
        self.assertIn("timestamp", payload)
        ts = payload["timestamp"]
        self.assertNotRegex(ts, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$",
                            f"裸 UTC（无偏移）会显示成错误的本地时刻：{ts!r}")
        parsed = datetime.datetime.fromisoformat(ts)
        self.assertIsNotNone(parsed.tzinfo)


class TestStrongSubjectTokens(unittest.TestCase):
    """(f) 强主题 token 必须保住目标开头的实体名。"""

    GOALS_AND_ENTITIES = [
        ("研究贵州茅台 2023 与 2024 两个年度的营业收入、归母净利润", "贵州茅台"),
        ("研究伊利股份（600887）2023 与 2024 两个年度", "伊利股份"),
        ("研究海天味业 2023 与 2024 两个年度营业收入", "海天味业"),
        ("研究洋河股份 2023 与 2024 两个年度经营情况", "洋河股份"),
    ]

    def test_entity_name_survives_the_cap(self):
        for goal, entity in self.GOALS_AND_ENTITIES:
            with self.subTest(entity=entity):
                toks = clean_data._strong_subject_tokens(goal)
                self.assertIn(entity, toks,
                              f"公司名「{entity}」被截断丢掉 → 门控失效：{toks}")

    def test_order_is_first_occurrence_not_alphabetical(self):
        goal = "研究贵州茅台 2023 与 2024 两个年度的营业收入"
        toks = list(clean_data._strong_subject_tokens(goal))
        # 字母序会把「两个年度」排在「贵州茅台」前面；首次出现顺序则相反
        self.assertLess(toks.index("贵州茅台"), toks.index("两个年度"))
        # 与目标中 4 字窗的出现顺序一致（第 3 个窗就是公司名）
        self.assertEqual(list(toks[:3]), ["究贵州茅", "贵州茅台", "两个年度"])

    def test_cap_still_bounds_the_set(self):
        long_goal = "研究" + "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥" * 4
        toks = clean_data._strong_subject_tokens(long_goal)
        self.assertLessEqual(len(toks), 24)
        self.assertEqual(len(set(toks)), len(toks), "token 不得重复")

    def test_short_goal_falls_back_to_empty(self):
        for goal in ("", "你好", "研究 A 股"):
            self.assertEqual(clean_data._strong_subject_tokens(goal), (),
                             f"取不到强主题时应当返回空以便调用方退回锚点门控：{goal!r}")


if __name__ == "__main__":
    unittest.main()

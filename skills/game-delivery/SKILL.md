---
name: game-delivery
description: 单文件 HTML 游戏/交互页交付标准。当目标要求做游戏、可玩页面、画布交互（贪吃蛇、打砖块、弹弓等）时触发。
owner: 产品组
version: 1
applies: code_execution
---

# 游戏交付 Skill

## 触发条件
- 目标包含：游戏、玩、canvas、pygame、贪吃蛇、打砖块、弹弓、可玩、2048、扫雷、五子棋、射击、闯关

## 工作流
1. 生成：code_execution 输出自包含单文件 HTML（内联 CSS/JS），保存为 index.html
2. 校验：node --check 校验内联 JS 语法；贯通测试验证页面可打开且画面有变化（canvas 指纹）
3. 修复：运行/审查不过则带具体错误重做（最多 3 轮）

## 质量标准
- 浏览器直接打开可玩，无需外部资源/服务器
- 键盘或鼠标可操控，有得分/进度反馈
- 游戏循环、碰撞/规则判定、结束条件三要素齐全
- 中文不乱码（<meta charset="utf-8">）

## 反模式
- 生成 Python+pygame 但环境无 pygame 依赖导致不可玩
- HTML 里引用了本地绝对路径或外部 CDN
- 只给设计文档不给可运行文件

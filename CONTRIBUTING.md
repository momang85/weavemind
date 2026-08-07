# 贡献指南 Contributing

欢迎任何形式的贡献：提 issue、改进 Worker、补测试、写文档、做宣传素材。

## 开发前准备

```bash
pip install -r requirements.txt
cd frontend && npm install
```

## 提 PR 前的检查

```bash
python test_common.py            # 基础库单测
python test_orchestrator_v2.py   # 编排器回归
python test_delivery_chain.py    # 交付链回归
cd frontend && npm run build     # 前端构建
```

CI 会执行同样的检查（Python 3.11 + Node 20），请确保本地全绿再提交。

## 风格约定

- Python 代码兼容 **3.10–3.14**：不要使用 3.12+ 专属语法（如 f-string 表达式内反斜杠），
  CI 的 3.11 会直接编译失败；
- 中文字符串保持 UTF-8，文件无需 `coding` 头；
- 修改 Worker 行为时同步更新 `test_delivery_chain.py` 中的回归用例。

## 敏感信息

`config.json`、`.env`、`*.db`、`chroma_memory*`、`logs/` 都在 `.gitignore` 中，
**任何真实 API Key 都不要提交**。

## 提出新想法

先在 [Issues](https://github.com/momang85/weavemind/issues) 里讨论，再动手实现，
避免重复劳动。

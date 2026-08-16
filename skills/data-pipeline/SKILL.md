---
name: data-pipeline
description: 数据管道标准工作流：加载→EDA→建模→报告。当目标涉及数据集、csv、EDA、训练模型、回归/预测、评分指标时触发。
owner: 数据组
version: 1
applies: data_loader, data_analyzer, model_trainer, report_generator
---

# 数据管道 Skill

## 触发条件
- 目标包含：数据集、csv、EDA、训练、模型、预测、回归、房价、评分、RMSE、R²

## 工作流
1. 加载：data_loader 按指令 URL/内置数据集落盘到任务 data 目录
2. EDA：data_analyzer 输出数据形状/缺失/相关性热力图/分布图
3. 建模：model_trainer 划分训练/测试集，输出 RMSE/R² 与特征重要性
4. 报告：report_generator 汇总数据规模、EDA 结论、模型指标与图表

## 质量标准
- 指标必须来自真实训练结果，禁止编造
- 图表带标题与轴标签
- 报告写明数据规模（行/列）、目标列、关键指标数值

## 反模式
- 把与主题无关的内置数据集（如加州房价）拉进无关任务
- 训练/测试不划分导致指标虚高
- 报告不含具体指标数值

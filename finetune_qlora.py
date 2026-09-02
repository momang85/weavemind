# -*- coding: utf-8 -*-
"""QLoRA 微调：Qwen2.5-7B-Instruct 4-bit + LoRA，蒸馏 content_summary 数据。

用法：python finetune_qlora.py [--data distill_data.jsonl] [--epochs 3] [--out lora_out]
12GB 显存：4-bit 量化（bitsandbytes）+ LoRA r=8 可训练。
"""
import argparse
import json
import os
import sys


def build_dataset(path: str):
    """读取蒸馏数据 → ChatML 格式训练样本。
    每条：<|im_start|>system\n{system}<|im_end|>\n<|im_start|>user\n{instruction}<|im_end|>\n<|im_start|>assistant\n{summary + charts}<|im_end|>
    """
    samples = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            instruction = str(rec.get("instruction") or "")
            summary = str(rec.get("summary") or "")
            charts = rec.get("charts") or []
            sources = rec.get("sources") or []
            if not instruction or not summary:
                continue
            charts_text = json.dumps(charts, ensure_ascii=False) if charts else "[]"
            # v2：含 sources（来源声明）字段 → 追加到输出，训练模型学会来源纪律
            sources_text = ""
            if sources:
                sources_text = "\n\n[SOURCES]" + json.dumps(sources, ensure_ascii=False)
            samples.append({
                "instruction": instruction,
                "output": f"{summary}\n\n[CHART_DATA]{charts_text}{sources_text}",
            })
    return samples


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="distill_data.jsonl")
    ap.add_argument("--model", default="Qwen/Qwen2.5-7B-Instruct")
    ap.add_argument("--epochs", type=int, default=3)
    ap.add_argument("--out", default="lora_out")
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--max_len", type=int, default=2048)
    args = ap.parse_args()

    import torch
    if not torch.cuda.is_available():
        print("ERROR: CUDA 不可用，无法训练（需 12GB 显存 GPU）")
        sys.exit(1)

    from datasets import Dataset
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
    from transformers import (
        AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig,
        TrainingArguments, Trainer, DataCollatorForLanguageModeling,
    )

    samples = build_dataset(args.data)
    print(f"训练样本: {len(samples)}")
    if not samples:
        print("ERROR: 无训练数据")
        sys.exit(1)

    # 4-bit 量化（12GB 显存可容纳 7B）
    bnb = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True,
    )
    tokenizer = AutoTokenizer.from_pretrained(args.model, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        args.model, quantization_config=bnb, device_map="auto",
        trust_remote_code=True,
    )
    model = prepare_model_for_kbit_training(model)

    lora = LoraConfig(
        r=8, lora_alpha=16, target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
        lora_dropout=0.05, bias="none", task_type="CAUSAL_LM",
    )
    model = get_peft_model(model, lora)
    model.print_trainable_parameters()

    # ChatML 化
    def fmt(s):
        return (
            "<|im_start|>system\n你是织光 WeaveMind 的内容总结 Worker。"
            "根据指令生成 Markdown 总结与图表数据。<|im_end|>\n"
            f"<|im_start|>user\n{s['instruction']}<|im_end|>\n"
            f"<|im_start|>assistant\n{s['output']}<|im_end|>"
        )

    ds = Dataset.from_list([{"text": fmt(s)} for s in samples])

    def tok(batch):
        out = tokenizer(batch["text"], truncation=True, max_length=args.max_len, padding=False)
        return {"input_ids": out["input_ids"], "attention_mask": out["attention_mask"]}

    ds = ds.map(tok, batched=True, remove_columns=["text"])

    train_args = TrainingArguments(
        output_dir=args.out,
        num_train_epochs=args.epochs,
        per_device_train_batch_size=1,
        gradient_accumulation_steps=8,
        learning_rate=args.lr,
        logging_steps=1,
        save_strategy="epoch",
        save_total_limit=2,
        fp16=True,
        remove_unused_columns=False,
        report_to=[],
    )
    collator = DataCollatorForLanguageModeling(tokenizer=tokenizer, mlm=False)
    trainer = Trainer(
        model=model, args=train_args, train_dataset=ds,
        data_collator=collator,
    )
    trainer.train()
    model.save_pretrained(args.out)
    tokenizer.save_pretrained(args.out)
    print(f"LoRA 已保存: {args.out}")


if __name__ == "__main__":
    main()

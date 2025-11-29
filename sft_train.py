"""Supervised fine-tuning with LoRA on knowledge base and session data."""

import argparse
import json
from pathlib import Path
from typing import Dict, List

from datasets import Dataset
from peft import LoraConfig, TaskType, get_peft_model
from transformers import (
    AutoModelForCausalLM,
    DataCollatorForLanguageModeling,
    PreTrainedTokenizerFast,
    Trainer,
    TrainingArguments,
)


def load_jsonl(paths: List[Path]) -> List[Dict[str, str]]:
    records: List[Dict[str, str]] = []
    for path in paths:
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                rec = json.loads(line)
                records.append(rec)
    return records


def to_sft_samples(records: List[Dict[str, str]]) -> List[Dict[str, str]]:
    samples = []
    for rec in records:
        stage = rec.get("stage", "")
        engine = rec.get("engine", "")
        genre = ",".join(rec.get("genre", []))
        inp = rec.get("input", "")
        out = rec.get("output", "")
        prompt = f"[STAGE:{stage}] [ENGINE:{engine}] [GENRE:{genre}]\nUSER: {inp}\nASSISTANT:"
        completion = f" {out}"
        samples.append({"text": prompt + completion})
    return samples


def parse_args():
    parser = argparse.ArgumentParser(description="SFT with LoRA on GameDev data")
    parser.add_argument("--base_model", type=Path, default=Path("artifacts/pretrain/variant_A"))
    parser.add_argument("--tokenizer", type=Path, default=Path("artifacts/pretrain/variant_A"))
    parser.add_argument("--data", type=Path, nargs="+", default=[Path("knowledge_base.jsonl"), Path("train.jsonl")])
    parser.add_argument("--output_dir", type=Path, default=Path("artifacts/sft_lora"))
    parser.add_argument("--per_device_batch_size", type=int, default=1)
    parser.add_argument("--gradient_accumulation_steps", type=int, default=16)
    parser.add_argument("--learning_rate", type=float, default=1e-4)
    parser.add_argument("--num_train_epochs", type=int, default=3)
    parser.add_argument("--max_seq_len", type=int, default=2048)
    return parser.parse_args()


def main():
    args = parse_args()
    tokenizer = PreTrainedTokenizerFast.from_pretrained(args.tokenizer)
    tokenizer.padding_side = "right"
    tokenizer.truncation_side = "left"
    records = load_jsonl(args.data)
    samples = to_sft_samples(records)
    dataset = Dataset.from_list(samples)

    def tokenize_fn(batch):
        return tokenizer(batch["text"], truncation=True, padding="max_length", max_length=args.max_seq_len)

    tokenized = dataset.map(tokenize_fn, batched=True)

    data_collator = DataCollatorForLanguageModeling(tokenizer=tokenizer, mlm=False)

    model = AutoModelForCausalLM.from_pretrained(args.base_model)
    lora_cfg = LoraConfig(
        task_type=TaskType.CAUSAL_LM,
        r=16,
        lora_alpha=32,
        lora_dropout=0.05,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
    )
    model = get_peft_model(model, lora_cfg)

    training_args = TrainingArguments(
        output_dir=str(args.output_dir),
        per_device_train_batch_size=args.per_device_batch_size,
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        learning_rate=args.learning_rate,
        num_train_epochs=args.num_train_epochs,
        logging_steps=50,
        save_steps=500,
        fp16=True,
    )

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=tokenized,
        data_collator=data_collator,
    )

    trainer.train()
    model.save_pretrained(args.output_dir)
    tokenizer.save_pretrained(args.output_dir)


if __name__ == "__main__":
    main()

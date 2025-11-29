"""Train LoRA adapter on Gemma 2B IT for GameDev Text LLM.
Usage: python train_lora.py --train_file data/train.jsonl --output_dir outputs/lora
"""

import argparse
import json
from pathlib import Path

import torch
from datasets import load_dataset
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
from transformers import AutoModelForCausalLM, AutoTokenizer, Trainer, TrainingArguments, DataCollatorForLanguageModeling

MODEL_NAME = "google/gemma-2-2b-it"


def load_jsonl(path: Path):
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            yield json.loads(line)


def build_dataset(train_path: Path):
    def gen():
        for row in load_jsonl(train_path):
            yield {
                "text": f"[STAGE:{row['stage']}] [ENGINE:{row['engine']}] [GENRE:{','.join(row['genre'])}] INPUT: {row['input']} OUTPUT: {row['output']}"
            }

    return load_dataset("json", data_files={"train": str(train_path)}, field=None, split="train").map(
        lambda x: x, batched=False
    )


def tokenize_function(tokenizer):
    def inner(examples):
        return tokenizer(examples["text"], truncation=True, max_length=1024)

    return inner


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--train_file", type=Path, required=True)
    parser.add_argument("--output_dir", type=Path, default=Path("outputs/lora"))
    parser.add_argument("--batch_size", type=int, default=2)
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--lr", type=float, default=2e-4)
    args = parser.parse_args()

    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME, use_fast=False)
    model = AutoModelForCausalLM.from_pretrained(
        MODEL_NAME,
        torch_dtype=torch.float16,
        device_map="auto",
    )
    model = prepare_model_for_kbit_training(model)

    lora_config = LoraConfig(
        r=16,
        lora_alpha=32,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
    )
    model = get_peft_model(model, lora_config)

    dataset = build_dataset(args.train_file)
    tokenized = dataset.map(tokenize_function(tokenizer), batched=True, remove_columns=dataset.column_names)

    collator = DataCollatorForLanguageModeling(tokenizer=tokenizer, mlm=False)

    training_args = TrainingArguments(
        output_dir=str(args.output_dir),
        per_device_train_batch_size=args.batch_size,
        num_train_epochs=args.epochs,
        learning_rate=args.lr,
        logging_steps=10,
        save_steps=100,
        save_total_limit=2,
        fp16=True,
        gradient_accumulation_steps=2,
    )

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=tokenized,
        data_collator=collator,
    )

    trainer.train()
    model.save_pretrained(str(args.output_dir))
    tokenizer.save_pretrained(str(args.output_dir))


if __name__ == "__main__":
    main()

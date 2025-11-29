"""Pretrain a causal LM from scratch for GameDev text (variants A/B)."""

import argparse
from pathlib import Path

from datasets import load_dataset
from transformers import (
    AutoModelForCausalLM,
    DataCollatorForLanguageModeling,
    LlamaConfig,
    PreTrainedTokenizerFast,
    Trainer,
    TrainingArguments,
)


MODEL_VARIANTS = {
    "A": {
        "hidden_size": 1024,
        "num_hidden_layers": 16,
        "num_attention_heads": 16,
        "intermediate_size": 2730,
        "rope_theta": 10000.0,
        "max_position_embeddings": 2048,
        "vocab_size": 32000,
    },
    "B": {
        "hidden_size": 4096,
        "num_hidden_layers": 32,
        "num_attention_heads": 32,
        "intermediate_size": 11008,
        "rope_theta": 10000.0,
        "max_position_embeddings": 4096,
        "vocab_size": 64000,
    },
}


def build_tokenizer(tokenizer_path: Path) -> PreTrainedTokenizerFast:
    tokenizer = PreTrainedTokenizerFast(tokenizer_file=str(tokenizer_path))
    tokenizer.bos_token = "<s>"
    tokenizer.eos_token = "</s>"
    tokenizer.unk_token = "<unk>"
    tokenizer.pad_token = "<pad>"
    return tokenizer


def load_text_dataset(data_dir: Path):
    files = sorted(data_dir.rglob("*.txt"))
    if not files:
        raise FileNotFoundError(f"No .txt files found in {data_dir}")
    return load_dataset("text", data_files={"train": [str(f) for f in files]})


def tokenize_dataset(dataset, tokenizer: PreTrainedTokenizerFast, block_size: int):
    def _tokenize(batch):
        return tokenizer(batch["text"])

    tokenized = dataset.map(_tokenize, batched=True, remove_columns=["text"])

    def _group_texts(examples):
        concatenated = sum(examples["input_ids"], [])
        total_length = (len(concatenated) // block_size) * block_size
        result = {
            "input_ids": [concatenated[i : i + block_size] for i in range(0, total_length, block_size)],
        }
        result["attention_mask"] = [[1] * block_size] * len(result["input_ids"])
        return result

    return tokenized.map(_group_texts, batched=True)


def build_config(variant: str, vocab_size: int) -> LlamaConfig:
    if variant not in MODEL_VARIANTS:
        raise ValueError(f"Unknown variant {variant}")
    cfg = MODEL_VARIANTS[variant]
    return LlamaConfig(
        vocab_size=vocab_size,
        hidden_size=cfg["hidden_size"],
        num_hidden_layers=cfg["num_hidden_layers"],
        num_attention_heads=cfg["num_attention_heads"],
        intermediate_size=cfg["intermediate_size"],
        max_position_embeddings=cfg["max_position_embeddings"],
        rope_theta=cfg["rope_theta"],
        pad_token_id=0,
    )


def parse_args():
    parser = argparse.ArgumentParser(description="Pretrain GameDev LM from scratch")
    parser.add_argument("--variant", choices=["A", "B"], default="A")
    parser.add_argument("--tokenizer", type=Path, default=Path("artifacts/tokenizer/tokenizer.model"))
    parser.add_argument("--data_dir", type=Path, default=Path("data/raw_corpus"))
    parser.add_argument("--output_dir", type=Path, default=Path("artifacts/pretrain"))
    parser.add_argument("--per_device_batch_size", type=int, default=1)
    parser.add_argument("--gradient_accumulation_steps", type=int, default=32)
    parser.add_argument("--learning_rate", type=float, default=2e-4)
    parser.add_argument("--num_train_epochs", type=int, default=1)
    parser.add_argument("--warmup_ratio", type=float, default=0.02)
    parser.add_argument("--weight_decay", type=float, default=0.1)
    parser.add_argument("--save_steps", type=int, default=5000)
    parser.add_argument("--logging_steps", type=int, default=100)
    parser.add_argument("--max_train_steps", type=int, default=None)
    parser.add_argument("--deepspeed", type=str, default=None, help="Path to DeepSpeed config json")
    return parser.parse_args()


def main():
    args = parse_args()
    tokenizer = build_tokenizer(args.tokenizer)
    dataset = load_text_dataset(args.data_dir)
    variant = args.variant
    block_size = MODEL_VARIANTS[variant]["max_position_embeddings"]
    tokenized = tokenize_dataset(dataset["train"], tokenizer, block_size=block_size)
    config = build_config(variant, vocab_size=len(tokenizer))
    model = AutoModelForCausalLM.from_config(config)

    data_collator = DataCollatorForLanguageModeling(tokenizer=tokenizer, mlm=False)

    training_args = TrainingArguments(
        output_dir=str(args.output_dir / f"variant_{variant}"),
        per_device_train_batch_size=args.per_device_batch_size,
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        learning_rate=args.learning_rate,
        num_train_epochs=args.num_train_epochs,
        warmup_ratio=args.warmup_ratio,
        weight_decay=args.weight_decay,
        logging_steps=args.logging_steps,
        save_steps=args.save_steps,
        max_steps=args.max_train_steps,
        fp16=True,
        deepspeed=args.deepspeed,
    )

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=tokenized,
        data_collator=data_collator,
    )

    trainer.train()
    trainer.save_model()
    tokenizer.save_pretrained(training_args.output_dir)


if __name__ == "__main__":
    main()

"""Train a SentencePiece Unigram tokenizer for GameDev data (ru+en+code)."""

import argparse
from pathlib import Path

import sentencepiece as spm


def collect_corpus(input_dir: Path, shard_path: Path) -> None:
    text_files = sorted(input_dir.rglob("*.txt"))
    shard_path.parent.mkdir(parents=True, exist_ok=True)
    with shard_path.open("w", encoding="utf-8") as out:
        for file in text_files:
            out.write(file.read_text(encoding="utf-8", errors="ignore"))
            out.write("\n")


def train_sentencepiece(args: argparse.Namespace) -> None:
    model_prefix = args.output_dir / "tokenizer"
    args.output_dir.mkdir(parents=True, exist_ok=True)
    combined = args.output_dir / "combined_corpus.txt"
    collect_corpus(args.input_dir, combined)
    spm.SentencePieceTrainer.Train(
        input=str(combined),
        model_prefix=str(model_prefix),
        vocab_size=args.vocab_size,
        model_type="unigram",
        character_coverage=0.9995,
        byte_fallback=True,
        input_sentence_size=args.sample_size,
        shuffle_input_sentence=True,
    )
    print(f"Saved tokenizer to {model_prefix}.model and {model_prefix}.vocab")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train SentencePiece Unigram tokenizer")
    parser.add_argument("--input_dir", type=Path, default=Path("data/raw_corpus"), help="Folder with *.txt shards")
    parser.add_argument("--output_dir", type=Path, default=Path("artifacts/tokenizer"))
    parser.add_argument("--vocab_size", type=int, default=32000, help="Vocabulary size (32k for A, 64k for B)")
    parser.add_argument("--sample_size", type=int, default=1000000, help="Number of sentences to sample for training")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    train_sentencepiece(args)


if __name__ == "__main__":
    main()

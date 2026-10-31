"""数据加载器 V2 - 支持多步轨迹和正确的 loss mask"""

import json
import torch
from torch.utils.data import Dataset
from typing import Dict, List, Tuple
from transformers import PreTrainedTokenizer

from .chat_format import format_trajectory_messages, load_openai_tools


class DeepResearchDatasetV2(Dataset):
    """Deep Research V2 训练数据集 - 支持多步轨迹"""

    def __init__(
        self,
        data_path: str,
        tokenizer: PreTrainedTokenizer,
        max_length: int = 4096,
        tool_schema_path: str = "config/deepresearch_tools.v1.json",
    ):
        self.tokenizer = tokenizer
        self.max_length = max_length
        self.tools = load_openai_tools(tool_schema_path)

        # 加载数据
        with open(data_path, "r", encoding="utf-8") as f:
            self.data = [json.loads(line) for line in f]

        # 确保 pad_token 存在
        if self.tokenizer.pad_token is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token
            self.tokenizer.pad_token_id = self.tokenizer.eos_token_id

        # 统计信息
        self._compute_stats()

    def _compute_stats(self):
        """计算数据集统计信息"""
        self.stats = {
            "total_samples": len(self.data),
            "industry_dist": {},
            "question_type_dist": {},
            "step_dist": {},
            "token_lengths": []
        }

        for item in self.data:
            meta = item.get("metadata", {})

            # 行业分布
            industry = meta.get("industry", "unknown")
            self.stats["industry_dist"][industry] = self.stats["industry_dist"].get(industry, 0) + 1

            # 问题类型分布
            q_type = meta.get("question_type", "unknown")
            self.stats["question_type_dist"][q_type] = self.stats["question_type_dist"].get(q_type, 0) + 1

            # 步数分布
            steps = meta.get("num_steps", 1)
            self.stats["step_dist"][steps] = self.stats["step_dist"].get(steps, 0) + 1

            # Token 长度（如果已计算）
            if "total_tokens" in meta and meta["total_tokens"] > 0:
                self.stats["token_lengths"].append(meta["total_tokens"])

    def print_stats(self):
        """打印数据集统计信息"""
        print("=" * 50)
        print("数据集统计")
        print("=" * 50)
        print(f"总样本数: {self.stats['total_samples']}")

        print("\n行业分布:")
        for k, v in sorted(self.stats["industry_dist"].items(), key=lambda x: -x[1]):
            pct = v / self.stats["total_samples"] * 100
            print(f"  {k}: {v} ({pct:.1f}%)")

        print("\n问题类型分布:")
        for k, v in sorted(self.stats["question_type_dist"].items(), key=lambda x: -x[1]):
            pct = v / self.stats["total_samples"] * 100
            print(f"  {k}: {v} ({pct:.1f}%)")

        print("\n步数分布:")
        for k, v in sorted(self.stats["step_dist"].items()):
            pct = v / self.stats["total_samples"] * 100
            print(f"  {k}步: {v} ({pct:.1f}%)")

        if self.stats["token_lengths"]:
            lengths = sorted(self.stats["token_lengths"])
            print("\nToken 长度统计:")
            print(f"  最小: {min(lengths)}")
            print(f"  最大: {max(lengths)}")
            print(f"  平均: {sum(lengths)/len(lengths):.0f}")
            print(f"  P50: {lengths[len(lengths)//2]}")
            print(f"  P90: {lengths[int(len(lengths)*0.9)]}")

    def __len__(self) -> int:
        return len(self.data)

    def __getitem__(self, idx: int) -> Dict[str, torch.Tensor]:
        item = self.data[idx]
        messages = item["messages"]

        # 转换消息格式为标准 chat 格式
        chat_messages = self._convert_to_chat_format(messages)

        # 使用 apply_chat_template 进行编码
        text = self._apply_chat_template(chat_messages)

        # 编码
        encoding = self.tokenizer(
            text,
            max_length=self.max_length,
            truncation=True,
            padding="max_length",
            return_tensors="pt"
        )

        input_ids = encoding["input_ids"].squeeze(0)

        # 创建 attention_mask
        attention_mask = encoding["attention_mask"].squeeze(0)

        # 创建 labels（带 loss mask）
        labels = self._create_labels_with_mask(input_ids, chat_messages)
        if not torch.any(labels != -100):
            trajectory_id = item.get("metadata", {}).get("trajectory_id", idx)
            raise ValueError(
                f"no assistant tokens selected for loss: trajectory={trajectory_id}"
            )

        return {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "labels": labels
        }

    def _convert_to_chat_format(self, messages: List[Dict]) -> List[Dict]:
        """保留标准 assistant.tool_calls 与 tool.tool_call_id。"""
        return format_trajectory_messages(messages)

    def _apply_chat_template(self, messages: List[Dict], *, generation_prompt: bool = False) -> str:
        try:
            return self.tokenizer.apply_chat_template(
                messages,
                tools=self.tools,
                tokenize=False,
                add_generation_prompt=generation_prompt,
            )
        except TypeError:
            return self.tokenizer.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=generation_prompt,
            )

    def _create_labels_with_mask(
        self,
        input_ids: torch.Tensor,
        chat_messages: List[Dict]
    ) -> torch.Tensor:
        """创建带 loss mask 的 labels，只对 assistant 内容计算 loss"""

        labels = input_ids.clone()

        # 尝试使用特殊 token 定位
        assistant_ranges = self._find_assistant_ranges(input_ids)

        if not assistant_ranges:
            # 降级方案：使用文本匹配
            assistant_ranges = self._find_assistant_ranges_fallback(
                input_ids, chat_messages
            )

        # 默认全部 mask 掉
        labels[:] = -100

        # 只对 assistant 内容取消 mask
        for start, end in assistant_ranges:
            labels[start:end] = input_ids[start:end]

        # padding 位置保持 -100
        labels[input_ids == self.tokenizer.pad_token_id] = -100

        return labels

    def _find_assistant_ranges(
        self,
        input_ids: torch.Tensor
    ) -> List[Tuple[int, int]]:
        """通过特殊 token 定位 assistant 内容范围"""

        ranges = []

        # Qwen 的特殊 token
        im_start_token = "<|im_start|>"
        im_end_token = "<|im_end|>"

        try:
            im_start_id = self.tokenizer.convert_tokens_to_ids(im_start_token)
            im_end_id = self.tokenizer.convert_tokens_to_ids(im_end_token)
        except Exception:
            return []

        if im_start_id is None or im_end_id is None:
            return []

        input_ids_list = input_ids.tolist()

        # 找到所有 <|im_start|> 的位置
        im_start_positions = [
            i for i, token_id in enumerate(input_ids_list)
            if token_id == im_start_id
        ]

        # 对每个 <|im_start|>，检查后面是否是 assistant
        for start_pos in im_start_positions:
            if start_pos + 1 >= len(input_ids_list):
                continue

            # 解码后续几个 token 检查是否是 "assistant"
            check_end = min(start_pos + 5, len(input_ids_list))
            check_tokens = input_ids_list[start_pos + 1:check_end]
            try:
                decoded = self.tokenizer.decode(check_tokens, skip_special_tokens=False)
                if not decoded.strip().startswith("assistant"):
                    continue
            except Exception:
                continue

            # 找到 content 开始位置和结束位置
            content_start = None
            for i in range(start_pos + 1, len(input_ids_list)):
                if content_start is None:
                    try:
                        partial = self.tokenizer.decode(
                            input_ids_list[start_pos + 1:i + 1],
                            skip_special_tokens=False
                        )
                        if "\n" in partial:
                            content_start = i + 1
                    except Exception:
                        continue

                if content_start is not None and input_ids_list[i] == im_end_id:
                    ranges.append((content_start, i))
                    break

        return ranges

    def _find_assistant_ranges_fallback(
        self,
        input_ids: torch.Tensor,
        chat_messages: List[Dict]
    ) -> List[Tuple[int, int]]:
        """降级方案：通过文本匹配定位 assistant 内容"""

        ranges = []
        full_text = self.tokenizer.decode(input_ids, skip_special_tokens=False)

        current_pos = 0
        for msg in chat_messages:
            if msg["role"] != "assistant":
                continue

            content = msg.get("content") or json.dumps(
                msg.get("tool_calls", []), ensure_ascii=False, sort_keys=True
            )
            if not content:
                continue

            content_start_text = content[:min(50, len(content))]

            try:
                text_pos = full_text.find(content_start_text, current_pos)
                if text_pos == -1:
                    continue

                prefix_text = full_text[:text_pos]
                prefix_tokens = self.tokenizer.encode(
                    prefix_text, add_special_tokens=False
                )
                token_start = len(prefix_tokens)

                content_tokens = self.tokenizer.encode(
                    content, add_special_tokens=False
                )
                token_end = token_start + len(content_tokens)
                token_end = min(token_end, len(input_ids))

                if token_start < token_end:
                    ranges.append((token_start, token_end))
                    current_pos = text_pos + len(content)

            except Exception:
                continue

        return ranges


def compute_token_lengths(data_path: str, tokenizer: PreTrainedTokenizer) -> Dict:
    """计算数据集中每条数据的 token 长度"""

    with open(data_path, "r", encoding="utf-8") as f:
        data = [json.loads(line) for line in f]

    lengths = []
    for item in data:
        messages = item["messages"]

        chat_messages = format_trajectory_messages(messages)

        # 编码
        text = tokenizer.apply_chat_template(chat_messages, tokenize=False)
        tokens = tokenizer.encode(text)
        lengths.append(len(tokens))

        # 更新 metadata
        item["metadata"]["total_tokens"] = len(tokens)

    # 计算统计
    lengths_sorted = sorted(lengths)
    stats = {
        "min": min(lengths),
        "max": max(lengths),
        "avg": sum(lengths) / len(lengths),
        "p50": lengths_sorted[len(lengths) // 2],
        "p90": lengths_sorted[int(len(lengths) * 0.9)],
        "p95": lengths_sorted[int(len(lengths) * 0.95)],
        "total": len(lengths)
    }

    return stats, data


def create_data_collator(tokenizer: PreTrainedTokenizer):
    """创建数据整理器"""

    def collate_fn(batch: List[Dict[str, torch.Tensor]]) -> Dict[str, torch.Tensor]:
        input_ids = torch.stack([item["input_ids"] for item in batch])
        attention_mask = torch.stack([item["attention_mask"] for item in batch])
        labels = torch.stack([item["labels"] for item in batch])

        return {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "labels": labels
        }

    return collate_fn

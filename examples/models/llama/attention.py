from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Dict, Optional, Tuple, Type, TypedDict

import torch
import torch.nn as nn
import torch.nn.functional as F
from executorch.examples.models.llama.model_args import ModelArgs
from executorch.examples.models.llama.norm import RMSNorm
from executorch.examples.models.llama.rope import Rope


class ForwardOptions(TypedDict, total=False):
    """Optional parameters for `Attention.forward` (compative with Python 3.10 and plus)."""

    mask: Optional[torch.Tensor]
    input_pos: Optional[torch.Tensor]
    freqs_cos_override: Optional[torch.Tensor]
    freqs_sin_override: Optional[torch.Tensor]
    in_cache_state: Optional[Any]
    out_cache_state: Optional[Any]


class Attention(nn.Module, ABC):
    """Abstract base class for attention mechanisms with unified interface."""

    @abstractmethod
    def forward(
        self,
        x: torch.Tensor,
        freqs_cos: torch.Tensor,
        freqs_sin: torch.Tensor,
        **kwargs: ForwardOptions,
    ) -> Tuple[torch.Tensor, Optional[Any]]:
        """Forward pass for attention mechanism.

        Args:
            x: Input tensor of shape (batch_size, seq_len, dim)
            freqs_cos, freqs_sin: Rotary position embedding frequencies
            ForwardOptions: grouped optional args

        Returns:
            Tuple of (output tensor, updated cache state)
        """
        pass


ATTENTION_REGISTRY: Dict[str, Type[Attention]] = {}


def register_attention(name: str):
    """Decorator to register attention classes"""

    def decorator(cls: Type[Attention]):
        ATTENTION_REGISTRY[name.lower()] = cls
        return cls

    return decorator


class KVCache(nn.Module):
    def __init__(
        self,
        max_batch_size: int,
        max_context_length: int,
        n_heads: int,
        head_dim: int,
        enable_dynamic_shape: bool,
        dtype=torch.float32,
    ):
        super().__init__()
        self.max_context_length = max_context_length
        cache_shape = (max_batch_size, n_heads, max_context_length, head_dim)

        self.max_batch_size = max_batch_size
        self.n_heads = n_heads
        self.head_dim = head_dim
        self.enable_dynamic_shape = enable_dynamic_shape
        self.register_buffer(
            "k_cache", torch.zeros(cache_shape, dtype=dtype, device="cpu")
        )
        self.register_buffer(
            "v_cache", torch.zeros(cache_shape, dtype=dtype, device="cpu")
        )

    def update(
        self, input_pos: torch.Tensor, k_val: torch.Tensor, v_val: torch.Tensor
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        # input_pos: [S], k_val: [B, H, S, D]
        if self.enable_dynamic_shape:
            start_pos = input_pos[0].item()
            torch._check_is_size(start_pos)
            torch._check(start_pos < self.max_context_length)
            dim_to_slice = 2
            seq_length = k_val.size(dim_to_slice)
            # Replace the entry in the cache for this token
            # The following lines are equivalent to:
            # cache_k[:bsz, start_pos : start_pos + seqlen] = xk
            # cache_v[:bsz, start_pos : start_pos + seqlen] = xv
            # when dim_to_slice is 1
            # We use .narrow() here to make the compiler happy
            # pyre-ignore: Incompatible parameter type [6]
            narrowed_k = self.k_cache.narrow(dim_to_slice, start_pos, seq_length)
            # pyre-ignore: Incompatible parameter type [6]
            narrowed_v = self.v_cache.narrow(dim_to_slice, start_pos, seq_length)

            narrowed_k.copy_(k_val)
            narrowed_v.copy_(v_val)
            return self.k_cache, self.v_cache
        else:
            k_out = self.k_cache
            v_out = self.v_cache
            k_out[:, :, input_pos] = k_val
            v_out[:, :, input_pos] = v_val

            return k_out, v_out


class SDPA(nn.Module):
    def __init__(
        self,
        dim: int,
        head_dim: int,
        n_rep: int,
        max_context_len: int,
        enable_dynamic_shape: bool,
    ):
        super().__init__()
        self.dim = dim
        self.head_dim = head_dim
        self.n_rep = n_rep
        self.max_context_len = max_context_len
        self.enable_dynamic_shape = enable_dynamic_shape

    def forward(
        self,
        input_pos: torch.Tensor,
        q: torch.Tensor,  # Already have rotary embeddings. (bs, n_local_heads, seqlen, head_dim)
        k: torch.Tensor,  # Already have rotary embeddings. (bs, n_local_kv_heads, seqlen, head_dim)
        v: torch.Tensor,  # (bs, n_local_kv_heads, seqlen, head_dim)
        bsz,
        seqlen,
        mask: torch.Tensor,
    ) -> torch.Tensor:
        if self.enable_dynamic_shape:
            start_pos = input_pos[-1].item()
            torch._check_is_size(start_pos)
            torch._check(start_pos < self.max_context_len)
            seq_length = q.size(2)
            # pyre-ignore: Incompatible parameter type [6]
            attn_mask = mask.narrow(0, start_pos, seq_length)
        else:
            attn_mask = mask[None, None, input_pos]

        # TODO(kimishpatel): This should not be necessary because scaled_dot_product_attention
        # can natively support GQA now. But needs enable_gqa=True
        k = k.repeat_interleave(self.n_rep, dim=1)
        v = v.repeat_interleave(self.n_rep, dim=1)
        y = F.scaled_dot_product_attention(q, k, v, attn_mask=attn_mask, dropout_p=0.0)

        return y.transpose(1, 2).contiguous().view(bsz, seqlen, self.dim)


class CacheUpdateStrategy(Enum):
    RING_BUFFER = "RingBuffer"
    INVALID = "Invalid"


class CachePositionsManager(nn.Module):
    def __init__(
        self,
        max_context_length: int,
        cache_update_strategy: CacheUpdateStrategy = CacheUpdateStrategy.RING_BUFFER,
    ):
        super().__init__()
        assert (
            cache_update_strategy == CacheUpdateStrategy.RING_BUFFER
        ), "Only RingBuffer is supported"
        self.max_context_length = max_context_length
        self.register_buffer(
            "cache_positions",
            torch.zeros((self.max_context_length), dtype=torch.long, device="cpu"),
        )

    def calculate_positions_and_update_indices(self, input_pos: torch.Tensor, seq_len):
        """
        Calculate indices, into k_cache, v_cache, where to put k_val tensor.
        Given the input_pos and length of k_val at sequence dim, the input pos may
        have to wrap around if it is smaller than the cache capacity.
        If it is larger than the cache capacity then just pick the last
        self.max_context_length entries.

        Additionally:
        Update the cache positions buffer with the new indices.
        Given the cache positions in sequence dim, indicated by indices,
        we can just update cache_positions buffer using orig_indices.
        For example
        Given cache capacity of 4 and update of length 3 with start_pos = 2
        will have following values
        indices = [2, 3, 0]
        orig_indices = [2, 3, 4]
        So cache_positions after the update will be [4, 1, 2, 3]
        Note cache_positions[1] = 1 that is from previous write to the cache.
        The corner case here is cache positions before cache rolls over.
        For example when start_pos = 0 and update is of length 2, then we have
        filled positions 0 and 1 in the buffer, while the rest are invalid. In this case
        we have
        indices = [0, 1]
        orig_indices = [0, 1]
        But if we have cache_positins = [0, 1, 0, 0] that is not valid. Hence we have
        to make sure that invalid positions have a sentinel value of - 1.
        """
        start_pos = input_pos[0].item()
        torch._check_is_size(start_pos)
        orig_indices = torch.arange(seq_len, dtype=torch.long) + start_pos
        indices = orig_indices % self.max_context_length

        full_t = torch.full((self.max_context_length,), -1, dtype=torch.long)
        arange_tensor = torch.arange(self.max_context_length, dtype=torch.long)
        cache_positions = torch.where(
            arange_tensor < start_pos, self.cache_positions, full_t
        )
        self.cache_positions.copy_(cache_positions)
        self.cache_positions.index_copy_(0, indices, orig_indices)

        return indices


class RingKVCache(KVCache):
    def __init__(
        self,
        max_batch_size: int,
        max_context_length: int,
        n_heads: int,
        head_dim: int,
        enable_dynamic_shape: bool,
        dtype=torch.float32,
    ):
        super().__init__(
            max_batch_size,
            max_context_length,
            n_heads,
            head_dim,
            enable_dynamic_shape,
            dtype,
        )
        self.cache_positions_manager = CachePositionsManager(max_context_length)

    def update(
        self, input_pos: torch.Tensor, k_val: torch.Tensor, v_val: torch.Tensor
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        # input_pos: [S], k_val: [B, H, S, D]
        seq_len = k_val.size(2)
        indices = self.cache_positions_manager.calculate_positions_and_update_indices(
            input_pos, seq_len
        )
        if self.enable_dynamic_shape:
            start_pos = input_pos[0].item()
            torch._check_is_size(start_pos)

            self.k_cache.index_copy_(2, indices, k_val)
            self.v_cache.index_copy_(2, indices, v_val)
        else:
            self.k_cache[:, :, indices] = k_val
            self.v_cache[:, :, indices] = v_val

        return self.k_cache, self.v_cache


@register_attention("mha")
class AttentionMHA(Attention):
    def __init__(self, args: ModelArgs, layer_id: int, rope: Rope):
        super().__init__()
        self.use_kv_cache = args.use_kv_cache
        self.n_heads = args.n_heads
        self.n_kv_heads = self.n_heads if args.n_kv_heads is None else args.n_kv_heads
        assert self.n_heads % self.n_kv_heads == 0
        model_parallel_size = 1
        self.n_local_heads = self.n_heads // model_parallel_size
        self.n_local_kv_heads = self.n_kv_heads // model_parallel_size
        self.n_rep = self.n_local_heads // self.n_local_kv_heads
        self.head_dim = args.head_dim
        self.max_batch_size = args.max_batch_size
        self.max_context_len = args.max_context_len
        self.dim = args.dim
        self.attention_qkv_bias = args.attention_qkv_bias
        self.use_qk_norm = args.use_qk_norm
        self.qk_norm_before_rope = args.qk_norm_before_rope

        if self.use_qk_norm:
            q_norm_dim = self.head_dim
            k_norm_dim = self.head_dim
            self.q_norm_fn = RMSNorm(q_norm_dim, eps=args.norm_eps)
            self.k_norm_fn = RMSNorm(k_norm_dim, eps=args.norm_eps)

        self.wq = nn.Linear(
            self.dim, self.n_heads * self.head_dim, bias=self.attention_qkv_bias
        )
        self.wk = nn.Linear(
            self.dim, self.n_kv_heads * self.head_dim, bias=self.attention_qkv_bias
        )
        self.wv = nn.Linear(
            self.dim, self.n_kv_heads * self.head_dim, bias=self.attention_qkv_bias
        )
        self.wo = nn.Linear(self.n_heads * self.head_dim, self.dim, bias=False)

        self.layer_id = layer_id

        self.rope = rope

        causal_mask = torch.tril(
            torch.ones(
                self.max_context_len,
                self.max_context_len,
                dtype=torch.bool,
                device="cpu",
            )
        )
        self.register_buffer("mask", causal_mask, persistent=False)

        if self.use_kv_cache:
            self.kv_cache = KVCache(
                args.max_batch_size,
                args.max_context_len,
                self.n_kv_heads,
                self.head_dim,
                args.enable_dynamic_shape,
            )
            self.SDPA = SDPA(
                dim=self.n_local_heads * self.head_dim,
                head_dim=self.head_dim,
                n_rep=self.n_rep,
                max_context_len=self.max_context_len,
                enable_dynamic_shape=args.enable_dynamic_shape,
            )

    def forward(
        self,
        x: torch.Tensor,
        freqs_cos: torch.Tensor,
        freqs_sin: torch.Tensor,
        **kwargs: ForwardOptions,
    ) -> Tuple[torch.Tensor, Optional[Any]]:
        input_pos = kwargs.get("input_pos")
        bsz, seqlen, _ = x.shape

        # QKV
        q, k, v = self.wq(x), self.wk(x), self.wv(x)
        # We need view_copy elimination
        q = q.view(bsz, seqlen, self.n_local_heads, self.head_dim)
        k = k.view(bsz, seqlen, self.n_local_kv_heads, self.head_dim)
        v = v.view(bsz, seqlen, self.n_local_kv_heads, self.head_dim)

        if self.use_qk_norm and self.qk_norm_before_rope:
            q = self.q_norm_fn(q)
            k = self.k_norm_fn(k)

        # RoPE relative positional embeddings
        q, k = self.rope.forward(q, k, freqs_cos, freqs_sin)

        q = q.transpose(1, 2)  # (bs, n_local_heads, seqlen, head_dim)
        k = k.transpose(1, 2)
        v = v.transpose(1, 2)

        if self.use_qk_norm and not self.qk_norm_before_rope:
            q = self.q_norm_fn(q)
            k = self.k_norm_fn(k)

        if self.use_kv_cache:
            assert input_pos is not None
            k, v = self.kv_cache.update(input_pos, k, v)
            output = self.SDPA(input_pos, q, k, v, bsz, seqlen, self.mask)
            return self.wo(output), None

        # grouped multiquery attention: expand out keys and values
        k = k.repeat_interleave(self.n_rep, dim=1)
        v = v.repeat_interleave(self.n_rep, dim=1)

        assert hasattr(self, "mask")

        mask = self.mask[:seqlen, :seqlen]

        output = F.scaled_dot_product_attention(q, k, v, attn_mask=mask, dropout_p=0.0)

        output = output.transpose(1, 2).contiguous().view(bsz, seqlen, -1)

        output = self.wo(output)

        return output, None

#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import copy
import unittest
from typing import Any, Dict, List, Optional

import hypothesis.strategies as st
import torch
import torch.nn as nn
from hypothesis import assume, given, settings, Verbosity
from torchrec import distributed as trec_dist
from torchrec.distributed.modular_embeddingbag import (
    EmbeddingBagCollectionSharder,
    ShardedEmbeddingBagCollection,
)
from torchrec.distributed.planner import (
    EmbeddingShardingPlanner,
    ParameterConstraints,
    Topology,
)

from torchrec.distributed.shard_embedding_modules import shard_embedding_modules
from torchrec.distributed.test_utils.multi_process import (
    MultiProcessContext,
    MultiProcessTestBase,
)
from torchrec.distributed.test_utils.test_sharding import copy_state_dict
from torchrec.distributed.types import (
    ModuleSharder,
    QuantizedCommCodecs,
    ShardingEnv,
    ShardingPlan,
    ShardingType,
)
from torchrec.modules.embedding_configs import EmbeddingBagConfig
from torchrec.modules.embedding_modules import EmbeddingBagCollection
from torchrec.optim.apply_overlapped_optimizer import apply_overlapped_optimizer

from torchrec.sparse.jagged_tensor import KeyedJaggedTensor
from torchrec.test_utils import check_model_characteristics, skip_if_asan_class


def _test_sharding(
    tables: List[EmbeddingBagConfig],
    initial_state_dict: Dict[str, Any],
    rank: int,
    world_size: int,
    kjt_input_per_rank: List[KeyedJaggedTensor],
    sharder: ModuleSharder[nn.Module],
    backend: str,
    constraints: Optional[Dict[str, ParameterConstraints]] = None,
    local_size: Optional[int] = None,
    is_data_parallel: bool = False,
    use_apply_overlapped_optimizer: bool = False,
) -> None:
    trec_dist.comm_ops.set_gradient_division(False)
    with MultiProcessContext(rank, world_size, backend, local_size) as ctx:
        kjt_input_per_rank = [kjt.to(ctx.device) for kjt in kjt_input_per_rank]
        initial_state_dict = {
            fqn: tensor.to(ctx.device) for fqn, tensor in initial_state_dict.items()
        }

        planner = EmbeddingShardingPlanner(
            topology=Topology(
                world_size, ctx.device.type, local_world_size=ctx.local_size
            ),
            constraints=constraints,
        )
        model = EmbeddingBagCollection(
            tables=tables,
            device=ctx.device,
        )

        if use_apply_overlapped_optimizer:
            apply_overlapped_optimizer(
                torch.optim.SGD,
                model.embedding_bags["table_0"].parameters(),
                {"lr": 1.0},
            )

            apply_overlapped_optimizer(
                torch.optim.SGD,
                model.embedding_bags["table_1"].parameters(),
                {"lr": 4.0},
            )

        plan: ShardingPlan = planner.collective_plan(model, [sharder], ctx.pg)
        sharded_model, sharded_parameter_names = shard_embedding_modules(
            module=model,
            env=ShardingEnv.from_process_group(ctx.pg),
            plan=plan,
            sharders=[sharder],
            device=ctx.device,
        )

        if not use_apply_overlapped_optimizer:
            unsharded_model_optimizer = torch.optim.SGD(model.parameters(), lr=0.01)
            sharded_model_optimizer = torch.optim.SGD(
                sharded_model.parameters(), lr=0.01
            )

        unsharded_model = model

        assert isinstance(sharded_model, ShardedEmbeddingBagCollection)

        unsharded_model.load_state_dict(copy.deepcopy(initial_state_dict))
        copy_state_dict(sharded_model.state_dict(), copy.deepcopy(initial_state_dict))

        feature_keys = []
        for table in tables:
            feature_keys.extend(table.feature_names)

        for _it in range(5):
            if not use_apply_overlapped_optimizer:
                unsharded_model_optimizer.zero_grad()
                sharded_model_optimizer.zero_grad()

            unsharded_model_pred_kt = []
            for rank in range(ctx.world_size):
                # simulate the unsharded model run on the entire batch
                unsharded_model_pred_kt.append(
                    unsharded_model(kjt_input_per_rank[rank])
                )

            all_unsharded_preds = []
            for rank in range(ctx.world_size):
                unsharded_model_pred_kt_mini_batch = unsharded_model_pred_kt[
                    rank
                ].to_dict()

                all_unsharded_preds.extend(
                    [
                        unsharded_model_pred_kt_mini_batch[feature]
                        for feature in feature_keys
                    ]
                )
                if rank == ctx.rank:
                    unsharded_model_pred = torch.stack(
                        [
                            unsharded_model_pred_kt_mini_batch[feature]
                            for feature in feature_keys
                        ]
                    )

            # sharded model
            # each rank gets a subbatch
            sharded_model_pred_kt = sharded_model(
                kjt_input_per_rank[ctx.rank]
            ).to_dict()
            sharded_model_pred = torch.stack(
                [sharded_model_pred_kt[feature] for feature in feature_keys]
            )

            # cast to CPU because when casting unsharded_model.to on the same module, there could some race conditions
            # in normal author modelling code this won't be an issue because each rank would individually create
            # their model. output from sharded_pred is correctly on the correct device.

            # Compare predictions of sharded vs unsharded models.
            torch.testing.assert_allclose(
                sharded_model_pred.cpu(), unsharded_model_pred.cpu()
            )

            sharded_model_pred.sum().backward()

            all_unsharded_preds = torch.stack(all_unsharded_preds)
            all_unsharded_preds.sum().backward()

            if not use_apply_overlapped_optimizer:
                pass
                # TODO, ShardedTensor.grad does not exist, need distributed to add this API
                # unsharded_model_optimizer.step()
                # sharded_model_optimizer.step()

        # check nn.Module APIs look the same
        check_model_characteristics(unsharded_model, sharded_model)

        for fqn in unsharded_model.state_dict():
            unsharded_state = unsharded_model.state_dict()[fqn]
            sharded_state = sharded_model.state_dict()[fqn]

            if is_data_parallel:
                continue
            else:
                out = (
                    torch.zeros(size=unsharded_state.shape, device=ctx.device)
                    if ctx.rank == 0
                    else None
                )
                sharded_state.gather(out=out)
                if ctx.rank == 0:
                    torch.testing.assert_allclose(
                        unsharded_state,
                        out,
                    )

        sharded_model.load_state_dict(sharded_model.state_dict())


class TestEmbeddingBagCollectionSharder(EmbeddingBagCollectionSharder):
    def __init__(
        self,
        sharding_type: str,
        qcomm_codecs_registry: Optional[Dict[str, QuantizedCommCodecs]] = None,
    ) -> None:
        super().__init__(qcomm_codecs_registry=qcomm_codecs_registry)
        self._sharding_type = sharding_type

    """
    Restricts sharding to single type only.
    """

    def sharding_types(self, compute_device_type: str) -> List[str]:
        return [self._sharding_type]


@skip_if_asan_class
class ShardedEmbeddingBagCollectionParallelTest(MultiProcessTestBase):
    @unittest.skipIf(
        torch.cuda.device_count() <= 1,
        "Not enough GPUs, this test requires at least two GPUs",
    )
    # pyre-fixme[56]
    @given(
        sharding_type=st.sampled_from(
            [
                ShardingType.TABLE_WISE.value,
                ShardingType.ROW_WISE.value,
                ShardingType.COLUMN_WISE.value,
                ShardingType.DATA_PARALLEL.value,
            ]
        ),
        use_apply_overlapped_optimizer=st.booleans(),
    )
    @settings(verbosity=Verbosity.verbose, max_examples=8, deadline=None)
    def test_sharding_ebc(
        self, sharding_type: str, use_apply_overlapped_optimizer: bool
    ) -> None:

        # TODO DistributedDataParallel needs full support of registering fused optims before we can enable this.
        assume(
            use_apply_overlapped_optimizer
            and sharding_type != ShardingType.DATA_PARALLEL.value,
        )

        WORLD_SIZE = 2

        embedding_bag_config = [
            EmbeddingBagConfig(
                name="table_0",
                feature_names=["feature_0"],
                embedding_dim=4,
                num_embeddings=4,
            ),
            EmbeddingBagConfig(
                name="table_1",
                feature_names=["feature_1"],
                embedding_dim=4,
                num_embeddings=4,
            ),
        ]

        # Rank 0
        #             instance 0   instance 1  instance 2
        # "feature_0"   [0, 1]       None        [2]
        # "feature_1"   [0, 1]       None        [2]

        # Rank 1

        #             instance 0   instance 1  instance 2
        # "feature_0"   [3, 2]       [1,2]        [0, 1,2,3]
        # "feature_1"   [2,3]       None        [2]

        kjt_input_per_rank = [  # noqa
            KeyedJaggedTensor.from_lengths_sync(
                keys=["feature_0", "feature_1"],
                values=torch.LongTensor([0, 1, 2, 0, 1, 2]),
                lengths=torch.LongTensor([2, 0, 1, 2, 0, 1]),
            ),
            KeyedJaggedTensor.from_lengths_sync(
                keys=["feature_0", "feature_1"],
                values=torch.LongTensor([3, 2, 1, 2, 0, 1, 2, 3, 2, 3, 2]),
                lengths=torch.LongTensor([2, 2, 4, 2, 0, 1]),
            ),
        ]
        self._run_multi_process_test(
            callable=_test_sharding,
            world_size=WORLD_SIZE,
            tables=embedding_bag_config,
            initial_state_dict={
                "embedding_bags.table_0.weight": torch.Tensor(
                    [
                        [1, 1, 1, 1],
                        [2, 2, 2, 2],
                        [4, 4, 4, 4],
                        [8, 8, 8, 8],
                    ]
                ),
                "embedding_bags.table_1.weight": torch.Tensor(
                    [
                        [101, 101, 101, 101],
                        [102, 102, 102, 102],
                        [104, 104, 104, 104],
                        [108, 108, 108, 108],
                    ]
                ),
            },
            kjt_input_per_rank=kjt_input_per_rank,
            sharder=TestEmbeddingBagCollectionSharder(sharding_type=sharding_type),
            backend="nccl"
            if (torch.cuda.is_available() and torch.cuda.device_count() >= 2)
            else "gloo",
            is_data_parallel=(sharding_type == ShardingType.DATA_PARALLEL.value),
            use_apply_overlapped_optimizer=use_apply_overlapped_optimizer,
        )

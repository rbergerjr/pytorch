# Copyright (c) Meta Platforms, Inc. and affiliates
# Owner(s): ["oncall: distributed"]

from typing import Any, Callable, Dict, Optional, Sequence
from unittest import skip

import torch

import torch.utils._pytree as pytree
from torch import Tensor

from torch.distributed._tensor import DeviceMesh, distribute_tensor, DTensor
from torch.distributed._tensor.placement_types import (
    _Partial,
    Placement,
    Replicate,
    Shard,
)
from torch.distributed.distributed_c10d import ReduceOp
from torch.testing._internal.common_utils import run_tests
from torch.testing._internal.distributed._tensor.common_dtensor import (
    skip_unless_torch_gpu,
)
from torch.testing._internal.common_distributed import (
    MultiThreadedTestCase,
    DEFAULT_WORLD_SIZE,
)

def no_op():
    return None


def deepcopy_convert_to_dtensor(
    val: Any,
    device_mesh: DeviceMesh,
    placements: Sequence[Placement],
) -> Any:
    """
    Recursively convert (over Sequence and Dict types) Tensors into DTensors.

    :param device_mesh: the DeviceMesh to use.
    :param placements: the Placement list to use.
    :return: the transformed structure.
    """

    def f(x):
        if isinstance(x, Tensor) and not isinstance(x, DTensor):
            return distribute_tensor(
                x,
                device_mesh=device_mesh,
                placements=placements,
            )
        return x

    return pytree.tree_map(f, [val])[0]


def deepcopy_convert_from_dtensor(val: Any) -> Any:
    """
    Recursive convert any DTensor to local Tensor.

    :param val: the structure to coerce.
    :return: the coerced structure.
    """

    def f(x):
        if isinstance(x, DTensor):
            return x.redistribute(
                device_mesh=x.device_mesh,
                placements=[Replicate()] * x.device_mesh.ndim,
            ).to_local()
        return x

    return pytree.tree_map(f, [val])[0]


class DistElementwiseOpsTest(MultiThreadedTestCase):
    @property
    def world_size(self) -> int:
        return DEFAULT_WORLD_SIZE

    @property
    def device_type(self) -> str:
        return "cuda" if torch.cuda.is_available() else "cpu"

    def build_device_mesh(self):
        return DeviceMesh(self.device_type, list(range(self.world_size)))

    def _compare_pairwise_ops(
        self,
        *,
        device_mesh: DeviceMesh,
        placements: Sequence[Placement],
        op: Callable,
        pre_op_fn: Optional[Callable] = None,
        args: Sequence[Any] = tuple(),
        kwargs: Optional[Dict[str, Any]] = None,
    ):
        if pre_op_fn is None:
            pre_op_fn = no_op

        if not kwargs:
            kwargs = {}

        dargs = deepcopy_convert_to_dtensor(
            args,
            device_mesh=device_mesh,
            placements=placements,
        )
        dkwargs = deepcopy_convert_to_dtensor(
            kwargs,
            device_mesh=device_mesh,
            placements=placements,
        )

        pre_op_fn()

        # run the reference first, in case the call is broken;
        # it's better to debug an incorrect call at this point.
        reference_result = op(*args, **kwargs)

        pre_op_fn()

        dist_result = op(*dargs, **dkwargs)

        collected_result = deepcopy_convert_from_dtensor(dist_result)

        self.assertEqualOnRank(reference_result, collected_result)

    # TODO: We need to add CPU tests for ops in the future.
    def _run_sharded_elementwise_ops(
        self,
        *,
        device_mesh: DeviceMesh,
        placements: Sequence[Placement],
        pre_op_fn: Optional[Callable] = None,
        input_size: Sequence[int],
        op: Callable,
        **kwargs,
    ):
        if pre_op_fn is None:
            pre_op_fn = no_op

        input_tensor = torch.randn(
            *input_size,
            device=self.device_type,
            requires_grad=True,
        )

        self._compare_pairwise_ops(
            device_mesh=device_mesh,
            placements=placements,
            pre_op_fn=pre_op_fn,
            op=op,
            args=(input_tensor,),
            kwargs=kwargs,
        )

    def test_activations(self):
        device_mesh = self.build_device_mesh()
        self._run_sharded_elementwise_ops(
            device_mesh=device_mesh,
            placements=[Shard(0)],
            input_size=(8, 5),
            op=torch.nn.functional.gelu,
        )
        self._run_sharded_elementwise_ops(
            device_mesh=device_mesh,
            placements=[Replicate()],
            input_size=(8, 5),
            op=torch.nn.functional.gelu,
        )
        self._run_sharded_elementwise_ops(
            device_mesh=device_mesh,
            placements=[Shard(1)],
            input_size=(3, 12),
            op=torch.nn.functional.relu,
        )
        self._run_sharded_elementwise_ops(
            device_mesh=device_mesh,
            placements=[Replicate()],
            input_size=(8, 5),
            op=torch.nn.functional.relu,
        )
        self._run_sharded_elementwise_ops(
            device_mesh=device_mesh,
            placements=[Shard(0)],
            input_size=(8, 5),
            op=torch.sigmoid,
        )
        self._run_sharded_elementwise_ops(
            device_mesh=device_mesh,
            placements=[Replicate()],
            input_size=(8, 5),
            op=torch.sigmoid,
        )

    @skip("testing RNG based ops is broken: https://github.com/pytorch/tau/issues/494")
    def test_dropout(self):
        device_mesh = self.build_device_mesh()

        def _reset_random_seed():
            torch.manual_seed(self.rank + 4)

        self._run_sharded_elementwise_ops(
            device_mesh=device_mesh,
            placements=[Shard(0)],
            input_size=(8, 5),
            op=torch.nn.functional.dropout,
            pre_op_fn=_reset_random_seed,
            p=0.4,
            training=False,
        )
        self._run_sharded_elementwise_ops(
            device_mesh=device_mesh,
            placements=[Shard(1)],
            input_size=(3, 14),
            op=torch.nn.functional.dropout,
            pre_op_fn=_reset_random_seed,
            p=0.5,
            training=True,
        )

    @skip_unless_torch_gpu
    def test_dropout_backward(self):
        device_mesh = self.build_device_mesh()
        placements = [Shard(0)]

        input_size = (8, 5)

        grad_output = torch.rand(
            input_size,
            device=self.device_type,
            requires_grad=True,
        )
        mask = (
            torch.rand(
                input_size,
                device=self.device_type,
                requires_grad=False,
            )
            < 0.8
        )

        self._compare_pairwise_ops(
            device_mesh=device_mesh,
            placements=placements,
            op=torch.ops.aten.native_dropout_backward,
            kwargs=dict(
                grad_output=grad_output,
                mask=mask,
                scale=0.3,
            ),
        )

    def test_dropout_errors(self):
        device_mesh = self.build_device_mesh()
        with self.assertRaisesRegex(RuntimeError, "supported"):
            self._run_sharded_elementwise_ops(
                device_mesh=device_mesh,
                placements=[_Partial(ReduceOp.SUM)],
                input_size=(8, 5),
                op=torch.nn.functional.dropout,
            )

    def _run_init_op(self, init_op, *args, **kwargs):
        device_mesh = self.build_device_mesh()
        shard_spec = [Shard(0)]
        input_size = (8, 4)
        input_tensor = torch.randn(*input_size, device=self.device_type)
        dtensor = DTensor.from_local(input_tensor, device_mesh, shard_spec)
        local_tensor_clone = torch.clone(input_tensor)
        torch.manual_seed(123)
        local_tensor_clone = init_op(local_tensor_clone, *args, **kwargs)
        torch.manual_seed(123)
        dtensor = init_op(dtensor, *args, **kwargs)
        dtensor_clone = DTensor.from_local(local_tensor_clone, device_mesh, shard_spec)
        self.assertEqualOnRank(dtensor_clone, dtensor)

    def test_init_ops(self):
        self._run_init_op(torch.nn.init.kaiming_uniform_, a=0, mode='fan_in', nonlinearity='leaky_relu')
        self._run_init_op(torch.nn.init.normal_, mean=1.5, std=0.8)
        self._run_init_op(torch.nn.init.uniform_, a=0, b=1.2)
        self._run_init_op(torch.nn.init.constant_, 2.4)


if __name__ == "__main__":
    run_tests()

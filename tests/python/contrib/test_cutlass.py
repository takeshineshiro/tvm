# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
import math
import pytest
import tvm
from tvm import relay
import numpy as np
from tvm.relay.op.contrib.cutlass import partition_for_cutlass
from tvm.contrib.cutlass import tune_cutlass_kernels, build_cutlass_kernels


def get_ref_rt_mod(mod, params):
    with tvm.transform.PassContext(opt_level=3):
        lib = relay.build(mod, target="cuda", params=params)
    dev = tvm.device("cuda", 0)
    rt_mod = tvm.contrib.graph_executor.GraphModule(lib["default"](dev))
    return rt_mod, dev


def get_output(rt_mod, x):
    rt_mod.set_input("data", x)
    rt_mod.run()
    return rt_mod.get_output(0).asnumpy()


def get_dense(M, N, K, out_dtype="float16"):
    data = relay.var("data", shape=(M, K), dtype="float16")
    weight = relay.var("weight", shape=(N, K), dtype="float16")
    return relay.nn.dense(data, weight, out_dtype=out_dtype)


def get_dense_bias(M, N, K, out_dtype="float16"):
    dense = get_dense(M, N, K, out_dtype=out_dtype)
    bias = relay.var("bias", shape=(N,), dtype=out_dtype)
    return relay.nn.bias_add(dense, bias)


def get_dense_bias_relu(M, N, K, out_dtype="float16"):
    return relay.nn.relu(get_dense_bias(M, N, K, out_dtype="float16"))


def get_dense_bias_gelu(M, N, K, out_dtype="float16"):
    bias_add = get_dense_bias(M, N, K, out_dtype)
    mul = bias_add * relay.const((1.0 / math.sqrt(2.0)), dtype=out_dtype)
    if out_dtype == "float16":
        erf = relay.cast(relay.op.erf(relay.cast(mul, "float32")), "float16")
    else:
        erf = relay.op.erf(mul)
    mul_half = erf * relay.const(0.5, dtype=out_dtype)
    add = mul_half + relay.const(0.5, dtype=out_dtype)
    return add * bias_add


def profile_and_build(mod, params, sm, tmp_dir="./tmp", lib_path="compile.so"):
    mod = partition_for_cutlass(mod)
    mod, num_cutlass_partition = tune_cutlass_kernels(
        mod, sm, profile_all=False, use_multiprocessing=False, tmp_dir=tmp_dir
    )
    with tvm.transform.PassContext(opt_level=3):
        lib = relay.build(mod, target="cuda", params=params)
    lib = build_cutlass_kernels(lib, sm, tmp_dir, lib_path)
    dev = tvm.device("cuda", 0)
    rt_mod = tvm.contrib.graph_executor.GraphModule(lib["default"](dev))
    return rt_mod, dev, num_cutlass_partition


def verify(func, M, N, K, sm=80, atol=1e-5, rtol=1e-5, run_benchmark=False):
    if not tvm.get_global_func("relay.ext.cutlass", True):
        return
    mod = tvm.IRModule.from_expr(func)
    typ = relay.transform.InferType()(mod)
    out_dtype = typ["main"].body.checked_type.dtype
    np_data = np.random.uniform(-1, 1, (M, K)).astype("float16")
    np_weight = np.random.uniform(-1, 1, (N, K)).astype("float16")
    np_bias = np.random.uniform(-1, 1, (N,)).astype(out_dtype)

    params = {"weight": np_weight, "bias": np_bias}

    rt_mod_ref, dev = get_ref_rt_mod(mod, params)
    rt_mod, dev, num_partition = profile_and_build(mod, params, sm)
    assert num_partition > 0

    x = tvm.nd.array(np_data, device=dev)

    out = get_output(rt_mod, x)
    ref_out = get_output(rt_mod_ref, x)

    np.testing.assert_allclose(out, ref_out, atol=atol, rtol=rtol)

    if run_benchmark:
        print("CUTLASS:", rt_mod.benchmark(dev, number=1, repeat=600))
        print("TVM Tensorcore (no tuning):", rt_mod_ref.benchmark(dev, number=1, repeat=600))


M = 1820
N = 768
K = 768


def test_dense():
    verify(get_dense(M, N, K), M, N, K)
    verify(get_dense(M, N, K, out_dtype="float32"), M, N, K)


def test_dense_bias():
    verify(get_dense_bias(M, N, K), M, N, K)
    verify(get_dense_bias(M, N, K, out_dtype="float32"), M, N, K)


def test_dense_bias_relu():
    verify(get_dense_bias_relu(M, N, K), M, N, K)
    verify(get_dense_bias_relu(M, N, K, out_dtype="float32"), M, N, K)


def test_dense_bias_gelu():
    verify(get_dense_bias_gelu(M, N, K), M, N, K, atol=1e-3, rtol=1e-3)
    verify(get_dense_bias_gelu(M, N, K, out_dtype="float32"), M, N, K, atol=1e-3, rtol=1e-3)


if __name__ == "__main__":
    pytest.main([__file__])
